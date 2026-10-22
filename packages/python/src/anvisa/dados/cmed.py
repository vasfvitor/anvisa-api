"""The CMED price list (PMC and PMVG) as rows keyed by GGREM.

CMED, the medicine price regulator with its secretariat at ANVISA, publishes a monthly list of
maximum prices per presentation: PF (preço fábrica), PMC (preço máximo ao consumidor) and PMVG
(preço máximo de venda ao governo), each at every ICMS rate. gov.br advertises it as two xlsx
files whose names carry a stamp; dados.anvisa.gov.br republishes the same cut as two CSVs,
`TA_PRECO_MEDICAMENTO.csv` (PF + PMC) and `TA_PRECO_MEDICAMENTO_GOV.csv` (PF + PMVG): UTF-8
with a BOM, `;`, no quoting, CRLF, 74 columns, one row per presentation, keyed by the 15-digit
GGREM. Compared cell by cell with the xlsx of 2026-10-09: same 26,635 GGREMs, same values but
for two rows the xlsx corrected two hours after the CSVs were written (see the fixture notes
in `fixtures/dados/manifest.json`).

Unlike ANVISA's other CSVs, these are not fed to `parse.normalize`: this module reads both,
merges them by GGREM, turns prices into integer centavos and flags into S/N, and writes one
normalized CSV with its own column names (`COLUMNS`), which the usual conversion then types.
It has no DuckDB dependency, so `anvisa-feeds` can import it to snapshot the list.
"""

from __future__ import annotations

import csv
import re
import unicodedata
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from ..errors import DadosError, SchemaDriftError

# file names under dados.anvisa.gov.br/dados/ (catalog.BASE_URL), by kind
FILES = {"pmc": "TA_PRECO_MEDICAMENTO.csv", "pmvg": "TA_PRECO_MEDICAMENTO_GOV.csv"}
KINDS = tuple(FILES)
ENCODING = "utf-8-sig"  # the BOM is stripped on read
DELIMITER = ";"

# ICMS rates in the column order of both files; "sem impostos" first. `,` in the header is
# `_` in the output name: "PF 17,5 %  ALC" -> pf_17_5_alc.
ALIQUOTAS = ("0", "12", "17", "17,5", "18", "19", "19,5", "20", "20,5", "21", "22", "22,5", "23")


def price_columns(prefix: str) -> list[str]:
    """The 26 price columns of one block (pf, pmc or pmvg): sem impostos, then each rate with
    its ALC (Área de Livre Comércio) variant; 0% has no ALC variant."""
    names = [f"{prefix}_sem_impostos", f"{prefix}_0"]
    for rate in ALIQUOTAS[1:]:
        base = f"{prefix}_{rate.replace(',', '_')}"
        names += [base, f"{base}_alc"]
    return names


# output column -> type, in the order of the normalized CSV and the Parquet. Prices are
# INTEGER centavos: exact, diff-friendly, and no DECIMAL type to add to the catalog.
_DESCRIPTIVE: dict[str, str] = {
    "ggrem": "VARCHAR",
    "registro": "VARCHAR",
    "registro_produto": "VARCHAR",
    "substancia": "VARCHAR",
    "cnpj": "VARCHAR",
    "laboratorio": "VARCHAR",
    "produto": "VARCHAR",
    "apresentacao": "VARCHAR",
    "classe_terapeutica": "VARCHAR",
    "tipo_produto": "VARCHAR",
    "regime_preco": "VARCHAR",
    "ean_1": "VARCHAR",
    "ean_2": "VARCHAR",
    "ean_3": "VARCHAR",
}
_FLAGS: dict[str, str] = {
    "pmc_asterisco": "BOOLEAN",
    "pmvg_asterisco": "BOOLEAN",
    "restricao_hospitalar": "BOOLEAN",
    "cap": "BOOLEAN",
    "confaz_87": "BOOLEAN",
    "icms_0": "BOOLEAN",
    "analise_recursal": "VARCHAR",
    "lista_pis_cofins": "VARCHAR",
    "comercializacao_periodo": "VARCHAR",
    "comercializacao": "BOOLEAN",
    "tarja": "VARCHAR",
    "publicado_em": "TIMESTAMP",
}
COLUMNS: dict[str, str] = {
    **_DESCRIPTIVE,
    **{c: "INTEGER" for c in price_columns("pf")},
    **{c: "INTEGER" for c in price_columns("pmc")},
    **{c: "INTEGER" for c in price_columns("pmvg")},
    **_FLAGS,
}
PRICE_COLUMNS = frozenset(c for c, t in COLUMNS.items() if t == "INTEGER")
_BOOLEANS = frozenset(c for c, t in _FLAGS.items() if t == "BOOLEAN")

# source header (whitespace collapsed, upper case, no space before %) -> output column
_FIXED: dict[str, str] = {
    "SUBSTÂNCIA": "substancia",
    "CNPJ": "cnpj",
    "LABORATÓRIO": "laboratorio",
    "CÓDIGO GGREM": "ggrem",
    "REGISTRO": "registro",
    "EAN 1": "ean_1",
    "EAN 2": "ean_2",
    "EAN 3": "ean_3",
    "PRODUTO": "produto",
    "APRESENTAÇÃO": "apresentacao",
    "CLASSE TERAPÊUTICA": "classe_terapeutica",
    "TIPO DE PRODUTO (STATUS DO PRODUTO)": "tipo_produto",
    "REGIME DE PREÇO": "regime_preco",
    "RESTRIÇÃO HOSPITALAR": "restricao_hospitalar",
    "CAP": "cap",
    "CONFAZ 87": "confaz_87",
    "ICMS 0%": "icms_0",
    "ANÁLISE RECURSAL": "analise_recursal",
    "LISTA DE CONCESSÃO DE CRÉDITO TRIBUTÁRIO (PIS/COFINS)": "lista_pis_cofins",
    "TARJA": "tarja",
}
# "DESTINAÇÃO COMERCIAL" (with a trailing nbsp in PMC, a "9" in PMVG): empty on every row
_IGNORED = re.compile(r"^DESTINAÇÃO COMERCIAL")
_PRICE = re.compile(r"^(PF|PMC|PMVG) (SEM IMPOSTOS|[0-9]+(?:,[0-9])?%)( ALC)?$")
_COMERCIALIZACAO = re.compile(r"^COMERCIALIZAÇÃO (\d{4})$")

# the source column behind each output column, for the data dictionary
SOURCE_NAMES: dict[str, str] = {
    **{out: src for src, out in _FIXED.items()},
    "registro_produto": "REGISTRO (9 primeiros dígitos)",
    "comercializacao_periodo": "COMERCIALIZAÇÃO <ano> (o ano do cabeçalho)",
    "comercializacao": "COMERCIALIZAÇÃO <ano>",
    "pmc_asterisco": f"* nos preços acima de 0 % ({FILES['pmc']})",
    "pmvg_asterisco": f"* nos preços acima de 0 % ({FILES['pmvg']})",
    "publicado_em": "Last-Modified do arquivo",
}
for _prefix, _label in (("pf", "PF"), ("pmc", "PMC"), ("pmvg", "PMVG")):
    for _name in price_columns(_prefix):
        _rate = _name[len(_prefix) + 1 :]
        if _rate == "sem_impostos":
            SOURCE_NAMES[_name] = f"{_label} Sem Impostos"
        else:
            _alc = _rate.endswith("_alc")
            _rate = _rate.removesuffix("_alc").replace("_", ",")
            SOURCE_NAMES[_name] = f"{_label} {_rate} %" + (" ALC" if _alc else "")
# the first columns are the same in both files; the kind-specific ones say so
for _name in price_columns("pmvg"):
    SOURCE_NAMES[_name] += f" ({FILES['pmvg']})"


# --- reading one file ------------------------------------------------------------------------


def _norm_header(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().replace(" %", "%").upper()


def column_for(header: str, kind: str) -> str | None:
    """The output column a source header feeds, or None for an ignored one."""
    h = _norm_header(header)
    if _IGNORED.match(h):
        return None
    if h in _FIXED:
        return _FIXED[h]
    m = _PRICE.match(h)
    if m:
        block, rate, alc = m.group(1).lower(), m.group(2), m.group(3)
        if rate == "SEM IMPOSTOS":
            return f"{block}_sem_impostos"
        name = f"{block}_{rate.rstrip('%').replace(',', '_')}"
        return name + ("_alc" if alc else "")
    if _COMERCIALIZACAO.match(h):
        return "comercializacao"
    raise SchemaDriftError(f"{FILES[kind]}: unknown column {header!r}; update anvisa/dados/cmed.py")


def _text(value: str | None) -> str | None:
    """A field as stripped text, or None for the `-` placeholder and empty fields."""
    if value is None:
        return None
    text = value.strip()
    return None if text in ("", "-") else text


def centavos(value: str | None) -> tuple[int | None, bool]:
    """`'27,44'` -> `(2744, False)`, `'27,44*'` -> `(2744, True)`; the `-` placeholder and
    empty fields -> `(None, False)`. The `*` marks every price above ICMS 0 % of a presentation
    whose rates "are shown but not all applicable" (note viii of the list's header): the
    exempt ones, and in the PMVG file also the CAP ones."""
    text = _text(value)
    if text is None:
        return None, False
    marked = text.endswith("*")
    try:
        return int(Decimal(text.rstrip("*").replace(".", "").replace(",", ".")) * 100), marked
    except InvalidOperation as exc:
        raise DadosError(f"CMED: not a price: {value!r}") from exc


def flag(value: str | None) -> str | None:
    """`Sim`/`Não` -> `S`/`N` (what the typed SELECT reads as BOOLEAN)."""
    text = _text(value)
    if text is None:
        return None
    key = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    if key == "sim":
        return "S"
    if key == "nao":
        return "N"
    raise DadosError(f"CMED: not Sim/Não: {value!r}")


def read_source(path: Path, kind: str) -> Iterator[dict[str, Any]]:
    """The rows of one source file as output columns -> normalized values (prices in centavos,
    flags as S/N, placeholders as None, CNPJ digits only, registro_produto derived)."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}: {kind!r}")
    with Path(path).open(encoding=ENCODING, newline="") as f:
        reader = csv.reader(f, delimiter=DELIMITER)
        header = next(reader, None)
        if not header:
            raise DadosError(f"{Path(path).name}: empty file")
        columns = [column_for(h, kind) for h in header]
        periodo = None  # the year in "COMERCIALIZAÇÃO 2025", which moves every year
        for h in header:
            m = _COMERCIALIZACAO.match(_norm_header(h))
            if m:
                periodo = m.group(1)
        expected = {"ggrem", "registro", "substancia", "pf_0", f"{kind}_0"}
        present = {c for c in columns if c}
        if not expected <= present:
            raise SchemaDriftError(
                f"{FILES[kind]}: missing columns {sorted(expected - present)}; "
                "update anvisa/dados/cmed.py"
            )
        width = len(header)
        for number, raw in enumerate(reader, 1):
            if not raw or all(not v.strip() for v in raw):
                continue
            if len(raw) != width:
                raise DadosError(
                    f"{Path(path).name}: record {number} has {len(raw)} fields, not {width}"
                )
            row: dict[str, Any] = {"comercializacao_periodo": periodo, f"{kind}_asterisco": "N"}
            for column, value in zip(columns, raw, strict=True):
                if column is None:
                    continue
                if column in PRICE_COLUMNS:
                    row[column], marked = centavos(value)
                    if marked:
                        row[f"{kind}_asterisco"] = "S"
                elif column in _BOOLEANS:
                    row[column] = flag(value)
                elif column == "cnpj":
                    text = _text(value)
                    row[column] = re.sub(r"\D", "", text) if text else None
                elif column == "registro":
                    text = _text(value)
                    row[column] = text
                    row["registro_produto"] = text[:9] if text and len(text) == 13 else None
                else:
                    row[column] = _text(value)
            if not row.get("ggrem"):
                raise DadosError(f"{Path(path).name}: record {number} has no GGREM")
            yield row


def read_header(path: Path) -> list[str]:
    """The source header of one file, raw (for the fixture check)."""
    with Path(path).open(encoding=ENCODING, newline="") as f:
        header = next(csv.reader(f, delimiter=DELIMITER), None)
    if not header:
        raise DadosError(f"{Path(path).name}: empty file")
    return header


# --- merging the two files -------------------------------------------------------------------


@dataclass(frozen=True)
class MergeStats:
    rows: int
    only_pmc: int
    only_pmvg: int


def merge(
    pmc: Iterable[dict[str, Any]], pmvg: Iterable[dict[str, Any]], *, publicado_em: str | None
) -> tuple[list[dict[str, Any]], MergeStats]:
    """One row per GGREM: the descriptive columns, flags and PF/PMC prices from the PMC file,
    the PMVG prices from the PMVG file. A GGREM in only one file keeps the other side None.
    `publicado_em` (dd/mm/yyyy HH:MM:SS, Brasília) is stamped on every row. Sorted by GGREM."""
    pmvg_rows = {r["ggrem"]: r for r in pmvg}
    merged: dict[str, dict[str, Any]] = {}
    only_pmc = 0
    for r in pmc:
        g = r["ggrem"]
        if g in merged:
            raise DadosError(f"{FILES['pmc']}: GGREM {g} listed twice")
        row = dict.fromkeys(COLUMNS)
        row.update({k: v for k, v in r.items() if k in COLUMNS})
        other = pmvg_rows.pop(g, None)
        if other is None:
            only_pmc += 1
        else:
            for c in price_columns("pmvg"):
                row[c] = other.get(c)
            for c in COLUMNS:  # the PMC file wins; fill what it left empty
                if row[c] is None and c not in PRICE_COLUMNS:
                    row[c] = other.get(c)
        merged[g] = row
    for g, other in pmvg_rows.items():  # only in the PMVG file
        row = dict.fromkeys(COLUMNS)
        row.update({k: v for k, v in other.items() if k in COLUMNS})
        merged[g] = row
    for row in merged.values():
        row["publicado_em"] = publicado_em
    rows = [merged[g] for g in sorted(merged)]
    return rows, MergeStats(len(rows), only_pmc, len(pmvg_rows))


def read_merged(
    pmc_path: Path, pmvg_path: Path, *, publicado_em: str | None
) -> tuple[list[dict[str, Any]], MergeStats]:
    return merge(
        read_source(pmc_path, "pmc"), read_source(pmvg_path, "pmvg"), publicado_em=publicado_em
    )


# --- the normalized CSV ----------------------------------------------------------------------


def write_csv(rows: Iterable[dict[str, Any]], target: Path) -> int:
    """RFC 4180 UTF-8 CSV with `COLUMNS` as header, None as an empty unquoted field (NULL to
    DuckDB): the form `parse.normalize` produces for ANVISA's CSVs, so conversion is shared."""
    n = 0
    with Path(target).open("w", encoding="utf-8", newline="") as out:
        writer = csv.writer(out, lineterminator="\n")
        writer.writerow(COLUMNS)
        for row in rows:
            writer.writerow(["" if row.get(c) is None else str(row[c]) for c in COLUMNS])
            n += 1
    return n


def read_csv_header(path: Path) -> list[str]:
    """The header of the normalized CSV (UTF-8, `,`), for the build's drift check."""
    with Path(path).open(encoding="utf-8", newline="") as f:
        header = next(csv.reader(f), None)
    if not header:
        raise DadosError(f"{Path(path).name}: empty file")
    return header


def count_csv_rows(path: Path) -> int:
    with Path(path).open(encoding="utf-8", newline="") as f:
        return max(sum(1 for _ in csv.reader(f)) - 1, 0)
