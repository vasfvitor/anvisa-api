"""Typed, sorted Parquet from a downloaded CSV. The only module that imports duckdb."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from ..errors import DadosError
from .catalog import Dataset
from .parse import normalize

# ~9 row groups for alimentos' 66k rows. Sorted by CNPJ, a point lookup with native DuckDB over
# HTTP (`read_parquet('https://...')`, not the browser: see the README) reads the footer and one
# group's column chunks; DuckDB's default (122,880) would make one group of it all.
ROW_GROUP_SIZE = 8192
NULL_LIMIT = 0.2  # a typed column losing more of its values than this fails the build
REJECT_LIMIT = 0.001  # so does skipping more than this share of malformed records
# DuckDB's cap, below the 7 GB of a GitHub runner (its default is 80% of RAM). Past it the
# load and the sort spill to `.duckdb_tmp` beside the CSV instead of failing.
MEMORY_LIMIT = "3GB"


@dataclass(frozen=True)
class Stats:
    rows: int
    rejected: tuple[int, ...]  # record numbers skipped for a wrong field count
    nulls_added: dict[str, int]  # per typed column: non-empty values that did not parse
    loaded_at: str | None  # max(DT_CARGA_ETL): when ANVISA's ETL produced the file


def _quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def typed(ds: Dataset, column: str) -> str:
    """SQL turning the VARCHAR `column` of the normalized CSV into its catalog type."""
    ref = f'"{column}"'
    kind = ds.columns[column]
    if kind == "VARCHAR":
        return ref
    if kind == "INTEGER":
        return f"TRY_CAST({ref} AS INTEGER)"
    if kind == "BOOLEAN":  # alimentos writes S/N, saneantes 1/0; anything else (an `X`) is NULL
        return (
            f"CASE {ref} WHEN 'S' THEN true WHEN '1' THEN true "
            "WHEN 'N' THEN false WHEN '0' THEN false END"
        )
    formats = "[" + ", ".join(_quote(f) for f in ds.formats_for(column)) + "]"
    parsed = f"try_strptime({ref}, {formats})"  # naive: Brasília local time, as ANVISA writes it
    return f"CAST({parsed} AS DATE)" if kind == "DATE" else parsed


def select_sql(ds: Dataset) -> str:
    columns = ",\n  ".join(f"{typed(ds, c)} AS {c.lower()}" for c in ds.columns)
    order = ", ".join(c.lower() for c in ds.sort)
    return f"SELECT\n  {columns}\nFROM raw\nORDER BY {order}"


def convert(
    ds: Dataset, csv_path: Path, parquet: Path, *, row_group_size: int = ROW_GROUP_SIZE
) -> Stats:
    try:
        import duckdb
    except ImportError:
        raise DadosError("building Parquet needs DuckDB: pip install 'anvisa[dados]'") from None

    clean = Path(csv_path).with_name(Path(csv_path).name + ".utf8.csv")
    spill = Path(csv_path).with_name(".duckdb_tmp")
    try:
        normalized = normalize(ds, Path(csv_path), clean)
        total = normalized.rows + len(normalized.rejected)
        if len(normalized.rejected) > REJECT_LIMIT * total:
            raise DadosError(
                f"{ds.file}: {len(normalized.rejected)} of {total} records have the wrong "
                f"number of fields (first: record {normalized.rejected[0]})"
            )
        with duckdb.connect() as con:
            con.execute("SET threads = 1")  # keeps row order and row-group layout deterministic
            con.execute(f"SET memory_limit = {_quote(MEMORY_LIMIT)}")
            con.execute(f"SET temp_directory = {_quote(str(spill))}")
            schema = "{" + ", ".join(f"{_quote(c)}: 'VARCHAR'" for c in ds.columns) + "}"
            con.execute(
                f"CREATE TABLE raw AS SELECT * FROM read_csv({_quote(str(clean))}, delim = ',', "
                "quote = '\"', escape = '\"', header = true, auto_detect = false, "
                f"strict_mode = true, columns = {schema})"
            )

            # data-quality report, before anything is written: per typed column, how many
            # values were there and how many of those did not parse
            checks = [c for c, kind in ds.columns.items() if kind != "VARCHAR"]
            measures = ["count(*)"]
            for c in checks:
                measures.append(f'count("{c}")')
                measures.append(
                    f'count(*) FILTER (WHERE "{c}" IS NOT NULL AND {typed(ds, c)} IS NULL)'
                )
            has_load_time = ds.columns.get(ds.load_time) == "TIMESTAMP"
            measures.append(f"max({typed(ds, ds.load_time)})" if has_load_time else "NULL")
            rows, *counts, loaded_at = con.execute(
                f"SELECT {', '.join(measures)} FROM raw"
            ).fetchone()
            nulls_added = {}
            for column, present, lost in zip(checks, counts[::2], counts[1::2], strict=True):
                nulls_added[column.lower()] = lost
                if lost > NULL_LIMIT * present:
                    raise DadosError(
                        f"{ds.file}: {lost} of {present} values in {column} do not parse as "
                        f"{ds.columns[column]}; did ANVISA change the format?"
                    )

            parquet.parent.mkdir(parents=True, exist_ok=True)
            con.execute(
                f"COPY ({select_sql(ds)}) TO {_quote(str(parquet))} "
                f"(FORMAT parquet, COMPRESSION zstd, ROW_GROUP_SIZE {int(row_group_size)})"
            )
    finally:
        clean.unlink(missing_ok=True)
        shutil.rmtree(spill, ignore_errors=True)
    return Stats(
        rows=rows,
        rejected=normalized.rejected,
        nulls_added=nulls_added,
        loaded_at=loaded_at.isoformat() if loaded_at else None,
    )
