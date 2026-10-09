"""Search files for a dataset with `busca` (format version 1): its rows published once more,
re-partitioned into Parquet files small enough for a browser that downloads whole files.

Under `data/<build_id>/<name>/`:

    palavras/NNNN.parquet   `palavra` + the row columns, one row per (word, source row);
                            consecutive words in byte order, packed to about TARGET rows
    empresas/NNN.parquet    the row columns by CNPJ, consecutive CNPJs packed the same way
    numeros/DDD.parquet     `num` + the row columns, where `num` is the processo and, when it
                            is all digits, the registro; DDD is the last three digits of `num`
    empresas.parquet        one row per CNPJ with its razão social
    indice.json             which file holds which range, with sizes

A search downloads `indice.json` once and then one or two files: a 14-digit query is a CNPJ,
a digits-only query a number, anything else is tokenized with `words()` and the files of its
rarest token are read. **Matching is by word start**, so the site and this module must split
names identically: the tokenizer here is the contract, and `fixtures/dados/tokens.json` holds
the cases both sides test against. Designed and measured by the anvisa-dash session
(2026-10-09); SPEC.md there is the source of this layout.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..errors import DadosError
from .catalog import Dataset
from .convert import _quote, connect

VERSION = 1
TARGET = 3000  # rows per file in palavras/ and empresas/
STOPWORDS = frozenset(["de", "da", "do", "das", "dos", "para", "com", "em", "e", "a", "o"])
APOSTROPHES = re.compile("['’‘`´]")
SEPARATOR = re.compile(r"[^a-z0-9]+")
COMPOUND = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)+")
# the row columns, as the Parquet names them; `dt_atualizacao` is constant (the manifest's
# `source.loaded_at`) and the razão social lives in empresas.parquet
ROW_COLUMNS = (
    "nu_processo",
    "no_produto",
    "nu_cnpj_empresa",
    "dt_vencimento",
    "st_situacao_produto",
    "nu_registro",
    "ds_tipo_peticao",
    "st_registrado",
)
COMPANY_NAME = "no_razao_social_empresa"
PARQUET = "FORMAT parquet, COMPRESSION zstd"


def normalize(text: str) -> str:
    """Apostrophes removed, NFKD, every combining mark (category M*) dropped, lowercase."""
    decomposed = unicodedata.normalize("NFKD", APOSTROPHES.sub("", text))
    return "".join(c for c in decomposed if not unicodedata.category(c).startswith("M")).lower()


def words(text: str | None) -> list[str]:
    """The distinct words of a product name, in order of first appearance: tokens of 2+
    characters that are not stopwords, then each hyphenated compound joined (ANTI-QUEDA gives
    anti, queda, antiqueda)."""
    if not text:
        return []
    n = normalize(text)
    tokens = SEPARATOR.split(n) + [c.replace("-", "") for c in COMPOUND.findall(n)]
    return list(dict.fromkeys(t for t in tokens if len(t) >= 2 and t not in STOPWORDS))


@dataclass
class Group:
    """One output file: the keys it holds, in byte order, and their row count."""

    first: str
    rows: int
    keys: list[str]


def pack(counts: list[tuple[str, int]], target: int) -> list[Group]:
    """Consecutive keys packed until adding the next would pass `target` rows; a key above it
    gets a file of its own."""
    groups: list[Group] = []
    for key, n in counts:
        if not groups or groups[-1].rows + n > target:
            groups.append(Group(key, 0, []))
        groups[-1].keys.append(key)
        groups[-1].rows += n
    return groups


def listed(index: dict[str, Any]) -> list[tuple[str, int]]:
    """Every file `indice.json` points to, relative to its folder, with its size."""
    files = [(e[2], e[3]) for e in index["palavras"] + index["empresas"]]
    files += [(f"numeros/{suffix}.parquet", size) for suffix, size in index["numeros"].items()]
    files.append(("empresas.parquet", index["empresas_parquet"]))
    return files


def _write_csv(path: Path, header: str, rows) -> None:
    # every value is [a-z0-9] or an integer: no quoting needed
    with path.open("w", encoding="ascii") as f:
        f.write(header + "\n")
        for row in rows:
            f.write(",".join(map(str, row)) + "\n")


def _load_csv(con, table: str, path: Path, types: dict[str, str]) -> None:
    con.execute(
        f"CREATE TABLE {table} AS SELECT * FROM read_csv({_quote(str(path))}, types = {types!r})"
    )


def _groups_table(con, table: str, key: str, groups: list[Group], tmp: Path) -> None:
    """A (key, file number) table from `groups`, through a CSV: thousands of keys per file."""
    _write_csv(
        tmp / f"{table}.csv", f"{key},arq", ((k, n) for n, g in enumerate(groups) for k in g.keys)
    )
    _load_csv(con, table, tmp / f"{table}.csv", {key: "VARCHAR", "arq": "INTEGER"})


def write(ds: Dataset, parquet: Path, folder: Path, *, target: int = TARGET) -> dict[str, Any]:
    """Write the search files of `parquet` (the converted `ds`) under `folder`, replacing it,
    and return the manifest's `busca` object with `indice` relative to the folder."""
    names = {c.lower() for c in ds.columns}
    missing = {*ROW_COLUMNS, COMPANY_NAME} - names
    if missing:
        raise DadosError(f"{ds.name}: busca needs the columns {sorted(missing)}")
    if folder.exists():
        shutil.rmtree(folder)
    for sub in ("palavras", "empresas", "numeros"):
        (folder / sub).mkdir(parents=True)
    columns = ", ".join(ROW_COLUMNS)
    src = _quote(str(parquet))
    with tempfile.TemporaryDirectory(prefix="busca-") as tmpdir:
        tmp = Path(tmpdir)
        with connect(tmp / "spill") as con:
            # DuckDB orders VARCHAR by bytes (no collation), the order the index relies on.
            # `i` is a stable row id for the joins, from the catalog's total order.
            order = ", ".join(c.lower() for c in ds.sort)
            con.execute(
                f"CREATE TABLE r AS SELECT row_number() OVER (ORDER BY {order}) AS i, "
                f"{columns} FROM read_parquet({src})"
            )

            # palavras: tokenized in Python, so the rule is exactly words() above
            def word_rows():
                cache: dict[str, list[str]] = {}
                cursor = con.execute("SELECT i, no_produto FROM r")
                while batch := cursor.fetchmany(50_000):
                    for i, name in batch:
                        ws = cache.get(name)
                        if ws is None:
                            ws = cache[name] = words(name)
                        for w in ws:
                            yield (w, i)

            _write_csv(tmp / "w.csv", "palavra,i", word_rows())
            _load_csv(con, "w", tmp / "w.csv", {"palavra": "VARCHAR", "i": "BIGINT"})
            counts = con.execute(
                "SELECT palavra, count(*) FROM w GROUP BY 1 ORDER BY palavra"
            ).fetchall()
            word_groups = pack(counts, target)
            _groups_table(con, "g", "palavra", word_groups, tmp)
            r_columns = ", ".join(f"r.{c}" for c in ROW_COLUMNS)
            con.execute(
                f"CREATE TABLE wp AS SELECT g.arq, w.palavra, {r_columns} "
                "FROM w JOIN g USING (palavra) JOIN r USING (i) "
                "ORDER BY g.arq, w.palavra, r.no_produto, r.nu_processo, r.st_registrado"
            )
            index_words = []
            for n, g in enumerate(word_groups):
                name = f"palavras/{n:04d}.parquet"
                rows_of = f"SELECT * EXCLUDE (arq) FROM wp WHERE arq = {n}"
                index_words.append([g.first, g.rows, name, _copy(con, rows_of, folder / name)])

            # empresas: consecutive CNPJs
            by_company = con.execute(
                "SELECT nu_cnpj_empresa, count(*) FROM r GROUP BY 1 ORDER BY 1"
            ).fetchall()
            company_groups = pack(by_company, target)
            _groups_table(con, "ge", "cnpj", company_groups, tmp)
            con.execute(
                f"CREATE TABLE ep AS SELECT ge.arq, {columns} "
                "FROM r JOIN ge ON ge.cnpj = r.nu_cnpj_empresa "
                "ORDER BY ge.arq, nu_cnpj_empresa, no_produto, nu_processo, st_registrado"
            )
            index_companies = []
            for n, g in enumerate(company_groups):
                name = f"empresas/{n:03d}.parquet"
                rows_of = f"SELECT * EXCLUDE (arq) FROM ep WHERE arq = {n}"
                index_companies.append([g.first, g.rows, name, _copy(con, rows_of, folder / name)])

            # numeros: the processo, and the registro when it is all digits, by last 3 digits
            con.execute(
                f"CREATE TABLE np AS SELECT right(num, 3) AS arq, num, {columns} FROM ("
                "SELECT nu_processo AS num, i FROM r UNION ALL "
                "SELECT nu_registro, i FROM r WHERE regexp_full_match(nu_registro, '[0-9]+')"
                ") JOIN r USING (i) ORDER BY arq, num, nu_processo, st_registrado"
            )
            index_numbers = {}
            for (suffix,) in con.execute("SELECT DISTINCT arq FROM np ORDER BY 1").fetchall():
                name = f"numeros/{suffix}.parquet"
                rows_of = f"SELECT * EXCLUDE (arq) FROM np WHERE arq = {_quote(suffix)}"
                index_numbers[suffix] = _copy(con, rows_of, folder / name)

            companies_size = _copy(
                con,
                f"SELECT nu_cnpj_empresa, min({COMPANY_NAME}) AS {COMPANY_NAME} "
                f"FROM read_parquet({src}) GROUP BY 1 ORDER BY 1",
                folder / "empresas.parquet",
            )

    index = {
        "versao": VERSION,
        "palavras": index_words,
        "empresas": index_companies,
        "numeros": index_numbers,
        "empresas_parquet": companies_size,
    }
    raw = json.dumps(index, ensure_ascii=False, separators=(",", ":")).encode()
    (folder / "indice.json").write_bytes(raw)
    files = [p for p in folder.rglob("*") if p.is_file()]
    return {
        "versao": VERSION,
        "indice": "indice.json",
        "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "arquivos": len(files),
        "bytes_total": sum(p.stat().st_size for p in files),
    }


def _copy(con, query: str, dest: Path) -> int:
    """COPY `query` to `dest` as zstd Parquet and return the file's size."""
    con.execute(f"COPY ({query}) TO {_quote(str(dest))} ({PARQUET})")
    return dest.stat().st_size
