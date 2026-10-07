"""One build: download, check, convert, and write `manifest.json` + `index.html` under `out`.

Layout of `out` (what GitHub Pages serves):

    manifest.json                     what the frontend reads first (schema below)
    index.html                        a human-readable listing
    data/<build_id>/<name>.parquet    immutable: a new build gets a new directory

Versioned paths are what make DuckDB-WASM's HTTP Range reads safe: bytes under one path never
change, so a browser session cannot mix row groups from two builds. After the next deploy the
old path answers 404, and the frontend re-reads the manifest.
"""

from __future__ import annotations

import hashlib
import html
import json
import tempfile
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from .. import __version__
from .catalog import CATALOG, Dataset
from .convert import ROW_GROUP_SIZE, Stats, convert
from .fetch import Source, download, http_client
from .parse import check_header, read_header

SCHEMA_VERSION = 1
TIMEZONE = "America/Sao_Paulo"  # TIMESTAMP columns are naive local time, as ANVISA writes them


@dataclass(frozen=True)
class Unchanged:
    """`build` skipped: every source answered 304 and the published build came from the same
    commit, so the published files are still current."""

    build_id: str | None


def build_id(now: datetime) -> str:
    return now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def published_manifest(http: httpx.Client, location: str) -> dict[str, Any]:
    """The manifest currently live at `location` (a URL or a local path), or {} if there is
    none yet or it cannot be read: the build then simply runs."""
    try:
        if location.startswith(("http://", "https://")):
            response = http.get(location, headers={"Cache-Control": "no-cache"})
            if not response.is_success:
                return {}
            data = response.json()
        else:
            data = json.loads(Path(location).read_text(encoding="utf-8"))
    except (httpx.HTTPError, OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def table_entry(
    ds: Dataset, source: Source, stats: Stats, parquet: Path, out: Path, row_group_size: int
) -> dict[str, Any]:
    return {
        "group": ds.group,
        "title": ds.title,
        "path": parquet.relative_to(out).as_posix(),
        "bytes": parquet.stat().st_size,
        "sha256": _sha256(parquet),
        "rows": stats.rows,
        "row_group_size": row_group_size,
        "sort": [c.lower() for c in ds.sort],
        "timezone": TIMEZONE,
        "columns": [
            {"name": name.lower(), "type": kind, "source": name}
            for name, kind in ds.columns.items()
        ],
        "source": {**asdict(source), "loaded_at": stats.loaded_at},
        "nulls_added": stats.nulls_added,
        "rejected_records": len(stats.rejected),
    }


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
    """Build every dataset into `out` and return the manifest written there.

    With `skip_unchanged` (the URL or path of the live manifest), downloads are conditional on
    the published ETags; if nothing changed and `commit` matches the published one, nothing is
    written and `Unchanged` is returned."""
    out = Path(out)
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    bid = build_id(now)
    own_http = http is None
    http = http or http_client()
    try:
        published = published_manifest(http, skip_unchanged) if skip_unchanged else {}
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
                check_header(ds, read_header(work / ds.file))

            tables = {}
            for ds in datasets:
                parquet = out / "data" / bid / f"{ds.name}.parquet"
                log(f"convert {ds.file} -> {parquet.relative_to(out)}")
                groups = ds.row_group_size or row_group_size
                stats = convert(ds, work / ds.file, parquet, row_group_size=groups)
                source = sources[ds.name]
                assert source is not None
                tables[ds.name] = table_entry(ds, source, stats, parquet, out, groups)
                log(f"  {stats.rows} rows, {tables[ds.name]['bytes']} bytes")
    finally:
        if own_http:
            http.close()

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "build_id": bid,
        "built_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "generator": f"anvisa-python/{__version__}",
        "commit": commit,
        "tables": tables,
    }
    (out / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (out / "index.html").write_text(index_html(manifest), encoding="utf-8")
    return manifest


def index_html(manifest: dict[str, Any]) -> str:
    e = html.escape
    rows = "\n".join(
        f'<tr><td><a href="{e(t["path"])}">{e(name)}</a></td><td>{e(t["title"])}</td>'
        f"<td>{t['rows']:,}</td><td>{t['bytes']:,}</td><td>{e(t['source']['loaded_at'] or '')}"
        f'</td><td><a href="{e(t["source"]["url"])}">{e(t["source"]["name"])}</a></td></tr>'
        for name, t in manifest["tables"].items()
    )
    first = next(iter(manifest["tables"].values()), {"path": "data/…/alimentos.parquet"})
    return f"""<!doctype html>
<html lang="pt-BR">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ANVISA dados abertos em Parquet</title>
<style>body{{font:16px/1.5 system-ui,sans-serif;max-width:60rem;margin:2rem auto;padding:0 1rem}}
table{{border-collapse:collapse}}
td,th{{border-bottom:1px solid #ccc;padding:.25rem .5rem;text-align:left}}
code{{word-break:break-all}}</style>
<h1>ANVISA dados abertos em Parquet</h1>
<p>Build <code>{e(manifest["build_id"])}</code> ({e(manifest["built_at"])}),
from <a href="https://dados.anvisa.gov.br/dados/">dados.anvisa.gov.br</a>.
Machine-readable index: <a href="manifest.json">manifest.json</a>.
Timestamps are Brasília local time.</p>
<table>
<tr><th>table</th><th>title</th><th>rows</th><th>bytes</th><th>ANVISA load</th><th>source</th></tr>
{rows}
</table>
<p>Query with DuckDB (the path is relative to this page):</p>
<pre><code>SELECT no_produto, marcas FROM '{e(first["path"])}'
WHERE nu_cnpj_empresa = '40208221000174';</code></pre>
<p>Generated by <a href="https://github.com/vasfvitor/anvisa-api">{e(manifest["generator"])}</a>.</p>
</html>
"""
