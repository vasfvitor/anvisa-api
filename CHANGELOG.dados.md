# Changelog of the published data

What changed in what <https://vasfvitor.github.io/anvisa-api/> serves: the manifest format,
the tables and their meaning. By date of the code change, with the first build that carried it
when known. The package's own `CHANGELOG.md` records the Python API; a data change that needs
no package release still appears here. `schema_version` in the manifest only changes when a
consumer must change to keep reading it; everything below has been additive, so it is still 1.

## 2026-10-22

- **`cmed`, the CMED price list**, the first table from a source other than ANVISA's
  Windows-1252 product files: CMED's monthly maximum prices per medicine presentation, from the
  two CSVs at the root of dados.anvisa.gov.br (`TA_PRECO_MEDICAMENTO.csv` and
  `TA_PRECO_MEDICAMENTO_GOV.csv`, UTF-8 with BOM, `;`) merged by `ggrem` (15 digits) into one
  wide row: `registro` (13 digits) and `registro_produto` (its 9-digit root, which joins
  `produtos_irregulares`), substância, laboratório (`cnpj` digits only), produto, apresentação,
  classe terapêutica, tipo and regime de preço, three EANs, the 26 PF, 26 PMC and 26 PMVG
  prices as **integer centavos** per ICMS rate (`pf_18`, `pmc_17_5_alc`, `pmvg_0`), the flags
  `restricao_hospitalar`, `cap`, `confaz_87`, `icms_0`, `comercializacao` (with the year it
  refers to in `comercializacao_periodo`), `analise_recursal`, `lista_pis_cofins`, `tarja`,
  and `publicado_em`, the files' Last-Modified (Brasília). 26,635 rows on the files of
  2026-10-09; `source.files` in the manifest lists both files with their ETag, size and hash,
  and `source.stamp` the day. Group `medicamentos`. Not in any derived table yet.

## 2026-10-12

- **`vencimento` in `produtos` and `alimentos_por_produto` is the latest expiry** among a
  food's apresentações, not the earliest: 141 products renew them at different times, and
  the question the date answers is whether the product is still valid. Asked by the site.
- **`alimentos_por_produto`**, the fifth derived table, asked by the site: the alimentos rows
  of `produtos` (same columns, same values) plus `situacao_assunto`, `alegacao`, `publicacao`
  and `situacao_em` (latest among the apresentações) and `inicio_analise` (earliest). A food
  list no longer needs the per-apresentação tables. 50,522 rows, 2.8 MB.
- **`cosmeticos_por_empresa`**, the fourth derived table: the first 100 cosmetics of each
  CNPJ (`ordem`: active first, then name, then processo), one row per processo with the
  unregistered row of a pair, `tipo` normalized (Registrado, Notificado, Isento de registro,
  Descartável) and `vencimento` as published. 5,246 companies, 1,963 of them capped;
  281,220 rows, 7.6 MB. A company page no longer needs the 24 MB table; its totals are in
  `empresas`, and the whole list is in the search files.
- **One row per saneante in `produtos`.** Six expedientes come twice in `saneantes`, as a
  Registrado and a Notificado row equal in everything else (found by the site against build
  `20261012T012351Z`); `produtos` now keeps the registered row, so `(area, id)` is unique
  again and `empresas.saneantes` counts expedientes: 144,407, not 144,413. The `saneantes`
  notes and the `nu_expediente` description no longer call it unique per row.
- `empresas` notes say that `medidas` counts dossiês of every ANVISA area: 457 companies
  exist there only through a measure in medicamentos, dispositivos or another area without a
  product table here.
- **Allergens on `produtos`.** Six new columns from `alimentos_resultado`, now a fonte:
  `gluten` and `lactose` (`Sim`, `Não`, `Varia` when the apresentações disagree), the sorted
  lists `contem` ("Contém" and "Contém derivado de"), `pode_conter` and `nao_contem` (an
  item some apresentação contains leaves the other two), and `alergia_varia`. NULL on the
  39,278 alimentos whose labels declare nothing and on every saneante; 11,244 products
  declare, 227 vary. The same rules as the site's own summary. The file grows 120 KB.
- **`medidas`**, the third derived table: one row per dossiê × produto of
  `produtos_irregulares` (which has one per dossiê × ação × atividade × produto), with `acoes`,
  `atividades` and `registros` as sorted lists, `primeira` and `ultima` publication, the
  target's `cnpj` only when the source has a 14-digit one, `pessoa` when the target is a CPF
  (then neither number nor name is published), and who filed the dossiê (`autor_cnpj`,
  `autor`). Sorted by `area_codigo`, `dossie`, `produto`; 14,092 rows, 426 KB.

## 2026-10-11

- **Descriptions and notes.** Every column of every table carries a Portuguese
  `description` in the manifest (`columns[].description`), and every table a list of `notes`
  for the reader: what one row is, which values are placeholders, how to count. A carried-over
  table takes the current text, not the one it was published with.
- **`dicionario.html`**, generated beside the manifest from the manifest alone: notes and
  columns of every table, with the build id. The manifest names it in the new top-level `docs`
  key; `license` says the data is ANVISA's open data and the pipeline MIT.
- **Derived tables**, rebuilt on every build from the build's own files, never carried over,
  byte-identical for identical inputs. Their entries carry `fontes` (the tables they read) and
  a `source` object like any table's, whose `url` is the recipe (`derivar.py` at that commit)
  and whose `loaded_at` is the latest among the sources:
  - `produtos`: one row per alimento (grouped by `co_seq_produto`) or saneante
    (`nu_expediente`), one schema, sorted by `cnpj`, `area`, `id`. Cosméticos stay in their
    search files.
  - `empresas`: one row per 14-digit CNPJ that holds a product or was the target of a
    fiscalização measure, with products and active products per area and in total, the
    commonest spelling of the name, `medidas` and `medida_ultima`.
- `index.html` links the dictionary and this file.

## 2026-10-10 (build `20261010T000506Z`)

- **Search files** for `cosmeticos` (`busca` key in its manifest entry, format version 1): the
  rows re-partitioned under `data/<build_id>/cosmeticos/` into 3,202 small Parquet files by
  word of the product name, by CNPJ and by processo or registro number, plus
  `empresas.parquet` and `indice.json` (240 MB in all, index 118,197 bytes). The tokenizer is
  the contract with the site: `fixtures/dados/tokens.json`.

## 2026-10-08 to 2026-10-09

- `cosmeticos`: `TA_CONSULTA_COSMETICOS.CSV`, 1,204,980 rows, every notified, exempt or
  registered cosmetic, sorted by `nu_cnpj_empresa`, `nu_processo`, `st_registrado`; 11,144
  processos are listed twice, once per `st_registrado`. 24 MB of Parquet.

## 2026-10-05 to 2026-10-08 (package 0.5.0)

- First publication: `alimentos` and `alimentos_resultado` as typed, sorted Parquet under
  `data/<build_id>/`, with `manifest.json` (schema version 1: per table `path`, `bytes`,
  `sha256`, `rows`, `row_group_size`, `sort`, `timezone`, `columns`, `source` with the ETag and
  ANVISA's load time, `nulls_added`, `rejected_records`) and `index.html`. Paths are immutable;
  the previous build disappears with the next deploy. Timestamps are naive Brasília time.
- `produtos_irregulares`: `TA_CONSULTA_PRODUTOS_IRREGULARES_RESULTADO.CSV`, the fiscalização
  measures of every area, 79,986 rows; `nu_cnpj` is who filed the dossiê, the target is
  `nu_cnpj_empresa_investigada`.
- `peticoes_alimento` and `peticoes_alimento_andamento`: the analysis cycle of every alimentos
  petição, finalized at least once (69,188 rows) or never (1,205 rows).
- `saneantes`: `TA_CONSULTA_SANEANTES.CSV`, 144,384 rows; dates are month first in the source.
- `alimentos_resultado` sorted by `co_produto`, `co_seq_apresentacao_produto`, row groups of
  2,048 rows, so one product's apresentações are one row group.
- A partial build (`-d <table> --skip-unchanged <manifest>`) carries the other published
  tables over, verified against the published size and sha256, so the manifest stays complete.
