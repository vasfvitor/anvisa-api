"""One build: download, check, convert, and write `manifest.json` + `index.html` under `out`.

Layout of `out` (what GitHub Pages serves):

    manifest.json                     what the frontend reads first (schema below)
    index.html                        a human-readable listing
    dicionario.html                   the data dictionary: notes and columns of every table
    data/<build_id>/<name>.parquet    immutable: a new build gets a new directory
    data/<build_id>/<name>/           the search files of a dataset with `busca` (busca.py)
    data/<build_id>/<derived>.parquet tables built from the others (derivar.py), every build

Versioned paths make a published file immutable: a browser that fetched the manifest keeps
reading files that all belong to one build, and caches (Pages, the browser) can never serve a
stale file under a current name. After the next deploy the old path answers 404, and the
frontend re-reads the manifest. (HTTP Range is not used on Pages; see the README.)
"""

from __future__ import annotations

import hashlib
import html
import json
import posixpath
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import httpx

from .. import __version__
from ..errors import DadosError
from . import busca, derivar
from .catalog import CATALOG, Dataset
from .convert import ROW_GROUP_SIZE, Stats, convert
from .fetch import Source, download, fetch_file, http_client
from .parse import check_header, read_header

SCHEMA_VERSION = 1
TIMEZONE = "America/Sao_Paulo"  # TIMESTAMP columns are naive local time, as ANVISA writes them
DOCS = "dicionario.html"  # the data dictionary, generated beside the manifest
LICENSE = {"data": "dados abertos da ANVISA (dados.anvisa.gov.br)", "code": "MIT"}
REPO = "https://github.com/vasfvitor/anvisa-api"
CHANGELOG = f"{REPO}/blob/main/CHANGELOG.dados.md"  # the data product's own log, by build
BY_NAME = {ds.name: ds for ds in CATALOG}


@dataclass(frozen=True)
class Unchanged:
    """`build` skipped: every source answered 304 and the published build came from the same
    commit, so the published files are still current."""

    build_id: str | None


def build_id(now: datetime) -> str:
    return now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def published_manifest(
    http: httpx.Client, location: str, log: Callable[[str], None] = lambda message: None
) -> dict[str, Any]:
    """The manifest currently live at `location` (a URL or a local path), or {} if there is
    none yet or it cannot be read: the build then simply runs, and says why."""
    try:
        if location.startswith(("http://", "https://")):
            response = http.get(location, headers={"Cache-Control": "no-cache"})
            if not response.is_success:
                raise DadosError(f"HTTP {response.status_code}")
            data = response.json()
        else:
            data = json.loads(Path(location).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise DadosError(f"not a JSON object: {type(data).__name__}")
    except (httpx.HTTPError, OSError, ValueError, DadosError) as exc:
        log(f"could not read the published manifest at {location} ({exc}): building everything")
        return {}
    return data


def _is_url(location: str) -> bool:
    return location.startswith(("http://", "https://"))


def _copy_published(http: httpx.Client, location: str, path: str, dest: Path) -> tuple[int, str]:
    """`path` (relative to the manifest at `location`) into `dest`: its size and SHA-256."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if _is_url(location):
        return fetch_file(http, urljoin(location, path), dest)
    source = Path(location).parent / path
    if not source.is_file():
        raise DadosError(f"published file is missing: {source}")
    shutil.copyfile(source, dest)
    return dest.stat().st_size, _sha256(dest)


def carry_over(
    http: httpx.Client,
    published: dict[str, Any],
    location: str,
    names: list[str],
    out: Path,
    bid: str,
    log: Callable[[str], None],
) -> dict[str, Any]:
    """Published tables not rebuilt this time: copied from the live site (or the local build
    `location` points into) under this build's directory, checked against the published size
    and SHA-256, and listed again under the new path. A deploy replaces the whole site, so a
    table left out of the manifest would simply disappear. A table's search files come along
    the same way: `indice.json` checked against its hash, then every file it lists against its
    size."""
    tables = {}
    for name in names:
        entry = published["tables"][name]
        if not all(key in entry for key in ("path", "sha256", "bytes")):
            raise DadosError(f"published table {name} lacks path/sha256/bytes; rebuild it")
        dest = out / "data" / bid / f"{name}.parquet"
        log(f"carry over {entry['path']} -> {dest.relative_to(out).as_posix()}")
        size, digest = _copy_published(http, location, entry["path"], dest)
        if (size, digest) != (entry["bytes"], entry["sha256"]):
            dest.unlink()
            raise DadosError(f"published table {name} does not match its manifest entry")
        tables[name] = refresh_metadata(
            {**entry, "path": dest.relative_to(out).as_posix()}, BY_NAME[name]
        )
        if "busca" in entry:
            folder = out / "data" / bid / name
            carried = carry_over_busca(http, entry["busca"], location, folder, log)
            tables[name]["busca"] = busca_entry(carried, folder, out)
    return tables


def carry_over_busca(
    http: httpx.Client,
    entry: dict[str, Any],
    location: str,
    folder: Path,
    log: Callable[[str], None],
) -> dict[str, Any]:
    """The published search files into `folder`; `entry` is the table's `busca` object."""
    name = folder.name
    index_path = entry["indice"]
    dest = folder / "indice.json"
    size, digest = _copy_published(http, location, index_path, dest)
    if (size, digest) != (entry["bytes"], entry["sha256"]):
        dest.unlink()
        raise DadosError(f"published search index of {name} does not match its manifest entry")
    files = busca.listed(json.loads(dest.read_text(encoding="utf-8")))
    log(f"carry over {len(files)} search files of {name}")
    for relative, expected in files:
        path = posixpath.join(posixpath.dirname(index_path), relative)
        size, _ = _copy_published(http, location, path, folder / relative)
        if size != expected:
            raise DadosError(f"published search file {name}/{relative} is not {expected} bytes")
    return entry


def busca_entry(info: dict[str, Any], folder: Path, out: Path) -> dict[str, Any]:
    """The manifest's `busca` object: `info` with `indice` relative to the manifest."""
    return {**info, "indice": (folder / "indice.json").relative_to(out).as_posix()}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def _entry(
    ds: Dataset | derivar.Derivada,
    parquet: Path,
    out: Path,
    *,
    rows: int,
    row_group_size: int,
    sort: list[str],
    columns: list[dict[str, str]],
    source: dict[str, Any],
    nulls_added: dict[str, int],
    rejected_records: int,
) -> dict[str, Any]:
    """The manifest entry every table shares, converted or derived: one builder, so a new key
    reaches both kinds at once. `path`, `bytes` and `sha256` come from the file itself."""
    return {
        "group": ds.group,
        "title": ds.title,
        "path": parquet.relative_to(out).as_posix(),
        "bytes": parquet.stat().st_size,
        "sha256": _sha256(parquet),
        "rows": rows,
        "row_group_size": row_group_size,
        "sort": sort,
        "timezone": TIMEZONE,
        "columns": columns,
        "notes": list(ds.notes),
        "source": source,
        "nulls_added": nulls_added,
        "rejected_records": rejected_records,
    }


def table_entry(
    ds: Dataset, source: Source, stats: Stats, parquet: Path, out: Path, row_group_size: int
) -> dict[str, Any]:
    return _entry(
        ds,
        parquet,
        out,
        rows=stats.rows,
        row_group_size=row_group_size,
        sort=[c.lower() for c in ds.sort],
        columns=[
            {
                "name": name.lower(),
                "type": kind,
                "source": name,
                "description": ds.descriptions[name],
            }
            for name, kind in ds.columns.items()
        ],
        source={**asdict(source), "loaded_at": stats.loaded_at},
        nulls_added=stats.nulls_added,
        rejected_records=len(stats.rejected),
    )


def refresh_metadata(entry: dict[str, Any], ds: Dataset) -> dict[str, Any]:
    """A carried-over entry with this code's `title`, `notes` and column descriptions. The
    data fields stay (the file is the published one); the text is documentation, and it would
    otherwise lag until ANVISA happens to change that file. Columns are matched by name, so a
    published column this catalog no longer has keeps whatever it carried."""
    texts = {name.lower(): text for name, text in ds.descriptions.items()}
    columns = [
        {**column, "description": texts[column["name"]]} if column["name"] in texts else column
        for column in entry.get("columns", [])
    ]
    return {**entry, "title": ds.title, "notes": list(ds.notes), "columns": columns}


def derived_entry(
    d: derivar.Derivada,
    parquet: Path,
    rows: int,
    columns: list[tuple[str, str]],
    tables: dict[str, Any],
    out: Path,
    commit: str | None,
) -> dict[str, Any]:
    """A derived table's manifest entry. `source` is an object with `name`, `url` and
    `loaded_at` like a converted table's, so every reader of the manifest treats it alike: the
    URL is the recipe (derivar.py at this commit), the load time the latest among its sources.
    `fontes` names the tables it was built from."""
    loaded = [tables[f]["source"].get("loaded_at") for f in d.fontes]
    loaded = [value for value in loaded if value]
    ref = commit or "main"
    entry = _entry(
        d,
        parquet,
        out,
        rows=rows,
        row_group_size=ROW_GROUP_SIZE,
        sort=list(d.sort),
        columns=[
            {"name": name, "type": kind, "description": d.descriptions[name]}
            for name, kind in columns
        ],
        source={
            "name": "derivada de " + ", ".join(d.fontes),
            "url": f"{REPO}/blob/{ref}/packages/python/src/anvisa/dados/derivar.py",
            "loaded_at": max(loaded) if loaded else None,
        },
        nulls_added={},
        rejected_records=0,
    )
    return {**entry, "fontes": list(d.fontes)}


def build(
    out: Path,
    *,
    datasets: tuple[Dataset, ...] = CATALOG,
    workdir: Path | None = None,
    skip_unchanged: str | None = None,
    commit: str | None = None,
    http: httpx.Client | None = None,
    now: datetime | None = None,
    row_group_size: int = ROW_GROUP_SIZE,
    log: Callable[[str], None] = lambda message: None,
) -> dict[str, Any] | Unchanged:
    """Build `datasets` into `out` and return the manifest written there.

    With `skip_unchanged` (the URL or path of the live manifest), downloads are conditional on
    the published ETags; if nothing changed and `commit` matches the published one, nothing is
    written and `Unchanged` is returned. Published tables not in `datasets` are carried over
    unchanged, so the manifest stays complete; without a published manifest it lists only
    `datasets`."""
    out = Path(out)
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    bid = build_id(now)
    own_http = http is None
    http = http or http_client()
    try:
        published = published_manifest(http, skip_unchanged, log) if skip_unchanged else {}
        known = published.get("tables") or {}
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(workdir or tmp)
            work.mkdir(parents=True, exist_ok=True)

            sources: dict[str, Source | None] = {}
            for ds in datasets:
                previous = (known.get(ds.name) or {}).get("source") or {}
                log(f"download {ds.url}")
                sources[ds.name] = download(
                    http,
                    ds,
                    work,
                    etag=previous.get("etag"),
                    last_modified=previous.get("last_modified"),
                )
                if sources[ds.name] is None:
                    log(f"  not modified since build {published.get('build_id')}")

            if all(s is None for s in sources.values()):
                if published.get("commit") == commit:
                    log(f"unchanged: build {published.get('build_id')} is current")
                    return Unchanged(published.get("build_id"))
                log(f"code changed ({published.get('commit')} -> {commit}): rebuilding")
            for ds in datasets:  # a partial update still publishes a complete, consistent set
                if sources[ds.name] is None:
                    sources[ds.name] = download(http, ds, work)

            for ds in datasets:  # every header first, so a drift fails before any Parquet
                check_header(ds, read_header(work / ds.local_file))

            tables = {}
            for ds in datasets:
                parquet = out / "data" / bid / f"{ds.name}.parquet"
                log(f"convert {ds.file} -> {parquet.relative_to(out)}")
                groups = ds.row_group_size or row_group_size
                stats = convert(ds, work / ds.local_file, parquet, row_group_size=groups)
                source = sources[ds.name]
                assert source is not None
                tables[ds.name] = table_entry(ds, source, stats, parquet, out, groups)
                log(f"  {stats.rows} rows, {tables[ds.name]['bytes']} bytes")
                if ds.busca:
                    folder = out / "data" / bid / ds.name
                    log(f"search files {parquet.name} -> {folder.relative_to(out)}/")
                    info = busca.write(ds, parquet, folder)
                    tables[ds.name]["busca"] = busca_entry(info, folder, out)
                    log(f"  {info['arquivos']} files, {info['bytes_total']} bytes")

            carried = [name for name in known if name not in tables and name in BY_NAME]
            derived = {d.name for d in derivar.DERIVADAS}
            for name in known.keys() - BY_NAME.keys() - derived:
                log(f"dropping {name}: no longer in the catalog")
            if carried:
                assert skip_unchanged is not None  # `known` is non-empty only through it
                tables.update(carry_over(http, published, skip_unchanged, carried, out, bid, log))
                tables = {ds.name: tables[ds.name] for ds in CATALOG if ds.name in tables}

            # derived tables: always rebuilt, from this build's files (fresh or carried alike)
            for d in derivar.DERIVADAS:
                missing = [f for f in d.fontes if f not in tables]
                if missing:  # every published fonte was carried over, so only `-d x` without
                    if datasets != CATALOG:  # a published site gets here: nothing to derive from
                        log(f"skipping {d.name}: {', '.join(missing)} not in this build")
                        continue
                    raise DadosError(f"{d.name} needs {', '.join(missing)}, which this build lacks")
                parquet = out / "data" / bid / f"{d.name}.parquet"
                log(f"derive {d.name} from {', '.join(d.fontes)} -> {parquet.relative_to(out)}")
                paths = {f: out / tables[f]["path"] for f in d.fontes}
                rows, columns = derivar.write(d, paths, parquet, work / "spill-derivadas")
                tables[d.name] = derived_entry(d, parquet, rows, columns, tables, out, commit)
                log(f"  {rows} rows, {tables[d.name]['bytes']} bytes")
    finally:
        if own_http:
            http.close()

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "build_id": bid,
        "built_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "generator": f"anvisa-python/{__version__}",
        "commit": commit,
        "docs": DOCS,
        "license": LICENSE,
        "tables": tables,
    }
    (out / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (out / "index.html").write_text(index_html(manifest), encoding="utf-8")
    (out / DOCS).write_text(dicionario_html(manifest), encoding="utf-8")
    return manifest


def _search_cell(table: dict[str, Any]) -> str:
    if "busca" not in table:
        return ""
    b = table["busca"]
    link = f'<a href="{html.escape(b["indice"])}">{b["arquivos"]:,} files</a>'
    return f"{link}, {b['bytes_total']:,} bytes"


def index_html(manifest: dict[str, Any]) -> str:
    e = html.escape
    rows = "\n".join(
        f'<tr><td><a href="{e(t["path"])}">{e(name)}</a></td><td>{e(t["title"])}</td>'
        f"<td>{t['rows']:,}</td><td>{t['bytes']:,}</td><td>{e(t['source']['loaded_at'] or '')}"
        f'</td><td><a href="{e(t["source"]["url"])}">{e(t["source"]["name"])}</a></td>'
        f"<td>{_search_cell(t)}</td></tr>"
        for name, t in manifest["tables"].items()
    )
    first = next(iter(manifest["tables"].values()), {"path": "data/…/alimentos.parquet"})
    title = "ANVISA dados abertos em Parquet"
    body = f"""<h1>{title}</h1>
<p>Build <code>{e(manifest["build_id"])}</code> ({e(manifest["built_at"])}),
from <a href="https://dados.anvisa.gov.br/dados/">dados.anvisa.gov.br</a>.
Machine-readable index: <a href="manifest.json">manifest.json</a>.
Column meanings and notes per table: <a href="{e(DOCS)}">dicionário de dados</a>;
what changed and when: <a href="{CHANGELOG}">CHANGELOG.dados.md</a>.
Timestamps are Brasília local time.
The data is ANVISA's open data; the pipeline is MIT.</p>
<table>
<tr><th>table</th><th>title</th><th>rows</th><th>bytes</th><th>ANVISA load</th><th>source</th>
<th>search files</th></tr>
{rows}
</table>
<p>Query with DuckDB (the path is relative to this page):</p>
<pre><code>SELECT no_produto, marcas FROM '{e(first["path"])}'
WHERE nu_cnpj_empresa = '40208221000174';</code></pre>
<p>Generated by <a href="{REPO}">{e(manifest["generator"])}</a>.</p>"""
    return _page(title, body)


def _page(title: str, body: str) -> str:
    """The shell both Pages documents share: one stylesheet, Portuguese, phone-wide."""
    return f"""<!doctype html>
<html lang="pt-BR">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>body{{font:16px/1.5 system-ui,sans-serif;max-width:60rem;margin:2rem auto;padding:0 1rem}}
table{{border-collapse:collapse;width:100%}}
td,th{{border-bottom:1px solid #ccc;padding:.25rem .5rem;text-align:left;vertical-align:top}}
code{{word-break:break-all}} section{{margin-top:3rem}} small{{color:#555}}</style>
{body}
</html>
"""


def _n(value: int) -> str:
    """1.234.567: the dictionary page is Portuguese."""
    return f"{value:,}".replace(",", ".")


def _fontes(t: dict[str, Any]) -> str:
    if not t.get("fontes"):
        return ""
    links = ", ".join(f'<a href="#{html.escape(f)}">{html.escape(f)}</a>' for f in t["fontes"])
    return f" Derivada de {links}, refeita a cada build."


def _table_section(name: str, t: dict[str, Any]) -> str:
    e = html.escape
    notes = "".join(f"<li>{e(note)}</li>" for note in t.get("notes", []))
    nulls = t.get("nulls_added") or {}
    columns = "\n".join(
        f"<tr><td><code>{e(c['name'])}</code></td><td>{e(c['type'])}</td>"
        f"<td><code>{e(c.get('source') or '')}</code></td><td>{e(c.get('description') or '')}"
        + (
            f" <small>({_n(nulls[c['name']])} valores não lidos viraram nulos)</small>"
            if nulls.get(c["name"])
            else ""
        )
        + "</td></tr>"
        for c in t["columns"]
    )
    search = ""
    if "busca" in t:
        b = t["busca"]
        search = (
            f'<p>Arquivos de busca: <a href="{e(b["indice"])}">{_n(b["arquivos"])} arquivos</a>, '
            f"{_n(b['bytes_total'])} bytes (formato {b['versao']}).</p>"
        )
    sort = ", ".join(f"<code>{e(c)}</code>" for c in t.get("sort", []))
    return f"""<section id="{e(name)}">
<h2><a href="{e(t["path"])}">{e(name)}</a></h2>
<p>{e(t["title"])}. {_n(t["rows"])} linhas, {_n(t["bytes"])} bytes, ordenado por {sort}.
Origem: <a href="{e(t["source"]["url"])}">{e(t["source"]["name"])}</a>
(carga da ANVISA: {e(t["source"].get("loaded_at") or "não informada")}).{_fontes(t)}</p>
{f"<ul>{notes}</ul>" if notes else ""}
{search}
<table>
<tr><th>coluna</th><th>tipo</th><th>origem</th><th>descrição</th></tr>
{columns}
</table>
</section>"""


def dicionario_html(manifest: dict[str, Any]) -> str:
    """The data dictionary: every table's notes and columns, from the manifest alone, so it
    describes exactly the files beside it."""
    e = html.escape
    tables = manifest["tables"]
    menu = " · ".join(f'<a href="#{e(name)}">{e(name)}</a>' for name in tables)
    sections = "\n".join(_table_section(name, t) for name, t in tables.items())
    body = f"""<h1>Dicionário de dados</h1>
<p>Build <code>{e(manifest["build_id"])}</code> ({e(manifest["built_at"])}).
As colunas têm o nome da ANVISA em minúsculas; os horários são de Brasília, sem fuso.
Índice legível por máquina: <a href="manifest.json">manifest.json</a>
(as descrições abaixo estão em <code>columns[].description</code> e <code>notes</code>);
lista de arquivos: <a href="index.html">index.html</a>;
histórico do formato e das tabelas: <a href="{CHANGELOG}">CHANGELOG.dados.md</a>.</p>
<p>{menu}</p>
{sections}
<p>Dados: {e(manifest["license"]["data"])}. Código: {e(manifest["license"]["code"])},
<a href="{REPO}">{e(manifest["generator"])}</a>.</p>"""
    return _page("Dicionário de dados: ANVISA dados abertos em Parquet", body)
