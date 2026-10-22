"""`anvisa` command line: fila, lista, UDI, nome técnico, assunto, produtos para saúde,
certificados de boas práticas and tabaco lookups, and `dados`."""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

import httpx
import typer
from pydantic import BaseModel
from rich.console import Console
from rich.table import Table

from . import __version__, dados
from .client import Client
from .dados.convert import ROW_GROUP_SIZE
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
    help="ANVISA Consultas Externas: filas, listas, UDI, nomes técnicos, assuntos, produtos para "
    "saúde, certificados de boas práticas e tabaco.",
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
saude_app = typer.Typer(help="Produtos para saúde regularizados.", no_args_is_help=True)
certificado_app = typer.Typer(
    help="Certificados de boas práticas (exceto medicamentos).", no_args_is_help=True
)
certificado_medicamento_app = typer.Typer(
    help="Certificados de boas práticas de fabricação de medicamentos.", no_args_is_help=True
)
tabaco_app = typer.Typer(help="Produtos derivados do tabaco.", no_args_is_help=True)
dados_app = typer.Typer(
    help="Dados abertos (dados.anvisa.gov.br): CSV → Parquet.", no_args_is_help=True
)
app.add_typer(fila_app, name="fila")
app.add_typer(lista_app, name="lista")
app.add_typer(udi_app, name="udi")
app.add_typer(nome_tecnico_app, name="nome-tecnico")
app.add_typer(assunto_app, name="assunto")
app.add_typer(saude_app, name="saude")
app.add_typer(certificado_app, name="certificado")
app.add_typer(certificado_medicamento_app, name="certificado-medicamento")
app.add_typer(tabaco_app, name="tabaco")
app.add_typer(dados_app, name="dados")


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
    return Client()


def dados_http() -> httpx.Client:
    """Separate so tests can swap in a client with a mocked transport."""
    return dados.fetch.http_client()


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


@fila_app.command("query")
def fila_query(
    ctx: typer.Context, subfila_id: int = typer.Argument(help="id from `anvisa fila subfilas`")
) -> None:
    """A fila calculada completa de uma subfila, em ordem."""
    with handle_errors(), make_client() as client:
        emit(ctx, client.fila.query(subfila_id), f"Fila da subfila {subfila_id}")


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


@lista_app.command("query")
def lista_query(
    ctx: typer.Context,
    sublista_id: int = typer.Argument(help="id from `anvisa lista sublistas`"),
) -> None:
    """A lista calculada completa de uma sublista."""
    with handle_errors(), make_client() as client:
        emit(ctx, client.lista.query(sublista_id), f"Lista da sublista {sublista_id}")


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
def udi_get(ctx: typer.Context, dispositivo_id: int) -> None:
    """Detalhe de um dispositivo (id interno, de `udi search`)."""
    with handle_errors(), make_client() as client:
        emit(ctx, client.udi.get(dispositivo_id), f"UDI {dispositivo_id}")


@udi_app.command("historico")
def udi_historico(ctx: typer.Context, id_dispositivo: int, id_historico: int) -> None:
    """Uma versão histórica de um dispositivo."""
    with handle_errors(), make_client() as client:
        emit(ctx, client.udi.get_historico(id_dispositivo, id_historico), "UDI (histórico)")


@udi_app.command("download")
def udi_download(dispositivo_id: int, out: str = OUT) -> None:
    """Exporta o detalhe de um dispositivo como planilha (udi.xlsx)."""
    with handle_errors(), make_client() as client:
        write(client.udi.download(dispositivo_id), out, f"udi_{dispositivo_id}.xlsx")


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


@udi_app.command("search-gmdn")
def udi_search_gmdn(
    ctx: typer.Context,
    texto: str = typer.Argument(help="text matched against the term's name and definition"),
    page: int = typer.Option(1, "--page", min=1),
    size: int = typer.Option(20, "--size", min=1),
) -> None:
    """Busca de termos GMDN por texto (nomes em português)."""
    with handle_errors(), make_client() as client:
        result = client.udi.search_gmdn(page=page, size=size, conteudo=texto)
        total = f" (página {page} de {result.totalPages}, {result.totalElements} no total)"
        emit(ctx, result.content or [], "GMDN" + total)


@udi_app.command("get-gmdn")
def udi_get_gmdn(ctx: typer.Context, codigo: str) -> None:
    """Termo GMDN por código."""
    with handle_errors(), make_client() as client:
        emit(ctx, client.udi.get_gmdn(codigo), f"GMDN {codigo}")


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


@assunto_app.command("list")
def assunto_list(
    ctx: typer.Context,
    busca: str | None = typer.Option(None, "--busca", "-b", help="filter locally by text"),
) -> None:
    """Todos os códigos de assunto de peticionamento (uma requisição, ~2.600 linhas)."""
    with handle_errors(), make_client() as client:
        rows = client.assunto.all()
        if busca:
            rows = [r for r in rows if busca.lower() in (r.descricao or "").lower()]
        emit(ctx, rows, "Assuntos")


@assunto_app.command("get")
def assunto_get(ctx: typer.Context, codigo: int) -> None:
    """Detalhe de um assunto: sistema, serviços, formulários, checklist e taxas por porte."""
    with handle_errors(), make_client() as client:
        emit(ctx, client.assunto.get(codigo), f"Assunto {codigo}")


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
    formulario_id: int = typer.Argument(help="formulários[].id from `anvisa assunto get`"),
    out: str = OUT,
) -> None:
    """Baixa o arquivo de um formulário de assunto (a resposta não traz nome nem tipo)."""
    with handle_errors(), make_client() as client:
        write(client.assunto.formulario(formulario_id), out, f"formulario_{formulario_id}")


# --- dados ------------------------------------------------------------------


@dados_app.command("list")
def dados_list(ctx: typer.Context) -> None:
    """As tabelas publicadas: as do catálogo (com a URL de origem) e as derivadas delas."""
    rows = [
        {
            "name": d.name,
            "group": d.group,
            "title": d.title,
            "url": getattr(d, "url", ""),  # a Derivada has no file of its own
            "fontes": ", ".join(getattr(d, "fontes", ())),
        }
        for d in (*dados.CATALOG, *dados.DERIVADAS)
    ]
    if ctx.obj == Format.json:
        typer.echo(json.dumps(rows, ensure_ascii=False, indent=2))
        return
    table = Table(title="Dados abertos")
    for column in rows[0]:
        table.add_column(column)
    for row in rows:
        table.add_row(*row.values())
    Console().print(table)


@dados_app.command("build")
def dados_build(
    out: Path = typer.Option(..., "--out", "-o", help="directory to write the site into"),
    dataset: list[str] = typer.Option(
        None,
        "--dataset",
        "-d",
        help="a table name or group; repeat for more (default: all). With --skip-unchanged the "
        "other published tables are carried over, so the manifest stays complete; without it "
        "the manifest lists only these. Derived tables (produtos, empresas, medidas, "
        "cosmeticos_por_empresa, alimentos_por_produto) cannot be selected: they are rebuilt "
        "on every build from the tables present",
    ),
    skip_unchanged: str | None = typer.Option(
        None,
        "--skip-unchanged",
        metavar="MANIFEST",
        help="URL or path of the published manifest.json: download only what changed, "
        "and write nothing if nothing did",
    ),
    commit: str | None = typer.Option(
        None, envvar="GITHUB_SHA", help="recorded in the manifest; a new commit forces a build"
    ),
    workdir: Path | None = typer.Option(
        None, "--workdir", help="keep the downloaded CSVs here (default: a temporary directory)"
    ),
    row_group_size: int = typer.Option(
        ROW_GROUP_SIZE, "--row-group-size", min=1, help="DuckDB rounds it up to a multiple of 2048"
    ),
) -> None:
    """Baixa os CSVs, converte para Parquet e escreve manifest.json + index.html em --out.

    Progress goes to stderr; stdout gets one JSON object (the build, or `skipped`)."""
    with handle_errors(), dados_http() as http:
        result = dados.build(
            out,
            datasets=dados.select(dataset) if dataset else dados.CATALOG,
            workdir=workdir,
            skip_unchanged=skip_unchanged,
            commit=commit,
            http=http,
            row_group_size=row_group_size,
            log=lambda message: typer.echo(message, err=True),
        )
    if isinstance(result, dados.Unchanged):
        summary = {"skipped": True, "reason": "unchanged", "published_build_id": result.build_id}
    else:
        summary = {
            "build_id": result["build_id"],
            "out": str(out),
            "tables": {
                name: {k: t[k] for k in ("rows", "bytes", "nulls_added", "rejected_records")}
                for name, t in result["tables"].items()
            },
        }
    typer.echo(json.dumps(summary, ensure_ascii=False, indent=2))


# --- saude ------------------------------------------------------------------


def paginated(
    ctx: typer.Context,
    domain: Any,
    page: int,
    size: int,
    all_pages: bool,
    title: str,
    **filters: Any,
) -> None:
    """Emit one page of a paginated search, or every page with `--all`."""
    with handle_errors(), make_client() as client:
        target = getattr(client, domain)
        if all_pages:
            emit(ctx, list(target.iter_search(size=size, **filters)), title)
        else:
            result = target.search(page=page, size=size, **filters)
            total = f" (página {page} de {result.totalPages}, {result.totalElements} no total)"
            emit(ctx, result.content or [], title + total)


PAGE = typer.Option(1, "--page", min=1)
SIZE = typer.Option(20, "--size", min=1)
ALL = typer.Option(False, "--all", help="iterate every page (respects the rate limit)")


@saude_app.command("search")
def saude_search(
    ctx: typer.Context,
    nome: str | None = typer.Option(None, "--nome", "-n", help="nomeProduto, prefix/substring"),
    cnpj: str | None = typer.Option(None, "--cnpj", help="cnpj of the company"),
    registro: str | None = typer.Option(None, "--registro", help="registro (untested filter)"),
    processo: str | None = typer.Option(None, "--processo", help="numeroProcesso (untested)"),
    page: int = PAGE,
    size: int = SIZE,
    all_pages: bool = ALL,
) -> None:
    """Busca de produtos para saúde regularizados. Sem filtro lista todos (193 mil)."""
    filters = {
        k: v
        for k, v in {
            "nomeProduto": nome,
            "cnpj": cnpj,
            "registro": registro,
            "numeroProcesso": processo,
        }.items()
        if v
    }
    paginated(ctx, "saude", page, size, all_pages, "Produtos para saúde", **filters)


@saude_app.command("get")
def saude_get(
    ctx: typer.Context, numero_processo: str = typer.Argument(help="processo, 17 digits")
) -> None:
    """Detalhe de um produto para saúde: apresentações, fabricantes, risco."""
    with handle_errors(), make_client() as client:
        emit(ctx, client.saude.get(numero_processo), f"Produto {numero_processo}")


@saude_app.command("download")
def saude_download(
    numero_processo: str = typer.Argument(help="processo, 17 digits"), out: str = OUT
) -> None:
    """Exporta o detalhe do produto como planilha (DetalheProdutoSaude.xls)."""
    with handle_errors(), make_client() as client:
        write(client.saude.download(numero_processo), out, f"saude_{numero_processo}.xls")


@saude_app.command("download-pdf")
def saude_download_pdf(
    numero_processo: str = typer.Argument(help="processo, 17 digits"), out: str = OUT
) -> None:
    """Exporta o detalhe do produto como PDF."""
    with handle_errors(), make_client() as client:
        write(client.saude.download_pdf(numero_processo), out, f"saude_{numero_processo}.pdf")


# --- certificado / certificado-medicamento ----------------------------------


def certificado_filters_from(
    nacional: bool,
    internacional: bool,
    status: str | None,
    cnpj: str | None,
    uf: str | None,
    **more: Any,
) -> dict[str, Any]:
    filters: dict[str, Any] = {}
    if nacional:
        filters["nacional"] = True
    if internacional:
        filters["internacional"] = True
    filters.update({k: v for k, v in {"status": status, "ufs": uf, **more}.items() if v})
    if "cnpjCertificada" not in filters and cnpj:
        filters["cnpjCertificada"] = cnpj
    return filters


NACIONAL = typer.Option(False, "--nacional", help="include Brazilian companies")
INTERNACIONAL = typer.Option(False, "--internacional", help="include foreign companies")
STATUS = typer.Option(None, "--status", help="ids from `status`, comma-separated (0 = válido)")
CNPJ_CERTIFICADA = typer.Option(None, "--cnpj", help="cnpjCertificada")
UF = typer.Option(None, "--uf", help="ufs, comma-separated (untested filter)")


@certificado_app.command("search")
def certificado_search(
    ctx: typer.Context,
    nacional: bool = NACIONAL,
    internacional: bool = INTERNACIONAL,
    status: str | None = STATUS,
    cnpj: str | None = CNPJ_CERTIFICADA,
    uf: str | None = UF,
    page: int = PAGE,
    size: int = SIZE,
    all_pages: bool = ALL,
) -> None:
    """Certificados de boas práticas (exceto medicamentos). Pelo menos um filtro é obrigatório."""
    filters = certificado_filters_from(nacional, internacional, status, cnpj, uf)
    paginated(ctx, "certificado", page, size, all_pages, "Certificados", **filters)


@certificado_app.command("get")
def certificado_get(ctx: typer.Context, certificado_id: int) -> None:
    """Um certificado por idCertificado, com as linhas de certificação."""
    with handle_errors(), make_client() as client:
        emit(ctx, client.certificado.get(certificado_id), f"Certificado {certificado_id}")


@certificado_app.command("status")
def certificado_status(ctx: typer.Context) -> None:
    """Status possíveis (Válido=0, Indeferido=1, Cancelado=2, Vencido=3)."""
    with handle_errors(), make_client() as client:
        emit(ctx, client.certificado.status(), "Status")


@certificado_app.command("concedida-por")
def certificado_concedida_por(ctx: typer.Context) -> None:
    """Formas de concessão da certificação (inspeção, análise de risco, ...)."""
    with handle_errors(), make_client() as client:
        emit(ctx, client.certificado.concedida_por(), "Certificação concedida por")


@certificado_app.command("download")
def certificado_download(
    nacional: bool = NACIONAL,
    internacional: bool = INTERNACIONAL,
    status: str | None = STATUS,
    cnpj: str | None = CNPJ_CERTIFICADA,
    uf: str | None = UF,
    out: str = OUT,
) -> None:
    """Exporta os certificados filtrados como planilha (consulta_certificado.xls, OOXML)."""
    filters = certificado_filters_from(nacional, internacional, status, cnpj, uf)
    with handle_errors(), make_client() as client:
        write(client.certificado.download(**filters), out, "consulta_certificado.xls")


@certificado_medicamento_app.command("search")
def certificado_medicamento_search(
    ctx: typer.Context,
    nacional: bool = NACIONAL,
    internacional: bool = INTERNACIONAL,
    status: str | None = STATUS,
    cnpj: str | None = CNPJ_CERTIFICADA,
    solicitante: str | None = typer.Option(None, "--solicitante", help="cnpjSolicitante"),
    uf: str | None = UF,
    page: int = PAGE,
    size: int = SIZE,
    all_pages: bool = ALL,
) -> None:
    """Certificados de boas práticas de medicamentos. Pelo menos um filtro é obrigatório."""
    filters = certificado_filters_from(
        nacional, internacional, status, cnpj, uf, cnpjSolicitante=solicitante
    )
    paginated(
        ctx,
        "certificado_medicamento",
        page,
        size,
        all_pages,
        "Certificados (medicamentos)",
        **filters,
    )


@certificado_medicamento_app.command("get")
def certificado_medicamento_get(ctx: typer.Context, certificado_id: int) -> None:
    """Um certificado por idCertificado, com linhas, classes e formas farmacêuticas."""
    with handle_errors(), make_client() as client:
        emit(
            ctx, client.certificado_medicamento.get(certificado_id), f"Certificado {certificado_id}"
        )


@certificado_medicamento_app.command("status")
def certificado_medicamento_status(ctx: typer.Context) -> None:
    """Status possíveis (Válido=0, Indeferido=1, Cancelado=2, Vencido=3)."""
    with handle_errors(), make_client() as client:
        emit(ctx, client.certificado_medicamento.status(), "Status")


@certificado_medicamento_app.command("concedida-por")
def certificado_medicamento_concedida_por(ctx: typer.Context) -> None:
    """Formas de concessão da certificação."""
    with handle_errors(), make_client() as client:
        emit(ctx, client.certificado_medicamento.concedida_por(), "Certificação concedida por")


@certificado_medicamento_app.command("linhas")
def certificado_medicamento_linhas(ctx: typer.Context) -> None:
    """Linhas de certificação (carbapenêmicos, cefalosporínicos, citotóxicos, ...)."""
    with handle_errors(), make_client() as client:
        emit(ctx, client.certificado_medicamento.linhas_certificacao(), "Linhas de certificação")


@certificado_medicamento_app.command("formas")
def certificado_medicamento_formas(ctx: typer.Context) -> None:
    """Formas farmacêuticas básicas."""
    with handle_errors(), make_client() as client:
        emit(ctx, client.certificado_medicamento.formas_farmaceuticas(), "Formas farmacêuticas")


@certificado_medicamento_app.command("classes")
def certificado_medicamento_classes(ctx: typer.Context) -> None:
    """Classes de certificação."""
    with handle_errors(), make_client() as client:
        emit(ctx, client.certificado_medicamento.classes_certificacao(), "Classes de certificação")


@certificado_medicamento_app.command("download")
def certificado_medicamento_download(
    nacional: bool = NACIONAL,
    internacional: bool = INTERNACIONAL,
    status: str | None = STATUS,
    cnpj: str | None = CNPJ_CERTIFICADA,
    solicitante: str | None = typer.Option(None, "--solicitante", help="cnpjSolicitante"),
    uf: str | None = UF,
    out: str = OUT,
) -> None:
    """Exporta os certificados filtrados como planilha (consulta_certificado.xls, OOXML)."""
    filters = certificado_filters_from(
        nacional, internacional, status, cnpj, uf, cnpjSolicitante=solicitante
    )
    with handle_errors(), make_client() as client:
        write(client.certificado_medicamento.download(**filters), out, "consulta_certificado.xls")


# --- tabaco -----------------------------------------------------------------


@tabaco_app.command("search")
def tabaco_search(
    ctx: typer.Context,
    nome: str | None = typer.Option(None, "--nome", "-n", help="nomeProduto"),
    page: int = PAGE,
    size: int = SIZE,
    all_pages: bool = ALL,
) -> None:
    """Produtos derivados do tabaco. Sem filtro lista todos (3,6 mil); a API ignora o CNPJ."""
    filters = {"nomeProduto": nome} if nome else {}
    paginated(ctx, "tabaco", page, size, all_pages, "Produtos de tabaco", **filters)


@tabaco_app.command("get")
def tabaco_get(
    ctx: typer.Context, cod_documento: int = typer.Argument(help="codDocumento from `search`")
) -> None:
    """Um produto com embalagens, rótulos e medidas cautelares."""
    with handle_errors(), make_client() as client:
        emit(ctx, client.tabaco.get(cod_documento), f"Produto {cod_documento}")


@tabaco_app.command("tipos")
def tabaco_tipos(ctx: typer.Context) -> None:
    """Tipos de produto (bidis, charuto, cigarro, ...)."""
    with handle_errors(), make_client() as client:
        emit(ctx, client.tabaco.tipos(), "Tipos de produto")


@tabaco_app.command("situacoes")
def tabaco_situacoes(ctx: typer.Context) -> None:
    """Situações (Ativo=S, Inativo=N)."""
    with handle_errors(), make_client() as client:
        emit(ctx, client.tabaco.situacoes(), "Situações")


@tabaco_app.command("origens")
def tabaco_origens(ctx: typer.Context) -> None:
    """Origens de fabricação."""
    with handle_errors(), make_client() as client:
        emit(ctx, client.tabaco.origens(), "Origens")


@tabaco_app.command("destinos")
def tabaco_destinos(ctx: typer.Context) -> None:
    """Destinos de comercialização."""
    with handle_errors(), make_client() as client:
        emit(ctx, client.tabaco.destinos(), "Destinos")


@tabaco_app.command("categorias-aditivo")
def tabaco_categorias_aditivo(ctx: typer.Context) -> None:
    """Categorias de aditivos."""
    with handle_errors(), make_client() as client:
        emit(ctx, client.tabaco.categorias_aditivo(), "Categorias de aditivo")


@tabaco_app.command("download")
def tabaco_download(out: str = OUT) -> None:
    """Exporta a tabela inteira como planilha (consulta_fumigenos.xls, ~900 KB)."""
    with handle_errors(), make_client() as client:
        write(client.tabaco.download(), out, "consulta_fumigenos.xls")


@tabaco_app.command("download-pdf")
def tabaco_download_pdf(
    cod_documento: int = typer.Argument(help="codDocumento from `search`"), out: str = OUT
) -> None:
    """Exporta o detalhe do produto como PDF."""
    with handle_errors(), make_client() as client:
        write(client.tabaco.download_pdf(cod_documento), out, f"tabaco_{cod_documento}.pdf")


@tabaco_app.command("download-rotulo")
def tabaco_download_rotulo(
    rotulo_id: int = typer.Argument(help="rotulos[].id from `get`"), out: str = OUT
) -> None:
    """Baixa o rótulo (PDF) de um produto."""
    with handle_errors(), make_client() as client:
        write(client.tabaco.download_rotulo(rotulo_id), out, f"rotulo_{rotulo_id}.pdf")
