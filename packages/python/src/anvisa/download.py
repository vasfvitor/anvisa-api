"""The file a download endpoint returns, and the `Content-Disposition` name parsing."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_FILENAME = re.compile(r"""filename\*?=(?:UTF-8'')?"?([^";]+)""", re.IGNORECASE)


def filename_from(content_disposition: str | None) -> str | None:
    """`attachment; filename=consulta_fila.xlsx` -> `consulta_fila.xlsx`, or None.

    ANVISA sends the plain unquoted form; the quoted and RFC 5987 forms are handled too
    because they cost one regex branch each."""
    match = _FILENAME.search(content_disposition or "")
    return match.group(1).strip() if match else None


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
        target = Path(path)
        if target.is_dir():
            target = target / (self.filename or default_name)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(self.content)
        return target
