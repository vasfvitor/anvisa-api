"""`anvisa` command line: fila, lista, UDI, nome técnico, assunto, produtos para saúde,
certificados de boas práticas, tabaco, bulário, medicamento, documento, Cannabis and restrição
lookups, and `dados`."""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

import httpx
import typer
from pydantic import BaseModel
from rich.console import Console
from rich.table import Table

from . import __version__, dados
from .client import Client, iterate_pages
from .dados.build import MIN_ROWS_RATIO, catalog_limits, published_manifest, source_ages
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
    "saúde, certificados de boas práticas, tabaco, bulário, medicamentos, documentos técnicos, "
    "Cannabis e restrições.",
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
bulario_app = typer.Typer(help="Bulário eletrônico: bulas e seu histórico.", no_args_is_help=True)
medicamento_app = typer.Typer(
    help="Medicamentos registrados e notificados, rótulos e listas de filtros.",
    no_args_is_help=True,
)
documento_app = typer.Typer(
    help="Documentos técnicos: petições de um processo, histórico, publicações no DOU.",
    no_args_is_help=True,
)
cannabis_app = typer.Typer(help="Produtos de Cannabis e seus arquivos.", no_args_is_help=True)
restricao_app = typer.Typer(help="Restrições de prescrição e uso.", no_args_is_help=True)
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
app.add_typer(bulario_app, name="bulario")
app.add_typer(medicamento_app, name="medicamento")
app.add_typer(documento_app, name="documento")
app.add_typer(cannabis_app, name="cannabis")
app.add_typer(restricao_app, name="restricao")
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
    size: int | None = typer.Option(
        None, "--size", min=1, help="rows per page: 20, or 100 with --all"
    ),
    all_pages: bool = typer.Option(
        False, "--all", help="iterate every page (respects the rate limit)"
    ),
) -> None:
    """Busca de dispositivos por UDI. Pelo menos um filtro é obrigatório."""
    filters = only_set(
        nomeComercial=nome, udiDi=udi_di, cnpjDetentora=cnpj, codigoGmdn=gmdn, nuRegistro=registro
    )
    paginated(ctx, "udi", page, size, all_pages, "UDI", **filters)


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
    size: int | None = typer.Option(
        None, "--size", min=1, help="rows per page: 20, or 100 with --all"
    ),
    all_pages: bool = typer.Option(
        False, "--all", help="iterate every page (respects the rate limit)"
    ),
) -> None:
    """Nomes técnicos, paginados. Sem filtro retorna todos (~2.700)."""
    filters = only_set(nomeTecnico=nome, codigo=codigo, categoriaProduto=categoria)
    paginated(ctx, "nome_tecnico", page, size, all_pages, "Nomes técnicos", **filters)


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
    filters = only_set(nomeTecnico=nome, categoriaProduto=categoria)
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
        help="a table name or group; repeat for more (default: all, but dcb only with --fontes, "
        "since the Biblioteca Digital blocks automated access). With --skip-unchanged the "
        "other published tables are carried over, so the manifest stays complete; without it "
        "the manifest lists only these. Derived tables (produtos, empresas, medidas, "
        "cosmeticos_por_empresa, alimentos_por_produto, medicamentos_por_produto, "
        "produtos_saude_por_produto, alimentos_fabricacao, certificados, "
        "produtos_saude_fabricacao, substancias_dcb) cannot be selected: they are "
        "rebuilt on every build from the tables present",
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
    fontes: Path | None = typer.Option(
        None, "--fontes", help="read the DCB list from dcb/ in this directory, with no request"
    ),
    row_group_size: int = typer.Option(
        ROW_GROUP_SIZE, "--row-group-size", min=1, help="DuckDB rounds it up to a multiple of 2048"
    ),
    min_rows_ratio: float = typer.Option(
        MIN_ROWS_RATIO,
        "--min-rows-ratio",
        min=0,
        max=1,
        help="refuse a table with fewer rows than this share of the published build",
    ),
) -> None:
    """Baixa os CSVs, converte para Parquet e escreve manifest.json + index.html em --out.

    Progress goes to stderr; stdout gets one JSON object (the build, or `skipped`)."""
    padrao = dados.CATALOG if fontes else dados.PADRAO
    with handle_errors(), dados_http() as http:
        result = dados.build(
            out,
            datasets=dados.select(dataset) if dataset else padrao,
            workdir=workdir,
            fontes=fontes,
            skip_unchanged=skip_unchanged,
            commit=commit,
            http=http,
            row_group_size=row_group_size,
            min_rows_ratio=min_rows_ratio,
            log=lambda message: typer.echo(message, err=True),
        )
    if isinstance(result, dados.Unchanged):
        summary = {"skipped": True, "reason": "unchanged", "published_build_id": result.build_id}
        if result.falhas:
            summary["falhas"] = result.falhas
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


@dados_app.command("idade")
def dados_idade(
    manifest: str = typer.Argument(..., help="URL or path of a manifest.json"),
    max_dias: int | None = typer.Option(
        None,
        "--max-dias",
        min=0,
        help="one limit for every table that has one, instead of each table's max_age_days",
    ),
) -> None:
    """Falha quando a fonte de alguma tabela passou do seu limite de dias sem mudar.

    Each table's limit is its catalog `max_age_days`: 7 for the files ANVISA rewrites on
    weekdays, 45 for the monthly CMED list, none for a file no longer refreshed or kept by hand.
    Exits 1 naming every table over its limit, or when the manifest cannot be read or records no
    Last-Modified, so a stopped ETL turns the scheduled job red even while another file moves."""
    with dados_http() as http:
        data = published_manifest(http, manifest, log=lambda message: typer.echo(message, err=True))
    limits = catalog_limits()
    if max_dias is not None:
        limits = {name: max_dias for name, limit in limits.items() if limit is not None}
    ages = source_ages(data, datetime.now(timezone.utc), limits)
    if not ages:
        typer.echo(f"::error::no source Last-Modified in {manifest}", err=True)
        raise typer.Exit(1)
    for age in ages:
        typer.echo(
            f"{age.table}: {age.last_modified:%Y-%m-%d %H:%M} ({age.days} days ago, "
            f"limit {age.limit})"
        )
    stale = [age for age in ages if age.stale]
    for age in stale:
        typer.echo(
            f"::error::{age.table}: ANVISA's file unchanged for {age.days} days "
            f"(since {age.last_modified:%Y-%m-%d}, limit {age.limit})",
            err=True,
        )
    if stale:
        raise typer.Exit(1)


# --- saude ------------------------------------------------------------------


def only_set(**filters: Any) -> dict[str, Any]:
    """The filters the user gave: unset (None) and empty options are not sent."""
    return {k: v for k, v in filters.items() if v}


def page_size(size: int | None, all_pages: bool) -> int:
    """`--size` when given; otherwise 20 for one page and 100 for `--all`, so walking every page
    takes a fifth of the requests (8,855 bulário rows: 89 instead of 443)."""
    return size or (100 if all_pages else 20)


def paginated(
    ctx: typer.Context,
    domain: Any,
    page: int,
    size: int | None,
    all_pages: bool,
    title: str,
    **filters: Any,
) -> None:
    """Emit one page of a paginated search, or every page with `--all`."""
    size = page_size(size, all_pages)
    with handle_errors(), make_client() as client:
        target = getattr(client, domain)
        if all_pages:
            emit(ctx, list(target.iter_search(size=size, **filters)), title)
        else:
            result = target.search(page=page, size=size, **filters)
            total = f" (página {page} de {result.totalPages}, {result.totalElements} no total)"
            emit(ctx, result.content or [], title + total)


PAGE = typer.Option(1, "--page", min=1)
SIZE = typer.Option(None, "--size", min=1, help="rows per page: 20, or 100 with --all")
ALL = typer.Option(False, "--all", help="iterate every page (respects the rate limit)")


@saude_app.command("search")
def saude_search(
    ctx: typer.Context,
    nome: str | None = typer.Option(None, "--nome", "-n", help="nomeProduto, prefix/substring"),
    cnpj: str | None = typer.Option(None, "--cnpj", help="cnpj of the company"),
    registro: str | None = typer.Option(None, "--registro", help="registro (untested filter)"),
    processo: str | None = typer.Option(None, "--processo", help="numeroProcesso (untested)"),
    page: int = PAGE,
    size: int | None = SIZE,
    all_pages: bool = ALL,
) -> None:
    """Busca de produtos para saúde regularizados. Sem filtro lista todos (193 mil)."""
    filters = only_set(nomeProduto=nome, cnpj=cnpj, registro=registro, numeroProcesso=processo)
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


@saude_app.command("download-anexo")
def saude_download_anexo(
    numero_processo: str = typer.Argument(help="processo, 17 digits"),
    anexo_cod: str = typer.Argument(help="arquivos[].anexoCod from `get`"),
    nome_arquivo: str = typer.Argument(help="arquivos[].nomeArquivo from `get`"),
    out: str = OUT,
) -> None:
    """Baixa um anexo do produto (arquivos[] do detalhe). Nunca visto com sucesso."""
    with handle_errors(), make_client() as client:
        write(
            client.saude.download_anexo(numero_processo, anexo_cod, nome_arquivo), out, nome_arquivo
        )


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
    filters.update(only_set(status=status, ufs=uf, **more))
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
    size: int | None = SIZE,
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
    size: int | None = SIZE,
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
    size: int | None = SIZE,
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


# --- bulario ----------------------------------------------------------------


@bulario_app.command("search")
def bulario_search(
    ctx: typer.Context,
    nome: str | None = typer.Option(None, "--nome", "-n", help="nomeProduto"),
    page: int = PAGE,
    size: int | None = SIZE,
    all_pages: bool = ALL,
) -> None:
    """Busca de produtos com bula. Sem filtro lista todos (8,8 mil)."""
    paginated(ctx, "bulario", page, size, all_pages, "Bulário", **only_set(nomeProduto=nome))


@bulario_app.command("get")
def bulario_get(
    ctx: typer.Context,
    id_produto: int = typer.Argument(help="idProduto from `search`"),
    page: int = PAGE,
    size: int = typer.Option(10, "--size", min=1),
) -> None:
    """Bula atual e uma página do histórico de bulas de um produto."""
    with handle_errors(), make_client() as client:
        emit(ctx, client.bulario.get(id_produto, page, size), f"Bulário {id_produto}")


# --- medicamento ------------------------------------------------------------


@medicamento_app.command("search")
def medicamento_search(
    ctx: typer.Context,
    nome: str | None = typer.Option(None, "--nome", "-n", help="nomeProduto"),
    cnpj: str | None = typer.Option(None, "--cnpj", help="cnpj of the company"),
    registro: str | None = typer.Option(None, "--registro", help="numeroRegistro (untested)"),
    processo: str | None = typer.Option(None, "--processo", help="numeroProcesso (untested)"),
    page: int = PAGE,
    size: int | None = SIZE,
    all_pages: bool = ALL,
) -> None:
    """Busca de medicamentos. Exige ao menos um filtro."""
    filters = only_set(
        nomeProduto=nome, cnpj=cnpj, numeroRegistro=registro, numeroProcesso=processo
    )
    paginated(ctx, "medicamento", page, size, all_pages, "Medicamentos", **filters)


@medicamento_app.command("get")
def medicamento_get(
    ctx: typer.Context, numero_processo: str = typer.Argument(help="processo.numero from `search`")
) -> None:
    """Detalhe de um medicamento: apresentações, rótulos, categoria regulatória."""
    with handle_errors(), make_client() as client:
        emit(ctx, client.medicamento.get(numero_processo), f"Medicamento {numero_processo}")


@medicamento_app.command("get-codigo")
def medicamento_get_codigo(
    ctx: typer.Context,
    codigo_produto: str = typer.Argument(help="produto.codigo from `search`"),
    codigo_notificacao: str | None = typer.Option(None, "--notificacao", help="untested"),
) -> None:
    """O mesmo detalhe, pelo código do produto."""
    with handle_errors(), make_client() as client:
        detail = client.medicamento.get_codigo(codigo_produto, codigo_notificacao)
        emit(ctx, detail, f"Medicamento {codigo_produto}")


@medicamento_app.command("clones")
def medicamento_clones(
    ctx: typer.Context, numero_processo: str = typer.Argument(help="processo, 17 digits")
) -> None:
    """Processos clone de um medicamento."""
    with handle_errors(), make_client() as client:
        emit(ctx, client.medicamento.clones(numero_processo), f"Clones de {numero_processo}")


@medicamento_app.command("ppam")
def medicamento_ppam(ctx: typer.Context) -> None:
    """Nomes da lista PPAM."""
    with handle_errors(), make_client() as client:
        names = client.medicamento.ppam()
    if ctx.obj == Format.json:
        typer.echo(json.dumps(names, ensure_ascii=False, indent=2))
    else:
        typer.echo("\n".join(names))


@medicamento_app.command("download")
def medicamento_download(
    nome: str | None = typer.Option(None, "--nome", "-n", help="nomeProduto"),
    cnpj: str | None = typer.Option(None, "--cnpj", help="cnpj of the company"),
    registro: str | None = typer.Option(None, "--registro", help="numeroRegistro (untested)"),
    processo: str | None = typer.Option(None, "--processo", help="numeroProcesso (untested)"),
    size: int = typer.Option(10, "--size", min=1, help="sent in the body; 10 when recorded"),
    out: str = OUT,
) -> None:
    """Exporta os medicamentos do filtro como planilha (consulta_medicamento.xls). Exige ao
    menos um filtro, como a busca."""
    filters = only_set(
        nomeProduto=nome, cnpj=cnpj, numeroRegistro=registro, numeroProcesso=processo
    )
    with handle_errors(), make_client() as client:
        write(client.medicamento.download(size, **filters), out, "consulta_medicamento.xls")


@medicamento_app.command("download-rotulo")
def medicamento_download_rotulo(
    tipo_produto: int = typer.Argument(help="tipoProduto from `get`"),
    numero_processo: str = typer.Argument(help="processo, 17 digits"),
    origem: str = typer.Argument(help="anexoRotulos[].origem from `get`"),
    rotulo: int = typer.Argument(help="anexoRotulos[].codigoAnexo from `get`"),
    out: str = OUT,
) -> None:
    """Baixa um rótulo do medicamento (anexoRotulos[] do detalhe)."""
    with handle_errors(), make_client() as client:
        download = client.medicamento.download_rotulo(tipo_produto, numero_processo, origem, rotulo)
        write(download, out, f"rotulo_{rotulo}.pdf")


@medicamento_app.command("categorias-regulatorias")
def medicamento_categorias_regulatorias(ctx: typer.Context) -> None:
    """Categorias regulatórias (novo, genérico, similar, ...)."""
    with handle_errors(), make_client() as client:
        emit(ctx, client.medicamento.categorias_regulatorias(), "Categorias regulatórias")


@medicamento_app.command("tipos-categoria-regulatoria")
def medicamento_tipos_categoria_regulatoria(ctx: typer.Context) -> None:
    """As mesmas categorias, por /tipoCategoriaRegulatoria."""
    with handle_errors(), make_client() as client:
        emit(ctx, client.medicamento.tipos_categoria_regulatoria(), "Categorias regulatórias")


@medicamento_app.command("formas-fisicas")
def medicamento_formas_fisicas(ctx: typer.Context) -> None:
    """Formas físicas."""
    with handle_errors(), make_client() as client:
        emit(ctx, client.medicamento.formas_fisicas(), "Formas físicas")


@medicamento_app.command("categorias")
def medicamento_categorias(ctx: typer.Context) -> None:
    """Categorias de medicamento (id em código ATC)."""
    with handle_errors(), make_client() as client:
        emit(ctx, client.medicamento.categorias(), "Categorias")


@medicamento_app.command("atc")
def medicamento_atc(ctx: typer.Context) -> None:
    """Códigos ATC, todos os níveis."""
    with handle_errors(), make_client() as client:
        emit(ctx, client.medicamento.atc(), "ATC")


@medicamento_app.command("restricoes")
def medicamento_restricoes(ctx: typer.Context) -> None:
    """Restrições de prescrição e uso."""
    with handle_errors(), make_client() as client:
        emit(ctx, client.medicamento.restricoes(), "Restrições")


@medicamento_app.command("tarjas")
def medicamento_tarjas(ctx: typer.Context) -> None:
    """Tarjas."""
    with handle_errors(), make_client() as client:
        emit(ctx, client.medicamento.tarjas(), "Tarjas")


@medicamento_app.command("tipos-priorizacao")
def medicamento_tipos_priorizacao(
    ctx: typer.Context, show_empty: bool = typer.Option(False, "--show-empty")
) -> None:
    """Tipos de priorização de análise."""
    with handle_errors(), make_client() as client:
        emit(ctx, client.medicamento.tipos_priorizacao(show_empty), "Tipos de priorização")


@medicamento_app.command("substancias")
def medicamento_substancias(
    ctx: typer.Context,
    nome: str = typer.Argument(help="part of the substance name"),
    page: int = PAGE,
    size: int | None = SIZE,
    all_pages: bool = ALL,
) -> None:
    """Busca de substâncias pelo nome."""
    size = page_size(size, all_pages)
    with handle_errors(), make_client() as client:
        if all_pages:
            rows = list(iterate_pages(lambda p: client.medicamento.substancias(nome, p, size)))
            emit(ctx, rows, "Substâncias")
            return
        result = client.medicamento.substancias(nome, page, size)
        total = f" (página {page} de {result.totalPages}, {result.totalElements} no total)"
        emit(ctx, result.content or [], "Substâncias" + total)


@medicamento_app.command("produtos-notificados")
def medicamento_produtos_notificados(ctx: typer.Context) -> None:
    """Todos os produtos notificados (a API ignora página e filtro)."""
    with handle_errors(), make_client() as client:
        emit(ctx, client.medicamento.produtos_notificados().content or [], "Produtos notificados")


# --- documento --------------------------------------------------------------


@documento_app.command("search")
def documento_search(
    ctx: typer.Context,
    processo: str = typer.Argument(help="processo, 17 digits"),
    page: int = PAGE,
    size: int | None = SIZE,
    all_pages: bool = ALL,
) -> None:
    """Petições de um processo."""
    paginated(ctx, "documento", page, size, all_pages, f"Petições de {processo}", processo=processo)


@documento_app.command("get")
def documento_get(
    ctx: typer.Context, processo: str = typer.Argument(help="processo, 17 digits")
) -> None:
    """Um processo com todas as petições e publicações."""
    with handle_errors(), make_client() as client:
        emit(ctx, client.documento.get(processo), f"Processo {processo}")


@documento_app.command("get-expediente")
def documento_get_expediente(ctx: typer.Context, expediente: str) -> None:
    """Uma petição pelo número do expediente."""
    with handle_errors(), make_client() as client:
        emit(ctx, client.documento.get_expediente(expediente), f"Expediente {expediente}")


@documento_app.command("situacoes")
def documento_situacoes(ctx: typer.Context, expediente: str) -> None:
    """Histórico de situações de uma petição."""
    with handle_errors(), make_client() as client:
        emit(ctx, client.documento.situacoes(expediente), f"Situações de {expediente}")


@documento_app.command("get-publicacao")
def documento_get_publicacao(
    ctx: typer.Context, publicacao_id: int = typer.Argument(help="lastPublicacao.id")
) -> None:
    """Uma publicação no DOU."""
    with handle_errors(), make_client() as client:
        pub = client.documento.get_publicacao(publicacao_id)
    if pub is None:  # 200 with an empty body
        typer.echo("null" if ctx.obj == Format.json else "(nenhum resultado)")
        return
    emit(ctx, pub, f"Publicação {publicacao_id}")


class Lista(str, Enum):
    situacoes = "situacoes"
    assuntos = "assuntos"


@documento_app.command("listas")
def documento_listas(ctx: typer.Context, qual: Lista = typer.Argument(Lista.situacoes)) -> None:
    """Códigos de situação ou de assunto (a resposta inteira tem 1,2 MB)."""
    with handle_errors(), make_client() as client:
        listas = client.documento.listas()
    rows = listas.listaSituacao if qual == Lista.situacoes else listas.listaAssunto
    emit(ctx, rows or [], "Situações" if qual == Lista.situacoes else "Assuntos")


@documento_app.command("tipos-produto")
def documento_tipos_produto(ctx: typer.Context) -> None:
    """Tipos de produto."""
    with handle_errors(), make_client() as client:
        emit(ctx, client.documento.tipos_produto(), "Tipos de produto")


@documento_app.command("download-parecer")
def documento_download_parecer(
    id_arquivo_parecer: int = typer.Argument(help="arquivoParecer.idArquivoParecer"),
    expediente: str = typer.Argument(help="the petição's expediente"),
    out: str = OUT,
) -> None:
    """Baixa um parecer técnico. Nunca visto com sucesso."""
    with handle_errors(), make_client() as client:
        download = client.documento.download_parecer(id_arquivo_parecer, expediente)
        write(download, out, f"parecer_{id_arquivo_parecer}.pdf")


# --- cannabis ---------------------------------------------------------------


@cannabis_app.command("search")
def cannabis_search(
    ctx: typer.Context,
    nome: str | None = typer.Option(None, "--nome", "-n", help="nomeProduto"),
    cnpj: str | None = typer.Option(None, "--cnpj", help="cnpj (untested)"),
    page: int = PAGE,
    size: int | None = SIZE,
    all_pages: bool = ALL,
) -> None:
    """Busca de produtos de Cannabis. Exige ao menos um filtro."""
    filters = only_set(nomeProduto=nome, cnpj=cnpj)
    paginated(ctx, "cannabis", page, size, all_pages, "Produtos de Cannabis", **filters)


@cannabis_app.command("get")
def cannabis_get(
    ctx: typer.Context, numero_processo: str = typer.Argument(help="processo.numero from `search`")
) -> None:
    """Detalhe de um produto de Cannabis, com os códigos dos arquivos (válidos 5 minutos)."""
    with handle_errors(), make_client() as client:
        emit(ctx, client.cannabis.get(numero_processo), f"Produto {numero_processo}")


@cannabis_app.command("download")
def cannabis_download(out: str = OUT) -> None:
    """Exporta a tabela inteira como planilha (consulta_produto_cannabis.xls)."""
    with handle_errors(), make_client() as client:
        write(client.cannabis.download(), out, "consulta_produto_cannabis.xls")


@cannabis_app.command("download-anexo")
def cannabis_download_anexo(
    tipo: str = typer.Argument(help="rotulo (verified); other kinds untested"),
    codigo_hash: str = typer.Argument(help="codigoHash from `get`, within 5 minutes"),
    out: str = OUT,
) -> None:
    """Baixa um arquivo do produto (rótulo) pelo codigoHash do detalhe."""
    with handle_errors(), make_client() as client:
        write(client.cannabis.download_anexo(tipo, codigo_hash), out, f"cannabis_{tipo}.pdf")


# --- restricao --------------------------------------------------------------


@restricao_app.command("all")
def restricao_all(ctx: typer.Context) -> None:
    """Todas as restrições."""
    with handle_errors(), make_client() as client:
        emit(ctx, client.restricao.all(), "Restrições")


@restricao_app.command("cannabis")
def restricao_cannabis(ctx: typer.Context) -> None:
    """As restrições que valem para produtos de Cannabis."""
    with handle_errors(), make_client() as client:
        emit(ctx, client.restricao.cannabis(), "Restrições (Cannabis)")
