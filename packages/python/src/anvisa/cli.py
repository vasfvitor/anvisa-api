"""`anvisa` command line: fila de análise, UDI and assunto lookups."""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from enum import Enum
from typing import Any

import typer
from pydantic import BaseModel
from rich.console import Console
from rich.table import Table

from . import __version__
from .client import Client
from .download import Download
from .errors import AnvisaError, CredentialsError

try:
    from zoneinfo import ZoneInfo

    BRT = ZoneInfo("America/Sao_Paulo")
except Exception:  # no tz database available
    BRT = None


class Format(str, Enum):
    json = "json"
    table = "table"


app = typer.Typer(
    help="ANVISA Consultas Externas: filas, listas, UDI, nomes técnicos e assuntos.",
    no_args_is_help=True,
)
fila_app = typer.Typer(
    help="Fila de análise (posição dos processos por subfila).", no_args_is_help=True
)
udi_app = typer.Typer(help="UDI de dispositivos médicos e termos GMDN.", no_args_is_help=True)
assunto_app = typer.Typer(
    help="Assuntos de peticionamento (documentos, formulários, taxas).", no_args_is_help=True
)
lista_app = typer.Typer(
    help="Listas calculadas (mesma estrutura da fila: área → grupo → sublista).",
    no_args_is_help=True,
)
nome_tecnico_app = typer.Typer(help="Nomes técnicos de produtos para saúde.", no_args_is_help=True)
app.add_typer(fila_app, name="fila")
app.add_typer(lista_app, name="lista")
app.add_typer(udi_app, name="udi")
app.add_typer(nome_tecnico_app, name="nome-tecnico")
app.add_typer(assunto_app, name="assunto")


def _version(value: bool) -> None:
    if value:
        typer.echo(f"anvisa {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    ctx: typer.Context,
    format: Format | None = typer.Option(
        None,
        "--format",
        "-f",
        help="json or table (default: table on a terminal, json when piped).",
    ),
    version: bool = typer.Option(
        False, "--version", callback=_version, is_eager=True, help="Print the version and exit."
    ),
) -> None:
    ctx.obj = format or (Format.table if sys.stdout.isatty() else Format.json)


def make_client() -> Client:
    """Separate so tests can swap in a client with a mocked transport."""
    return Client.from_env()


@contextmanager
def handle_errors() -> Iterator[None]:
    try:
        yield
    except CredentialsError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(2) from None
    except AnvisaError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from None


def emit(ctx: typer.Context, data: BaseModel | list[BaseModel], title: str = "") -> None:
    rows = data if isinstance(data, list) else [data]
    console = Console()  # created per call so COLUMNS/TTY are read at run time
    if ctx.obj == Format.json:
        payload = [r.model_dump(mode="json") for r in rows]
        typer.echo(
            json.dumps(
                payload if isinstance(data, list) else payload[0], ensure_ascii=False, indent=2
            )
        )
        return
    if not rows:
        console.print("(nenhum resultado)")
        return
    if isinstance(data, list):
        table = Table(title=title or None)
        columns = list(rows[0].model_dump())
        for column in columns:
            table.add_column(column)
        for row in rows:
            values = row.model_dump()
            table.add_row(*(render(values[c]) for c in columns))
    else:
        table = Table(title=title or None, show_header=False)
        for key, value in rows[0].model_dump().items():
            table.add_row(key, render(value))
    console.print(table)


OUT = typer.Option(
    ".", "--out", "-o", help="file or directory to write to; `-` writes the bytes to stdout"
)


def write(download: Download, out: str, default_name: str) -> None:
    """Save a download, or stream it to stdout for `-`. The path goes to stderr so that
    piping `--format json` output stays clean."""
    if out == "-":
        sys.stdout.buffer.write(download.content)
        sys.stdout.buffer.flush()
        return
    path = download.save(out, default_name)
    typer.echo(f"saved {path} ({len(download.content)} bytes)", err=True)


def render(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        local = value.astimezone(BRT) if BRT else value
        return (
            local.strftime("%Y-%m-%d")
            if (local.hour, local.minute) == (0, 0)
            else local.strftime("%Y-%m-%d %H:%M")
        )
    if isinstance(value, Enum):
        return str(value.value)
    if isinstance(value, dict | list):
        return json.dumps(value, ensure_ascii=False, default=str)
    return str(value)


# --- fila -------------------------------------------------------------------


@fila_app.command("areas")
def fila_areas(ctx: typer.Context) -> None:
    """Áreas de interesse (Medicamento=1, Dispositivos Médicos=8, ...)."""
    with handle_errors(), make_client() as client:
        emit(ctx, client.fila.areas(), "Áreas")


@fila_app.command("grupos")
def fila_grupos(
    ctx: typer.Context, area_id: int = typer.Argument(help="id from `anvisa fila areas`")
) -> None:
    """Grupos de fila de uma área."""
    with handle_errors(), make_client() as client:
        emit(ctx, client.fila.grupos(area_id), f"Grupos da área {area_id}")


@fila_app.command("subfilas")
def fila_subfilas(
    ctx: typer.Context, grupo_id: int = typer.Argument(help="id from `anvisa fila grupos`")
) -> None:
    """Subfilas de um grupo."""
    with handle_errors(), make_client() as client:
        emit(ctx, client.fila.subfilas(grupo_id), f"Subfilas do grupo {grupo_id}")


@fila_app.command("consulta")
def fila_consulta(
    ctx: typer.Context, subfila_id: int = typer.Argument(help="id from `anvisa fila subfilas`")
) -> None:
    """A fila calculada completa de uma subfila, em ordem."""
    with handle_errors(), make_client() as client:
        emit(ctx, client.fila.consulta(subfila_id), f"Fila da subfila {subfila_id}")


@fila_app.command("download")
def fila_download(
    subfila_id: int = typer.Argument(help="id from `anvisa fila subfilas`"),
    out: str = OUT,
) -> None:
    """Exporta a fila de uma subfila como planilha (consulta_fila.xlsx)."""
    with handle_errors(), make_client() as client:
        write(client.fila.download(subfila_id), out, f"consulta_fila_{subfila_id}.xlsx")


# --- lista ------------------------------------------------------------------


@lista_app.command("areas")
def lista_areas(ctx: typer.Context) -> None:
    """Áreas com listas (Empresas=7, Insumo Farmacêutico=15, Medicamento=1, Toxicologia=9)."""
    with handle_errors(), make_client() as client:
        emit(ctx, client.lista.areas(), "Áreas")


@lista_app.command("grupos")
def lista_grupos(
    ctx: typer.Context, area_id: int = typer.Argument(help="id from `anvisa lista areas`")
) -> None:
    """Grupos de lista de uma área."""
    with handle_errors(), make_client() as client:
        emit(ctx, client.lista.grupos(area_id), f"Grupos da área {area_id}")


@lista_app.command("sublistas")
def lista_sublistas(
    ctx: typer.Context, grupo_id: int = typer.Argument(help="id from `anvisa lista grupos`")
) -> None:
    """Sublistas de um grupo."""
    with handle_errors(), make_client() as client:
        emit(ctx, client.lista.sublistas(grupo_id), f"Sublistas do grupo {grupo_id}")


@lista_app.command("consulta")
def lista_consulta(
    ctx: typer.Context,
    sublista_id: int = typer.Argument(help="id from `anvisa lista sublistas`"),
) -> None:
    """A lista calculada completa de uma sublista."""
    with handle_errors(), make_client() as client:
        emit(ctx, client.lista.consulta(sublista_id), f"Lista da sublista {sublista_id}")


@lista_app.command("download")
def lista_download(
    sublista_id: int = typer.Argument(help="id from `anvisa lista sublistas`"),
    out: str = OUT,
) -> None:
    """Exporta a lista de uma sublista como planilha (consulta_lista.xlsx)."""
    with handle_errors(), make_client() as client:
        write(client.lista.download(sublista_id), out, f"consulta_lista_{sublista_id}.xlsx")


# --- udi --------------------------------------------------------------------


@udi_app.command("search")
def udi_search(
    ctx: typer.Context,
    nome: str | None = typer.Option(None, "--nome", "-n", help="nomeComercial, substring"),
    udi_di: str | None = typer.Option(None, "--udi-di", help="udiDi, exact"),
    cnpj: str | None = typer.Option(None, "--cnpj", help="cnpjDetentora"),
    gmdn: str | None = typer.Option(None, "--gmdn", help="codigoGmdn"),
    registro: str | None = typer.Option(None, "--registro", help="nuRegistro"),
    page: int = typer.Option(1, "--page", min=1),
    size: int = typer.Option(20, "--size", min=1),
    all_pages: bool = typer.Option(
        False, "--all", help="iterate every page (respects the rate limit)"
    ),
) -> None:
    """Busca de dispositivos por UDI. Pelo menos um filtro é obrigatório."""
    filters = {
        k: v
        for k, v in {
            "nomeComercial": nome,
            "udiDi": udi_di,
            "cnpjDetentora": cnpj,
            "codigoGmdn": gmdn,
            "nuRegistro": registro,
        }.items()
        if v
    }
    with handle_errors(), make_client() as client:
        if all_pages:
            emit(ctx, list(client.udi.iter_search(size=size, **filters)), "UDI")
        else:
            result = client.udi.search(page=page, size=size, **filters)
            total = f" (página {page} de {result.totalPages}, {result.totalElements} no total)"
            emit(ctx, result.content or [], "UDI" + total)


@udi_app.command("get")
def udi_get(ctx: typer.Context, id: int) -> None:
    """Detalhe de um dispositivo (id interno, de `udi search`)."""
    with handle_errors(), make_client() as client:
        emit(ctx, client.udi.get(id), f"UDI {id}")


@udi_app.command("historico")
def udi_historico(ctx: typer.Context, id_dispositivo: int, id_historico: int) -> None:
    """Uma versão histórica de um dispositivo."""
    with handle_errors(), make_client() as client:
        emit(ctx, client.udi.get_historico(id_dispositivo, id_historico), "UDI (histórico)")


@udi_app.command("download")
def udi_download(id: int, out: str = OUT) -> None:
    """Exporta o detalhe de um dispositivo como planilha (udi.xlsx)."""
    with handle_errors(), make_client() as client:
        write(client.udi.download(id), out, f"udi_{id}.xlsx")


@udi_app.command("download-historico")
def udi_download_historico(id_dispositivo: int, id_historico: int, out: str = OUT) -> None:
    """Exporta uma versão histórica de um dispositivo como planilha."""
    with handle_errors(), make_client() as client:
        download = client.udi.download_historico(id_dispositivo, id_historico)
        write(download, out, f"udi_{id_dispositivo}_{id_historico}.xlsx")


@udi_app.command("snapshot")
def udi_snapshot(
    id_historico: int = typer.Argument(help="snapshot id, from GET /api/v1/udi/historico"),
    out: str = typer.Option(".", "--out", "-o", help="file or directory to write the zip to"),
) -> None:
    """Baixa o zip de um snapshot do UDI (streamed; ~230 KB, sem Content-Length)."""
    with handle_errors(), make_client() as client:
        path = client.udi.download_snapshot(id_historico, out)
        typer.echo(f"saved {path} ({path.stat().st_size} bytes)", err=True)


@udi_app.command("gmdn-search")
def udi_gmdn_search(
    ctx: typer.Context,
    texto: str = typer.Argument(help="text matched against the term's name and definition"),
    page: int = typer.Option(1, "--page", min=1),
    size: int = typer.Option(20, "--size", min=1),
) -> None:
    """Busca de termos GMDN por texto (nomes em português)."""
    with handle_errors(), make_client() as client:
        result = client.udi.termos_gmdn(page=page, size=size, conteudo=texto)
        total = f" (página {page} de {result.totalPages}, {result.totalElements} no total)"
        emit(ctx, result.content or [], "GMDN" + total)


@udi_app.command("gmdn")
def udi_gmdn(ctx: typer.Context, codigo: str) -> None:
    """Termo GMDN por código."""
    with handle_errors(), make_client() as client:
        emit(ctx, client.udi.termo_gmdn(codigo), f"GMDN {codigo}")


# --- nome-tecnico -----------------------------------------------------------


@nome_tecnico_app.command("search")
def nome_tecnico_search(
    ctx: typer.Context,
    nome: str | None = typer.Option(None, "--nome", "-n", help="nomeTecnico, substring"),
    codigo: str | None = typer.Option(None, "--codigo", help="codigo (ANVISA's example, untested)"),
    categoria: str | None = typer.Option(
        None, "--categoria", help="categoriaProduto, id from `categorias`"
    ),
    page: int = typer.Option(1, "--page", min=1),
    size: int = typer.Option(20, "--size", min=1),
    all_pages: bool = typer.Option(
        False, "--all", help="iterate every page (respects the rate limit)"
    ),
) -> None:
    """Nomes técnicos, paginados. Sem filtro retorna todos (~2.700)."""
    filters = {
        k: v
        for k, v in {"nomeTecnico": nome, "codigo": codigo, "categoriaProduto": categoria}.items()
        if v
    }
    with handle_errors(), make_client() as client:
        if all_pages:
            emit(ctx, list(client.nome_tecnico.iter_search(size=size, **filters)), "Nomes técnicos")
        else:
            result = client.nome_tecnico.search(page=page, size=size, **filters)
            total = f" (página {page} de {result.totalPages}, {result.totalElements} no total)"
            emit(ctx, result.content or [], "Nomes técnicos" + total)


@nome_tecnico_app.command("categorias")
def nome_tecnico_categorias(ctx: typer.Context) -> None:
    """Categorias (Equipamento ou Material=8, Diagnóstico in vitro=12)."""
    with handle_errors(), make_client() as client:
        emit(ctx, client.nome_tecnico.categorias(), "Categorias")


@nome_tecnico_app.command("download")
def nome_tecnico_download(
    nome: str | None = typer.Option(None, "--nome", "-n", help="nomeTecnico, substring"),
    categoria: str | None = typer.Option(
        None, "--categoria", help="categoriaProduto, id from `categorias`"
    ),
    out: str = OUT,
) -> None:
    """Exporta nomes técnicos como planilha (.xls). Sem filtro exporta todos."""
    filters = {k: v for k, v in {"nomeTecnico": nome, "categoriaProduto": categoria}.items() if v}
    with handle_errors(), make_client() as client:
        write(
            client.nome_tecnico.download(**filters),
            out,
            "consulta_nomes_tecnicos_produto_saude.xls",
        )


# --- assunto ----------------------------------------------------------------


@assunto_app.command("lista")
def assunto_lista(
    ctx: typer.Context,
    busca: str | None = typer.Option(None, "--busca", "-b", help="filter locally by text"),
) -> None:
    """Todos os códigos de assunto de peticionamento (uma requisição, ~2.600 linhas)."""
    with handle_errors(), make_client() as client:
        rows = client.assunto.lista()
        if busca:
            rows = [r for r in rows if busca.lower() in (r.descricao or "").lower()]
        emit(ctx, rows, "Assuntos")


@assunto_app.command("get")
def assunto_get(ctx: typer.Context, codigo: int) -> None:
    """Detalhe de um assunto: sistema, serviços, formulários, checklist e taxas por porte."""
    with handle_errors(), make_client() as client:
        emit(ctx, client.assunto.detalhe(codigo), f"Assunto {codigo}")


@assunto_app.command("servicos-associados")
def assunto_servicos_associados(ctx: typer.Context, codigo: str) -> None:
    """Serviços do gov.br associados a um código de serviço."""
    with handle_errors(), make_client() as client:
        emit(ctx, client.assunto.servicos_associados(codigo), f"Serviços de {codigo}")


@assunto_app.command("download")
def assunto_download(
    codigo: list[int] = typer.Option(
        None, "--codigo", "-c", help="codigosAssunto; repeat for more than one"
    ),
    out: str = OUT,
) -> None:
    """Exporta assuntos como planilha (.xls). Sem --codigo exporta os ~2.600 (cerca de 5 MB)."""
    filters = {"codigosAssunto": list(codigo)} if codigo else {}
    with handle_errors(), make_client() as client:
        write(client.assunto.download(**filters), out, "consulta_assuntos.xls")


@assunto_app.command("formulario")
def assunto_formulario(
    id: int = typer.Argument(help="formulários[].id from `anvisa assunto get`"),
    out: str = OUT,
) -> None:
    """Baixa o arquivo de um formulário de assunto (a resposta não traz nome nem tipo)."""
    with handle_errors(), make_client() as client:
        write(client.assunto.formulario(id), out, f"formulario_{id}")
