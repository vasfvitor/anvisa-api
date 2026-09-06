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
    help="ANVISA Consultas Externas: fila de análise, UDI de dispositivos médicos e assuntos.",
    no_args_is_help=True,
)
fila_app = typer.Typer(
    help="Fila de análise (posição dos processos por subfila).", no_args_is_help=True
)
udi_app = typer.Typer(help="UDI de dispositivos médicos e termos GMDN.", no_args_is_help=True)
assunto_app = typer.Typer(
    help="Assuntos de peticionamento (documentos, formulários, taxas).", no_args_is_help=True
)
app.add_typer(fila_app, name="fila")
app.add_typer(udi_app, name="udi")
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


# --- udi --------------------------------------------------------------------


@udi_app.command("search")
def udi_search(
    ctx: typer.Context,
    nome: str | None = typer.Option(None, "--nome", "-n", help="nomeComercial (verified filter)"),
    udi_di: str | None = typer.Option(None, "--udi-di", help="udiDi (unverified)"),
    cnpj: str | None = typer.Option(None, "--cnpj", help="cnpjDetentora (unverified)"),
    gmdn: str | None = typer.Option(None, "--gmdn", help="codigoGmdn (unverified)"),
    registro: str | None = typer.Option(None, "--registro", help="nuRegistro (unverified)"),
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


@udi_app.command("gmdn")
def udi_gmdn(ctx: typer.Context, codigo: str) -> None:
    """Termo GMDN por código."""
    with handle_errors(), make_client() as client:
        emit(ctx, client.udi.termo_gmdn(codigo), f"GMDN {codigo}")


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
