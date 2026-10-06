"""Downloading the source CSVs: streamed to disk, hashed on the way, conditional on ETag."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import httpx

from ..client import USER_AGENT
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
    """Fetch `ds` into `workdir/ds.file`, or return None when the server answers 304 to the
    `etag`/`last_modified` of a previous download. The file only appears once it is complete."""
    headers = {}
    if etag:
        headers["If-None-Match"] = etag
    elif last_modified:
        headers["If-Modified-Since"] = last_modified
    dest = Path(workdir) / ds.file
    part = dest.with_name(dest.name + ".part")
    digest, size = hashlib.sha256(), 0
    try:
        with http.stream("GET", ds.url, headers=headers) as response:
            if response.status_code == 304:
                return None
            if not response.is_success:
                raise DadosError(f"HTTP {response.status_code} for {ds.url}")
            with part.open("wb") as f:
                for chunk in response.iter_bytes(CHUNK):
                    f.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
            declared = response.headers.get("Content-Length")
            encoded = response.headers.get("Content-Encoding", "identity") != "identity"
            if declared is not None and not encoded and int(declared) != size:
                raise DadosError(f"{ds.url}: got {size} of {declared} bytes (truncated)")
    except httpx.HTTPError as exc:
        part.unlink(missing_ok=True)
        raise DadosError(f"{ds.url}: {exc}") from exc
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    part.replace(dest)
    return Source(
        name=ds.file,
        url=ds.url,
        etag=response.headers.get("ETag"),
        last_modified=response.headers.get("Last-Modified"),
        bytes=size,
        sha256=digest.hexdigest(),
    )
