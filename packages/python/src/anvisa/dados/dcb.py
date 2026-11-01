"""The DCB list (Denominações Comuns Brasileiras) as rows keyed by the DCB number.

The DCB is the official Portuguese name of each pharmaceutical substance (active ingredients,
excipients, biologicals, medicinal plants, homeopathic stocks, radiopharmaceuticals). ANVISA's
Coordenação da Farmacopeia publishes the consolidated list as one xlsx per version in the
Biblioteca Digital (DSpace), collection `COLLECTION_URL`; each new Instrução Normativa adds an
item, titled "Lista consolidada das DCB: versão IN ... - VIGENTE", and retitles the previous
one "não vigente". Not on dados.anvisa.gov.br.

Like cmed.py, this module turns the source into the normalized CSV the usual conversion types
(`COLUMNS`), with the standard library only (zipfile + ElementTree): no DuckDB, no openpyxl.
Finding the current item and its xlsx in the HTML pages is here too (`current_item`,
`spreadsheet_url`); fetching them is fetch.py's.
"""

from __future__ import annotations

import csv
import html
import re
import zipfile
from collections.abc import Iterable, Iterator
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urljoin
from xml.etree import ElementTree as ET

from ..errors import DadosError, SchemaDriftError

COLLECTION_URL = "https://bibliotecadigital.anvisa.gov.br/jspui/handle/anvisa/11933"
XLSX = "dcb.xlsx"  # the spreadsheet's name in the work directory and in `fontes/dcb/`
META = "dcb.json"  # beside it in `fontes/dcb/`: what a browser download does not keep
# the items of the collection that are versions of the list (it also holds the 2013 manual)
TITLE_PREFIX = "lista consolidada das dcb"

# the source header (whitespace collapsed; ANVISA writes "Nº DCB " with a trailing space)
HEADER = ("Nº DCB", "DENOMINAÇÃO COMUM BRASILEIRA", "Nº CAS", "CLASSIFICAÇÃO", "HISTÓRICO")
COLUMNS: dict[str, str] = {
    "numero_dcb": "INTEGER",
    "denominacao": "VARCHAR",
    "cas": "VARCHAR",
    "cas_referencia": "INTEGER",
    "classificacao": "VARCHAR",
    "historico": "VARCHAR",
    "publicado_em": "TIMESTAMP",
}
SOURCE_NAMES: dict[str, str] = {
    "numero_dcb": "Nº DCB",
    "denominacao": "DENOMINAÇÃO COMUM BRASILEIRA",
    "cas": "Nº CAS",
    "cas_referencia": "Nº CAS ([Ref. n])",
    "classificacao": "CLASSIFICAÇÃO",
    "historico": "HISTÓRICO",
    "publicado_em": "Last-Modified do arquivo",
}
HISTORICO_SEP = " | "  # joins the notes of a row and of the rows merged under it

_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
_PKG_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}Relationship"
# built-in number formats that are dates (ECMA-376 18.8.30); custom ones are told by their code
_DATE_FORMATS = frozenset({14, 15, 16, 17, 18, 19, 20, 21, 22, 45, 46, 47})
_REFERENCIA = re.compile(r"\[?\s*Ref\.?\s*(\d+)\s*\.?\s*\]?", re.IGNORECASE)
_HYPHENS = str.maketrans(dict.fromkeys("‐‑‒–—―", "-"))


# --- finding the current version ------------------------------------------------------------


def current_item(page: str, base: str = COLLECTION_URL) -> tuple[str, str]:
    """`(title, url)` of the list's current version on the collection page: the item whose
    title ends in VIGENTE (not "não vigente"), or, if none says so, the first listed (DSpace
    lists the newest deposit first). Items that are not a version of the list are ignored."""
    items = []
    for href, text in re.findall(r'<a href="(/jspui/handle/anvisa/\d+)">([^<]+)</a>', page):
        title = " ".join(html.unescape(text).split())
        if title.lower().startswith(TITLE_PREFIX):
            items.append((title, urljoin(base, href)))
    if not items:
        raise DadosError(f"{base}: no 'Lista consolidada das DCB' item on the collection page")
    for title, url in items:
        status = title.lower().rsplit(" ", 2)
        if status[-1] == "vigente" and status[-2] not in ("não", "nao"):
            return title, url
    return items[0]


def spreadsheet_url(page: str, base: str) -> str:
    """The xlsx bitstream linked from an item page."""
    for href in re.findall(r'href="(/jspui/bitstream/[^"?#]+\.xlsx)"', page, re.IGNORECASE):
        return urljoin(base, html.unescape(href))
    raise DadosError(f"{base}: no xlsx file on the item page")


def file_name(url: str) -> str:
    return unquote(url.rsplit("/", 1)[-1])


# --- reading the xlsx -----------------------------------------------------------------------


def _column(ref: str) -> int:
    n = 0
    for ch in ref:
        if not ch.isalpha():
            break
        n = n * 26 + ord(ch.upper()) - 64
    return n - 1


def _shared_strings(z: zipfile.ZipFile) -> list[str]:
    """Each <si> as text: its <t>, or its rich-text runs joined (phonetic runs left out)."""
    if "xl/sharedStrings.xml" not in z.namelist():
        return []
    out = []
    for si in ET.fromstring(z.read("xl/sharedStrings.xml")).iterfind(_NS + "si"):
        t = si.find(_NS + "t")
        if t is not None:
            out.append(t.text or "")
        else:
            out.append("".join(r.findtext(_NS + "t") or "" for r in si.iterfind(_NS + "r")))
    return out


def _date_styles(z: zipfile.ZipFile) -> frozenset[int]:
    """The cell style indexes (`s`) whose number format is a date."""
    if "xl/styles.xml" not in z.namelist():
        return frozenset()
    styles = ET.fromstring(z.read("xl/styles.xml"))
    custom = set()
    for fmt in styles.iter(_NS + "numFmt"):
        code = re.sub(r'"[^"]*"|\[[^\]]*\]|\\.', "", fmt.get("formatCode") or "").lower()
        if re.search(r"[dy]|m(?!s)", code):
            custom.add(int(fmt.get("numFmtId")))
    xfs = styles.find(_NS + "cellXfs")
    if xfs is None:
        return frozenset()
    return frozenset(
        i
        for i, xf in enumerate(xfs.iterfind(_NS + "xf"))
        if int(xf.get("numFmtId") or 0) in _DATE_FORMATS | custom
    )


def _first_sheet(z: zipfile.ZipFile) -> tuple[str, bool]:
    """The first worksheet's part name, and whether the workbook counts dates from 1904."""
    workbook = ET.fromstring(z.read("xl/workbook.xml"))
    pr = workbook.find(_NS + "workbookPr")
    date1904 = pr is not None and pr.get("date1904") in ("1", "true")
    sheet = workbook.find(f"{_NS}sheets/{_NS}sheet")
    if sheet is None:
        raise DadosError("DCB xlsx: the workbook has no sheet")
    rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
    target = next(r.get("Target") for r in rels.iter(_PKG_REL) if r.get("Id") == sheet.get(_REL))
    return ("xl/" + target.removeprefix("/").removeprefix("xl/")), date1904


def excel_date(serial: str, date1904: bool = False) -> date:
    """A date cell's serial number as a date (time of day dropped)."""
    days = int(Decimal(serial))
    return (date(1904, 1, 1) if date1904 else date(1899, 12, 30)) + timedelta(days=days)


def sheet_rows(path: Path) -> Iterator[tuple[int, list[str | date | None]]]:
    """`(row number, cells)` of the first worksheet, in file order: text for strings and
    numbers (as written in the XML), a `date` for a number styled as a date, None for an empty
    cell. Rows without any value are skipped."""
    with zipfile.ZipFile(path) as z:
        shared = _shared_strings(z)
        dates = _date_styles(z)
        part, date1904 = _first_sheet(z)
        root = ET.fromstring(z.read(part))
    for position, row in enumerate(root.iter(_NS + "row"), 1):
        cells: dict[int, str | date] = {}
        for i, c in enumerate(row.iterfind(_NS + "c")):
            kind = c.get("t", "n")
            v = c.find(_NS + "v")
            if kind == "inlineStr":
                value: str | date = "".join(t.text or "" for t in c.iter(_NS + "t"))
            elif v is None or v.text is None:
                continue
            elif kind == "s":
                value = shared[int(v.text)]
            elif kind == "n" and int(c.get("s") or 0) in dates:
                value = excel_date(v.text, date1904)
            else:  # n, str (a formula's text), b, e: as written
                value = v.text
            cells[_column(c.get("r")) if c.get("r") else i] = value
        if cells:
            yield int(row.get("r") or position), [cells.get(i) for i in range(max(cells) + 1)]


def _text(value: str | date | None) -> str | None:
    """A cell as text with its whitespace collapsed (no-break spaces included), or None."""
    if value is None:
        return None
    if isinstance(value, date):
        return value.isoformat()
    text = " ".join(value.split())
    return text or None


def read_header(path: Path) -> list[str]:
    """The source header, raw: the first row with three or more values (the one above it is
    the title, LISTA DCB - CONSOLIDADA)."""
    for _, cells in sheet_rows(path):
        if sum(1 for c in cells if _text(c)) >= 3:
            return ["" if c is None else str(c) for c in cells]
    raise DadosError(f"{Path(path).name}: no header row")


def check_header(header: list[str], name: str = XLSX) -> None:
    found = tuple(_text(h) or "" for h in header)
    if found != HEADER:
        raise SchemaDriftError(
            f"{name}: header is {list(found)}, expected {list(HEADER)}; update anvisa/dados/dcb.py"
        )


def numero(value: str | date | None) -> int | None:
    text = _text(value)
    if text is None:
        return None
    try:
        number = Decimal(text)
    except InvalidOperation:
        raise DadosError(f"DCB: not a DCB number: {value!r}") from None
    if number != number.to_integral_value():
        raise DadosError(f"DCB: not a whole DCB number: {value!r}")
    return int(number)


def cas(value: str | date | None) -> tuple[str | None, int | None]:
    """`(cas, cas_referencia)` from the Nº CAS cell. A CAS that Excel took for a date when it
    was typed (7758-11-4 is 4 Nov 7758) comes back as the text it was; "[Ref. 8]" and its
    variants (no CAS: the list cites its reference 8 instead) give `(None, 8)`. Otherwise the
    text with whitespace collapsed and look-alike hyphens (U+2011...) made `-`, as written."""
    if isinstance(value, date):
        return f"{value.year}-{value.month:02d}-{value.day}", None
    text = _text(value)
    if text is None:
        return None, None
    m = _REFERENCIA.fullmatch(text)
    if m:
        return None, int(m.group(1))
    return text.translate(_HYPHENS), None


def read_entries(path: Path, *, publicado_em: str | None) -> list[dict[str, Any]]:
    """One dict per DCB (`COLUMNS`), in sheet order. A row without number and name continues
    the entry above it: ANVISA merges the first four cells of an entry over two rows and puts a
    later change (new CAS, corrected name) in the second row's HISTÓRICO, which is appended to
    the entry's with HISTORICO_SEP. `publicado_em` (dd/mm/yyyy HH:MM:SS) is stamped on every
    row. A repeated DCB number is an error."""
    rows = sheet_rows(path)
    for _, cells in rows:
        if sum(1 for c in cells if _text(c)) >= 3:
            check_header(["" if c is None else str(c) for c in cells], Path(path).name)
            break
    else:
        raise DadosError(f"{Path(path).name}: no header row")
    entries: list[dict[str, Any]] = []
    seen: dict[int, int] = {}
    for number, cells in rows:
        cells = cells + [None] * (len(HEADER) - len(cells))
        if any(_text(c) for c in cells[len(HEADER) :]):
            raise SchemaDriftError(
                f"{Path(path).name}: row {number} has values past column {len(HEADER)}"
            )
        n, nome, numero_cas, classe, historico = (cells[i] for i in range(len(HEADER)))
        if _text(n) is None and _text(nome) is None:
            note = _text(historico)
            if note is None and not any(_text(c) for c in cells):
                continue
            if not entries or _text(numero_cas) or _text(classe):
                raise DadosError(f"{Path(path).name}: row {number} has no DCB number or name")
            previous = entries[-1]
            previous["historico"] = HISTORICO_SEP.join(
                x for x in (previous["historico"], note) if x
            )
            continue
        value, referencia = cas(numero_cas)
        entry = {
            "numero_dcb": numero(n),
            "denominacao": _text(nome),
            "cas": value,
            "cas_referencia": referencia,
            "classificacao": _text(classe),
            "historico": _text(historico),
            "publicado_em": publicado_em,
        }
        if entry["numero_dcb"] is not None:
            if entry["numero_dcb"] in seen:
                raise DadosError(
                    f"{Path(path).name}: DCB {entry['numero_dcb']} on rows "
                    f"{seen[entry['numero_dcb']]} and {number}"
                )
            seen[entry["numero_dcb"]] = number
        entries.append(entry)
    return entries


# --- the normalized CSV ----------------------------------------------------------------------


def write_csv(rows: Iterable[dict[str, Any]], target: Path) -> int:
    """RFC 4180 UTF-8 CSV with `COLUMNS` as header, None as an empty field (cmed.write_csv)."""
    n = 0
    with Path(target).open("w", encoding="utf-8", newline="") as out:
        writer = csv.writer(out, lineterminator="\n")
        writer.writerow(COLUMNS)
        for row in rows:
            writer.writerow(["" if row.get(c) is None else str(row[c]) for c in COLUMNS])
            n += 1
    return n
