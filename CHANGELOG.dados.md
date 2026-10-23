# Changelog of the published data

What changed in what <https://vasfvitor.github.io/anvisa-api/> serves: the manifest format,
the tables and their meaning. By date of the code change, with the first build that carried it
when known. The package's own `CHANGELOG.md` records the Python API; a data change that needs
no package release still appears here. `schema_version` in the manifest only changes when a
consumer must change to keep reading it; everything below has been additive, so it is still 1.

## 2026-10-23

- **`cbpf` and `cbpf_medicamentos`**, the good manufacturing (CBPF) and distribution and
  storage (CBPDA) practices certificates, from `CONSULTAS/EMPRESA_FISCALIZACAO_PRODUTO/`,
  group `cbpf`. `cbpf` (`TA_CONSULTA_CBPF.CSV`, 22,039 rows on 2026-10-13) is one row per
  petição of every area but medicines, with 80 rows that are exact copies of another
  (21,959 petições); the inspected plant is a CNPJ in Brazil, an ANVISA code abroad, or
  missing on the older 72%; `status` is ANVISA's verdict on the load day. Dates month
  first; `COM&Eacute-RCIO` and `&#8203-` kept as written. `cbpf_medicamentos`
  (`TA_CONSULTA_CBPF_MEDICAMENTO_DETALHE.CSV`, 14,803 rows, 13,651 petições) is one row
  per certified line of a medicines' petição, and **stale**: `dt_carga_etl` is 2025-09-08
  on every row although ANVISA republished it on 2026-05-15 (the main
  `TA_CONSULTA_CBPF_MEDICAMENTO.CSV` has 4 rows); its notes say so.
  `TA_CONSULTA_CBPF_DETALHE.CSV` is left out, stale and inconsistent with the main file.
- **`certificados`**, one row per petição of both files: solicitante, inspected plant
  (`inspecionada_cnpj` / `inspecionada_codigo`), `tipo`, `area` (`produtos_saude` incl.
  MDSAP, `insumos`, `medicamentos`, `cosmeticos`, `saneantes`, `alimentos`, `outros`),
  `situacao` (synthesised from `status` for medicines, whose file has none), `vigente`
  (decided, not `cancelado`, `validade >= dados_de`: against each file's own load date,
  never the build's), `vinculo` (`propria`, `grupo`, `terceira`, `exterior`,
  `desconhecida`), the medicines' certified lines as the sorted list `linhas`, and
  `dados_de`, which a consumer must show for medicines. On the files of 2026-10-13 and
  2025-09-08: 35,610 rows (21,959 + 13,651), 3,963 + 2,484 vigentes; on the `cbpf` part
  5,174 exterior, 1,006 propria, 20 grupo, 7 terceira and 15,752 desconhecida. Group
  `cbpf`, fontes `cbpf` and `cbpf_medicamentos`.
- **`produtos_saude_fabricacao`**, who makes each company's medical devices: one row per
  holder CNPJ, manufacturer `chave` and `pais`, from `produtos_saude`'s fabricante
  columns. ANVISA gives manufacturers no id, so the key is the name normalized
  (`derivar.chave_sql`: five compatibility characters mapped, accents stripped, lower
  case, only `[a-z0-9]`), never merged across countries; the rule is
  `fixtures/dados/chaves.json`, a second contract tested on both sides like
  `tokens.json`. `fabricante` is the commonest spelling; `vinculo` is `propria` (the
  holder's own name, in Brazil), `terceira` or `exterior`; `registros`,
  `registros_ativos`, `linhas`. On 2026-10-13: 16,725 rows (14,531 exterior, 2,064
  propria, 130 terceira), 11,333 manufacturers.
- **`produtos_saude_por_produto` changes shape**: `fabricantes_chaves` and
  `fabricantes_paises` appended after `fabricantes`, aligned index by index with it (one
  distinct set, one order, so the existing `fabricantes` values do not change); with
  `cnpj` they are the key of `produtos_saude_fabricacao`. `fabricantes` is `[]` instead
  of NULL on the registros without a manufacturer, as its note already promised.
- 10 derived tables, 22 published.
- **`alimentos_fabricacao`**, who packs or makes each company's food: one row per holder CNPJ
  and manufacturer, read from the two manufacturer columns of `alimentos_resultado` (one row
  per apresentação). `empresas_envasadoras` gives Brazilian packers as
  `NOME - CIDADE - PAÍS ; CNPJ` (key `fabricante_cnpj`; city and country read from the right,
  because names contain " - "), `empresas_internacionais` foreign manufacturers as
  `NOME - PAÍS ; CÓDIGO` (key `fabricante_codigo`, or the name when the code is empty).
  `vinculo` is `propria` (same CNPJ), `grupo` (same 8-digit root, other branch), `terceira` or
  `exterior`; `produtos`, `produtos_ativos` and `apresentacoes` count what names the pair. On
  the food files of build 20261022T230331Z: 1,155 CNPJ pairs, of which 575 propria, 86 grupo
  and 494 terceira, plus 327 foreign pairs (317 by code, 10 by name); 1,482 rows, 54 KB. The
  356 resultado rows without an `alimentos` row are dropped. Group `alimentos`, fontes
  `alimentos` and `alimentos_resultado`.
- **`empresas` changes shape**: two columns appended after `medida_ultima`, `envasa_para`
  (distinct other CNPJs this one packs food for) and `envasado_por` (distinct other CNPJs that
  pack its food), from the `alimentos_fabricacao` pairs with `vinculo` grupo or terceira, 0 when
  none. A packer with no product and no measure now gets a row too (150, with `produtos` 0),
  named as the packer list writes it. `alimentos_resultado` joins its fontes.
- The build pins DuckDB's `default_null_order` to `NULLS LAST`, so a sort on a nullable column
  (`alimentos_fabricacao`'s) gives the same file on every DuckDB version; no existing table
  sorts on one, so their files are unchanged.

## 2026-10-22

- **`medicamentos_por_produto.eans`**: the distinct EAN 1 of the product's presentations in the
  CMED list (`cmed.registro_produto`), sorted; empty when the product is not in the CMED. The
  table now derives from `medicamentos` and `cmed`. For the site's search by the barcode on
  the box without downloading the 5.6 MB `cmed` table.
- **`palavras.parquet`** in the cosméticos search folder (`busca`): every distinct word of a
  product name with its row count (121k rows), listed in `indice.json` as `palavras_parquet`.
  For the site's "você quis dizer" on a token that found nothing. Format version stays 1: the
  key is additive, and an index carried over from an older build may lack it.
- **`404.html`** beside the manifest: GitHub Pages serves it for any missing path, which is
  mostly a `data/<old build>/` URL after a deploy; it points at the current manifest, the
  listing and the dictionary, and is `noindex`.
- **`medicamentos` and `produtos_saude`**, the registries behind "está liberado?" for medicines
  and medical devices, converted from `DADOS_ABERTOS_MEDICAMENTOS.csv` and
  `TA_PRODUTO_SAUDE_SITE.csv` at the root of dados.anvisa.gov.br. `medicamentos` keeps the
  file as published: 228,869 rows of which only 42,661 are distinct (ANVISA repeats product ×
  processo rows), `numero_registro_produto` 9 digits (the product; `cmed.registro_produto`
  joins it), dd/mm/yyyy dates, month-precision `data_vencimento_registro`, the CNPJ and name
  joined in `empresa_detentora_registro`, and the source's own spelling of categories. The
  file has no load-time column: `loaded_at` is null and the manifest keeps the Last-Modified.
  `produtos_saude` is one row per registro × fabricante (116,008 rows, 100,681 registros),
  `validade_registro_cadastro` a date or the word `VIGENTE`, `dt_atualizacao_dado` the load
  time (month first, unlike its publication date). It is the first open-data file that quotes
  nothing, so the catalog gained `Dataset.quoted` (false: one record per line, `"` an ordinary
  character). Groups `medicamentos` (shared with `cmed`) and `produtos_saude`.
- **Medicines and medical devices in the derived tables.** `produtos` gains the areas
  `medicamentos` (one row per 9-digit registro, 36,427 on 2026-10-13; a product the source
  lists without a registro, 3,627 of them, gets a stable `sem-registro-<hash>` id from its
  company, name and processo) and `produtos_saude` (one row per registro or cadastro,
  100,681), with the shared columns: there `tipo` is the regulatory category or
  Registrado/Cadastrado and `categoria` the therapeutic class or `Classe I` to `IV`. **`empresas`
  changes shape**: four new counts (`medicamentos`, `medicamentos_ativos`, `produtos_saude`,
  `produtos_saude_ativos`) and `produtos`/`ativos` now sum five areas, so a company's totals
  grow. Two new per-area tables carry what the common schema cannot:
  `medicamentos_por_produto` (principio_ativo, classe_terapeutica, categoria_regulatoria,
  `registro_produto` to join `cmed`, how many rows and processos the source repeats) and
  `produtos_saude_por_produto` (nome_tecnico, classe_risco, the sorted `fabricantes` list as
  "nome (país)"). `ativo` on produtos para saúde, which have no status column, is VIGENTE or a
  validity not yet past on the file's own load date, so nothing depends on the day of the
  build. 7 derived tables, 17 published.
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
