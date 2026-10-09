"""Downloading the source CSVs: streamed to disk, hashed on the way, conditional on ETag."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import httpx

from ..client import USER_AGENT
from ..download import write_stream
from ..errors import DadosError
from .catalog import Dataset

CHUNK = 1 << 20


@dataclass(frozen=True)
class Source:
    """What was downloaded: recorded in the manifest so the next run can ask for changes only."""

    name: str
    url: str
    etag: str | None
    last_modified: str | None
    bytes: int
    sha256: str


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
    headers = {}
    if etag:
        headers["If-None-Match"] = etag
    elif last_modified:
        headers["If-Modified-Since"] = last_modified
    dest = Path(workdir) / ds.local_file
    try:
        with http.stream("GET", ds.url, headers=headers) as response:
            if response.status_code == 304:
                return None
            if not response.is_success:
                raise DadosError(f"HTTP {response.status_code} for {ds.url}")
            size, sha256 = write_stream(response.iter_bytes(CHUNK), dest)
            declared = response.headers.get("Content-Length")
            encoded = response.headers.get("Content-Encoding", "identity") != "identity"
            if declared is not None and not encoded and int(declared) != size:
                dest.unlink(missing_ok=True)
                raise DadosError(f"{ds.url}: got {size} of {declared} bytes (truncated)")
    except httpx.HTTPError as exc:
        raise DadosError(f"{ds.url}: {exc}") from exc
    return Source(
        name=ds.file,
        url=ds.url,
        etag=response.headers.get("ETag"),
        last_modified=response.headers.get("Last-Modified"),
        bytes=size,
        sha256=sha256,
    )


def fetch_file(http: httpx.Client, url: str, dest: Path) -> tuple[int, str]:
    """GET `url` into `dest` (which appears only once complete) and return its size and SHA-256:
    for files whose expected hash the caller already knows, such as a published Parquet."""
    try:
        with http.stream("GET", url) as response:
            if not response.is_success:
                raise DadosError(f"HTTP {response.status_code} for {url}")
            return write_stream(response.iter_bytes(CHUNK), dest)
    except httpx.HTTPError as exc:
        raise DadosError(f"{url}: {exc}") from exc
