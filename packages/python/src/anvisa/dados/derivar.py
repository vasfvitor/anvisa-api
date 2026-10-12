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


def _produtos(t: dict[str, str]) -> str:
    # alimentos: one row per apresentação, grouped into the product (co_seq_produto). Name,
    # brands, company, processo and situação are constant within a product (README,
    # "Products"), so min() reads the one value; the dates take the earliest.
    alimentos = f"""
        SELECT 'alimentos' AS area,
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
          min(dt_vencimento_registro) AS vencimento,
          CAST(count(*) AS INTEGER) AS apresentacoes,
          coalesce(list(nu_registro ORDER BY nu_registro) FILTER (WHERE nu_registro IS NOT NULL),
            []) AS registros
        FROM {t["alimentos"]}
        GROUP BY co_seq_produto"""
    # saneantes: one row per record already (nu_expediente is unique), no detail file
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
          CAST([] AS VARCHAR[]) AS registros
        FROM {t["saneantes"]}"""
    return f"{alimentos}\n        UNION ALL BY NAME\n{saneantes}"


PRODUTOS = Derivada(
    name="produtos",
    group="produtos",
    title="Produtos: uma linha por alimento ou saneante regularizado",
    fontes=("alimentos", "saneantes"),
    sort=("cnpj", "area", "id"),
    notes=(
        "Uma linha por produto, com as mesmas colunas para alimentos (agrupados por "
        "co_seq_produto: um produto, várias apresentações) e saneantes (uma linha por "
        "nu_expediente). Derivada de alimentos e saneantes a cada build.",
        "Os cosméticos não estão aqui (1,2 milhão de linhas): use os arquivos de busca da "
        "tabela cosmeticos.",
        "vencimento nas notificações de alimentos é 2029-12-01, um valor de preenchimento da "
        "ANVISA; desde (primeira regularização) e categoria só existem para alimentos.",
        "ativo é a situação na ANVISA no dia do build; se a data de vencimento já passou, "
        "decida na consulta, com a data do dia.",
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
          FROM {t["saneantes"]} GROUP BY 1),
        co AS (
          SELECT cnpj, CAST(count(*) AS INTEGER) AS cosmeticos,
            CAST(count(*) FILTER (WHERE ativo) AS INTEGER) AS cosmeticos_ativos
          FROM (SELECT nu_cnpj_empresa AS cnpj, nu_processo,
                  arg_min(st_situacao_produto, CAST(st_registrado AS INTEGER)) AS ativo
                FROM {t["cosmeticos"]} GROUP BY 1, 2)
          GROUP BY cnpj),
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
                  UNION ALL SELECT nu_cnpj_empresa, no_razao_social_empresa FROM {t["cosmeticos"]})
                WHERE nome IS NOT NULL GROUP BY 1, 2)
          GROUP BY cnpj),
        chaves AS (
          SELECT cnpj FROM al UNION SELECT cnpj FROM sa UNION SELECT cnpj FROM co
          UNION SELECT cnpj FROM me)
        SELECT k.cnpj,
          coalesce(nomes.nome, me.nome) AS nome,
          coalesce(al.alimentos, 0) AS alimentos,
          coalesce(al.alimentos_ativos, 0) AS alimentos_ativos,
          coalesce(sa.saneantes, 0) AS saneantes,
          coalesce(sa.saneantes_ativos, 0) AS saneantes_ativos,
          coalesce(co.cosmeticos, 0) AS cosmeticos,
          coalesce(co.cosmeticos_ativos, 0) AS cosmeticos_ativos,
          coalesce(al.alimentos, 0) + coalesce(sa.saneantes, 0) + coalesce(co.cosmeticos, 0)
            AS produtos,
          coalesce(al.alimentos_ativos, 0) + coalesce(sa.saneantes_ativos, 0)
            + coalesce(co.cosmeticos_ativos, 0) AS ativos,
          coalesce(me.medidas, 0) AS medidas,
          me.medida_ultima
        FROM chaves k
        LEFT JOIN al USING (cnpj) LEFT JOIN sa USING (cnpj) LEFT JOIN co USING (cnpj)
        LEFT JOIN me USING (cnpj) LEFT JOIN nomes USING (cnpj)"""


EMPRESAS = Derivada(
    name="empresas",
    group="empresas",
    title="Empresas: uma linha por CNPJ, com produtos por área e medidas de fiscalização",
    fontes=("alimentos", "saneantes", "cosmeticos", "produtos_irregulares"),
    sort=("cnpj",),
    notes=(
        "Uma linha por CNPJ (14 dígitos) que detém um alimento, saneante ou cosmético ou que "
        "foi alvo de uma medida de fiscalização. Derivada a cada build.",
        "Os totais contam produtos, não linhas: alimentos por co_seq_produto e cosméticos por "
        "nu_processo (um processo listado duas vezes conta uma, e a linha sem registro decide "
        "se está ativo).",
        "nome é a grafia mais frequente da razão social nas tabelas de produtos; empresas "
        "conhecidas só por medidas levam o nome escrito na medida.",
        "medidas conta dossiês de fiscalização contra a empresa (nu_cnpj_empresa_investigada), "
        "não os que ela abriu. Alvos sem CNPJ de 14 dígitos (CPF, texto, CNPJ com 13 dígitos) "
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

DERIVADAS: tuple[Derivada, ...] = (PRODUTOS, EMPRESAS, MEDIDAS)


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
