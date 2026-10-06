"""Reading ANVISA's CSV dialect, and rewriting it as plain UTF-8 CSV for DuckDB.

Verified on both alimentos files (2026-10-06, `fixtures/dados/`):

- The bytes are Windows-1252, not Latin-1: 0x96 (–), 0x92 (’), 0x93/0x94 (“ ”), 0x99 (™)
  appear hundreds of times.
- `;` separates fields; text fields are wrapped in `"` but quotes *inside* them are never
  escaped (`biscoito tipo "cookies"`, and a value ending in a quote comes out as `...azul
  brilhante. ""`), and they may hold LF or CRLF line breaks.

No stock CSV reader gets all of that right. DuckDB's strict mode refuses the file, its lenient
mode merges records, Python's `csv` drops quotes and splits two records. What makes the format
unambiguous is that a quote only *closes* a field when a `;`, a line break or the end of the
file follows it. Read that way, every record of both files has exactly the header's field
count.
"""

from __future__ import annotations

import csv
import html
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from ..errors import DadosError, SchemaDriftError
from .catalog import Dataset

_FIELD = re.compile(r'"(.*?)"(?=;|\r?\n|\Z)|([^;\r\n"]*)', re.DOTALL)
_END = re.compile(r";|\r?\n|\Z")
_ENTITY = re.compile(r"&(?:#[0-9]+|#[xX][0-9a-fA-F]+|[A-Za-z][A-Za-z0-9]*);")

# Python's cp1252 codec raises on the five bytes Windows-1252 leaves undefined (0x81, 0x8D,
# 0x8F, 0x90, 0x9D). Decoding as Latin-1 and remapping 0x80-0x9F is the WHATWG "windows-1252"
# decoder: same result for every defined byte, and the undefined ones pass through as U+0081...
_UNDEFINED = (0x81, 0x8D, 0x8F, 0x90, 0x9D)
_CP1252 = str.maketrans(
    {chr(b): bytes([b]).decode("cp1252") for b in range(0x80, 0xA0) if b not in _UNDEFINED}
)


def decode(raw: bytes) -> str:
    return raw.decode("latin-1").translate(_CP1252)


def records(text: str) -> Iterator[list[str]]:
    """Split decoded text into records of raw field values (no trimming, no unescaping)."""
    record: list[str] = []
    pos = 0
    while pos < len(text):
        field = _FIELD.match(text, pos)
        end = _END.match(text, field.end())
        if end is None:  # a quote inside an unquoted field: not this dialect
            snippet = text[max(0, field.end() - 40) : field.end() + 40]
            raise DadosError(f"unparseable CSV at character {field.end()}: {snippet!r}")
        record.append(field.group(1) if field.group(1) is not None else field.group(2))
        pos = end.end()
        if end.group() != ";":
            yield record
            record = []


def read_header(path: Path) -> list[str]:
    """The first record of a downloaded file, without reading the rest."""
    with Path(path).open("rb") as f:
        first = f.readline()
    header = next(records(decode(first)), None)
    if not header or header == [""]:
        raise DadosError(f"{Path(path).name}: empty file")
    return header


def check_header(ds: Dataset, header: list[str]) -> None:
    expected = list(ds.columns)
    if header == expected:
        return
    added = [c for c in header if c not in ds.columns]
    missing = [c for c in expected if c not in header]
    position = next(
        (i for i, (a, b) in enumerate(zip(header, expected, strict=False)) if a != b),
        min(len(header), len(expected)),
    )
    raise SchemaDriftError(
        f"{ds.file}: header changed (added {added}, missing {missing}, first difference at "
        f"column {position + 1}); update the `{ds.name}` entry in anvisa/dados/catalog.py"
    )


def unescape(value: str) -> str:
    """Decode `&apos;`, `&quot;`, `&#8208;`... but only the `;`-terminated forms:
    `html.unescape` alone would also turn a bare `&not` in `M&notícia` into `¬`."""
    return _ENTITY.sub(lambda m: html.unescape(m.group()), value)


@dataclass(frozen=True)
class Normalized:
    rows: int
    rejected: tuple[int, ...]  # 1-based record numbers whose field count was wrong


def normalize(ds: Dataset, source: Path, target: Path) -> Normalized:
    """Rewrite `source` as RFC 4180 UTF-8 CSV: header checked, every value stripped of
    surrounding whitespace (`\\xa0\\r\\n` included), entities decoded in `ds.unescape`, empty
    values left unquoted so DuckDB reads them as NULL. Records with the wrong field count are
    skipped and reported; whether that is acceptable is the caller's call."""
    it = records(decode(Path(source).read_bytes()))
    check_header(ds, next(it, []))
    width = len(ds.columns)
    entity_cols = [i for i, name in enumerate(ds.columns) if name in ds.unescape]
    rows, rejected = 0, []
    with Path(target).open("w", encoding="utf-8", newline="") as out:
        writer = csv.writer(out, lineterminator="\n")
        writer.writerow(ds.columns)
        for number, record in enumerate(it, 1):
            if len(record) != width:
                rejected.append(number)
                continue
            values = [v.strip() for v in record]
            for i in entity_cols:
                values[i] = unescape(values[i])
            writer.writerow(values)
            rows += 1
    return Normalized(rows, tuple(rejected))
