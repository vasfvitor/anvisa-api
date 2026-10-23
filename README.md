# `anvisa`

Open source client for ANVISA's official [Portal de APIs](https://api.anvisa.gov.br/),
specifically the **Consultas Externas** API: fila de análise, UDI de dispositivos médicos, termos GMDN,
nomes técnicos, listas e assuntos de peticionamento, and since 2026-10-01 produtos para saúde,
certificados de boas práticas (genéricos e de medicamentos) e produtos derivados do tabaco.

ANVISA publishes an OpenAPI document for this API, but it is wrong or silent about most of
what you need to call it successfully. This repository records what the API actually does and
ships a client that encodes it.

## What the spec doesn't tell you (all verified live, 2026-09-06, 2026-09-08 and 2026-10-22)

| Behavior | Reality |
|---|---|
| User-Agent | Cloudflare answers **403** to curl's default UA, even for the spec URL. Send a descriptive one. |
| Auth | OAuth 2.0 client credentials (Keycloak, realm `externo`). Token lasts **1740 s** (the portal tutorial says 300), **no refresh token**, one role `CONSULTASEXTERNAS_LEITURA`. |
| Rate limit | Token bucket, **burst 25, refill 1/s**, reported only in `X-RateLimit-*` headers. Keyed by **source address**, not client id: the unauthenticated portal endpoints on the same gateway drain the same bucket. |
| Pagination | Requests are **1-based** (`page: 0` → error); responses are Spring `Page` objects, **0-based**. |
| Filters | `POST /udi` needs **at least one** `filter` key; `POST /fila/consulta` **and** `POST /lista/consulta` need `filter.subfila` (yes, also for listas). `POST /certificado/` and `POST /certificadoMedicamento/` need one key **besides `contexto`** (else 500 `MSG-013`), and answer an empty-bodied **404** when nothing matches. `POST /tabaco/tabacos` silently **ignores** `cnpjEmpresa`/`cnpj`. |
| Errors | Validation failures come back as **HTTP 500** with `{status, mensagem, data_hora, mensagem_detalhada}`. |
| `fila/consulta`, `lista/consulta` | Return the **whole** subqueue/sublista as an array (40, 35, 71 and 555 rows observed); `page`/`size` are ignored. A subfila with nothing queued is an empty-bodied **404**, which the client returns as `[]`. |
| Dates | Integer **epoch milliseconds**. |
| Downloads | The spec says `200 OK` with no content and nothing about `Accept`. Every download answers **500 "Could not find acceptable representation"** to `Accept: application/json` (all but `GET /udi/{id}/download`); send `Accept: */*`. `Content-Type` is `application/vnd.ms-excel` even when the bytes are OOXML or a zip (the certificado exports are OOXML named `.xls`; the tabaco label is a PDF/A sent as `application/force-download`). `GET /tabaco/download` takes its `PaginationBuilder` as JSON **in the query string** (`filtro=`), ignores it, and answers 200 where the spec promises 201. `GET /tabaco/detailTabaco/{id}` wants `codDocumento`, not the processo number. `POST /assunto/downloadAssuntoFormulario` takes a **bare JSON integer** body, not the declared `PaginationBuilder`, and returns the file with **no `Content-Type` and no `Content-Disposition`**. An empty subfila is a **500** here, not the empty 404 `fila/consulta` gives. |
| Filter keys | Verified live: `udi` accepts `nomeComercial` (substring), `udiDi` (exact), `cnpjDetentora`, `codigoGmdn`, `nuRegistro`; `nomeTecnico` accepts `nomeTecnico` (substring) and `categoriaProduto`; `termoGmdn` accepts `conteudo`; `saude` accepts `nomeProduto` and `cnpj`; `certificado` accepts `nacional`, `internacional`, `status`, `cnpjCertificada` (plus `cnpjSolicitante` on medicamentos); `tabaco` accepts `nomeProduto`. `POST /assunto/` is **broken** (500, body not bound). |
| Coverage | All **59** endpoints of the spec are wrapped: the 32 it had until 2026-10-01 and the **27** ANVISA published then (produtos saúde, certificados de boas práticas, certificados de medicamentos, tabaco), recorded on 2026-10-22 and wrapped as `saude`, `certificado`, `certificado_medicamento` and `tabaco`. The empresa nacional/internacional, dossiê and alimentos pages left the portal menu without ever being served. Alimentos and other product families exist as bulk CSV on [`dados.anvisa.gov.br/dados/CONSULTAS/`](https://dados.anvisa.gov.br/dados/CONSULTAS/) (for example `TA_CONSULTA_PRODUTOS_IRREGULARES_RESULTADO.CSV`, refreshed on weekdays); the alimentos files are republished here as Parquet, see [Dados abertos](#dados-abertos-parquet-on-github-pages). |

## Layout

```
spec/       ANVISA's OpenAPI document (as published, only reformatted) + an OpenAPI Overlay
            with the corrections above + the resolved spec. Language-neutral source of truth.
            spec/portal/ holds the portal's own doc pages and menu, fetched by snapshot.py.
fixtures/   Real responses recorded from the API, with a manifest. Shared test fixtures.
            fixtures/dados/ holds byte-exact samples of the open-data CSVs.
packages/python/   The `anvisa` Python library and CLI.
```

The overlay follows the [OpenAPI Overlay Specification 1.0](https://spec.openapis.org/overlay/v1.0.0.html),
so any overlay tool can apply it. `spec/apply_overlay.py` is the small applier used here.
Models are generated from the resolved spec; everything else is hand-written and small.

Two sibling repositories build on this one: `anvisa-feeds` (daily fila snapshots published as
Atom feeds; installs `anvisa` from PyPI) and `anvisa-dash` (a DuckDB-WASM search over the
Parquet files published here, see "Dados abertos" below).

## Python package

```bash
uv tool install anvisa      # or: pipx install anvisa
```

Credentials come from the portal (login Gov.br → Client ID / Client Secret). Put them in
`~/.config/anvisa/credentials.env` (chmod 600):

```
CLIENT_ID=...
CLIENT_SECRET=...
```

or export `ANVISA_CLIENT_ID` / `ANVISA_CLIENT_SECRET`.

```bash
anvisa fila areas                       # Medicamento=1, Dispositivos Médicos=8, ...
anvisa fila grupos 8                    # Registros, Alterações, Revalidações, ...
anvisa fila subfilas 285
anvisa fila query 167                # the queue, in order, with protocol numbers
anvisa lista areas && anvisa lista grupos 1 && anvisa lista sublistas 921
anvisa lista query 2141              # same shape as fila query
anvisa udi search --nome cateter --size 5
anvisa udi get 377
anvisa udi get-gmdn 47852
anvisa udi search-gmdn pacing           # GMDN terms by text (names are in Portuguese)
anvisa nome-tecnico search --size 50    # nomes técnicos with risk class
anvisa nome-tecnico categorias
anvisa assunto list --busca bioequival   # petition subject codes
anvisa assunto get 10013                  # documents, forms, legal basis, fees by size
anvisa assunto servicos-associados 13497  # gov.br services behind a serviço code
anvisa saude search --nome 1000VINYL      # produtos para saúde (193k rows unfiltered)
anvisa saude get 25351279419201172        # apresentações, fabricantes, risco
anvisa certificado search --nacional --internacional --status 0
anvisa certificado-medicamento search --solicitante 39809660000153
anvisa certificado-medicamento linhas     # also: classes, formas, status, concedida-por
anvisa tabaco search --nome "7 PONTAS"
anvisa tabaco get 33007111                # codDocumento from search: embalagens, rótulos
anvisa --format json fila query 167 | jq length
```

Downloads. `-o` takes a file or a directory (default `.`, filled in with the name the server
sent) and `-o -` writes the bytes to stdout; the saved path is printed to stderr:

```bash
anvisa fila download 167 -o ./exports      # consulta_fila.xlsx, the whole subfila
anvisa lista download 2141
anvisa nome-tecnico download --nome cateter
anvisa assunto download -c 10013 -c 10014  # no --codigo exports all ~2,600 (≈5 MB)
anvisa assunto formulario 8016             # formulários[].id, from `assunto get`
anvisa udi download 377 -o - > udi.xlsx
anvisa udi snapshot 173 -o ./exports       # the week's zip, streamed to disk
anvisa saude download 25351279419201172    # DetalheProdutoSaude.xls; download-pdf for the PDF
anvisa certificado download --cnpj 05993698000450   # consulta_certificado.xls (OOXML)
anvisa tabaco download                     # the whole table, consulta_fumigenos.xls
anvisa tabaco download-rotulo 3631632      # the label PDF, rotulos[].id from `tabaco get`
```

```python
from anvisa import Client

with Client() as anvisa:  # credentials from the environment or ~/.config/anvisa/credentials.env
    for row in anvisa.fila.query(167):
        print(row.nuOrdem, row.numeroProcessoFormatado, row.dsAssunto)

    page = anvisa.udi.search(nomeComercial="cateter", size=50)
    for device in anvisa.udi.iter_search(nomeComercial="cateter"):   # every page, throttled
        print(device.udiDi, device.nomeComercial)

    anvisa.fila.download(167).save("exports/")        # consulta_fila.xlsx
    anvisa.udi.download_snapshot(173, "exports/")     # streamed, never buffered
```

The client sends a User-Agent, caches the token and renews it before expiry, mirrors the
gateway's token bucket so a loop never hits 429, sends `Accept: */*` on every download, and
raises typed exceptions (`MissingFilterError`, `InvalidPageError`, `MalformedRequestError`,
`BlockedError`, `EmptyExportError`, `NoResultError`, and others) instead of a bare 500.

### Development

```bash
cd packages/python && uv sync --extra dados
uv run pytest             # fixture-only, no network
uv run pytest -m live     # 6 real requests: 4 to the API (credentials) + the token + a HEAD on dados.anvisa.gov.br
make spec && make models  # at the repo root; a non-empty git diff means the overlay drifted
make snapshot             # re-download the spec and portal docs; a diff means ANVISA changed them
```

## Dados abertos: Parquet on GitHub Pages

The Consultas Externas API has no food products. ANVISA publishes them as bulk CSV on
[dados.anvisa.gov.br](https://dados.anvisa.gov.br/dados/CONSULTAS/PRODUTOS/), refreshed on
weekdays, with no CORS headers, so a browser cannot read them; the analysis cycle of every
alimentos petição sits at the root of the [same server](https://dados.anvisa.gov.br/dados/), and
the measures behind the portal's "Consulta de produtos irregulares", for every area, and the
good-practice certificates (CBPF/CBPDA) in `CONSULTAS/EMPRESA_FISCALIZACAO_PRODUTO/`; cosméticos and saneantes sit beside alimentos. A
daily workflow here (`.github/workflows/dados.yml`) converts them to typed, sorted Parquet and
publishes them on GitHub Pages, at <https://vasfvitor.github.io/anvisa-api/>. Row counts below are from the
day the table was added; the live `manifest.json` has today's:

| Table | Source | Rows (file of) | One row per |
|---|---|---|---|
| `alimentos` | `TA_CONSULTA_ALIMENTOS.CSV` | 66,681 (10-05) | apresentação of a registered or notified food product, sorted by `nu_cnpj_empresa`, `nu_processo` |
| `alimentos_resultado` | `TA_CONSULTA_ALIMENTOS_RESULTADO.CSV` | 66,941 (10-05) | apresentação detail (embalagem, tabela nutricional, alergênicos), sorted by `co_produto`, `co_seq_apresentacao_produto`; join on `co_seq_apresentacao_produto`, or fetch a whole product with `co_produto = alimentos.co_seq_produto` |
| `saneantes` | `TA_CONSULTA_SANEANTES.CSV` | 144,384 (10-05) | registered or notified saneante (`is_registrado`), one row per processo; sorted by `nu_cnpj_empresa`, `nu_processo`. No detail file. |
| `peticoes_alimento` | `CICLO_ANALISE_PETICOES_ALIMENTO.CSV` | 69,188 (10-06) | stage (fila, análise, exigência, finalização…) of an alimentos petição finalized at least once: 20,518 petições since 1998, sorted by `num_processo_peticao`, `num_expediente_peticao`, `ordem_ocorre_grupo_etapa_asc` |
| `peticoes_alimento_andamento` | `CICLO_ANALISE_PETICOES_ANDAMENTO_ALIMENTO.CSV` | 1,205 (10-06) | stage of a petição **never finalized**, what is in análise today (336 petições); same columns minus the two finalization dates, same sort |
| `produtos_irregulares` | `TA_CONSULTA_PRODUTOS_IRREGULARES_RESULTADO.CSV` | 79,986 (10-05) | product × ação (suspensão, proibição, recolhimento, apreensão, interdição, inutilização) × atividade of a fiscalização dossiê, every area (Alimento: 8,743 rows, 722 dossiês); sorted by `co_tipo_produto` (6 = Alimento), `nu_cnpj_empresa_investigada`, `co_seq_dossie_investig_med` |
| `medicamentos` | `DADOS_ABERTOS_MEDICAMENTOS.csv` | 228,869 (10-13) | medicamento registry, active and inactive, one row per product × processo **repeated** (42,661 distinct rows); `numero_registro_produto` is the 9-digit product (the 13-digit apresentação is in `cmed`), empty on 21k rows; dates dd/mm/yyyy, vencimento mmyyyy; `empresa_detentora_registro` is `<CNPJ> - <name>`; sorted by `numero_registro_produto`, `numero_processo`. Monthly. |
| `produtos_saude` | `TA_PRODUTO_SAUDE_SITE.csv` | 116,008 (10-13) | produto para saúde (dispositivo médico) × fabricante, 100,681 registros; `numero_registro_cadastro` 10 or 11 digits (8… cadastro, 1… registro); `validade_registro_cadastro` is a date or `VIGENTE`; the one open-data file that **quotes nothing** (`"` inside values), DT_ATUALIZACAO_DADO month first while DT_PUB is day first; sorted by `cnpj_detentor_registro_cadastro`, `numero_registro_cadastro`. No dictionary. |
| `cosmeticos` | `TA_CONSULTA_COSMETICOS.CSV` | 1,204,980 (10-07) | notified, exempt (`ISENTO DE REGISTRO`) or registered cosmetic, one row per processo except 11,144 processos listed twice (`st_registrado` false, then true, same `nu_registro`); sorted by `nu_cnpj_empresa`, `nu_processo`, `st_registrado`. **24 MB** of Parquet, far above the others, so it is also published as **search files** (`busca` below). No detail file. |
| `cmed` | `TA_PRECO_MEDICAMENTO.csv` + `TA_PRECO_MEDICAMENTO_GOV.csv` (root of the server) | 26,635 (10-09) | medicine presentation priced by CMED, keyed by `ggrem` (15 digits): the two monthly files (PF + PMC, PF + PMVG) merged by GGREM into one wide row, prices as **integer centavos** per ICMS rate (`pf_18`, `pmc_17_5_alc`, `pmvg_0`…), flags as booleans, `registro` (13 digits) and its 9-digit `registro_produto`; `publicado_em` is the files' Last-Modified. Sorted by `ggrem`. The same cut as the xlsx gov.br advertises, but for two rows corrected there after the CSVs were written (2026-10-09). Read by `anvisa.dados.cmed` (UTF-8 with BOM, `;`, no quoting), not by the Windows-1252 parser; monthly, not weekdays. |
| `cbpf` | `TA_CONSULTA_CBPF.CSV` | 22,039 (10-13) | petição for a good manufacturing (CBPF) or distribution and storage (CBPDA) practices certificate of every area **but medicines** (produtos para saúde and MDSAP, insumos, cosméticos, saneantes, alimentos): `nu_cnpj_solicitante` asked for it, the inspected plant is `nu_cnpj_empresa_inspecionada` in Brazil or `codigounico_inspecionada` abroad (`[ABC]` + 6 digits), and neither on 72% of the rows (older petições); 80 rows are **exact copies** of another (21,959 petições); `status` is computed by ANVISA on the load day and empty on a thousand rows, so `certificados.vigente` recomputes it. Dates month first. Sorted by `nu_cnpj_solicitante`, `co_documento_peticao`. The sibling `TA_CONSULTA_CBPF_DETALHE.CSV` is stale (2025-09-08) and is not read. |
| `cbpf_medicamentos` | `TA_CONSULTA_CBPF_MEDICAMENTO_DETALHE.CSV` | 14,803 (05-15, data of 2025-09-08) | **certified line** of a medicines' CBPF/CBPDA petição (13,651 petições, up to 6 lines each, 25 rows exact copies): the same solicitante/plant columns as `cbpf` (the plant's name is `razaosocial`, which ANVISA's dictionary misdescribes), addresses, the resolution and DOU, `linhacertificacao` and its parts, `st_liberacao_parametrica` (S/N), `ds_motivo_cancelamento` with `¿` where ANVISA lost characters. **Stale**: ANVISA stopped loading it on 2025-09-08 (`dt_carga_etl`) but keeps republishing it. Sorted by `nu_cnpj_solicitante`, `co_documento_peticao`, `linhacertificacao`. |

Ten more tables are **derived** from these on every build (`anvisa/dados/derivar.py`), so that
"one row per product" and "one row per company" are decided once, here, instead of in every
consumer's SQL. Their manifest entries carry `fontes` (the tables they were built from) and a
`source.url` that points at the recipe; they are never carried over, always rebuilt from the
build's own files, and byte-identical for identical inputs (one DuckDB thread, every aggregate
ordered):

| Table | Built from | One row per |
|---|---|---|
| `produtos` | `alimentos`, `alimentos_resultado`, `saneantes`, `medicamentos`, `produtos_saude` | product: alimentos grouped by `co_seq_produto` (first regularization, apresentação count, the list of `registros`), saneantes one per `nu_expediente`; one schema (`area`, `id`, `nome`, `marcas`, `empresa`, `cnpj`, `processo`, `registro`, `ativo`, `tipo`, `categoria`, `desde`, `vencimento`, `apresentacoes`, `registros`), plus what the label declares, from `alimentos_resultado` across the product's apresentações: `gluten` and `lactose` (Sim, Não, Varia), the sorted lists `contem` (Contém and Contém derivado de), `pode_conter` and `nao_contem` (an item some apresentação contains leaves the other two), and `alergia_varia`; NULL when no apresentação declares anything (39,278 of 50,522 alimentos) and on saneantes. Sorted by `cnpj`, `area`, `id`. Cosméticos are not in it (1.2M rows): use the search files. |
| `empresas` | `alimentos`, `saneantes`, `cosmeticos`, `medicamentos`, `produtos_saude`, `produtos_irregulares`, `alimentos_resultado` | 14-digit CNPJ that holds a product, was the target of a measure or packs food for another company: commonest spelling of the name, products and active products per area (five areas) and in total (cosméticos by distinct processo, medicamentos by 9-digit registro), `medidas` (dossiês against it), `medida_ultima`, and `envasa_para` / `envasado_por`, the other CNPJs it packs food for and that pack its food (`alimentos_fabricacao` pairs with `vinculo` grupo or terceira; a packer with no product of its own has `produtos` 0 and the name the packer list gives it); sorted by `cnpj`. |
| `medidas` | `produtos_irregulares` | dossiê × produto (the source has one row per dossiê × ação × atividade × produto): `area`/`area_codigo`, `risco`, `processo` (the measure's), `cnpj` only when the target is a 14-digit CNPJ, `empresa`, `pessoa` (target is a CPF: no number, no name), sorted lists `registros`, `acoes`, `atividades`, `primeira`/`ultima` publication, `autor_cnpj`/`autor` (who filed); sorted by `area_codigo`, `dossie`, `produto`. 14,092 rows, 0.4 MB on 2026-10-12. |
| `cosmeticos_por_empresa` | `cosmeticos` | the first 100 cosmetics of each CNPJ (`ordem` 1 to 100: active first, then name, then processo), one row per processo (the unregistered row of a duplicate pair, as `empresas` counts them), with `produtos`-like columns (`id`, `nome`, `empresa`, `processo`, `registro`, `ativo`, `tipo` normalized to Registrado, Notificado, Isento de registro, Descartável, `vencimento`); totals per company are in `empresas`. 281,220 rows, 7.6 MB on 2026-10-12; sorted by `cnpj`, `ordem`. |
| `alimentos_por_produto` | `alimentos`, `alimentos_resultado` | the alimentos rows of `produtos`, same columns and values, plus the petição's `situacao_assunto` and `alegacao` (constant within a product) and three timestamps: `publicacao` and `situacao_em` (latest among the apresentações), `inicio_analise` (earliest). A list of foods without the per-apresentação table. 50,522 rows, 2.8 MB on 2026-10-12; sorted by `cnpj`, `id`. |
| `medicamentos_por_produto` | `medicamentos`, `cmed` | one row per medicine (the 9-digit `registro_produto`, or a stable `sem-registro-<hash>` id when the source gives none): the `produtos` columns plus `principio_ativo`, `classe_terapeutica`, `categoria_regulatoria`, `processos` and `linhas` (how many the source repeats) and `eans`, the distinct EAN 1 of its CMED presentations (empty when it is not in the CMED); `ativo` when any source row is Ativo; joins `cmed` on `registro_produto`. 36,427 rows, 1.0 MB on 2026-10-13; sorted by `cnpj`, `id`. |
| `produtos_saude_por_produto` | `produtos_saude` | one row per registro or cadastro of a medical device: `nome_tecnico`, `classe_risco`, `tipo` (Registrado or Cadastrado), `ativo` (VIGENTE, or a validity not past on the file's own date), `publicacao`, `vencimento`, the sorted `fabricantes` list as "nome (país)" (`[]` when the source names none) and, index by index, `fabricantes_chaves` and `fabricantes_paises`, the key of each manufacturer in `produtos_saude_fabricacao`. 100,681 rows, 3.5 MB; sorted by `cnpj`, `id`. |
| `alimentos_fabricacao` | `alimentos`, `alimentos_resultado` | holder CNPJ × manufacturer of its food, read from the apresentações' `empresas_envasadoras` (Brazilian packer, keyed by `fabricante_cnpj`; name, `cidade` and `pais` read from the right, since names contain " - ") and `empresas_internacionais` (foreign manufacturer, keyed by its ANVISA `fabricante_codigo`, or by name when the code is empty); `vinculo` is `propria` (same CNPJ), `grupo` (same 8-digit root), `terceira` or `exterior`; `produtos`, `produtos_ativos` and `apresentacoes` naming it. Apresentações without an `alimentos` row are dropped. 1,155 CNPJ pairs (575 propria, 86 grupo, 494 terceira) and 327 foreign ones, 1,482 rows, 54 KB on 2026-10-22; sorted by `cnpj`, `fabricante_cnpj`, `fabricante_codigo`, `fabricante` (NULLs last). |
| `certificados` | `cbpf`, `cbpf_medicamentos` | CBPF/CBPDA petição of either file (copies collapsed, the medicines' lines gathered in the sorted list `linhas`): solicitante `cnpj` and `empresa`, the plant (`inspecionada_cnpj` in Brazil, `inspecionada_codigo` abroad, `inspecionada`, `pais`), `tipo` (CBPF, CBPDA), `area` (`produtos_saude` incl. MDSAP, `insumos`, `medicamentos`, `cosmeticos`, `saneantes`, `alimentos`, or `outros` for a family not seen yet), `assunto`, `emissao`, `situacao` (synthesised from `status` for medicines), `publicacao`, `validade`, `vigente` (decided, not cancelled, `validade >= dados_de`), `cancelado`, `vinculo` (`propria`, `grupo` by 8-digit root, `terceira`, `exterior`, `desconhecida`), `processo`, `dados_de` (the source file's load date: **2025-09-08 for medicines**). 35,610 rows (21,959 + 13,651), 3,963 + 2,484 vigentes, 0.9 MB on the files of 2026-10-13 and 2025-09-08; sorted by `cnpj`, `id`. |
| `produtos_saude_fabricacao` | `produtos_saude` | holder CNPJ × device manufacturer, the manufacturer keyed by `chave` (its name lowercased, accents stripped, only `[a-z0-9]`; the rule is `fixtures/dados/chaves.json`, shared with the site) and `pais`, never merged across countries; `fabricante` is the commonest spelling of that key; `vinculo` `propria` (the holder's own name, in Brazil), `terceira` or `exterior`; `registros`, `registros_ativos`, `linhas`. 16,725 rows (14,531 exterior, 2,064 propria, 130 terceira) for 11,333 manufacturers on 2026-10-13; sorted by `cnpj`, `chave`, `pais`. |

Column names are ANVISA's, lowercased, so the
[data dictionary](https://dados.anvisa.gov.br/dados/CONSULTAS/PRODUTOS/Documentacao_e_Dicionario_de_Dados_Regularizados_Alimentos.pdf)
applies. Timestamps are naive **Brasília local time**, as ANVISA writes them. What each column
means and what a reader of each table should know is published with the data, in Portuguese:
[`dicionario.html`](https://vasfvitor.github.io/anvisa-api/dicionario.html), generated at build
from `dados/descricoes.py` and each dataset's `notes` in the catalog, and the same text in the
manifest (`columns[].description`, `notes`). This README keeps what a parser needs; that page
keeps what a consumer needs.

What the files don't tell you (verified on the 2026-10-05 files):

| Behavior | Reality |
|---|---|
| Encoding | **Windows-1252**, not Latin-1: `–` `’` `“ ”` `™` are bytes 0x96, 0x92, 0x93/0x94, 0x99. |
| Quoting | `;`-separated, text in `"..."`, but quotes **inside** a value are not escaped (`biscoito tipo "cookies"`; a value ending in `"` is written `...brilhante. ""`), and values contain LF and CRLF. DuckDB's strict reader refuses the file, its lenient one merges records, Python's `csv` splits two. A quote closes a field only before `;` or a line break, and (saneantes: `AMBIENTE "AS MENINAS"` + CRLF CRLF + `SPRAY` inside one name) a line break can only close the record's last field; read that way, every record of every file has the header's field count. |
| Dates | `dd/mm/yyyy HH:MM:SS` in the alimentos files, `peticoes_alimento_andamento` and cosméticos (`29/09/2005`), **`mm/dd/yyyy HH:MM:SS`** in saneantes (`06/21/2031`), `peticoes_alimento`, `produtos_irregulares`, `cbpf` and `cbpf_medicamentos`. One order per dataset in the catalog, never both: given both, `12/08/2026` would quietly parse as whichever comes first. With one, the wrong order leaves every date with a day above 12 unparsed (65% of them in the open file) and the 20% null guard fails the build. Saneantes expiries run to the year 3033. |
| Booleans | `S`/`N` in alimentos (`ST_PRODUTO_ATIVO`, plus one `X`), `1`/`0` in saneantes (`IS_REGISTRADO`); cosméticos has one of each (`ST_SITUACAO_PRODUTO`, `ST_REGISTRADO`). |
| `DT_VENCIMENTO_REGISTRO` | Month and year, **`MMYYYY`** (`122029`), three rows `MM/YYYY`. Published as a DATE on the first of the month. Every notificação carries `122029`, a placeholder; registros expire 5, 10, 15 or 20 years after `DT_REGULARIZACAO`. |
| Text | `MARCAS` and `NO_PRODUTO` carry HTML entities (`L&apos;ANA MED`, `&quot;`, `&#8208;`), as do the irregular products' `PRODUTO`, `PRODUTOS_CONCATENADOS` and `NO_EMPRESA_INVESTIGADA`, and cosméticos' `NO_PRODUTO` (`&AMP;` in capitals, `&#9679;`; `&KISS;` is no entity and stays); decoded on the way. Entities whose `;` the export turned into `-` or `,` (`COM&Eacute-RCIO` and `&#8203-` in `cbpf`, `&#8203,` in `peticoes_alimento`) are not entities any more and are kept as written. Some values end in `\xa0\r\n`; every value is stripped. `ST_PRODUTO_ATIVO` is `S`/`N` plus one `X` (NULL in the BOOLEAN). |
| Server | ETag and Last-Modified on every file; `If-None-Match` answers **304**, so an unchanged day costs one empty response per file. TLS verifies with certifi. |
| `NU_PROCESSO` | Digits only, but **not** a fixed length: 17 on 36,496 rows (all but 90 of the active ones), 13 on 29,647 old ones, **14 on 400** (the length of a CNPJ), a few shorter. Strip punctuation from user input and compare as text. |
| Registration numbers | `NU_REGISTRO_PRODUTO` always equals `NU_REGISTRO_NOTIFICACAO_PRODUTO` when present. For notificações the latter equals `NU_PROCESSO`; for registros it is the 9-digit registro. `NU_REGISTRO` is per apresentação: registro + 4-digit suffix (13 digits, like an old processo). |
| Products | Within one `CO_SEQ_PRODUTO`, processo, CNPJ, situação, name and brands never differ: it is a clean grouping unit (50,474 products, up to 64 apresentações each, p99 = 8). `alimentos_resultado.CO_PRODUTO` equals it on every joined row. |
| Detail coverage | 94 apresentações have no `alimentos_resultado` row, all active, regularized 2025-06 to 2026-10: ANVISA's detail export lags new notificações. |
| Petition files | The two are **disjoint** (no expediente in both): finalized at least once, or never. The finalized file's header starts with `#` (`#NUM_EXPEDIENTE_PETICAO`), dropped on read. **Neither names the company**: join `num_processo_peticao` to `alimentos.nu_processo`, which finds 72% of the finalized file's processos, 163 of the 172 open petições of type Petição, and **none** of the 164 open of type Processo (new registration and evaluation requests: the product does not exist yet). Every open petição also has one `Todos` row (`ordem_ocorre_grupo_etapa_asc` = 0, no end date, starting the day its first stage did): leave it out when adding up stage durations. One assunto ends in `&#8203,`, an entity whose `;` the export turned into `,`; kept as written, and `cod_assunto_peticao` maps one to one to the text. |
| Irregular products | **`nu_cnpj` is who filed the dossiê, not the company acted against**: ANVISA itself (`03112386000111`) on 1,698 of 4,916 dossiês, a marketplace on some, the company itself on others (4,636 of the 8,743 Alimento rows carry the same CNPJ in both columns). The company is `nu_cnpj_empresa_investigada`, which is not always a CNPJ: 14 digits on 35,029 rows, a CPF's 11 on 2,334, text on most of the rest (`DESCONHECIDO`, `Desconhecido`, `Não se aplica`, `NA`…), empty on 31,658. Only 1,667 of the 8,693 Alimento rows that name one join `alimentos.nu_cnpj_empresa`, and only 8 of 1,580 product names match a regularized one: most food measures target products and companies with no registration. `registro` is empty on every Alimento row; `produtos_concatenados` is cut at 4,000 characters (use `produto`); 38 rows equal another once a trailing space is stripped (`HARVONI` / `HARVONI `); `dt_publicacao_medida` is the dossiê's latest `dt_publicacao`. |
| Cosméticos | Verified on the 2026-10-07 file. 11,144 processos are listed twice, with the same CNPJ and `nu_registro`: once with `st_registrado` false and `ds_tipo_peticao` `ISENTO DE REGISTRO` (8,936), `REGISTRO` (2,207) or `DESCARTAVEL` (1), once with true; `ds_tipo_peticao` is empty on exactly the registered rows. Upstream the two sit up to a million records apart; the sort puts them together. Count products with `DISTINCT nu_processo`. `nu_registro` holds one literal `null`. Kept as written: 260 razões sociais that begin with control characters (`\x17\x10\x16\x18VITORIA FACE...`) and 11 names that were UTF-8 read as Windows-1252 before ANVISA exported them (`LOÃ‡ÃƒO`). |
| Completeness | ANVISA's own panel (`consultas.anvisa.gov.br/#/alimentos/`) shows nothing `alimentos` and `alimentos_resultado` lack: product page and apresentação page compared by hand on 2026-10-06. The other tables were not compared against anything. |

`manifest.json` is the entry point (`schema_version` 1): for each table, its `path` (relative
to the manifest, under `data/<build_id>/`), rows, bytes, sha256, column names, types and
descriptions, `notes` for the reader, sort order, the source file's ETag and ANVISA's load time,
and `nulls_added`, the count of values per typed column that did not parse (the build fails if a
column loses more than 20%). At the top level, `docs` names the generated dictionary page and
`license` says whose the data is (ANVISA's) and the code's (MIT). Data paths are immutable: a new
build gets a new directory and the previous one disappears with the next deploy. A table carried
over from the previous build keeps its file but takes the current code's title, notes and
descriptions.

A table too big to download whole (`cosmeticos`) also carries a `busca` key: the same rows
published once more under `data/<build_id>/<name>/`, re-partitioned into about 3,200 small
Parquet files (most 45 to 75 KB, the largest 2.5 MB) by word of the product name
(`palavras/`), by CNPJ (`empresas/`) and by processo or registro number (`numeros/`), plus
`empresas.parquet` (CNPJ → razão social), `palavras.parquet` (every distinct word with its row
count, for the site's "você quis dizer"; since 2026-10-22) and `indice.json`, which says which file holds
which range. A search downloads the index once and then one or two files. The key gives the
index's path, size and sha256, the file count and the folder's total bytes:

```json
"busca": {"versao": 1, "indice": "data/<build_id>/cosmeticos/indice.json",
          "bytes": 118197, "sha256": "…", "arquivos": 3202, "bytes_total": 240128664}
```

Matching is by **word start**, so the site must split names exactly as the build did:
apostrophes removed, accents stripped, lowercase, split on anything but `[a-z0-9]`, tokens of
two or more characters that are not stopwords, hyphenated compounds also joined
(`ANTI-QUEDA` gives `anti`, `queda`, `antiqueda`). `fixtures/dados/tokens.json` holds 40 cases
that both this package (`anvisa.dados.busca.words`) and the site test against. The layout,
the lookup and the measurements are in `anvisa/dados/busca.py`'s docstring and in the
anvisa-dash repository.

For a frontend on DuckDB-WASM (what the first one, `anvisa-dash`, learned on 2026-10-06 with
`@duckdb/duckdb-wasm` 1.33):

- Fetch `manifest.json` (add `?t=<now>` to bypass the 10-minute Pages cache) and resolve each
  `path` against the manifest URL. A 404 on a data path means a deploy happened mid-session:
  re-read the manifest.
- **Download whole files; do not rely on HTTP Range on GitHub Pages.** The files are kept at a
  few MB each on purpose (`bytes` in the manifest); `cosmeticos`, at 24 MB, has the `busca`
  files instead. Range reads worked in Chrome but: Pages answers `HEAD` + `Range`
  with 200, which breaks duckdb-wasm's `reliableHeadRequests`; Firefox's synchronous XHR with
  `Range` fails with a NetworkError; full responses are gzip-encoded and ranged ones are not; and
  in Chrome a ranged `fetch` **poisons the cache**, so the next plain `fetch` of the same URL
  returns a 1-byte body. The cause (found 2026-10-09): Pages gzips `application/octet-stream`
  when the client accepts gzip and applies the range to the *gzipped* bytes, and Firefox before
  148 does not send `Accept-Encoding: identity` on Range requests (bug 1983387), so it gets
  compressed bytes it cannot decode. `fetch()` each file, check `length === bytes` and that it ends in
  `PAR1` (refetch with `cache: "reload"` otherwise), `registerFileBuffer`, `CREATE VIEW`. After
  that every query costs no network.
- The sort orders and 2,048-row groups still matter for anyone reading over HTTP with native
  DuckDB (`read_parquet('https://...')`): only the **first** sort key prunes, so CNPJ and
  `co_produto` lookups read one row group, a processo lookup scans.
- Return dates as `strftime(...)` text: Arrow temporal units vary with the apache-arrow version.
- To reproduce the Pages code path locally, serve `dist/` with something that does CORS (and
  Range, if you test it); Python's `http.server` does neither.

```sql
-- duckdb, anywhere: the path comes from manifest.json
SELECT no_produto, marcas, situacao_registro
FROM 'https://vasfvitor.github.io/anvisa-api/data/<build_id>/alimentos.parquet'
WHERE nu_cnpj_empresa = '40208221000174';

-- what one company has in análise today, on products it already has (see "Petition files")
SELECT num_processo_peticao, desc_assunto_peticao, desc_situacao_atual_peticao,
       strftime(data_ini_ocorrencia_grp_etapa, '%Y-%m-%d') AS desde
FROM peticoes_alimento_andamento
WHERE desc_grupo_etapa_ciclo_analise = 'Todos'
  AND num_processo_peticao IN (SELECT nu_processo FROM alimentos WHERE nu_cnpj_empresa = ?)
ORDER BY desde;

-- fiscalização measures against one company since 2024 (see "Irregular products")
SELECT ds_acao_fiscalizacao, count(DISTINCT co_seq_dossie_investig_med) AS dossies,
       strftime(max(dt_publicacao), '%Y-%m-%d') AS ultima
FROM produtos_irregulares
WHERE nu_cnpj_empresa_investigada = ? AND dt_publicacao >= DATE '2024-01-01'
GROUP BY 1 ORDER BY 2 DESC;
```

The same build runs locally (no credentials; DuckDB comes with the `dados` extra):

```bash
pip install 'anvisa[dados]'
anvisa dados list
anvisa dados build --out dist                     # dist/manifest.json, index.html, data/<id>/
anvisa dados build --out dist2 --skip-unchanged dist/manifest.json   # 304s → {"skipped": true}
```

## Scope

Covered: every endpoint of the spec, 59 as of 2026-10-22: the 32 it had before 2026-10-01 as
the `fila`, `lista`, `udi`, `nome_tecnico` and `assunto` domains (eight file downloads and
`servicosAssociados` included, wrapped on 2026-09-08), and the 27 ANVISA added on 2026-10-01 as
`saude`, `certificado`, `certificado_medicamento` and `tabaco` (recorded and wrapped on
2026-10-22, 47 requests). From the open data (`anvisa.dados`), twelve tables:
`alimentos`, `alimentos_resultado`, `saneantes`, `peticoes_alimento`,
`peticoes_alimento_andamento`, `produtos_irregulares`, `cosmeticos`, `medicamentos`,
`produtos_saude`, `cmed`, `cbpf` and `cbpf_medicamentos`, plus ten derived ones. The SNGPC and SNCR APIs (separate
services) are out of scope; CMED's list comes from the open data, not from the SAMMED API.

What comes next, and what was ruled out and why, is in [ROADMAP.md](ROADMAP.md).

Not affiliated with ANVISA. MIT.
