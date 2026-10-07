"""Which open-data CSVs are published, and the expected header and type of each column.

A dataset's `columns` is the source header **in order**: the header check, the typed SELECT and
the manifest's column list all derive from it. Output column names are the source names
lowercased, so they stay traceable to ANVISA's data dictionary
(`PRODUTOS/Documentacao_e_Dicionario_de_Dados_Regularizados_Alimentos.pdf` for alimentos).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from ..errors import DadosError

BASE_URL = "https://dados.anvisa.gov.br/dados/CONSULTAS/PRODUTOS/"
TIMESTAMP_BR = ("%d/%m/%Y %H:%M:%S", "%d/%m/%Y")
# INTEGER, not BIGINT: the ids stay below 2**31, and DuckDB-WASM hands BIGINT to JS as BigInt
TYPES = frozenset({"VARCHAR", "INTEGER", "BOOLEAN", "DATE", "TIMESTAMP"})


@dataclass(frozen=True)
class Dataset:
    """One source CSV, published as one Parquet file named `name`."""

    name: str  # Parquet file name and manifest key
    group: str  # what `--dataset` also accepts; a main file and its detail file share one
    file: str  # under BASE_URL
    title: str
    columns: dict[str, str]  # source header, in order -> one of TYPES
    sort: tuple[str, ...]  # source column names; the Parquet row order
    timestamp_formats: tuple[str, ...] = TIMESTAMP_BR  # strptime formats for DATE/TIMESTAMP
    formats: dict[str, tuple[str, ...]] = field(default_factory=dict)  # per-column overrides
    unescape: frozenset[str] = frozenset()  # VARCHAR columns that carry HTML entities
    row_group_size: int | None = None  # rows per Parquet row group; None = the build's default
    load_time: str = "DT_CARGA_ETL"  # TIMESTAMP column whose max is when ANVISA produced the file

    @property
    def url(self) -> str:
        return BASE_URL + self.file

    def formats_for(self, column: str) -> tuple[str, ...]:
        return self.formats.get(column, self.timestamp_formats)


ALIMENTOS = Dataset(
    name="alimentos",
    group="alimentos",
    file="TA_CONSULTA_ALIMENTOS.CSV",
    title="Alimentos: registros e notificações",
    columns={
        "NO_PRODUTO": "VARCHAR",
        "NU_REGISTRO_NOTIFICACAO_PRODUTO": "VARCHAR",
        "NU_PROCESSO": "VARCHAR",  # digits only, 6 to 17 of them: 17 today, 13 on old rows
        "NO_RAZAO_SOCIAL_EMPRESA": "VARCHAR",
        "NU_CNPJ_EMPRESA": "VARCHAR",  # 14 chars, zero-padded
        "DS_SITUACAO_ASSUNTO_DOC": "VARCHAR",
        "DT_VENCIMENTO_REGISTRO": "DATE",  # month and year only, see `formats`
        "CO_SEQ_PRODUTO": "INTEGER",
        "DT_REGULARIZACAO": "TIMESTAMP",
        "CO_SEQ_APRESENTACAO_PRODUTO": "INTEGER",
        "CO_TIPO_REGULARIZACAO": "INTEGER",
        "CO_SITUACAO_ASSUNTO_DOC": "INTEGER",
        "DT_PUBLICACAO": "TIMESTAMP",
        "DT_SITUACAO": "TIMESTAMP",
        "DT_INICIO_ANALISE": "TIMESTAMP",
        "NU_REGISTRO_PRODUTO": "VARCHAR",
        "ST_PRODUTO_ATIVO": "BOOLEAN",
        "NU_REGISTRO": "VARCHAR",
        "CO_FORMA_FISICA": "INTEGER",
        "MARCAS": "VARCHAR",  # several brands joined by " ; "
        "SITUACAO_REGISTRO": "VARCHAR",
        "TIPO_REGULARIZACAO": "VARCHAR",
        "DS_CATEGORIA_PRODUTO": "VARCHAR",
        "DS_ALEGACAO_FUNCIONAL": "VARCHAR",
        "DT_CARGA_ETL": "TIMESTAMP",
    },
    # Only the first key prunes row groups on a point lookup: a processo lookup scans the file.
    sort=("NU_CNPJ_EMPRESA", "NU_PROCESSO"),
    # 122029 = December 2029 (stored as 2029-12-01); 3 of 66,681 rows write 03/2031. Every
    # notificação carries 122029, registros expire 5, 10, 15 or 20 years after DT_REGULARIZACAO.
    formats={"DT_VENCIMENTO_REGISTRO": ("%m%Y", "%m/%Y")},
    unescape=frozenset({"MARCAS", "NO_PRODUTO"}),  # L&apos;ANA MED, &quot;, &#8208;
)

ALIMENTOS_RESULTADO = Dataset(
    name="alimentos_resultado",
    group="alimentos",
    file="TA_CONSULTA_ALIMENTOS_RESULTADO.CSV",
    title="Alimentos: detalhe das apresentações",
    columns={
        "CO_SEQ_APRESENTACAO_PRODUTO": "INTEGER",  # joins alimentos.co_seq_apresentacao_produto
        "NU_APRESENTACAO_PRODUTO": "VARCHAR",
        "NU_REGISTRO": "VARCHAR",
        "VALIDADE": "VARCHAR",  # "03 Meses"
        "DS_FORMA_FISICA": "VARCHAR",
        "SITUACAO_APRESENTACAO": "VARCHAR",
        "MATERIAL_EMBALAGENS": "VARCHAR",
        "TIPO_EMBALAGENS": "VARCHAR",
        "EMPRESAS_ENVASADORAS": "VARCHAR",
        "EMPRESAS_INTERNACIONAIS": "VARCHAR",
        "GRUPOS_POPULACIONAIS": "VARCHAR",
        "VIAS_ADMINISTRACAO": "VARCHAR",
        "TABELA_NUTRICIONAL": "VARCHAR",
        "INTOLERANCIAS": "VARCHAR",  # " | "-separated
        "ALERGENICOS": "VARCHAR",  # " | " between groups, "#" between items
        "CO_PRODUTO": "INTEGER",  # = alimentos.co_seq_produto on every joined row
        "DT_CARGA_ETL": "TIMESTAMP",
    },
    # The one query on this table is "every apresentação of one product", so the product id leads:
    # a product's rows are contiguous and `WHERE co_produto = ?` prunes to one row group.
    sort=("CO_PRODUTO", "CO_SEQ_APRESENTACAO_PRODUTO"),
    # Recent rows carry long tabela_nutricional text: at 8192 rows a group reached 411 KB.
    row_group_size=2048,
)

SANEANTES = Dataset(
    name="saneantes",
    group="saneantes",
    file="TA_CONSULTA_SANEANTES.CSV",
    title="Saneantes: registros e notificações",
    # Profiled 2026-10-06 on the 2026-10-05 file: 144,384 records, all 10 fields wide once a
    # `"` + line break inside a product name is read as part of the value (see parse.py).
    columns={
        "NO_PRODUTO": "VARCHAR",  # 36 with inner quotes, 182 with line breaks, one &#8207;
        "NU_PROCESSO": "VARCHAR",  # digits only; 17 on 137,771 rows, 15 on 5,420, 13 on 1,184
        "NU_CNPJ_EMPRESA": "VARCHAR",  # always 14 digits
        "NO_RAZAO_SOCIAL_EMPRESA": "VARCHAR",  # 1,502 end in whitespace; stripped
        "ST_PRODUTO_ATIVO": "BOOLEAN",  # S 88,238 / N 56,146
        "NU_REGISTRO_PRODUTO": "VARCHAR",  # 9 digits on registros (one 10), empty on notificações
        "DT_VENCIMENTO_PRODUTO": "TIMESTAMP",  # 8,563 empty; years 2001 to 3033 (sic)
        "IS_REGISTRADO": "BOOLEAN",  # 0 = notificado (115,561), 1 = registrado (28,823)
        "NU_EXPEDIENTE": "VARCHAR",  # 9 or 10 digits, leading zeros; unique per record
        "DT_ATUALIZACAO": "TIMESTAMP",  # the same value on every row: ANVISA's load time
    },
    sort=("NU_CNPJ_EMPRESA", "NU_PROCESSO"),
    timestamp_formats=("%m/%d/%Y %H:%M:%S", "%m/%d/%Y"),  # month first, unlike alimentos
    unescape=frozenset({"NO_PRODUTO"}),
    load_time="DT_ATUALIZACAO",
)

CATALOG: tuple[Dataset, ...] = (ALIMENTOS, ALIMENTOS_RESULTADO, SANEANTES)


def select(names: Iterable[str], catalog: tuple[Dataset, ...] = CATALOG) -> tuple[Dataset, ...]:
    """The datasets matching any of `names` (a dataset name or a group), in catalog order."""
    wanted = set(names)
    unknown = wanted - {d.name for d in catalog} - {d.group for d in catalog}
    if unknown:
        valid = sorted({d.name for d in catalog} | {d.group for d in catalog})
        raise DadosError(f"unknown dataset {sorted(unknown)}; choose from {valid}")
    return tuple(d for d in catalog if d.name in wanted or d.group in wanted)
