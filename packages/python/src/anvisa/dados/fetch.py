"""Downloading the source CSVs: streamed to disk, hashed on the way, conditional on ETag."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, TypeVar
from zoneinfo import ZoneInfo

import httpx

from ..client import USER_AGENT
from ..download import write_stream
from ..errors import DadosError
from . import cmed, dcb
from .catalog import BASE_URL, Dataset

CHUNK = 1 << 20
BRASILIA = ZoneInfo("America/Sao_Paulo")
# dados.anvisa.gov.br drops connections now and then ("Server disconnected without sending a
# response", two dados.yml runs in a row on 2026-10-23): a transport error gets these pauses and
# another attempt. HTTP status errors and short bodies are not retried: those are answers.
BACKOFF = (2.0, 6.0)
_sleep: Callable[[float], None] = time.sleep

T = TypeVar("T")


def _with_retries(url: str, attempt: Callable[[], T]) -> T:
    """Run `attempt` and, on an httpx transport error, try again after each pause in BACKOFF;
    the last failure becomes a DadosError naming the URL and the number of attempts. Any
    other httpx error is a DadosError at once."""
    for n in range(len(BACKOFF) + 1):
        try:
            return attempt()
        except httpx.TransportError as exc:
            if n == len(BACKOFF):
                raise DadosError(f"{url}: {exc} (after {n + 1} attempts)") from exc
            _sleep(BACKOFF[n])
        except httpx.HTTPError as exc:
            raise DadosError(f"{url}: {exc}") from exc
    raise AssertionError("unreachable")


@dataclass(frozen=True)
class Source:
    """What was downloaded: recorded in the manifest so the next run can ask for changes only."""

    name: str
    url: str
    etag: str | None
    last_modified: str | None
    bytes: int
    sha256: str
    # cmed only: the month stamp of the pair of spreadsheets (what "unchanged" is decided on)
    # and each file's name, URL, size and hash; `bytes` is their sum and `sha256` the PMC file's
    stamp: str | None = None
    files: tuple[dict[str, Any], ...] = ()


def http_client(
    *, timeout: float = 120.0, transport: httpx.BaseTransport | None = None
) -> httpx.Client:
    # identity: the server would gzip if asked, and then Content-Length and the hash would be
    # about the compressed stream rather than the file
    headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "identity"}
    return httpx.Client(
        timeout=timeout, transport=transport, headers=headers, follow_redirects=True
    )


def download(
    http: httpx.Client,
    ds: Dataset,
    workdir: Path,
    *,
    etag: str | None = None,
    last_modified: str | None = None,
) -> Source | None:
    """Fetch `ds` into `workdir/ds.local_file`, or return None when the server answers 304 to
    the `etag`/`last_modified` of a previous download. The file only appears once complete."""
    return download_url(
        http,
        ds.url,
        Path(workdir) / ds.local_file,
        name=ds.file,
        etag=etag,
        last_modified=last_modified,
    )


def download_url(
    http: httpx.Client,
    url: str,
    dest: Path,
    *,
    name: str,
    etag: str | None = None,
    last_modified: str | None = None,
) -> Source | None:
    """GET `url` into `dest`, conditional on `etag` (else `last_modified`): None on 304."""
    headers = {}
    if etag:
        headers["If-None-Match"] = etag
    elif last_modified:
        headers["If-Modified-Since"] = last_modified

    def attempt() -> Source | None:
        with http.stream("GET", url, headers=headers) as response:
            if response.status_code == 304:
                return None
            if not response.is_success:
                raise DadosError(f"HTTP {response.status_code} for {url}")
            size, sha256 = write_stream(response.iter_bytes(CHUNK), dest)
            declared = response.headers.get("Content-Length")
            encoded = response.headers.get("Content-Encoding", "identity") != "identity"
            if declared is not None and not encoded and int(declared) != size:
                dest.unlink(missing_ok=True)
                raise DadosError(f"{url}: got {size} of {declared} bytes (truncated)")
        return Source(
            name=name,
            url=url,
            etag=response.headers.get("ETag"),
            last_modified=response.headers.get("Last-Modified"),
            bytes=size,
            sha256=sha256,
        )

    return _with_retries(url, attempt)


def cmed_stamp(last_modified: str | None) -> tuple[str | None, str | None]:
    """`(stamp, publicado_em)` from a file's Last-Modified: the day as YYYYMMDD and the moment
    as dd/mm/yyyy HH:MM:SS, both in Brasília, like every other TIMESTAMP of the Parquet."""
    if not last_modified:
        return None, None
    try:
        moment = parsedate_to_datetime(last_modified).astimezone(BRASILIA)
    except (TypeError, ValueError):
        return None, None
    return moment.strftime("%Y%m%d"), moment.strftime("%d/%m/%Y %H:%M:%S")


def download_cmed(
    http: httpx.Client, ds: Dataset, workdir: Path, *, previous: dict[str, Any] | None = None
) -> Source | None:
    """Download the two CMED files into `workdir` (conditional on the ETags recorded in
    `previous["files"]`) and write `workdir/ds.local_file`, the merged, normalized CSV. None
    when both answer 304; if only one changed, the other is fetched again, so the pair is one
    cut. `stamp` is the PMC file's Last-Modified day."""
    known = {f.get("name"): f for f in (previous or {}).get("files") or ()}
    fetched: dict[str, Source | None] = {}
    for kind, file in cmed.FILES.items():
        before = known.get(file) or {}
        fetched[kind] = download_url(
            http,
            BASE_URL + file,
            Path(workdir) / file,
            name=file,
            etag=before.get("etag"),
            last_modified=before.get("last_modified"),
        )
    if all(s is None for s in fetched.values()):
        return None
    for kind, file in cmed.FILES.items():
        if fetched[kind] is None:
            fetched[kind] = download_url(http, BASE_URL + file, Path(workdir) / file, name=file)
    sources = [fetched[kind] for kind in cmed.KINDS]
    assert all(sources)
    pmc = sources[0]
    assert pmc is not None
    stamp, publicado_em = cmed_stamp(pmc.last_modified)
    rows, _stats = cmed.read_merged(
        Path(workdir) / cmed.FILES["pmc"],
        Path(workdir) / cmed.FILES["pmvg"],
        publicado_em=publicado_em,
    )
    cmed.write_csv(rows, Path(workdir) / ds.local_file)
    files = tuple(
        {k: v for k, v in asdict(s).items() if k not in ("stamp", "files")}
        for s in sources
        if s is not None
    )
    return Source(
        name=", ".join(f["name"] for f in files),
        url=ds.url,
        etag=pmc.etag,
        last_modified=pmc.last_modified,
        bytes=sum(f["bytes"] for f in files),
        sha256=pmc.sha256,
        stamp=stamp,
        files=files,
    )


def get_page(http: httpx.Client, url: str) -> str:
    """An HTML page as text, with the same retries as a download."""

    def attempt() -> str:
        response = http.get(url)
        if not response.is_success:
            raise DadosError(f"HTTP {response.status_code} for {url}")
        return response.text

    return _with_retries(url, attempt)


def download_dcb(
    http: httpx.Client, ds: Dataset, workdir: Path, *, previous: dict[str, Any] | None = None
) -> Source | None:
    """Find the DCB list's current version (the collection page at `ds.url`, then the item
    page), download its xlsx into `workdir` and write `workdir/ds.local_file`, the normalized
    CSV. Three requests. The download is conditional on `previous` only when the xlsx URL is
    the one recorded there (a new version is a new item, so a new URL); None on 304. The
    Source's `name` is the version's title ("Lista consolidada das DCB: versão IN ...")."""
    previous = previous or {}
    title, item_url = dcb.current_item(get_page(http, ds.url), ds.url)
    xlsx_url = dcb.spreadsheet_url(get_page(http, item_url), item_url)
    same = previous.get("url") == xlsx_url
    source = download_url(
        http,
        xlsx_url,
        Path(workdir) / dcb.XLSX,
        name=title,
        etag=previous.get("etag") if same else None,
        last_modified=previous.get("last_modified") if same else None,
    )
    if source is None:
        return None
    _write_dcb(ds, workdir, source.last_modified)
    return source


def local_dcb(
    ds: Dataset, workdir: Path, fontes: Path, *, previous: dict[str, Any] | None = None
) -> Source | None:
    """The DCB list from the copy kept in `fontes/dcb/`, with no request: the Biblioteca Digital
    blocks automated access, so a person downloads each version in a browser. `dcb.json` holds
    what the download does not keep (`title`, `url`, `last_modified`). None when the file is the
    one recorded in `previous`; otherwise like `download_dcb`."""
    folder = Path(fontes) / ds.name
    try:
        meta = json.loads((folder / dcb.META).read_text(encoding="utf-8"))
        title, url, last_modified = meta["title"], meta["url"], meta["last_modified"]
        data = (folder / dcb.XLSX).read_bytes()
    except (OSError, ValueError, KeyError) as exc:
        raise DadosError(f"{folder}: cannot read the DCB copy: {exc!r}") from exc
    sha256 = hashlib.sha256(data).hexdigest()
    if (previous or {}).get("sha256") == sha256:
        return None
    (Path(workdir) / dcb.XLSX).write_bytes(data)
    _write_dcb(ds, workdir, last_modified)
    return Source(
        name=title, url=url, etag=None, last_modified=last_modified, bytes=len(data), sha256=sha256
    )


def _write_dcb(ds: Dataset, workdir: Path, last_modified: str | None) -> None:
    _stamp, publicado_em = cmed_stamp(last_modified)
    rows = dcb.read_entries(Path(workdir) / dcb.XLSX, publicado_em=publicado_em)
    dcb.write_csv(rows, Path(workdir) / ds.local_file)


def fetch_source(
    http: httpx.Client,
    ds: Dataset,
    workdir: Path,
    previous: dict[str, Any] | None = None,
    *,
    fontes: Path | None = None,
) -> Source | None:
    """`download`, `download_cmed`, or for the DCB `local_dcb` with `fontes` and `download_dcb`
    without it, by `ds.kind`, conditional on `previous` (the published manifest's `source` of
    this table), or unconditional without it."""
    previous = previous or {}
    if ds.kind == "cmed":
        return download_cmed(http, ds, workdir, previous=previous)
    if ds.kind == "dcb" and fontes:
        return local_dcb(ds, workdir, fontes, previous=previous)
    if ds.kind == "dcb":
        return download_dcb(http, ds, workdir, previous=previous)
    return download(
        http,
        ds,
        workdir,
        etag=previous.get("etag"),
        last_modified=previous.get("last_modified"),
    )


def fetch_file(http: httpx.Client, url: str, dest: Path) -> tuple[int, str]:
    """GET `url` into `dest` (which appears only once complete) and return its size and SHA-256:
    for files whose expected hash the caller already knows, such as a published Parquet."""

    def attempt() -> tuple[int, str]:
        with http.stream("GET", url) as response:
            if not response.is_success:
                raise DadosError(f"HTTP {response.status_code} for {url}")
            return write_stream(response.iter_bytes(CHUNK), dest)

    return _with_retries(url, attempt)
