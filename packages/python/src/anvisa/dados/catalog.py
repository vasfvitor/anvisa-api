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
from . import cmed
from .descricoes import DESCRICOES

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
    # what a reader of the Parquet should know, in Portuguese: what one row is, which values are
    # placeholders, how to count. Published in the manifest and the Pages dictionary; the
    # parser-level quirks (encoding, quoting, date order) stay in the README
    notes: tuple[str, ...] = ()
    # "csv": one of ANVISA's Windows-1252 `;` files at `url`, normalized by parse.py.
    # "cmed": the two monthly CMED spreadsheets, found on the page at `url` and written as an
    # already-normalized CSV by cmed.py; `columns` are then the output names, not a source header
    kind: str = "csv"
    # output column -> the name it had in the source, when not the column key itself (cmed)
    source_names: dict[str, str] = field(default_factory=dict)
    # False for a "csv" file that quotes nothing: one record per line, `"` an ordinary
    # character (parse.unquoted); True is ANVISA's usual dialect (every text field quoted)
    quoted: bool = True

    def __post_init__(self) -> None:
        if self.kind not in ("csv", "cmed"):
            raise ValueError(f"{self.name}: unknown kind {self.kind!r}")
        if self.directory and not self.directory.endswith("/") or self.directory.startswith("/"):
            raise ValueError(f"{self.name}: directory must be '' or end in '/': {self.directory!r}")

    @property
    def descriptions(self) -> dict[str, str]:
        """Source header -> Portuguese description (descricoes.py)."""
        return DESCRICOES[self.name]

    @property
    def url(self) -> str:
        """Where the file is; for `cmed`, the first of its two files (cmed.FILES)."""
        if self.kind == "cmed":
            return BASE_URL + cmed.FILES["pmc"]
        return BASE_URL + self.directory + self.file

    def source_name(self, column: str) -> str:
        return self.source_names.get(column, column)

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
    notes=(
        "Uma linha por apresentação de um alimento ou suplemento regularizado; o produto é "
        "co_seq_produto, e nome, marcas, empresa, processo e situação são iguais em todas as "
        "suas apresentações.",
        "O detalhe de cada apresentação (embalagem, ingredientes, alergênicos) está em "
        "alimentos_resultado, junto por co_seq_apresentacao_produto; apresentações recentes "
        "podem ainda não ter detalhe.",
        "dt_vencimento_registro tem precisão de mês; nas notificações é sempre 12/2029, um "
        "valor de preenchimento.",
        "nu_processo não tem tamanho fixo (17 dígitos nos atuais, 13 nos antigos): compare "
        "como texto, sem pontuação.",
    ),
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
    notes=(
        "Uma linha por apresentação, com o que o rótulo diz: embalagem, forma física, "
        "ingredientes (em tabela_nutricional), intolerâncias e alergênicos.",
        "Para todas as apresentações de um produto, filtre co_produto = alimentos.co_seq_produto; "
        "as linhas de um produto são contíguas.",
        'Os campos de lista usam " | " entre itens e, nos alergênicos, "#" entre os itens de '
        "um grupo.",
    ),
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
        # 9 or 10 digits, leading zeros; 6 expedientes appear twice (Registrado + Notificado,
        # equal otherwise), the other 144,401 once (2026-10-11 file)
        "NU_EXPEDIENTE": "VARCHAR",
        "DT_ATUALIZACAO": "TIMESTAMP",  # the same value on every row: ANVISA's load time
    },
    sort=("NU_CNPJ_EMPRESA", "NU_PROCESSO"),
    timestamp_formats=("%m/%d/%Y %H:%M:%S", "%m/%d/%Y"),  # month first, unlike alimentos
    unescape=frozenset({"NO_PRODUTO"}),
    load_time="DT_ATUALIZACAO",
    notes=(
        "Uma linha por processo de saneante (produto de limpeza e afins) registrado ou "
        "notificado; is_registrado distingue os dois. Não há arquivo de detalhe.",
        "dt_vencimento_produto está vazio em parte das linhas e a ANVISA publica datas até o ano "
        "3033; não trate uma data distante como erro de leitura.",
        "nu_expediente identifica o produto, mas seis vêm em par: uma linha Registrado e uma "
        "Notificado, iguais no resto (a tabela produtos fica com a registrada). nu_processo "
        "tem 17, 15 ou 13 dígitos.",
    ),
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
    notes=(
        "Uma linha por etapa (fila, análise, exigência, finalização…) de cada petição de "
        "alimentos finalizada ao menos uma vez; as linhas de um processo são contíguas.",
        "O arquivo não traz a empresa: junte num_processo_peticao com alimentos.nu_processo, "
        "que encontra o produto de cerca de 72% dos processos.",
        "As petições nunca finalizadas estão em peticoes_alimento_andamento; nenhuma petição "
        "aparece nos dois arquivos.",
    ),
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
    notes=(
        "Uma linha por etapa de cada petição de alimentos ainda em análise (nunca finalizada); "
        "mesmas colunas de peticoes_alimento menos as duas datas de finalização.",
        'Cada petição tem também uma linha "Todos" (ordem 0, sem data de fim) que cobre o ciclo '
        "inteiro: deixe-a de fora ao somar durações.",
        'As petições do tipo "Processo" são pedidos novos, de produtos que ainda não existem em '
        "alimentos.",
    ),
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
    notes=(
        "Medidas de fiscalização (suspensão, proibição, recolhimento, apreensão, interdição, "
        "inutilização) de todas as áreas da ANVISA, uma linha por dossiê × ação × atividade × "
        "produto; co_tipo_produto 6 é alimento.",
        "nu_cnpj é quem abriu o dossiê (muitas vezes a própria ANVISA), não a empresa "
        "fiscalizada: essa está em nu_cnpj_empresa_investigada, que nem sempre é um CNPJ.",
        "A maioria das medidas de alimentos atinge produtos e empresas sem regularização: poucas "
        "linhas juntam com alimentos por CNPJ ou nome.",
        "produtos_concatenados é cortado em 4.000 caracteres; use produto.",
    ),
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
    notes=(
        "Uma linha por processo de cosmético, perfume ou produto de higiene notificado, isento "
        "de registro ou registrado, exceto 11.144 processos listados duas vezes (st_registrado "
        "falso e depois verdadeiro, mesmo nu_registro): conte produtos com DISTINCT nu_processo.",
        "O Parquet tem 24 MB; para consultas no navegador use os arquivos de busca (chave busca "
        "do manifest), partidos por palavra, CNPJ e número.",
        "dt_vencimento está vazio em 15% das linhas, e há produtos ativos com data já passada.",
        "Nomes com caracteres de controle no início e com acentos trocados (LOÃ‡ÃƒO) vêm assim "
        "da ANVISA e são mantidos.",
    ),
)

MEDICAMENTOS = Dataset(
    name="medicamentos",
    group="medicamentos",
    file="DADOS_ABERTOS_MEDICAMENTOS.csv",
    directory="",
    title="Medicamentos: registros, ativos e inativos",
    # Profiled 2026-10-22 on the 2026-10-13 file (45.9 MB): 228,869 records, 11 fields each,
    # Windows-1252, the usual quoted dialect. Only 42,661 records are distinct: ANVISA writes
    # the same product × processo row up to dozens of times (the derived tables count once).
    columns={
        "TIPO_PRODUTO": "VARCHAR",  # always MEDICAMENTO
        "NOME_PRODUTO": "VARCHAR",  # 440 with numeric entities (&#231;); "N?O DECLARADO" (sic)
        "DATA_FINALIZACAO_PROCESSO": "DATE",  # dd/mm/yyyy; when the registro was granted
        # Similar, Genérico, Novo, Específico, Biológico, Fitoterápico, Dinamizado,
        # Radiofármaco, BAIXO RISCO...; the source also writes DINAMIZADO, FITOTER?PICO (184,
        # a literal ?), and truncates to 12 chars ("Gases Medici", "Produto de T"); 9,381 empty
        "CATEGORIA_REGULATORIA": "VARCHAR",
        # 9 digits on 207,316 rows (the product; the 13-digit apresentação never appears),
        # empty on 21,495 (dinamizados and BAIXO RISCO notified products), 58 shorter or longer
        "NUMERO_REGISTRO_PRODUTO": "VARCHAR",
        "DATA_VENCIMENTO_REGISTRO": "DATE",  # mmyyyy (213,396), mm/yyyy (51); see `formats`
        "NUMERO_PROCESSO": "VARCHAR",  # digits only: 17 (142k), 15 (44k), 13 (21k); 21k empty
        "CLASSE_TERAPEUTICA": "VARCHAR",  # 505 values, upper case; 26k empty
        "EMPRESA_DETENTORA_REGISTRO": "VARCHAR",  # always "<14-digit CNPJ> - <razão social>"
        "SITUACAO_REGISTRO": "VARCHAR",  # Ativo (85,092 rows) or Inativo (143,777)
        "PRINCIPIO_ATIVO": "VARCHAR",  # lower case; 371 list several with "+" or ","
    },
    sort=("NUMERO_REGISTRO_PRODUTO", "NUMERO_PROCESSO"),
    timestamp_formats=("%d/%m/%Y",),  # day first (141,658 rows prove it, none the reverse)
    # month and year only, like alimentos' DT_VENCIMENTO_REGISTRO; 23 rows of 4 or 5 digits
    # become NULL
    formats={"DATA_VENCIMENTO_REGISTRO": ("%m%Y", "%m/%Y")},
    unescape=frozenset({"NOME_PRODUTO", "PRINCIPIO_ATIVO"}),
    load_time=None,  # no load-time column; the manifest keeps the file's Last-Modified
    notes=(
        "Uma linha por medicamento registrado na ANVISA, ativo ou inativo, repetida: o "
        "arquivo traz a mesma linha (produto e processo) até dezenas de vezes, e 228.869 "
        "linhas são 42.661 distintas. Conte produtos por numero_registro_produto, ou leia "
        "medicamentos_por_produto, que tem uma linha por registro.",
        "numero_registro_produto tem 9 dígitos e identifica o produto; as apresentações (13 "
        "dígitos, os 9 do produto mais 4) não estão aqui, estão na tabela cmed "
        "(registro_produto liga as duas). Vazio em 21 mil linhas, quase todas de dinamizados "
        "e de produtos de baixo risco notificados.",
        "empresa_detentora_registro junta o CNPJ (14 dígitos) e a razão social com ' - '.",
        "data_vencimento_registro tem precisão de mês; situacao_registro é a situação no dia do "
        "build, e há registros ativos com vencimento passado.",
        "categoria_regulatoria vem como a ANVISA escreve, com grafias duplas (Dinamizado e "
        "DINAMIZADO), um ? no lugar do Á de Fitoterápico em 184 linhas e nomes truncados em "
        "12 caracteres.",
        "A ANVISA atualiza o arquivo mensalmente, com dados até o último dia do mês anterior.",
    ),
)

PRODUTOS_SAUDE = Dataset(
    name="produtos_saude",
    group="produtos_saude",
    file="TA_PRODUTO_SAUDE_SITE.csv",
    directory="",
    title="Produtos para saúde: registros e cadastros",
    # Profiled 2026-10-22 on the 2026-10-13 file (27.7 MB): 116,008 records of 12 fields,
    # Windows-1252 with undefined bytes (0x8F), LF line ends, and NO quoting at all: 105 lines
    # carry a literal `"` inside a value (`Sistema "Holter"`) and no value holds `;` or a line
    # break, hence `quoted=False`. No data dictionary on the server (the two PDFs named
    # PRODUTO_SAUDE are the petition-lifecycle one, mislabeled).
    columns={
        "NUMERO_REGISTRO_CADASTRO": "VARCHAR",  # 11 digits (115,841) or 10 (167); 8... cadastro
        "NUMERO_PROCESSO": "VARCHAR",  # digits only, 17 on 114,437 rows
        "NOME_TECNICO": "VARCHAR",  # 2,816 values; 1,845 end in a space (stripped)
        "CLASSE_RISCO": "VARCHAR",  # I (37,679), II (48,291), III (19,313), IV (10,725)
        "NOME_COMERCIAL": "VARCHAR",
        "CNPJ_DETENTOR_REGISTRO_CADASTRO": "VARCHAR",  # always 14 digits
        "DETENTOR_REGISTRO_CADASTRO": "VARCHAR",
        "NOME_FABRICANTE": "VARCHAR",  # 12,733 empty
        "NOME_PAIS_FABRIC": "VARCHAR",  # empty with the fabricante
        "DT_PUB_REGISTRO_CADASTRO": "DATE",  # dd/mm/yyyy; 22 empty
        # dd/mm/yyyy, or the word VIGENTE on 85,970 rows (the cadastros): kept as text
        "VALIDADE_REGISTRO_CADASTRO": "VARCHAR",
        "DT_ATUALIZACAO_DADO": "TIMESTAMP",  # one value per file, month first; see `formats`
    },
    sort=("CNPJ_DETENTOR_REGISTRO_CADASTRO", "NUMERO_REGISTRO_CADASTRO"),
    timestamp_formats=("%d/%m/%Y",),  # 72,852 rows prove day first on DT_PUB, none the reverse
    formats={"DT_ATUALIZACAO_DADO": ("%m/%d/%Y %H:%M:%S",)},  # 10/12/2026 19:00:00 = 12 Oct
    load_time="DT_ATUALIZACAO_DADO",
    quoted=False,
    notes=(
        "Uma linha por registro ou cadastro de produto para saúde (dispositivo médico) e por "
        "fabricante: 100.681 registros em 116.008 linhas, e 14.348 registros com mais de um "
        "fabricante repetem tudo menos nome_fabricante e nome_pais_fabric. Leia "
        "produtos_saude_por_produto para uma linha por registro.",
        "numero_registro_cadastro tem 10 ou 11 dígitos: os que começam com 8 são cadastros "
        "(notificação, classes I e II), os que começam com 1 são registros.",
        "Não há coluna de situação. validade_registro_cadastro é a palavra VIGENTE nos "
        "cadastros (sem prazo) e uma data nos registros; compare com a data do dia na "
        "consulta.",
        "nome_fabricante e nome_pais_fabric ficam vazios em 12.733 linhas.",
    ),
)

CMED = Dataset(
    name="cmed",
    group="medicamentos",
    kind="cmed",
    file=" + ".join(cmed.FILES.values()),  # the pair, at the root of BASE_URL (cmed.FILES)
    directory="",
    title="CMED: preços máximos de medicamentos por apresentação",
    columns=cmed.COLUMNS,  # 26,635 rows on 2026-10-09; GGREM unique in both files
    source_names=cmed.SOURCE_NAMES,
    sort=("ggrem",),
    load_time="publicado_em",  # the files' Last-Modified, stamped on every row by cmed.py
    notes=(
        "Uma linha por apresentação de medicamento com preço regulado ou liberado pela CMED, "
        "identificada pelo ggrem (15 dígitos); registro é a apresentação na ANVISA (13 "
        "dígitos) e registro_produto os 9 primeiros, o produto.",
        "Preços em centavos de real: pf_* é o preço fábrica, pmc_* o máximo ao consumidor e "
        "pmvg_* o máximo de venda ao governo, cada um por alíquota de ICMS (pf_18 = 18 %; "
        "pf_17_5 = 17,5 %; _alc = Área de Livre Comércio; _0 = isento). Nulo onde a lista traz "
        "'-': pmc_* nos 3.961 produtos de uso restrito a hospitais. A lista marca com * as "
        "alíquotas exibidas mas não aplicáveis (isentos de ICMS; no PMVG também os com CAP): "
        "o * sai do valor e vai para pmc_asterisco e pmvg_asterisco.",
        "Os dois arquivos mensais (PMC e PMVG) são juntados pelo ggrem; publicado_em é a data "
        "dos arquivos, que a CMED publica uma vez por mês e reajusta em março ou abril.",
        "comercializacao diz se a apresentação foi comercializada no ano em "
        "comercializacao_periodo; a CMED troca o ano do cabeçalho todo ano.",
    ),
)

CBPF = Dataset(
    name="cbpf",
    group="cbpf",
    file="TA_CONSULTA_CBPF.CSV",
    directory="CONSULTAS/EMPRESA_FISCALIZACAO_PRODUTO/",
    title="Certificados de boas práticas (CBPF e CBPDA), exceto medicamentos",
    # Profiled 2026-10-23 on the 2026-10-13 file (8,042,875 bytes): Windows-1252 (0x96 67 times,
    # 0x92 8), LF only, 22,039 records of 16 fields, no line break or `;` inside any value. Text
    # quoted, CO_DOCUMENTO_PETICAO bare. 80 records are exact copies of another (71 petições
    # twice, 3 four times; no petição has two different rows). Dictionary:
    # Documentacao_e_Dicionario_de_Dados_Certificado_de_Boas_Praticas.pdf (05/2025) beside it.
    # The medicines' certificates are in TA_CONSULTA_CBPF_MEDICAMENTO_DETALHE.CSV
    # (cbpf_medicamentos); TA_CONSULTA_CBPF_DETALHE.CSV is stale (DT_CARGA_ETL 2025-09-08) and
    # inconsistent with this file, and TA_CONSULTA_CBPF_MEDICAMENTO.CSV has 4 rows: neither is read.
    columns={
        "CO_DOCUMENTO_PETICAO": "INTEGER",  # 7 or 8 digits (max 35,557,109); the certificate
        "RAZAOSOCIAL_SOLICITANTE": "VARCHAR",  # 52 end in whitespace (stripped); 4 carry
        # `COM&Eacute-RCIO`, an entity whose `;` the export turned into `-` (kept as written)
        "NU_CNPJ_SOLICITANTE": "VARCHAR",  # always 14 digits; 3,257 distinct
        "RAZAOSOCIAL_INSPECIONADA": "VARCHAR",  # empty on 15,808 (older rows: plant unknown)
        # [ABC] + 6 digits on 5,189 rows, every one abroad: C = devices, A/B = APIs; never
        # together with a CNPJ
        "CODIGOUNICO_INSPECIONADA": "VARCHAR",
        "NU_CNPJ_EMPRESA_INSPECIONADA": "VARCHAR",  # 14 digits on 1,042 rows, always BRASIL
        "NO_PAIS_INSPECIONADA": "VARCHAR",  # empty with the plant; CHINA, REPÚBLICA POPULAR...
        "NO_TIPO_CERTIFICADO": "VARCHAR",  # CBPF 21,628 / CBPDA 411
        # `<code> - <FAMÍLIA> - <text>`, 81 values; families PRODUTOS PARA SAÚDE 12,456 (40 with a
        # trailing space), MDSAP 4,346 (devices; one `MDSAP -PRODUTOS`), INSUMOS FARMACÊUTICOS
        # 2,669 (9 with an en dash), INSUMOS FARMACÊUTICOS ATIVOS BIOLÓGICOS 2,269, COSMÉTICOS…
        # 235, SANEANTES 62, ALIMENTOS 2. 12 rows carry inner quotes (`"in vitro"`), 45 end
        # in `&#8203-`. No medicines.
        "ASSUNTO": "VARCHAR",
        "DT_PUBLICACAO": "TIMESTAMP",  # always 00:00:00; 13,707 rows prove month first
        "DT_VALIDADE": "TIMESTAMP",  # empty on 789: 377 indeferidos, 7 cancelados, 405 others
        # empty 16,353; ANÁLISE DE RISCO 2,322; INSPEÇÃO 1,720; RECONHECIMENTO DE OUTRAS
        # AUTORIDADES 870; RENOVAÇÃO AUTOMÁTICA 404; NÃO INFORMADO 147; APROVAÇÃO TÁCITA 133;
        # OUTROS 50; ANÁLISE DE REQUISITOS DA RDC 982/2025 22; MDSAP 11; RDC 346/2020 7
        "DS_TP_EMISSAO_CERTIFICADO": "VARCHAR",
        # Publicado deferimento 20,612; Revalidação automática 1,027; Publicado indeferimento
        # 377; Renovação automática 22; Certificado emitido 1
        "SITUACAO": "VARCHAR",
        "NU_PROCESSO": "VARCHAR",  # digits; 17 on 21,967, 15 on 58, 13 on 10, 16 on 3, 7 on 1
        # computed by ANVISA on the load day: VENCIDO 17,273; VALIDO (no accent) 3,348; empty
        # 1,034 (629 of them valid by date); INDEFERIDO 377; CANCELADO 7. `certificados.vigente`
        # recomputes it from SITUACAO, DT_VALIDADE and DT_CARGA_ETL
        "STATUS": "VARCHAR",
        "DT_CARGA_ETL": "TIMESTAMP",  # 10/13/2026 00:00:01 on every row
    },
    sort=("NU_CNPJ_SOLICITANTE", "CO_DOCUMENTO_PETICAO"),  # a petição's copies are adjacent
    timestamp_formats=("%m/%d/%Y %H:%M:%S",),  # month first, like produtos_irregulares
    notes=(
        "Uma linha por petição de Certificado de Boas Práticas de Fabricação (CBPF) ou de "
        "Distribuição e/ou Armazenagem (CBPDA) de produtos para saúde, insumos farmacêuticos, "
        "cosméticos, saneantes e alimentos, deferida ou não; os certificados de medicamentos "
        "estão em cbpf_medicamentos. 80 linhas repetem outra, iguais em tudo: conte "
        "certificados com DISTINCT co_documento_peticao, ou leia certificados, que tem uma "
        "linha por petição.",
        "nu_cnpj_solicitante é quem pediu o certificado (o detentor); a unidade fabril "
        "inspecionada vem em nu_cnpj_empresa_inspecionada (no Brasil) ou "
        "codigounico_inspecionada (no exterior: letra e 6 dígitos), e fica vazia em 72% das "
        "linhas, as mais antigas.",
        "status é a situação calculada pela ANVISA no dia da carga (dt_carga_etl) e vem vazio "
        "em mil linhas, inclusive válidas: para saber se um certificado vigora, compare "
        "dt_validade com a data de referência, como certificados.vigente faz com dt_carga_etl.",
        "assunto começa com o código e a família da petição (PRODUTOS PARA SAÚDE, MDSAP, "
        "INSUMOS FARMACÊUTICOS...), que certificados.area resume. Entidades HTML cujo ; virou "
        "- na origem (COM&Eacute-RCIO, &#8203-) vêm como estão.",
    ),
)

CBPF_MEDICAMENTOS = Dataset(
    name="cbpf_medicamentos",
    group="cbpf",
    file="TA_CONSULTA_CBPF_MEDICAMENTO_DETALHE.CSV",
    directory="CONSULTAS/EMPRESA_FISCALIZACAO_PRODUTO/",
    title="Certificados de boas práticas de medicamentos, uma linha por linha certificada",
    # Profiled 2026-10-23 on the file of Last-Modified 2026-05-15 (9,844,514 bytes):
    # Windows-1252 (0x96 4,662 times, 0x92 46; no undefined byte), LF only, 14,803 records of
    # 32 fields, no line break or `;` inside any value. Text quoted, CO_DOCUMENTO_PETICAO bare.
    # 13,651 petições, one row per certified line (1 line on 12,737, up to 6); within a
    # petição only the line columns (LINHACERTIFICACAO, LINHACBPF, CLASSECERTIFICACAO,
    # ETAPASFABRICACAO, FORMAFARMACEUTICA, ST_LIBERACAO_PARAMETRICA) vary. 25 rows are exact
    # copies of another; 10 more petições repeat a LINHACERTIFICACAO with other line columns
    # different. DT_CARGA_ETL is 09/08/2025 15:28:51 or :52 on every row: ANVISA stopped
    # refreshing this file but republishes it (the main TA_CONSULTA_CBPF_MEDICAMENTO.CSV has 4
    # rows); certificados.dados_de carries that date. No id in common with cbpf.
    columns={
        "CO_DOCUMENTO_PETICAO": "INTEGER",  # 7 or 8 digits (2,172,612 to 33,450,767)
        "EMPRESASOLICITANTE": "VARCHAR",  # 8 end in whitespace (stripped)
        "NU_CNPJ_SOLICITANTE": "VARCHAR",  # always 14 digits; 1,259 distinct, 622 also in cbpf
        "ENDERECOSOLICITANTE": "VARCHAR",
        "CIDADESOLICITANTE": "VARCHAR",
        "UFSOLICITANTE": "VARCHAR",  # two letters, never empty
        # the inspected plant (the PDF describes it as the solicitante, a copy-paste error);
        # empty on 747 rows (older petições: plant unknown); one plant abroad (LEMERY, MÉXICO)
        # has a name and country but neither code nor CNPJ
        "RAZAOSOCIAL": "VARCHAR",
        "CODIGOUNICO_INSPECIONADA": "VARCHAR",  # A + 6 digits on 9,650 rows, all abroad
        "NU_CNPJ_EMPRESA_INSPECIONADA": "VARCHAR",  # 14 digits on 4,405 rows, always BRASIL
        "ENDERECO": "VARCHAR",  # `&B,` and the like are text, not entities
        "PAIS": "VARCHAR",  # BRASIL 4,405, ALEMANHA 1,460, ÍNDIA 1,297...; 65 values
        "CO_UF_INSPECIONADA": "VARCHAR",  # two letters on every Brazilian plant
        "CIDADE_INSPECIONADA": "VARCHAR",
        "NO_TIPO_CERTIFICADO": "VARCHAR",  # CBPF 13,735 / CBPDA 1,068 rows
        # `<code> - <FAMÍLIA> - <text>`, 54 values: MEDICAMENTOS 14,528 rows (4,248 with an en
        # dash after it, the international renewals), PRODUTOS BIOLÓGICOS 154, MEDICAMENTO E
        # INSUMOS FARMACÊUTICOS 100, MEDICAMENTO 21 (cancellations); no inner quote, no entity
        "ASSUNTO": "VARCHAR",
        "DT_PUBLICACAO": "TIMESTAMP",  # 2012-01-02 to 2025-06-02; 8,869 rows prove month first
        "DT_VALIDADE": "TIMESTAMP",  # empty on exactly the 85 CANCELADO and 40 INDEFERIDO rows
        "NU_RESOLUCAO": "VARCHAR",  # 1.234 (8,878), 630, 1234, 1,234...: as written
        "DT_RESOLUCAO": "TIMESTAMP",  # month first too (8,966 rows prove it)
        "NU_DOU": "VARCHAR",  # 1 to 3 digits; `-` 10, `99-A` 9
        # the line as one sentence, empty on 2,111 rows (every line column empty with it:
        # petições of 2012-2017); `Medicamentos` on the CBPDA rows
        "LINHACERTIFICACAO": "VARCHAR",
        "LINHACBPF": "VARCHAR",  # Sólidos não estéreis 4,867, Produtos estéreis 4,654... 7 values
        "CLASSECERTIFICACAO": "VARCHAR",  # Comum 10,244, Citotóxicos, Hormônios... 10 values
        "ETAPASFABRICACAO": "VARCHAR",  # comma-joined: Granel, Embalagem primária, secundária
        # 448 values, comma-joined; 4,949 end in a space (stripped), 273 hold a \xa0 inside
        "FORMAFARMACEUTICA": "VARCHAR",
        "ST_LIBERACAO_PARAMETRICA": "BOOLEAN",  # N 11,606 / S 28 / empty 3,169
        # not the petition's type: Medicamentos sintéticos 6,172, biológicos 1,606,
        # Radiofármacos 53, Não se aplica 1,609, empty 5,363
        "TIPOPETICAO": "VARCHAR",
        # empty 8,419; INSPEÇÃO 2,694; ANÁLISE DE RISCO 2,061; APROVAÇÃO TÁCITA 1,176; RDC
        # 346/2020 248; RECONHECIMENTO DE OUTRAS AUTORIDADES 99; RENOVAÇÃO AUTOMÁTICA 87...
        "DS_TP_EMISSAO_CERTIFICADO": "VARCHAR",
        # filled on 182 rows (CANCELADO, INDEFERIDO and some VENCIDO): 749 `¿` stand for the
        # characters ANVISA lost upstream (n¿o, Pr¿ticas), 12 hold inner quotes; kept
        "DS_MOTIVO_CANCELAMENTO": "VARCHAR",
        "NU_PROCESSO": "VARCHAR",  # digits; 17 on 14,800, 16 on 3
        # on 2025-09-08: VENCIDO 11,662; VALIDO 2,547; empty 469 (CBPDA, 435 of them valid by
        # date); CANCELADO 85; INDEFERIDO 40. 302 VALIDO petições have a validade before the
        # load day (renewal pending?): certificados.vigente is false on them
        "STATUS": "VARCHAR",
        "DT_CARGA_ETL": "TIMESTAMP",
    },
    sort=("NU_CNPJ_SOLICITANTE", "CO_DOCUMENTO_PETICAO", "LINHACERTIFICACAO"),
    timestamp_formats=("%m/%d/%Y %H:%M:%S",),  # month first in all four date columns
    notes=(
        "Uma linha por linha certificada (linhacertificacao) de cada petição de certificado "
        "de boas práticas de medicamentos: 14.803 linhas, 13.651 petições, 25 linhas "
        "repetidas iguais em tudo. Conte certificados com DISTINCT co_documento_peticao, ou "
        "leia certificados, que tem uma linha por petição.",
        "Dados de 08/09/2025: a ANVISA deixou de atualizar este arquivo (dt_carga_etl é a data "
        "da carga, igual em todas as linhas), embora o republique. Certificados publicados "
        "depois não estão aqui, e status foi calculado naquele dia.",
        "Mesmo desenho de cbpf para solicitante e unidade inspecionada (CNPJ no Brasil, "
        "codigounico_inspecionada no exterior; razaosocial é a unidade inspecionada), mais "
        "endereço, cidade e UF dos dois, a resolução e o DOU da publicação, e o motivo do "
        "cancelamento ou indeferimento quando há, com acentos trocados por ¿ na origem.",
        "tipopeticao é o tipo de medicamento (sintéticos, biológicos, radiofármacos), não o "
        "tipo da petição; a situação da petição só aparece em status.",
    ),
)

CATALOG: tuple[Dataset, ...] = (
    ALIMENTOS,
    ALIMENTOS_RESULTADO,
    SANEANTES,
    PETICOES_ALIMENTO,
    PETICOES_ALIMENTO_ANDAMENTO,
    PRODUTOS_IRREGULARES,
    COSMETICOS,
    MEDICAMENTOS,
    PRODUTOS_SAUDE,
    CMED,
    CBPF,
    CBPF_MEDICAMENTOS,
)


def select(names: Iterable[str], catalog: tuple[Dataset, ...] = CATALOG) -> tuple[Dataset, ...]:
    """The datasets matching any of `names` (a dataset name or a group), in catalog order."""
    wanted = set(names)
    unknown = wanted - {d.name for d in catalog} - {d.group for d in catalog}
    if unknown:
        valid = sorted({d.name for d in catalog} | {d.group for d in catalog})
        raise DadosError(f"unknown dataset {sorted(unknown)}; choose from {valid}")
    return tuple(d for d in catalog if d.name in wanted or d.group in wanted)
