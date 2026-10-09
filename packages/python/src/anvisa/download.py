"""Files that come back from the API or the open-data site: the `Content-Disposition` name,
where to put a file, and writing a stream to disk so that it only appears once complete."""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

_FILENAME = re.compile(r"""filename\*?=(?:UTF-8'')?"?([^";]+)""", re.IGNORECASE)


def filename_from(content_disposition: str | None) -> str | None:
    """`attachment; filename=consulta_fila.xlsx` -> `consulta_fila.xlsx`, or None.

    ANVISA sends the plain unquoted form; the quoted and RFC 5987 forms are handled too
    because they cost one regex branch each."""
    match = _FILENAME.search(content_disposition or "")
    return match.group(1).strip() if match else None


def target_path(path: str | Path, name: str | None, default_name: str) -> Path:
    """Where a file goes. `path` is a directory when it exists as one or is written with a
    trailing separator (`Path("exports/")` alone forgets the slash, so a new directory would
    otherwise become the file's name); the file is then `name` (what the server sent) or
    `default_name`. Anything else is the file itself. Parent directories are created."""
    target = Path(path)
    if target.is_dir() or str(path).endswith(("/", os.sep)):
        target = target / (name or default_name)
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def write_stream(chunks: Iterable[bytes], dest: Path) -> tuple[int, str]:
    """Write `chunks` to `dest` through a `.part` file, so `dest` appears only once complete,
    and return its size and SHA-256. On any error the partial file is removed."""
    part = dest.with_name(dest.name + ".part")
    digest, size = hashlib.sha256(), 0
    try:
        with part.open("wb") as f:
            for chunk in chunks:
                f.write(chunk)
                digest.update(chunk)
                size += len(chunk)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    part.replace(dest)
    return size, digest.hexdigest()


@dataclass(frozen=True)
class Download:
    """One file as the API returned it. `filename` and `content_type` are None when the
    response carried no `Content-Disposition` / `Content-Type` (the assunto formulário)."""

    content: bytes
    filename: str | None = None
    content_type: str | None = None

    def save(self, path: str | Path = ".", default_name: str = "download") -> Path:
        """Write the bytes and return where they went. A directory (existing, or a path
        ending in a separator) gets `filename`, or `default_name` when the server sent none;
        anything else is used as the file name itself."""
        target = target_path(path, self.filename, default_name)
        target.write_bytes(self.content)
        return target
