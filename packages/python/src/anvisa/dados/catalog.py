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

BASE_URL = "https://dados.anvisa.gov.br/dados/"
TIMESTAMP_BR = ("%d/%m/%Y %H:%M:%S", "%d/%m/%Y")
# INTEGER, not BIGINT: the ids stay below 2**31, and DuckDB-WASM hands BIGINT to JS as BigInt
TYPES = frozenset({"VARCHAR", "INTEGER", "BOOLEAN", "DATE", "TIMESTAMP"})


@dataclass(frozen=True)
class Dataset:
    """One source CSV, published as one Parquet file named `name` (plus, with `busca`, a
    folder of small search files beside it; see busca.py)."""

    name: str  # Parquet file name and manifest key
    group: str  # what `--dataset` also accepts; a main file and its detail file share one
    file: str  # under BASE_URL + directory
    title: str
    columns: dict[str, str]  # source header, in order -> one of TYPES
    sort: tuple[str, ...]  # source column names; the Parquet row order
    timestamp_formats: tuple[str, ...] = TIMESTAMP_BR  # strptime formats for DATE/TIMESTAMP
    formats: dict[str, tuple[str, ...]] = field(default_factory=dict)  # per-column overrides
    unescape: frozenset[str] = frozenset()  # VARCHAR columns that carry HTML entities
    row_group_size: int | None = None  # rows per Parquet row group; None = the build's default
    load_time: str | None = "DT_CARGA_ETL"  # TIMESTAMP column whose max is when ANVISA produced
    # the file; None when it has none
    directory: str = "CONSULTAS/PRODUTOS/"  # under BASE_URL; "" for the files at its root
    # also publish `data/<build_id>/<name>/`: the rows re-partitioned by word, CNPJ and number
    # into files small enough for a browser that downloads whole files (busca.py)
    busca: bool = False

    def __post_init__(self) -> None:
        if self.directory and not self.directory.endswith("/") or self.directory.startswith("/"):
            raise ValueError(f"{self.name}: directory must be '' or end in '/': {self.directory!r}")

    @property
    def url(self) -> str:
        return BASE_URL + self.directory + self.file

    @property
    def local_file(self) -> str:
        """The downloaded file's name in the work directory: by dataset, not by source name,
        since ANVISA reuses file names across folders."""
        return f"{self.name}.csv"

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

# The petition files: one row per stage (fila, análise, exigência, finalização...) of each
# petição of the alimentos area. Profiled 2026-10-06 on that day's files. The two are disjoint:
# the first holds the 20,518 petitions finalized at least once (all have
# DATA_PRIMEIRA_FINALIZACAO; 9 show an open situação again), the second the 336 never
# finalized, which is why it lacks the two finalization columns. Neither names the company:
# NUM_PROCESSO_PETICAO joins alimentos.nu_processo when the processo is a product's (72% of the
# finalized ones' processos; none of the 164 open "Processo" documents, new requests not
# registered yet). Sorted by processo so a processo's whole history is contiguous.
_PETICOES_SORT = ("NUM_PROCESSO_PETICAO", "NUM_EXPEDIENTE_PETICAO", "ORDEM_OCORRE_GRUPO_ETAPA_ASC")

PETICOES_ALIMENTO = Dataset(
    name="peticoes_alimento",
    group="peticoes_alimento",
    file="CICLO_ANALISE_PETICOES_ALIMENTO.CSV",
    directory="",
    title="Alimentos: ciclo de análise das petições finalizadas",
    columns={  # the header line reads `#NUM_EXPEDIENTE_PETICAO;...`; parse drops the `#`
        "NUM_EXPEDIENTE_PETICAO": "VARCHAR",  # 9 or 10 digits (one of 8), leading zeros
        "NUM_PROCESSO_PETICAO": "VARCHAR",  # digits; 17 on 57,288 rows, 13 on 11,357, 14-16 on 543
        "S_N_PETICAO_PRIMARIA": "BOOLEAN",  # S: the petição that opened the processo
        "COD_ASSUNTO_PETICAO": "INTEGER",  # one-to-one with DESC_ASSUNTO_PETICAO (98 of each)
        # one assunto ends in `&#8203,`: an entity whose `;` the export turned into `,`; kept
        "DESC_ASSUNTO_PETICAO": "VARCHAR",
        "DATA_SITUACAO_ATUAL_PETICAO": "TIMESTAMP",
        "DESC_SITUACAO_ATUAL_PETICAO": "VARCHAR",  # Publicado deferimento 15,905 petitions...
        "DATA_PRIMEIRA_FINALIZACAO": "TIMESTAMP",
        "DATA_FINALIZACAO_ATUAL": "TIMESTAMP",
        "DESC_TIPO_DOCUMENTO": "VARCHAR",  # Petição, Processo, Processo de Alimento...
        "DESC_AREA_INTERESSE": "VARCHAR",  # always Alimento
        "DESC_FILA_ANALISE": "VARCHAR",
        "DESC_SUB_FILA_LISTA_ANALISE": "VARCHAR",  # 915 end in a space; stripped
        "DESC_GRUPO_ETAPA_CICLO_ANALISE": "VARCHAR",  # the stage
        "DATA_INI_OCORRENCIA_GRP_ETAPA": "TIMESTAMP",
        "DATA_FIM_OCORRENCIA_GRP_ETAPA": "TIMESTAMP",
        "ORDEM_OCORRE_GRUPO_ETAPA_ASC": "INTEGER",  # 1.. per petição; unique with the expediente
        "ORDEM_OCORRE_GRUPO_ETAPA_DESC": "INTEGER",  # ..1, so 1 is the latest stage
    },
    sort=_PETICOES_SORT,
    timestamp_formats=("%m/%d/%Y %H:%M:%S",),  # month first; never also day first, see README
    load_time=None,
)

PETICOES_ALIMENTO_ANDAMENTO = Dataset(
    name="peticoes_alimento_andamento",
    group="peticoes_alimento",
    file="CICLO_ANALISE_PETICOES_ANDAMENTO_ALIMENTO.CSV",
    directory="",
    title="Alimentos: petições em análise",
    # PETICOES_ALIMENTO's columns minus the two finalization dates, spelled out so that a drift
    # in one file is fixed in its own entry. No `#` on this header.
    columns={
        "NUM_EXPEDIENTE_PETICAO": "VARCHAR",
        "NUM_PROCESSO_PETICAO": "VARCHAR",
        "S_N_PETICAO_PRIMARIA": "BOOLEAN",
        "COD_ASSUNTO_PETICAO": "INTEGER",
        "DESC_ASSUNTO_PETICAO": "VARCHAR",
        "DATA_SITUACAO_ATUAL_PETICAO": "TIMESTAMP",
        "DESC_SITUACAO_ATUAL_PETICAO": "VARCHAR",
        "DESC_TIPO_DOCUMENTO": "VARCHAR",
        "DESC_AREA_INTERESSE": "VARCHAR",
        "DESC_FILA_ANALISE": "VARCHAR",
        "DESC_SUB_FILA_LISTA_ANALISE": "VARCHAR",
        "DESC_GRUPO_ETAPA_CICLO_ANALISE": "VARCHAR",
        "DATA_INI_OCORRENCIA_GRP_ETAPA": "TIMESTAMP",
        "DATA_FIM_OCORRENCIA_GRP_ETAPA": "TIMESTAMP",
        "ORDEM_OCORRE_GRUPO_ETAPA_ASC": "INTEGER",
        "ORDEM_OCORRE_GRUPO_ETAPA_DESC": "INTEGER",
    },
    sort=_PETICOES_SORT,
    # Day first, unlike the finalized file. Each petição also has a `Todos` row (stage order
    # 0/0, no end date) spanning the whole cycle: leave it out when adding up stage durations.
    timestamp_formats=("%d/%m/%Y %H:%M:%S",),
    load_time=None,
)

PRODUTOS_IRREGULARES = Dataset(
    name="produtos_irregulares",
    group="produtos_irregulares",
    file="TA_CONSULTA_PRODUTOS_IRREGULARES_RESULTADO.CSV",
    directory="CONSULTAS/EMPRESA_FISCALIZACAO_PRODUTO/",
    title="Produtos irregulares: medidas de fiscalização",
    # Profiled 2026-10-06 on the 2026-10-05 file: 79,986 records, 4,916 dossiês, every area (8,743
    # rows and 722 dossiês are Alimento). One row per dossiê x ação x atividade x produto; 38 rows
    # equal another once values are stripped (`HARVONI` / `HARVONI `). Columns marked "per
    # dossiê" are the same on all of a dossiê's rows.
    columns={
        "CO_SEQ_DOSSIE_INVESTIG_MED": "INTEGER",  # the dossiê; one area each
        "NU_PROCESSO": "VARCHAR",  # 17 digits, per dossiê: the measure's processo, not a product's
        # per dossiê: who filed it, **not the company acted against**. ANVISA itself
        # (03112386000111) on 1,698 dossiês, a marketplace on some, the company itself on others
        "NU_CNPJ": "VARCHAR",
        "NO_RAZAO_SOCIAL": "VARCHAR",
        "CO_TIPO_PRODUTO": "INTEGER",  # 6 = Alimento, 3 = Saneantes, 1 = Medicamento...
        "DS_TIPO_PRODUTO": "VARCHAR",
        "CO_RISCO": "INTEGER",  # 19, 20, 21 = Risco I, II, III
        "DS_RISCO_PRODUTO": "VARCHAR",
        "TOTAL_MEDIDA_CAUTELAR": "INTEGER",  # 1-5; = the dossiê's distinct DT_PUBLICACAO on 99%
        "NO_EMPRESA_INVESTIGADA": "VARCHAR",  # 24,943 empty
        # the company acted against: 14 digits on 35,029 rows, a CPF's 11 on 2,334, text on
        # most other filled ones (DESCONHECIDO, Desconhecido, Não se aplica, NA...), 31,658 empty
        "NU_CNPJ_EMPRESA_INVESTIGADA": "VARCHAR",
        "CO_ASSUNTO": "INTEGER",
        "DT_PUBLICACAO_MEDIDA": "TIMESTAMP",  # per dossiê: the latest DT_PUBLICACAO
        "CO_ACAO_FISCALIZACAO": "INTEGER",  # 1-6: Suspensão, Proibição, Recolhimento, ...
        "CO_ATIVIDADE_FISCALIZACAO": "INTEGER",  # 12,497 empty
        "DT_PUBLICACAO": "TIMESTAMP",  # when this measure was published
        # per dossiê, every product joined by ". "; cut at 4,000 characters as written (5,924
        # rows), so use PRODUTO
        "PRODUTOS_CONCATENADOS": "VARCHAR",
        "DS_ACAO_FISCALIZACAO": "VARCHAR",
        "DS_ATIVIDADE_FISCALIZACAO": "VARCHAR",
        "ACAO_ATIVIDADE": "VARCHAR",  # the dossiê's ações and atividades as one sentence
        "PRODUTO": "VARCHAR",  # 220 with unescaped inner quotes, 49 empty
        "REGISTRO": "VARCHAR",  # empty on every Alimento row; up to 11 digits elsewhere
        "DT_CARGA_ETL": "TIMESTAMP",
    },
    # Area first, so one area's rows are contiguous, then the company acted against
    sort=("CO_TIPO_PRODUTO", "NU_CNPJ_EMPRESA_INVESTIGADA", "CO_SEQ_DOSSIE_INVESTIG_MED"),
    timestamp_formats=("%m/%d/%Y %H:%M:%S",),  # month first, like saneantes
    unescape=frozenset({"PRODUTO", "PRODUTOS_CONCATENADOS", "NO_EMPRESA_INVESTIGADA"}),
)

COSMETICOS = Dataset(
    name="cosmeticos",
    group="cosmeticos",
    file="TA_CONSULTA_COSMETICOS.CSV",
    title="Cosméticos: registros e notificações",
    # Profiled 2026-10-08 on the 2026-10-07 file (228,157,510 bytes): Windows-1252 (0x96 21,282
    # times, 0x92 5,590), LF only, 1,204,980 records, all 10 fields wide, no line break inside
    # any value. One row per processo, except 11,144 processos listed twice with the same CNPJ
    # and registro: once with ST_REGISTRADO 0 (ISENTO DE REGISTRO 8,936, REGISTRO 2,207,
    # DESCARTAVEL 1) and once with 1. The 1 rows come after record 1,089,484, so a pair sits
    # up to a million records apart, and a company's rows are scattered (1,133,012 runs for
    # 5,246 CNPJs).
    columns={
        # digits only: 17 on 1,126,326 rows, 15 on 69,822, 13 on 8,826, 14 on 6
        "NU_PROCESSO": "VARCHAR",
        # 737 with inner quotes, 52,698 with surrounding spaces (stripped); 193 entities, 157
        # of them `&AMP;` in capitals, then &#9679;, &#61656; (private use), &#8207;...; `&KISS;`
        # is no entity and stays. 11 are UTF-8 read as cp1252 upstream (LOÃ‡ÃƒO), two with
        # the bytes cp1252 leaves undefined (0x81, 0x8D): kept as written
        "NO_PRODUTO": "VARCHAR",
        "NU_CNPJ_EMPRESA": "VARCHAR",  # always 14 digits; one razão social per CNPJ
        # 10,295 with surrounding whitespace (stripped); 260 begin with the control characters
        # \x17\x10\x16\x18 (VITORIA FACE...), kept; `&G `, `&CO.` are text, not entities
        "NO_RAZAO_SOCIAL_EMPRESA": "VARCHAR",
        # 178,403 empty (14.8%); years 1997 to 2056; 167,350 with a time other than 00:00:00,
        # all but 120 of them on Notificado rows
        "DT_VENCIMENTO": "TIMESTAMP",
        "ST_SITUACAO_PRODUTO": "BOOLEAN",  # S 1,008,853 / N 196,127
        # 9 digits on 95,192 rows (every ST_REGISTRADO 1, and 13,283 of the 0s), one `null`
        # as text (kept), empty on the rest
        "NU_REGISTRO": "VARCHAR",
        # ISENTO DE REGISTRO 591,252, Notificado 524,497, DESCARTAVEL 5,043, REGISTRO 2,278;
        # empty on exactly the ST_REGISTRADO 1 rows
        "DS_TIPO_PETICAO": "VARCHAR",
        "ST_REGISTRADO": "BOOLEAN",  # 0 on 1,123,070 / 1 on 81,910
        "DT_ATUALIZACAO": "TIMESTAMP",  # 06/10/2026 00:00:00 on every row: ANVISA's load time
    },
    # NU_PROCESSO is unique across CNPJs, ST_REGISTRADO tells a pair apart: a total order
    sort=("NU_CNPJ_EMPRESA", "NU_PROCESSO", "ST_REGISTRADO"),
    # day first, like alimentos and unlike saneantes: 636,367 expiries have a day above 12
    # (29/09/2005 00:00:00), none a month above 12; every value carries a time
    timestamp_formats=("%d/%m/%Y %H:%M:%S",),
    unescape=frozenset({"NO_PRODUTO"}),
    load_time="DT_ATUALIZACAO",
    # 24 MB of Parquet is too much for a browser that downloads whole files: the search files
    # (about 3,200 of them, 45-75 KB each) answer a query with one or two downloads
    busca=True,
)

CATALOG: tuple[Dataset, ...] = (
    ALIMENTOS,
    ALIMENTOS_RESULTADO,
    SANEANTES,
    PETICOES_ALIMENTO,
    PETICOES_ALIMENTO_ANDAMENTO,
    PRODUTOS_IRREGULARES,
    COSMETICOS,
)


def select(names: Iterable[str], catalog: tuple[Dataset, ...] = CATALOG) -> tuple[Dataset, ...]:
    """The datasets matching any of `names` (a dataset name or a group), in catalog order."""
    wanted = set(names)
    unknown = wanted - {d.name for d in catalog} - {d.group for d in catalog}
    if unknown:
        valid = sorted({d.name for d in catalog} | {d.group for d in catalog})
        raise DadosError(f"unknown dataset {sorted(unknown)}; choose from {valid}")
    return tuple(d for d in catalog if d.name in wanted or d.group in wanted)
