"""Reading ANVISA's CSV dialect, and rewriting it as plain UTF-8 CSV for DuckDB.

Verified on both alimentos files, on saneantes and on the two petition files (2026-10-06,
`fixtures/dados/`):

- The bytes are Windows-1252, not Latin-1: 0x96 (–), 0x92 (’), 0x93/0x94 (“ ”), 0x99 (™)
  appear hundreds of times.
- `;` separates fields; text fields are wrapped in `"` but quotes *inside* them are never
  escaped (`biscoito tipo "cookies"`, and a value ending in a quote comes out as `...azul
  brilhante. ""`), and they may hold LF or CRLF line breaks.

No stock CSV reader gets all of that right. DuckDB's strict mode refuses the file, its lenient
mode merges records, Python's `csv` drops quotes and splits two records. What makes the format
unambiguous is that a quote only *closes* a field when a `;`, a line break or the end of the
file follows it, and, given the record width, that a line break can only close the record's
*last* field while `;` can only close a non-last one (saneantes has `AMBIENTE "AS
MENINAS"\\r\\n\\r\\nSPRAY` inside a product name). Read that way, every record of all three
files has exactly the header's field count.

The finalized-petitions file starts its header with `#` (`#NUM_EXPEDIENTE_PETICAO;...`), a
comment marker: `read_header` and `normalize` drop it before comparing names.

`normalize` streams the file in `CHUNK`-byte pieces (`stream`), so memory is one chunk plus
one record whatever the file size (cosméticos is 228 MB, AFE 314 MB). A chunk boundary cannot
change a reading: until the last chunk the patterns have no end-of-text alternative, so a
field that would need text beyond the buffer simply fails to match, and the record is parsed
again once the next chunk has arrived.
"""

from __future__ import annotations

import csv
import html
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

from ..errors import DadosError, SchemaDriftError
from .catalog import Dataset

CHUNK = 4 << 20  # bytes read at a time
MAX_RECORD = 4 * CHUNK  # characters a record may span before the parser gives up on it
_CONTEXT = 40  # characters shown on each side of a parse error

# A field is quoted (closing quote followed by what may end it) or bare (no quote at all).
# Index 0: the text is complete, so its end (`\Z`) may close a field or a record. Index 1: more
# text may follow, so only `;` or a line break can; a match that would need `\Z` fails instead,
# which means "not enough text yet", never "malformed".
_FIELD_ANY = (  # width unknown
    re.compile(r'"(.*?)"(?=;|\r?\n|\Z)|([^;\r\n"]*)', re.DOTALL),
    re.compile(r'"(.*?)"(?=;|\r?\n)|([^;\r\n"]*)', re.DOTALL),
)
_FIELD_MID = (re.compile(r'"(.*?)"(?=;)|([^;\r\n"]*)', re.DOTALL),) * 2  # not the record's last
_FIELD_LAST = (  # the record's last
    re.compile(r'"(.*?)"(?=\r?\n|\Z)|([^;\r\n"]*)', re.DOTALL),
    re.compile(r'"(.*?)"(?=\r?\n)|([^;\r\n"]*)', re.DOTALL),
)
_END = (re.compile(r";|\r?\n|\Z"), re.compile(r";|\r?\n"))
_QUOTE_BREAK = re.compile(r'"\r?\n')  # inside a quoted value: where the width-blind rule would stop
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


def records(text: str, width: int | None = None) -> Iterator[list[str]]:
    """Split decoded text into records of raw field values (no trimming, no unescaping).

    With `width` (the header's field count), a quote followed by a line break closes only the
    record's last field and a quote followed by `;` only a non-last one, which is what tells an
    inner `"..."\\n` apart from the end of a record. Without it (the header line), either does."""
    for record, _ in _parse(text, width):
        yield record


def stream(chunks: Iterable[str], width: int | None = None) -> Iterator[list[str]]:
    """`records` over decoded chunks, holding only the record in progress (plus `_CONTEXT`
    characters before it, for error messages). A record that does not end inside the buffer
    is parsed again with the next chunk; only after the last chunk is that an error."""
    buf, offset, lead = "", 0, 0  # offset: file position of buf[0]; lead: where parsing starts
    for chunk in chunks:
        buf += chunk
        done = lead
        for record, stop in _parse(buf, width, final=False, offset=offset, pos=lead):
            yield record
            done = stop
        if len(buf) - done > MAX_RECORD:  # a stray quote is swallowing the file
            raise DadosError(
                f"unparseable CSV at character {offset + done}: no record ends within the next "
                f"{MAX_RECORD} characters"
            )
        keep = max(0, done - _CONTEXT)
        buf, offset, lead = buf[keep:], offset + keep, done - keep
    for record, _ in _parse(buf, width, final=True, offset=offset, pos=lead):
        yield record


def _parse(
    text: str, width: int | None, final: bool = True, offset: int = 0, pos: int = 0
) -> Iterator[tuple[list[str], int]]:
    """`records` plus where each record ends in `text`; see `_records` for the other arguments."""
    for record, start, stop, merged in _records(text, width, final, offset, pos):
        if width is None or (len(record) == width and not merged):
            yield record, stop
            continue
        # The record either has the wrong width or was read across a `"`+line break. Both are
        # also what a *truncated* record followed by a normal one looks like, and that reading
        # must win when it is possible: re-split the span the width-blind way. If that parses,
        # it is the answer (a short record gets reported as such); if it raises (`SPRAY";...`,
        # a bare field with a quote in it), the inner-quote reading was the only consistent one.
        try:
            blind = [sub for sub, _, _, _ in _records(text[start:stop], None)]
        except DadosError:
            yield record, stop
        else:
            for sub in blind:
                yield sub, stop


def _records(
    text: str, width: int | None, final: bool = True, offset: int = 0, pos: int = 0
) -> Iterator[tuple[list[str], int, int, bool]]:
    """`records` plus each record's character span `[start, stop)` (line break included) and
    whether one of its quoted fields was read across a `"` followed by a line break.

    Parsing starts at `pos`. With `final` false the text may be cut short, so a field that
    cannot be closed inside it ends the iteration instead of raising. `offset` is the file
    position of `text[0]`, so that error messages report the position in the file."""
    k = 0 if final else 1
    record: list[str] = []
    start = pos
    merged = False
    while pos < len(text):
        if width is None:
            pattern = _FIELD_ANY[k]
        else:
            pattern = _FIELD_LAST[k] if len(record) == width - 1 else _FIELD_MID[k]
        field = pattern.match(text, pos)
        end = _END[k].match(text, field.end()) if field else None
        if field is None or end is None:  # a quote where this dialect allows none
            if not final:  # or a field the next chunk completes
                return
            at = field.end() if field else pos
            snippet = text[max(0, at - _CONTEXT) : at + _CONTEXT]
            raise DadosError(f"unparseable CSV at character {at + offset}: {snippet!r}")
        quoted = field.group(1)
        if quoted is not None and pattern is _FIELD_MID[k] and _QUOTE_BREAK.search(quoted):
            merged = True
        record.append(quoted if quoted is not None else field.group(2))
        pos = end.end()
        if end.group() != ";":
            yield record, start, pos, merged
            record = []
            start = pos
            merged = False


def _names(header: list[str]) -> list[str]:
    return [header[0].removeprefix("#"), *header[1:]] if header else header


def read_header(path: Path) -> list[str]:
    """The first record of a downloaded file, without reading the rest."""
    with Path(path).open("rb") as f:
        first = f.readline()
    header = next(records(decode(first)), None)
    if not header or header == [""]:
        raise DadosError(f"{Path(path).name}: empty file")
    return _names(header)


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


def normalize(ds: Dataset, source: Path, target: Path, *, chunk_size: int = CHUNK) -> Normalized:
    """Rewrite `source` as RFC 4180 UTF-8 CSV: header checked, every value stripped of
    surrounding whitespace (`\\xa0\\r\\n` included), entities decoded in `ds.unescape`, empty
    values left unquoted so DuckDB reads them as NULL. Records with the wrong field count are
    skipped and reported; whether that is acceptable is the caller's call."""
    width = len(ds.columns)
    entity_cols = [i for i, name in enumerate(ds.columns) if name in ds.unescape]
    rows, rejected = 0, []
    with Path(source).open("rb") as f:
        # Windows-1252 is a single-byte encoding, so chunks can be decoded one by one.
        it = stream(map(decode, iter(lambda: f.read(chunk_size), b"")), width)
        check_header(ds, _names(next(it, [])))
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
