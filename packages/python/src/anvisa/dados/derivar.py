"""Derived tables: Parquet built from the converted tables, not from an ANVISA file.

The site used to derive these itself, in SQL run by every visitor and again at its build. Here
they are built once per build, from the tables of that same build (fresh or carried over), so
the three places that need "one row per product" or "one row per company" agree by construction.

Rules for a `Derivada`:

- `sql` gets a map fonte name -> `read_parquet('…')` expression and returns a SELECT; `write`
  adds the ORDER BY and the COPY. Every aggregate must be order-stable (`min`, `arg_min`,
  `list(x ORDER BY x)`, `first(x ORDER BY …)`): DuckDB does not promise scan order even with
  one thread, and the build's Parquet must be byte-identical for identical inputs.
- No column depends on the day of the build (no "vencida" flag): the site computes that when
  the page is viewed. No search-normalized text: that is the site's concern.
- Output column names are the row's own: `cnpj`, `nome`, not ANVISA's prefixed headers.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .convert import ROW_GROUP_SIZE, _quote, connect, copy_parquet
from .descricoes import DESCRICOES


@dataclass(frozen=True)
class Derivada:
    """One derived Parquet file named `name`, rebuilt on every build from `fontes`."""

    name: str
    group: str
    title: str
    fontes: tuple[str, ...]  # catalog table names it reads, in the order worth listing
    sort: tuple[str, ...]  # output column names; the Parquet row order
    notes: tuple[str, ...]  # for the reader, Portuguese, like Dataset.notes
    sql: Callable[[dict[str, str]], str]  # fonte -> read_parquet() expression -> a SELECT

    @property
    def descriptions(self) -> dict[str, str]:
        """Output column -> Portuguese description (descricoes.py)."""
        return DESCRICOES[self.name]


def _alergia(resultado: str) -> str:
    # What the labels declare, per product, from alimentos_resultado (one row per apresentação):
    # `intolerancias` is "Contém Glúten - Sim | Contém Lactose - Não"; `alergenicos` is groups
    # "Rótulo - item#item" joined by " | ", the rótulos being Não contém, Pode conter, Contém
    # derivado de and Contém (profiled 2026-10-12: the only four; 11,244 of 50,522 products
    # declare anything). Apresentações of one product can disagree (227 products): the lists
    # join every apresentação, an item some apresentação contains leaves "pode conter" and
    # "não contém", and alergia_varia says so. Same rules as the site's resumirAlergia.
    rotulo = "lower(strip_accents(rotulo))"
    sinal = "CASE WHEN {sim} AND {nao} THEN 'Varia' WHEN {sim} THEN 'Sim' WHEN {nao} THEN 'Não' END"
    flag = "bool_or(lower(strip_accents(intolerancias)) LIKE '%{termo} - {valor}%')"
    return f"""
        WITH grupos AS (
          SELECT co_produto, trim(g) AS g
          FROM (SELECT co_produto, unnest(string_split(alergenicos, '|')) AS g
                FROM {resultado} WHERE alergenicos <> '')
          WHERE trim(g) <> ''),
        itens AS (
          SELECT co_produto, trim(item) AS item,
            CASE WHEN strpos(g, ' - ') > 0 THEN trim(g[1:strpos(g, ' - ') - 1]) ELSE '' END
              AS rotulo
          FROM (SELECT co_produto, g,
                  unnest(string_split(CASE WHEN strpos(g, ' - ') > 0 THEN g[strpos(g, ' - ') + 3:]
                                           ELSE g END, '#')) AS item
                FROM grupos)
          WHERE trim(item) <> ''),
        listas AS (
          SELECT co_produto,
            coalesce(list_sort(list(DISTINCT item)
              FILTER (WHERE {rotulo} NOT IN ('nao contem', 'pode conter'))), []) AS contem,
            coalesce(list_sort(list(DISTINCT item) FILTER (WHERE {rotulo} = 'pode conter')), [])
              AS pode,
            coalesce(list_sort(list(DISTINCT item) FILTER (WHERE {rotulo} = 'nao contem')), [])
              AS nao
          FROM itens GROUP BY co_produto),
        sinais AS (
          SELECT co_produto,
            {flag.format(termo="gluten", valor="sim")} AS g_sim,
            {flag.format(termo="gluten", valor="nao")} AS g_nao,
            {flag.format(termo="lactose", valor="sim")} AS l_sim,
            {flag.format(termo="lactose", valor="nao")} AS l_nao,
            count(DISTINCT nullif(alergenicos, '')) > 1 OR count(DISTINCT intolerancias) > 1
              AS varia,
            count(nullif(alergenicos, '')) + count(intolerancias) > 0 AS declara
          FROM {resultado} GROUP BY co_produto)
        SELECT co_produto,
          {sinal.format(sim="g_sim", nao="g_nao")} AS gluten,
          {sinal.format(sim="l_sim", nao="l_nao")} AS lactose,
          CASE WHEN declara THEN coalesce(contem, []) END AS contem,
          CASE WHEN declara THEN
            list_filter(coalesce(pode, []), x -> NOT list_contains(coalesce(contem, []), x)) END
            AS pode_conter,
          CASE WHEN declara THEN
            list_filter(coalesce(nao, []), x -> NOT list_contains(coalesce(contem, []), x)
                                               AND NOT list_contains(coalesce(pode, []), x)) END
            AS nao_contem,
          CASE WHEN declara THEN varia END AS alergia_varia
        FROM sinais LEFT JOIN listas USING (co_produto)"""


def _saneantes_unicos(saneantes: str) -> str:
    """saneantes with one row per nu_expediente: of a Registrado/Notificado pair, the registered
    one; the remaining ORDER BY terms only make a future tie deterministic."""
    return f"""
        SELECT * FROM {saneantes}
        QUALIFY row_number() OVER (PARTITION BY nu_expediente
          ORDER BY is_registrado DESC, dt_vencimento_produto DESC NULLS LAST,
                   nu_registro_produto NULLS LAST, no_produto, nu_processo) = 1"""


def _alimentos(t: dict[str, str], extras: dict[str, str] | None = None) -> str:
    """alimentos as one row per product (co_seq_produto) with the produtos columns, plus the
    `extras` (output name -> aggregate over the product's apresentações) after them. Name,
    brands, company, processo and situação are constant within a product (README,
    "Products"), so min() reads the one value; desde is the earliest regularization and
    vencimento the latest expiry (141 products renew apresentações at different times; the
    site asked for the latest, 2026-10-12). The label's
    allergen declaration joins from alimentos_resultado, per product (_alergia)."""
    names = "".join(f", {name}" for name in extras or ())
    more = "".join(f",\n          {expr} AS {name}" for name, expr in (extras or {}).items())
    return f"""
        SELECT base.* EXCLUDE (co{names}),
          gluten, lactose, contem, pode_conter, nao_contem, alergia_varia{names}
        FROM (
        SELECT 'alimentos' AS area,
          co_seq_produto AS co,
          CAST(co_seq_produto AS VARCHAR) AS id,
          min(no_produto) AS nome,
          min(marcas) AS marcas,
          min(no_razao_social_empresa) AS empresa,
          min(nu_cnpj_empresa) AS cnpj,
          min(nu_processo) AS processo,
          min(nu_registro_notificacao_produto) AS registro,
          bool_or(situacao_registro = 'Ativo') AS ativo,
          min(tipo_regularizacao) AS tipo,
          min(ds_categoria_produto) AS categoria,
          CAST(min(dt_regularizacao) AS DATE) AS desde,
          max(dt_vencimento_registro) AS vencimento,
          CAST(count(*) AS INTEGER) AS apresentacoes,
          coalesce(list(nu_registro ORDER BY nu_registro) FILTER (WHERE nu_registro IS NOT NULL),
            []) AS registros{more}
        FROM {t["alimentos"]}
        GROUP BY co_seq_produto) base
        LEFT JOIN ({_alergia(t["alimentos_resultado"])}) alergia ON alergia.co_produto = base.co"""


def _medicamentos_unicos(t: dict[str, str]) -> str:
    """medicamentos as one row per product. The source repeats product × processo rows (228,869
    rows, 42,661 distinct on 2026-10-13) and lists a registro under Ativo and Inativo rows at
    once (2,429 of 32,800): name, company and CNPJ are constant within a registro, so min()
    reads the one value and `ativo` is true when any row is. The 21,495 rows without a
    registro (3,627 distinct products: dinamizados and notified low-risk products, 21,390 of
    them without a processo either) get a stable id from what they have."""
    cnpj = "regexp_extract(empresa_detentora_registro, '^([0-9]{14})', 1)"
    empresa = "regexp_replace(empresa_detentora_registro, '^[0-9]{14} - ', '')"
    return f"""
        SELECT id,
          min(numero_registro_produto) AS registro_produto,
          min(numero_processo) AS processo,
          min(nome_produto) AS nome,
          min(cnpj) AS cnpj,
          min(empresa) AS empresa,
          bool_or(situacao_registro = 'Ativo') AS ativo,
          min(categoria_regulatoria) AS categoria_regulatoria,
          min(classe_terapeutica) AS classe_terapeutica,
          min(principio_ativo) AS principio_ativo,
          min(data_finalizacao_processo) AS desde,
          max(data_vencimento_registro) AS vencimento,
          CAST(count(DISTINCT numero_processo) AS INTEGER) AS processos,
          CAST(count(*) AS INTEGER) AS linhas
        FROM (
          SELECT *,
            coalesce(numero_registro_produto, 'sem-registro-' || substr(md5(
              cnpj || '|' || nome_produto || '|' || coalesce(numero_processo, '')), 1, 12)) AS id
          FROM (SELECT *, {cnpj} AS cnpj, {empresa} AS empresa FROM {t["medicamentos"]}))
        GROUP BY id"""


def _produtos_saude_unicos(t: dict[str, str]) -> str:
    """produtos_saude as one row per registro: the source has one row per registro ×
    fabricante (116,008 rows, 100,681 registros), everything but the fabricante equal. The
    registration is in force when the source says VIGENTE (cadastros, no term) or its date is
    on or after the file's own load date (dt_atualizacao_dado): nothing here depends on the
    day of the build."""
    validade = "try_strptime(validade_registro_cadastro, '%d/%m/%Y')"
    fabricante = "nome_fabricante || coalesce(' (' || nome_pais_fabric || ')', '')"
    return f"""
        SELECT numero_registro_cadastro AS id,
          numero_registro_cadastro AS registro,
          min(numero_processo) AS processo,
          min(nome_comercial) AS nome,
          min(nome_tecnico) AS nome_tecnico,
          min(classe_risco) AS classe_risco,
          min(cnpj_detentor_registro_cadastro) AS cnpj,
          min(detentor_registro_cadastro) AS empresa,
          CASE WHEN substr(numero_registro_cadastro, 1, 1) = '8' THEN 'Cadastrado'
               ELSE 'Registrado' END AS tipo,
          bool_or(validade_registro_cadastro = 'VIGENTE'
                  OR {validade} >= dt_atualizacao_dado) AS ativo,
          min(dt_pub_registro_cadastro) AS publicacao,
          CAST(max({validade}) AS DATE) AS vencimento,
          list_sort(list_distinct(list({fabricante}) FILTER (WHERE nome_fabricante IS NOT NULL)))
            AS fabricantes,
          CAST(count(*) AS INTEGER) AS linhas
        FROM {t["produtos_saude"]}
        GROUP BY numero_registro_cadastro"""


def _produtos(t: dict[str, str]) -> str:
    alimentos = _alimentos(t)
    # saneantes: one row per nu_expediente, no detail file. Six expedientes come twice, as a
    # Registrado and a Notificado row equal in everything else (found by the site, 2026-10-11):
    # the registered row stays, the registro being what exists.
    saneantes = f"""
        SELECT 'saneantes' AS area,
          nu_expediente AS id,
          no_produto AS nome,
          CAST(NULL AS VARCHAR) AS marcas,
          no_razao_social_empresa AS empresa,
          nu_cnpj_empresa AS cnpj,
          nu_processo AS processo,
          nu_registro_produto AS registro,
          st_produto_ativo AS ativo,
          CASE WHEN is_registrado THEN 'Registrado' ELSE 'Notificado' END AS tipo,
          CAST(NULL AS VARCHAR) AS categoria,
          CAST(NULL AS DATE) AS desde,
          CAST(dt_vencimento_produto AS DATE) AS vencimento,
          1 AS apresentacoes,
          CAST([] AS VARCHAR[]) AS registros,
          CAST(NULL AS VARCHAR) AS gluten,
          CAST(NULL AS VARCHAR) AS lactose,
          CAST(NULL AS VARCHAR[]) AS contem,
          CAST(NULL AS VARCHAR[]) AS pode_conter,
          CAST(NULL AS VARCHAR[]) AS nao_contem,
          CAST(NULL AS BOOLEAN) AS alergia_varia
        FROM ({_saneantes_unicos(t["saneantes"])})"""
    nulos = """
          CAST(NULL AS VARCHAR) AS marcas,
          CAST([] AS VARCHAR[]) AS registros,
          CAST(NULL AS VARCHAR) AS gluten,
          CAST(NULL AS VARCHAR) AS lactose,
          CAST(NULL AS VARCHAR[]) AS contem,
          CAST(NULL AS VARCHAR[]) AS pode_conter,
          CAST(NULL AS VARCHAR[]) AS nao_contem,
          CAST(NULL AS BOOLEAN) AS alergia_varia"""
    # medicamentos: tipo is the regulatory category (Genérico, Similar, Novo...), categoria the
    # therapeutic class; the registro is the 9-digit product
    medicamentos = f"""
        SELECT 'medicamentos' AS area, id, nome, empresa, cnpj, processo,
          registro_produto AS registro, ativo,
          categoria_regulatoria AS tipo, classe_terapeutica AS categoria,
          desde, vencimento, 1 AS apresentacoes,{nulos}
        FROM ({_medicamentos_unicos(t)})"""
    produtos_saude = f"""
        SELECT 'produtos_saude' AS area, id, nome, empresa, cnpj, processo, registro, ativo,
          tipo, 'Classe ' || classe_risco AS categoria,
          publicacao AS desde, vencimento, 1 AS apresentacoes,{nulos}
        FROM ({_produtos_saude_unicos(t)})"""
    return "\n        UNION ALL BY NAME\n".join(
        [alimentos, saneantes, medicamentos, produtos_saude]
    )


PRODUTOS = Derivada(
    name="produtos",
    group="produtos",
    title="Produtos: uma linha por alimento, saneante, medicamento ou produto para saúde",
    fontes=("alimentos", "alimentos_resultado", "saneantes", "medicamentos", "produtos_saude"),
    sort=("cnpj", "area", "id"),
    notes=(
        "Uma linha por produto, com as mesmas colunas para alimentos (agrupados por "
        "co_seq_produto: um produto, várias apresentações), saneantes (uma linha por "
        "nu_expediente; dos seis expedientes que a origem lista como Registrado e Notificado, "
        "fica a linha registrada), medicamentos (uma linha por registro de 9 dígitos, ou por "
        "nome e empresa quando a origem não dá registro: id começa com sem-registro-) e "
        "produtos para saúde (uma linha por registro ou cadastro). Derivada a cada build.",
        "Nos medicamentos, tipo é a categoria regulatória (Genérico, Similar, Novo, "
        "Biológico...) e categoria a classe terapêutica; nos produtos para saúde, tipo é "
        "Registrado ou Cadastrado e categoria a classe de risco (Classe I a IV). Mais colunas "
        "dessas áreas em medicamentos_por_produto e produtos_saude_por_produto.",
        "gluten, lactose, contem, pode_conter, nao_contem e alergia_varia resumem o que os "
        "rótulos das apresentações do alimento declaram (alimentos_resultado); nulos quando "
        "nenhuma apresentação declara nada (a maioria) e nos saneantes. contem junta "
        '"Contém" e "Contém derivado de"; um item que alguma apresentação contém sai de '
        "pode_conter e de nao_contem; alergia_varia marca apresentações que divergem.",
        "Os cosméticos não estão aqui (1,2 milhão de linhas): use os arquivos de busca da "
        "tabela cosmeticos.",
        "vencimento nas notificações de alimentos é 2029-12-01, um valor de preenchimento da "
        "ANVISA; desde (primeira regularização) e categoria só existem para alimentos.",
        "ativo é a situação na ANVISA no dia do build; se a data de vencimento já passou, "
        "decida na consulta, com a data do dia. Nos produtos para saúde, que não têm situação "
        "na origem, ativo é VIGENTE (cadastros) ou validade não vencida na data do arquivo.",
    ),
    sql=_produtos,
)


def _empresas(t: dict[str, str]) -> str:
    # One row per 14-digit CNPJ seen as the holder of a product or as the target of a measure.
    # Counts per area are products, not rows: alimentos by co_seq_produto, cosméticos by
    # nu_processo (the unregistered row of a duplicate pair decides `ativo`, as the site does;
    # a processo listed once is its own only row). The name is the commonest spelling across
    # the product tables, ties alphabetical; a company known only from measures takes the
    # name written there.
    cnpj14 = "regexp_full_match(nu_cnpj_empresa_investigada, '[0-9]{14}')"
    return f"""
        WITH al AS (
          SELECT cnpj, CAST(count(*) AS INTEGER) AS alimentos,
            CAST(count(*) FILTER (WHERE ativo) AS INTEGER) AS alimentos_ativos
          FROM (SELECT min(nu_cnpj_empresa) AS cnpj, bool_or(situacao_registro = 'Ativo') AS ativo
                FROM {t["alimentos"]} GROUP BY co_seq_produto)
          GROUP BY cnpj),
        sa AS (
          SELECT nu_cnpj_empresa AS cnpj, CAST(count(*) AS INTEGER) AS saneantes,
            CAST(count(*) FILTER (WHERE st_produto_ativo) AS INTEGER) AS saneantes_ativos
          FROM ({_saneantes_unicos(t["saneantes"])}) GROUP BY 1),
        co AS (
          SELECT cnpj, CAST(count(*) AS INTEGER) AS cosmeticos,
            CAST(count(*) FILTER (WHERE ativo) AS INTEGER) AS cosmeticos_ativos
          FROM (SELECT nu_cnpj_empresa AS cnpj, nu_processo,
                  arg_min(st_situacao_produto, CAST(st_registrado AS INTEGER)) AS ativo
                FROM {t["cosmeticos"]} GROUP BY 1, 2)
          GROUP BY cnpj),
        md AS (
          SELECT cnpj, CAST(count(*) AS INTEGER) AS medicamentos,
            CAST(count(*) FILTER (WHERE ativo) AS INTEGER) AS medicamentos_ativos
          FROM ({_medicamentos_unicos(t)}) GROUP BY cnpj),
        ps AS (
          SELECT cnpj, CAST(count(*) AS INTEGER) AS produtos_saude,
            CAST(count(*) FILTER (WHERE ativo) AS INTEGER) AS produtos_saude_ativos
          FROM ({_produtos_saude_unicos(t)}) GROUP BY cnpj),
        me AS (
          SELECT nu_cnpj_empresa_investigada AS cnpj,
            CAST(count(DISTINCT co_seq_dossie_investig_med) AS INTEGER) AS medidas,
            CAST(max(dt_publicacao) AS DATE) AS medida_ultima,
            min(nullif(trim(no_empresa_investigada), '')) AS nome
          FROM {t["produtos_irregulares"]}
          WHERE {cnpj14} AND nu_cnpj_empresa_investigada <> '00000000000000'
          GROUP BY 1),
        nomes AS (
          SELECT cnpj, first(nome ORDER BY n DESC, nome) AS nome
          FROM (SELECT cnpj, nome, count(*) AS n FROM (
                  SELECT nu_cnpj_empresa AS cnpj, no_razao_social_empresa AS nome
                  FROM {t["alimentos"]}
                  UNION ALL SELECT nu_cnpj_empresa, no_razao_social_empresa FROM {t["saneantes"]}
                  UNION ALL SELECT nu_cnpj_empresa, no_razao_social_empresa FROM {t["cosmeticos"]}
                  UNION ALL SELECT cnpj, empresa FROM ({_medicamentos_unicos(t)})
                  UNION ALL SELECT cnpj_detentor_registro_cadastro, detentor_registro_cadastro
                  FROM {t["produtos_saude"]})
                WHERE nome IS NOT NULL GROUP BY 1, 2)
          GROUP BY cnpj),
        chaves AS (
          SELECT cnpj FROM al UNION SELECT cnpj FROM sa UNION SELECT cnpj FROM co
          UNION SELECT cnpj FROM md UNION SELECT cnpj FROM ps UNION SELECT cnpj FROM me)
        SELECT k.cnpj,
          coalesce(nomes.nome, me.nome) AS nome,
          coalesce(al.alimentos, 0) AS alimentos,
          coalesce(al.alimentos_ativos, 0) AS alimentos_ativos,
          coalesce(sa.saneantes, 0) AS saneantes,
          coalesce(sa.saneantes_ativos, 0) AS saneantes_ativos,
          coalesce(co.cosmeticos, 0) AS cosmeticos,
          coalesce(co.cosmeticos_ativos, 0) AS cosmeticos_ativos,
          coalesce(md.medicamentos, 0) AS medicamentos,
          coalesce(md.medicamentos_ativos, 0) AS medicamentos_ativos,
          coalesce(ps.produtos_saude, 0) AS produtos_saude,
          coalesce(ps.produtos_saude_ativos, 0) AS produtos_saude_ativos,
          coalesce(al.alimentos, 0) + coalesce(sa.saneantes, 0) + coalesce(co.cosmeticos, 0)
            + coalesce(md.medicamentos, 0) + coalesce(ps.produtos_saude, 0) AS produtos,
          coalesce(al.alimentos_ativos, 0) + coalesce(sa.saneantes_ativos, 0)
            + coalesce(co.cosmeticos_ativos, 0) + coalesce(md.medicamentos_ativos, 0)
            + coalesce(ps.produtos_saude_ativos, 0) AS ativos,
          coalesce(me.medidas, 0) AS medidas,
          me.medida_ultima
        FROM chaves k
        LEFT JOIN al USING (cnpj) LEFT JOIN sa USING (cnpj) LEFT JOIN co USING (cnpj)
        LEFT JOIN md USING (cnpj) LEFT JOIN ps USING (cnpj)
        LEFT JOIN me USING (cnpj) LEFT JOIN nomes USING (cnpj)"""


EMPRESAS = Derivada(
    name="empresas",
    group="empresas",
    title="Empresas: uma linha por CNPJ, com produtos por área e medidas de fiscalização",
    fontes=(
        "alimentos",
        "saneantes",
        "cosmeticos",
        "medicamentos",
        "produtos_saude",
        "produtos_irregulares",
    ),
    sort=("cnpj",),
    notes=(
        "Uma linha por CNPJ (14 dígitos) que detém um alimento, saneante, cosmético, "
        "medicamento ou produto para saúde, ou que foi alvo de uma medida de fiscalização. "
        "Derivada a cada build.",
        "Os totais contam produtos, não linhas: alimentos por co_seq_produto, saneantes por "
        "nu_expediente, cosméticos por nu_processo (um processo listado duas vezes conta uma, "
        "e a linha sem registro decide se está ativo), medicamentos por registro de 9 dígitos "
        "(ou por nome quando não há registro) e produtos para saúde por registro ou cadastro.",
        "nome é a grafia mais frequente da razão social nas tabelas de produtos; empresas "
        "conhecidas só por medidas levam o nome escrito na medida.",
        "medidas conta dossiês de fiscalização contra a empresa (nu_cnpj_empresa_investigada) "
        "em todas as áreas da ANVISA, não só nas três com tabela de produtos aqui: 457 empresas "
        "existem só por uma medida em medicamentos, dispositivos ou outra área. Não conta os "
        "dossiês que ela abriu. Alvos sem CNPJ de 14 dígitos (CPF, texto, CNPJ com 13 dígitos) "
        "ficam de fora; o CNPJ da própria ANVISA (03112386000111) aparece como alvo em alguns "
        "dossiês e vem como está.",
    ),
    sql=_empresas,
)


def _medidas(t: dict[str, str]) -> str:
    # One row per dossiê × produto; the source has one per dossiê × ação × atividade × produto
    # (about 6 per pair). The per-dossiê columns are constant within a dossiê (profiled
    # 2026-10-12: one distinct value each), so min() reads the one value; ações, atividades and
    # registros become sorted lists. The target's CNPJ is kept only when it is one (14 digits,
    # not the 00000000000000 filler); a person (CPF) is flagged and neither number nor name is
    # published, as the site already does.
    alvo = "nu_cnpj_empresa_investigada"
    cnpj14 = f"regexp_full_match({alvo}, '[0-9]{{14}}') AND {alvo} <> '00000000000000'"
    pessoa = f"coalesce(bool_or(regexp_full_match({alvo}, '[0-9]{{11}}')), false)"
    lista = "coalesce(list_sort(list(DISTINCT {x}) FILTER (WHERE {x} IS NOT NULL)), [])"
    return f"""
        SELECT co_seq_dossie_investig_med AS dossie,
          trim(produto) AS produto,
          min(ds_tipo_produto) AS area,
          co_tipo_produto AS area_codigo,
          min(ds_risco_produto) AS risco,
          min(nu_processo) AS processo,
          min({alvo}) FILTER (WHERE {cnpj14}) AS cnpj,
          CASE WHEN {pessoa} THEN NULL ELSE min(nullif(trim(no_empresa_investigada), '')) END
            AS empresa,
          {pessoa} AS pessoa,
          {lista.format(x="nullif(regexp_replace(registro, '[^0-9]', '', 'g'), '')")} AS registros,
          {lista.format(x="ds_acao_fiscalizacao")} AS acoes,
          {lista.format(x="ds_atividade_fiscalizacao")} AS atividades,
          CAST(min(dt_publicacao) AS DATE) AS primeira,
          CAST(max(dt_publicacao) AS DATE) AS ultima,
          min(nu_cnpj) AS autor_cnpj,
          min(no_razao_social) AS autor
        FROM {t["produtos_irregulares"]}
        WHERE coalesce(trim(produto), '') <> ''
        GROUP BY co_tipo_produto, co_seq_dossie_investig_med, trim(produto)"""


MEDIDAS = Derivada(
    name="medidas",
    group="produtos_irregulares",
    title="Medidas de fiscalização: uma linha por dossiê e produto",
    fontes=("produtos_irregulares",),
    sort=("area_codigo", "dossie", "produto"),
    notes=(
        "Uma linha por dossiê × produto de produtos_irregulares, que tem uma por dossiê × ação "
        "× atividade × produto: as ações e atividades viram listas. Todas as áreas; filtre por "
        "area_codigo (6 é alimento). Derivada a cada build.",
        "cnpj é a empresa fiscalizada só quando a origem traz um CNPJ de 14 dígitos; CPF, texto "
        '("DESCONHECIDO", "Não se aplica") e o preenchimento 00000000000000 viram nulo. Quando '
        "o alvo é uma pessoa (CPF), pessoa é verdadeiro e empresa também é nulo.",
        "processo é o da medida, não o de um produto; registros são os números de registro "
        "citados (vazio nos alimentos, que a origem não traz).",
        "Linhas com produto vazio na origem (49 em 80 mil) ficam de fora.",
    ),
    sql=_medidas,
)

COSMETICOS_POR_EMPRESA = 100


def _cosmeticos_por_empresa(t: dict[str, str]) -> str:
    # The first COSMETICOS_POR_EMPRESA cosmetics of each CNPJ, active first, then by name, then by
    # processo: what a company page lists, without the 24 MB table. One row per processo, the
    # unregistered row of a duplicate pair (the one carrying ds_tipo_peticao and, where only
    # one has it, the later expiry), as in empresas and the site's sqlCos. The tipo labels are
    # the site's; st_registrado is always false on the kept row, so ds_tipo_peticao decides.
    return f"""
        SELECT nu_cnpj_empresa AS cnpj,
          CAST(row_number() OVER (PARTITION BY nu_cnpj_empresa
            ORDER BY st_situacao_produto DESC, no_produto, nu_processo) AS INTEGER) AS ordem,
          nu_processo AS id,
          no_produto AS nome,
          no_razao_social_empresa AS empresa,
          nu_processo AS processo,
          nu_registro AS registro,
          st_situacao_produto AS ativo,
          CASE WHEN st_registrado OR ds_tipo_peticao = 'REGISTRO' THEN 'Registrado'
               WHEN ds_tipo_peticao = 'Notificado' THEN 'Notificado'
               WHEN ds_tipo_peticao = 'ISENTO DE REGISTRO' THEN 'Isento de registro'
               WHEN ds_tipo_peticao = 'DESCARTAVEL' THEN 'Descartável'
               ELSE ds_tipo_peticao END AS tipo,
          CAST(dt_vencimento AS DATE) AS vencimento
        FROM (SELECT * FROM {t["cosmeticos"]}
              QUALIFY row_number() OVER (PARTITION BY nu_cnpj_empresa, nu_processo
                ORDER BY st_registrado, no_produto, nu_registro NULLS LAST) = 1)
        QUALIFY ordem <= {COSMETICOS_POR_EMPRESA}"""


COSMETICOS_POR_EMPRESA_TABELA = Derivada(
    name="cosmeticos_por_empresa",
    group="cosmeticos",
    title=f"Cosméticos: os {COSMETICOS_POR_EMPRESA} primeiros de cada empresa",
    fontes=("cosmeticos",),
    sort=("cnpj", "ordem"),
    notes=(
        f"Os {COSMETICOS_POR_EMPRESA} primeiros cosméticos de cada CNPJ, ativos antes, depois por "
        "nome e processo (ordem diz a posição); os totais da empresa estão em empresas.cosmeticos "
        "e cosmeticos_ativos. Para todos os produtos de uma empresa, use os arquivos de busca "
        "da tabela cosmeticos. Derivada a cada build.",
        "Uma linha por processo: dos 11.144 processos que cosmeticos lista duas vezes "
        "(st_registrado falso e verdadeiro), fica a linha sem registro, a que traz "
        "ds_tipo_peticao.",
        "tipo normaliza ds_tipo_peticao: Registrado, Notificado, Isento de registro, Descartável.",
        "vencimento vem como a ANVISA publica, inclusive anos depois de 2100; sem data em parte "
        "das linhas.",
    ),
    sql=_cosmeticos_por_empresa,
)


def _alimentos_por_produto(t: dict[str, str]) -> str:
    # The produtos row of every alimento plus what a list of foods still needed from the raw
    # table (asked by the site, 2026-10-12): the petição's status and claim, constant within a
    # product, and three dates, of which only dt_publicacao and dt_situacao differ within a
    # product (2 of 50,522): the latest publication and status, the earliest analysis start.
    return _alimentos(
        t,
        {
            "situacao_assunto": "min(ds_situacao_assunto_doc)",
            "alegacao": "min(ds_alegacao_funcional)",
            "publicacao": "max(dt_publicacao)",
            "situacao_em": "max(dt_situacao)",
            "inicio_analise": "min(dt_inicio_analise)",
        },
    )


ALIMENTOS_POR_PRODUTO = Derivada(
    name="alimentos_por_produto",
    group="alimentos",
    title="Alimentos: uma linha por produto, com o status da petição",
    fontes=("alimentos", "alimentos_resultado"),
    sort=("cnpj", "id"),
    notes=(
        "As linhas de alimentos da tabela produtos (mesmas colunas, mesmos valores, inclusive "
        "os alergênicos) mais o status e a alegação da petição e três datas, para listar "
        "alimentos sem ler a tabela alimentos (uma linha por apresentação). Derivada a cada "
        "build.",
        "situacao_assunto e alegacao são iguais em todas as apresentações de um produto; "
        "publicacao e situacao_em são as mais recentes entre elas, inicio_analise a mais "
        "antiga. Horários de Brasília, sem fuso.",
        "Para as apresentações de um produto, leia alimentos (co_seq_produto = id) e "
        "alimentos_resultado (co_produto = id).",
    ),
    sql=_alimentos_por_produto,
)

MEDICAMENTOS_POR_PRODUTO = Derivada(
    name="medicamentos_por_produto",
    group="medicamentos",
    title="Medicamentos: uma linha por produto, com princípio ativo e classe terapêutica",
    fontes=("medicamentos",),
    sort=("cnpj", "id"),
    notes=(
        "Uma linha por medicamento: as linhas de medicamentos da tabela produtos (mesmo id) "
        "mais o princípio ativo, a classe terapêutica, a categoria regulatória e quantas "
        "linhas e processos a origem traz. Derivada de medicamentos a cada build.",
        "id é o registro de 9 dígitos (registro_produto); quando a origem não dá registro "
        "(dinamizados e produtos de baixo risco notificados, sem processo na maioria), id "
        "começa com sem-registro- seguido de um hash de empresa, nome e processo, estável "
        "entre builds, e registro_produto é nulo.",
        "ativo é verdadeiro quando alguma linha da origem está Ativo: 2.429 registros vêm "
        "com linhas Ativo e Inativo ao mesmo tempo. vencimento é o mais tardio, com precisão "
        "de mês; desde é a concessão mais antiga.",
        "Preços por apresentação na tabela cmed: cmed.registro_produto = registro_produto.",
    ),
    sql=lambda t: (
        f"""
        SELECT id, registro_produto, processo, nome, cnpj, empresa, ativo,
          categoria_regulatoria, classe_terapeutica, principio_ativo, desde, vencimento,
          processos, linhas
        FROM ({_medicamentos_unicos(t)})"""
    ),
)

PRODUTOS_SAUDE_POR_PRODUTO = Derivada(
    name="produtos_saude_por_produto",
    group="produtos_saude",
    title="Produtos para saúde: uma linha por registro, com nome técnico, classe e fabricantes",
    fontes=("produtos_saude",),
    sort=("cnpj", "id"),
    notes=(
        "Uma linha por registro ou cadastro de produto para saúde (dispositivo médico): as "
        "linhas de produtos_saude da tabela produtos (mesmo id) mais o nome técnico, a classe "
        "de risco, a lista de fabricantes com o país e quantas linhas a origem traz. Derivada "
        "de produtos_saude a cada build.",
        "tipo é Cadastrado (registro começa com 8: notificação, classes I e II, sem prazo) ou "
        "Registrado. ativo é verdadeiro nos cadastros (VIGENTE) e nos registros cuja validade "
        "não tinha vencido na data do arquivo (dt_atualizacao_dado); vencimento é essa "
        "validade, nula nos cadastros.",
        "fabricantes lista cada fabricante como 'nome (país)', sem repetição, em ordem; vazia "
        "quando a origem não informa.",
    ),
    sql=lambda t: (
        f"""
        SELECT id, registro, processo, nome, nome_tecnico, classe_risco, cnpj, empresa, tipo,
          ativo, publicacao, vencimento, fabricantes, linhas
        FROM ({_produtos_saude_unicos(t)})"""
    ),
)

DERIVADAS: tuple[Derivada, ...] = (
    PRODUTOS,
    EMPRESAS,
    MEDIDAS,
    COSMETICOS_POR_EMPRESA_TABELA,
    ALIMENTOS_POR_PRODUTO,
    MEDICAMENTOS_POR_PRODUTO,
    PRODUTOS_SAUDE_POR_PRODUTO,
)


def write(
    d: Derivada, paths: dict[str, Path], parquet: Path, spill: Path
) -> tuple[int, list[tuple[str, str]]]:
    """Build `d` from the Parquet files in `paths` (fonte -> file) into `parquet`: the row
    count and the output columns as (name, DuckDB type)."""
    sources = {name: f"read_parquet({_quote(str(path))})" for name, path in paths.items()}
    order = ", ".join(d.sort)
    query = f"SELECT * FROM ({d.sql(sources)}) ORDER BY {order}"
    parquet.parent.mkdir(parents=True, exist_ok=True)
    spill.mkdir(parents=True, exist_ok=True)
    with connect(spill) as con:
        rows = copy_parquet(con, query, parquet, ROW_GROUP_SIZE)
        columns = [(row[0], row[1]) for row in con.execute(f"DESCRIBE {query}").fetchall()]
    return rows, columns
