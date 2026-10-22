# Changelog

## 0.7.0 (2026-10-22)

- **`medicamentos` and `produtos_saude` datasets** (`DADOS_ABERTOS_MEDICAMENTOS.csv`,
  `TA_PRODUTO_SAUDE_SITE.csv`) and the derived `medicamentos_por_produto` and
  `produtos_saude_por_produto`; `produtos` and `empresas` cover five areas. `Dataset.quoted`
  (default true) tells `parse.normalize` to read a file that quotes nothing line by line, with
  `"` as an ordinary character (`parse.unquoted`): TA_PRODUTO_SAUDE_SITE is the first such file.
  Details in `CHANGELOG.dados.md`.
- **`anvisa.dados.cmed`, the CMED price list**: ANVISA republishes CMED's monthly list of
  maximum medicine prices as two CSVs on the open-data server (`TA_PRECO_MEDICAMENTO.csv`,
  PF + PMC; `TA_PRECO_MEDICAMENTO_GOV.csv`, PF + PMVG; UTF-8 with BOM, `;`, no quoting, one
  row per presentation keyed by the 15-digit GGREM). The module reads both, merges them by
  GGREM, turns prices into integer centavos and flags into booleans, and writes one normalized
  CSV; it has no DuckDB dependency, so `anvisa-feeds` can snapshot the list with it. In the
  pipeline this is the new `cmed` table (group `medicamentos`, 102 columns, 26,635 rows on
  2026-10-09; see `CHANGELOG.dados.md`). `Dataset` gained `kind` (`csv` or `cmed`) and
  `source_names` (the source column behind each output column, for the dictionary); `Source`
  gained `stamp` and `files` for a table built from more than one file; `fetch.download_url`
  is the conditional GET `download` always was, now callable by URL. Recorded on 2026-10-22
  (2 files, 2 fixtures of 22 records each). The xlsx gov.br advertises is the same cut, but
  for two rows corrected there two hours later.
- `models.py` regenerated as `make models` writes it (one line wrapped), so the `spec-drift`
  CI job is green again.

## 0.6.0 (2026-10-22)

- **The four domains ANVISA added on 2026-10-01 are wrapped**: `saude` (produtos para saúde:
  `search`, `iter_search`, `get`, `download`, `download_pdf`), `certificado` and
  `certificado_medicamento` (`search`, `iter_search`, `get`, `status`, `concedida_por`,
  `download`; the medicamentos one also `linhas_certificacao`, `formas_farmaceuticas`,
  `classes_certificacao`) and `tabaco` (`search`, `iter_search`, `get`, `tipos`, `situacoes`,
  `origens`, `destinos`, `categorias_aditivo`, `download`, `download_pdf`, `download_rotulo`),
  with the matching `anvisa saude|certificado|certificado-medicamento|tabaco` commands. Every
  one of the 59 operations of the spec is now called; `test_spec` asserts it. Recorded on
  2026-10-22 (47 requests, 38 fixtures). What the spec got wrong, now in overlay actions 29 to
  39: the certificado searches need one filter key besides `contexto` (HTTP 500
  `mensagens.MSG-013`, raised locally as `MissingFilterError` and mapped when it comes from
  the API) and answer an empty 404 when nothing matches; `ChaveValorObject.id` is a string or
  null, not an object; `GET /tabaco/detailTabaco/{id}` takes `codDocumento`, not the processo
  number; `POST /tabaco/tabacos` ignores `cnpjEmpresa`; `GET /tabaco/download` takes its
  `PaginationBuilder` as JSON in the query string (`filtro=`), ignores it, and answers 200 for
  the declared 201; the certificado exports are OOXML named `.xls`; the tabaco label is a PDF/A
  sent as `application/force-download`; `POST /saude/{numeroProcesso}` works with an empty
  body. `Client.get_bytes` takes `params`.
- **The published data has its own surface.** `CHANGELOG.dados.md` (repo root) logs the data
  product by build; the Pages site gains a generated `dicionario.html` (notes and columns of
  every table, from the manifest alone), and the manifest gains top-level `docs` and `license`
  keys, a Portuguese `description` on every column and `notes` on every table. The text lives
  in `dados/descricoes.py` (119 columns) and in each `Dataset.notes`;
  `test_descriptions_cover_every_column` is the gate. A carried-over table takes the current
  text (`build.refresh_metadata`). `schema_version` stays 1.
- **Derived tables** (`anvisa.dados.derivar`, `Derivada`, `DERIVADAS`): Parquet built on every
  build from the build's own converted tables, never carried over, byte-identical for
  identical inputs (one thread, every aggregate ordered). `produtos` (one row per alimento or
  saneante, one schema; 194,935 rows, 6.0 MB on 2026-10-11) and `empresas` (one row per
  14-digit CNPJ holding a product or targeted by a measure; 16,923 rows, 450 KB) take 0.3 s
  each; `medidas` (one row per dossiê × produto of produtos_irregulares, lists of ações and
  atividades, the target's CNPJ only when it is one; 14,092 rows, 426 KB) takes 0.1 s. `produtos` also carries the label's allergen declaration
  (`gluten`, `lactose`, `contem`, `pode_conter`, `nao_contem`, `alergia_varia`) from
  `alimentos_resultado`. Saneantes listed twice (Registrado and Notificado) are one row in
  `produtos` and count once in `empresas`. `cosmeticos_por_empresa` (the first 100 cosmetics of
  each CNPJ, 281,220 rows, 7.6 MB, 1.8 s) is the fourth derived table, `alimentos_por_produto` (the alimentos rows of
  `produtos` plus the petição's status, claim and dates; 50,522 rows, 2.8 MB) the fifth. Manifest entries carry `fontes` and a `source.url` pointing at `derivar.py` at the
  build's commit; `anvisa dados list` lists them with their sources; `-d` cannot select them,
  and a full build that lacks a source fails instead of publishing without it. Sums were
  checked against the raw tables and the cosméticos pair rule against a hand count.
- **Search files** (`anvisa.dados.busca`, format version 1): a dataset with `busca=True` in
  the catalog (cosméticos) is published once more under `data/<build_id>/<name>/`, its rows
  re-partitioned into about 3,200 small Parquet files by word of the product name, by CNPJ and
  by processo or registro number, with `empresas.parquet` and an `indice.json` that maps
  ranges to files. The table's manifest entry gains a `busca` key (index path, size, sha256,
  file count, total bytes); `schema_version` stays 1. A browser that downloads whole files
  answers a search with one or two downloads instead of the 24 MB Parquet. The tokenizer
  (`busca.words`) is the contract with anvisa-dash: `fixtures/dados/tokens.json` holds 40 cases
  both sides test against. Partial builds carry the folder over like the Parquet, checking the
  index's hash and every file's size; `index.html` shows the file count. Format and lookup
  designed and measured by the anvisa-dash session (2026-10-09); this port reproduces its
  reference output byte for byte (3,202 files, 240,128,664 bytes, index sha256 identical) in
  37 s after the conversion, peaking at 2.7 GiB under DuckDB's 3 GB cap; three runs gave the
  same sha256 for every file, and every one of the 26 acceptance queries matched a full scan.
- `convert.connect` is the one place DuckDB is opened for a build (one thread, memory limit,
  spill directory); `busca` shares it.
- `cosmeticos`: `TA_CONSULTA_COSMETICOS.CSV` (228 MB, 1,204,980 rows of 10 fields), every
  notified, exempt or registered cosmetic, sorted by CNPJ, processo and `st_registrado`. Dates
  are day first (unlike saneantes, whose columns it otherwise resembles); 11,144 processos are
  listed twice, once unregistered and once registered, so `st_registrado` completes the sort
  key. `&AMP;` in capitals is decoded like any entity. No parser change. **24.2 MB of
  Parquet**, the first table well above a few MB (hence the search files above).
- `convert.py` caps DuckDB at `MEMORY_LIMIT` (3 GB, below a 7 GB GitHub runner) and lets it
  spill to `.duckdb_tmp` beside the CSV, removed afterwards. Converting cosméticos peaks at
  ~700 MiB and does not spill; the Parquet of the other six datasets is byte-identical
  (sha256, same sources, downloaded 2026-10-08).

## 0.5.0 (2026-10-08)

**Breaking.** One naming rule across the domains (CONTRIBUTING, "Adding a client method"):
ANVISA's nouns stay Portuguese, verbs are English and mean the same everywhere.

| before | after | CLI |
|---|---|---|
| `fila.consulta(subfila_id)` | `fila.query(subfila_id)` | `fila consulta` → `fila query` |
| `lista.consulta(sublista_id)` | `lista.query(sublista_id)` | `lista consulta` → `lista query` |
| `udi.get(id)`, `udi.download(id)` | parameter `dispositivo_id` | unchanged |
| `udi.termos_gmdn(...)` | `udi.search_gmdn(...)` | `udi gmdn-search` → `udi search-gmdn` |
| `udi.termo_gmdn(codigo)` | `udi.get_gmdn(codigo)` | `udi gmdn` → `udi get-gmdn` |
| `assunto.lista()` | `assunto.all()` | `assunto lista` → `assunto list` |
| `assunto.detalhe(codigo)` | `assunto.get(codigo)` | `assunto get` (unchanged) |
| `assunto.busca(...)` | `assunto.search(...)` | (no command) |
| `Client.from_env()` | `Client()` | |
| `Client.stream_to(...)` | `Client.get_stream(...)` | |

- `Download.save("exports/")` and `udi.download_snapshot(..., "exports/")` created a *file*
  named `exports` when the directory did not exist yet (`Path` drops the trailing slash before
  `is_dir()` is asked). `download.target_path` now treats a trailing separator as a directory,
  as the docstring and README always said. Found in a code review.
- Docs caught up with the code: README scope names the six open-data tables and the two sibling
  repositories (`anvisa-feeds`, `anvisa-dash`); the live-test and snapshot request counts are
  the real ones (6 and ~20); the overlay's info block and the package description name every
  domain; the `build.py`/`convert.py` comments no longer justify the layout with HTTP Range on
  Pages, which the README says not to use.
- `spec/snapshot.py` removes portal pages that left the menu, so `spec/portal/` is always the
  current site and the drift check sees a page disappear. Five pages that left it between
  2026-09-06 and 2026-10-01 (consulta-alimentos, dossie-doc, empresa-internacional-doc,
  funcionamento-empresa-internacional, funcionamento-empresa-nacional) are removed now. The
  docstring catches up: the OpenAPI document carries certificados, produtos de saúde and
  tabaco since 2026-10-01, and a snapshot is ~20 requests, not 12.
- `tests/test_spec.py` holds the client against the resolved spec: every path the client calls
  is in it, the 27 it does not call are exactly the tabaco/saude/certificado domains ANVISA
  added on 2026-10-01 (wrapping one, or ANVISA adding one, fails the test), `x-accept: */*` is
  exactly the set of byte-returning methods, and every `x-required-filters` rule is either
  refused before a request (`anyOf`) or present in the body sent (`allOf`). Until now nothing
  read the overlay's `x-*` keys.
- The generated models are used where the client used to hand-build the same shapes:
  `page_body` is `models.PaginationBuilder` (so `sorting` values must be `ASC`/`DESC` before the
  request goes out) and `raise_for_response` reads `models.ErroApi`. For that the overlay
  corrects `PaginationBuilder.filter`: ANVISA's document types every value as an object, the
  recorded requests send strings and numbers. The version has one source, `anvisa.__version__`
  (hatch reads it; `pyproject.toml` no longer repeats it). `Client.from_env()` is gone:
  `Client()` already reads the environment when given no credentials.
- `anvisa dados build -d <table> --skip-unchanged <manifest>` carries the other published
  tables over into the new build (fetched from the live site, checked against the published
  size and SHA-256, listed under the new `data/<build_id>/` path), so a partial build no longer
  publishes a manifest with one table and takes the rest offline. Without a published manifest
  the manifest still lists only the tables built. A published manifest that cannot be read is
  now logged (URL, status) instead of silently turning into a full rebuild.
- `iterate_pages` counts pages itself instead of trusting the response's `number`: a page with
  `number: null` and `last: false` would have fetched page 2 forever.
- One request path in `Client` (`_send`: throttle, send, learn the bucket, raise) instead of
  three copies, and one stream-to-disk helper (`download.write_stream`: `.part` file, SHA-256 on
  the way, rename when complete) shared by `udi.download_snapshot` and the open-data downloads;
  the snapshot used to leave a partial file behind on failure. `Client.stream_to` is gone
  (use `get_stream`), `Client.post` takes any JSON body.
- `parse.normalize` streams the source in 4 MiB chunks (`parse.stream`) instead of decoding
  the whole file: a record that does not end inside a chunk is parsed again with the next one,
  and the end-of-text rule applies only to the last chunk, so a chunk boundary cannot change a
  reading. The Parquet output of all six datasets is byte-identical (sha256, same sources).
  Converting `produtos_irregulares` (98 MB) peaks at 414 MiB instead of 464, the parser itself
  at 75 MiB; what remains is DuckDB's. Groundwork for cosméticos (228 MB) and AFE (314 MB).
- Downloads land in the work directory as `<dataset>.csv` (`Dataset.local_file`) instead of
  the source file name, which ANVISA reuses across folders; `Dataset` rejects a `directory`
  that does not end in `/`; `peticoes_alimento_andamento` spells out its own columns so a
  header drift in one petition file is fixed in its own entry. From a code review.
- `produtos_irregulares`: `TA_CONSULTA_PRODUTOS_IRREGULARES_RESULTADO.CSV` (79,986 rows, every
  area, 1.1 MB of Parquet), the fiscalização measures behind the portal's "Consulta de produtos
  irregulares", one row per dossiê × ação × atividade × produto, sorted by area and then the
  company acted against. That company is `nu_cnpj_empresa_investigada`; `nu_cnpj` is who filed
  the dossiê, ANVISA itself on a third of them. No parser change: the existing reader gives every
  record its 23 fields.
- `peticoes_alimento` and `peticoes_alimento_andamento`: the analysis cycle of alimentos
  petições, one row per stage, from `CICLO_ANALISE_PETICOES_ALIMENTO.CSV` (69,188 rows, 1.2 MB of
  Parquet; petições finalized at least once) and `CICLO_ANALISE_PETICOES_ANDAMENTO_ALIMENTO.CSV`
  (1,205 rows; never finalized). The files are disjoint, carry no company (join the processo to
  `alimentos`), and order their dates differently: month first in the finalized file, day first
  in the open one. `Dataset.directory` places a file under `dados/` (these two sit at its
  root), the header check drops a leading `#`, and `Dataset.load_time` may be None.
- `saneantes`: `TA_CONSULTA_SANEANTES.CSV` (144,384 rows, 3.7 MB of Parquet) joins the
  catalog. Dates are month-first there, `IS_REGISTRADO` is `1`/`0`, and the load time comes from
  `DT_ATUALIZACAO` (`Dataset.load_time`). One product name holds `"AS MENINAS"` followed by
  CRLF CRLF, which the alimentos-era rule read as the end of the record; the parser now knows the
  record width (a line break can only close the last field) and, when a quoted field was read
  across a `"` + line break, re-splits that span the width-blind way and keeps that reading if
  it parses, so a truncated record is still reported as one. Every record of all three files
  comes out whole.
- README: on GitHub Pages, download whole Parquet files instead of relying on HTTP Range
  (HEAD+Range answers 200, Firefox's XHR fails, a ranged fetch poisons Chrome's cache); found
  by `anvisa-dash`.
- `alimentos_resultado` is now sorted by `co_produto, co_seq_apresentacao_produto` (was
  `co_seq_apresentacao_produto` alone) in 2,048-row groups (was 8,192, up to 411 KB each): a
  product's apresentações are contiguous and `WHERE co_produto = ?` reads one ~100 KB group over
  HTTP Range. `Dataset.row_group_size` overrides the build default per table; the manifest `sort`
  and `row_group_size` reflect it. Found by the frontend built on these files.
- README: `nu_processo` is not 17 digits on 44% of rows (13 on old ones, 14 on 400, the length
  of a CNPJ); only the first sort key prunes, so a processo lookup scans; registration-number
  relations; 94 active apresentações without detail; DuckDB-WASM settings for Range reads.
- Spec snapshot of 2026-10-06: ANVISA added 27 operations and 33 schemas to the Consultas
  Externas document on 2026-10-01 (`saude`, `certificado`, `certificadoMedicamento`, `tabaco`),
  live behind the token but not wrapped yet; `models.py` gained their schemas. The portal menu
  was reorganized (per-domain doc pages, new SAMMED and SNCR sections) and the empresa,
  dossiê and alimentos pages left it. See ROADMAP.md.
- `anvisa.dados` and `anvisa dados list|build`: ANVISA's open-data alimentos CSVs
  (`TA_CONSULTA_ALIMENTOS.CSV`, `TA_CONSULTA_ALIMENTOS_RESULTADO.CSV`) to typed, sorted Parquet
  plus `manifest.json` (schema version 1) and `index.html`. DuckDB does the conversion and comes
  with the new `dados` extra (`pip install 'anvisa[dados]'`); `import anvisa` does not need it.
  Downloads are conditional on the published ETags (`--skip-unchanged`), and a run where nothing
  changed writes nothing.
- The CSVs are Windows-1252 with unescaped quotes inside quoted fields, which no stock reader
  handles; `anvisa.dados.parse` reads them by the rule that a quote closes a field only before
  `;` or a line break, and every record of both files comes out whole. `DT_VENCIMENTO_REGISTRO`
  is `MMYYYY` and becomes a DATE. Byte-exact samples in `fixtures/dados/`.
- New errors `DadosError` and `SchemaDriftError` (a changed CSV header).
- `.github/workflows/dados.yml`: daily build, deployed to GitHub Pages when something changed.
  CI installs the `dados` extra so the Parquet tests run on every Python version.
- 2026-09-15: `spec/snapshot.py` normalizes the OpenAPI documents before writing them: `responses`
  and `components.schemas` sorted by key, Keycloak `nonce` stripped from the OAuth 2.0 URLs. That
  day's `drift` run went red on exactly that noise. Snapshots, `resolved.json` and `models.py` are
  re-recorded in the new order; no schema changed.

## 0.4.0 (2026-09-08)

The nine endpoints left out of 0.3.0 are wrapped, so all 32 in the published spec are covered.
Eight of them return files, and the spec describes none of that.

- Downloads need `Accept: */*`. With the `Accept: application/json` the client sends by
  default, every one of them but `GET /udi/{id}/download` answers HTTP 500 "Could not find
  acceptable representation" (fixture `err_not_acceptable`). The client now sets `*/*` on
  every download request, and `NotAcceptableError` names the header for anyone building
  requests by hand.
- New methods returning a `Download` (bytes, the name from `Content-Disposition`, the content
  type, and `save(path)`): `fila.download`, `lista.download`, `nome_tecnico.download`,
  `assunto.download`, `assunto.formulario`, `udi.download`, `udi.download_historico`. Plus
  `udi.download_snapshot`, which streams the weekly zip to disk (chunked, no
  `Content-Length`, 232,435 bytes for snapshot 173), and `assunto.servicos_associados`.
- `POST /assunto/downloadAssuntoFormulario` takes a **bare JSON integer**, the formulário id
  from `DetalheAssunto.formularios[].id`, not the `PaginationBuilder` the spec declares: an
  object is a Jackson "Cannot deserialize value of type `java.lang.Long`" 500. Its response
  carries neither `Content-Type` nor `Content-Disposition`, so the client falls back to
  `formulario_<id>` and takes an optional name.
- `filter.codigosAssunto` binds on `POST /assunto/download` (13 KB for `[10013]` against 5 MB
  for the whole catalog) even though `POST /assunto/` still cannot bind its body at all.
  `size` is ignored on `nomeTecnico/download`, as on the paginated siblings.
- Exporting an empty subfila is HTTP 500 "Nenhum resultado encontrado para exportação.", not
  the empty 404 `fila/consulta` gives for the same id; that is now `EmptyExportError`. An
  unknown formulário id is `NoResultError` (`javax.persistence.NoResultException`).
  `GET /udi/{idDispositivo}/{idHistorico}/download` throws a NullPointerException when the
  device is not in the snapshot; it stays a plain `ApiError` and is documented.
- Commands: `anvisa fila download`, `lista download`, `nome-tecnico download`,
  `assunto download`, `assunto formulario`, `assunto servicos-associados`, `udi download`,
  `udi download-historico` and `udi snapshot`. `-o` takes a file or a directory, `-o -`
  writes to stdout, and the saved path goes to stderr so piped output stays clean.

## 0.3.0 (2026-09-06)

Every filter key is now verified live, and an empty subfila no longer raises.

- `fila.consulta` and `lista.consulta` return `[]` for the empty-bodied 404 the API gives a
  subfila with nothing queued (88 of 314 subfilas in a full crawl on 2026-09-06).
- Dossiê probed too (3 requests): not deployed either, seven of the portal-only endpoints
  confirmed as 404. README now points at the bulk CSV exports on dados.anvisa.gov.br for
  those domains.
- Verification session (17 requests, 2026-09-06): every `udi` filter key the command line exposes, both
  `nome_tecnico` filters, the GMDN text search (`conteudo`), the daily UDI snapshots
  (`historicos`) and the four `assunto` catalogs are now recorded as fixtures and tested.
  `POST /assunto/` (`assunto.busca`) answers HTTP 500 with the body unbound and is documented
  as not usable. New command `anvisa udi gmdn-search <texto>`.

## 0.2.0 (2026-09-06)

Every JSON endpoint in the published spec now has a client method and a command; the
drift check watches what the spec leaves out.

- `client.lista` (`areas`, `grupos`, `sublistas`, `consulta`) and `client.nome_tecnico`
  (`search`, `iter_search`, `categorias`), with the commands `anvisa lista` and
  `anvisa nome-tecnico`.
  Recorded live on 2026-09-06 (4 requests): `POST /lista/consulta` requires the filter key
  `subfila`, not `sublista`, and returns the whole sublista unpaginated like `fila/consulta`.
- `spec/portal/` snapshots of the portal's documentation pages plus a twice-monthly `drift` workflow.
  The pages document 35 endpoints missing from the OpenAPI spec; four probed on 2026-09-06
  (`GET /empresa/{cnpj}`, `POST /consulta/saude`, `GET /empresa/tipoEmpresa`,
  `GET /certificado/status`) return a plain 404, so they are documented but not deployed.
- Overlay notes from the research handoff, namely the JWT role `CONSULTASEXTERNAS_LEITURA`, the
  filter-before-page validation order, the portal tutorial's wrong `expires_in`, and a second
  recorded queue (subfila 161, 35 rows) alongside 167 (40) and a 555-row lista: no size cap.
- Rate limit corrected: the bucket is shared per source address with the portal's
  unauthenticated endpoints, not per client id.

## 0.1.0 (2026-09-06)

First release. Python client and command-line tool for the `fila` (queue of analysis), `udi` (medical
device identification) and `assunto` (petition subject codes) domains of ANVISA's Consultas
Externas API, built on ANVISA's OpenAPI document plus an overlay recording the behavior
observed live:

- Cloudflare rejects curl's default User-Agent; a descriptive one is sent.
- OAuth 2.0 client-credentials tokens last 1740 s with no refresh token; renewed before expiry
  and once after a 401.
- Rate limit is a per-client token bucket (burst 25, refill 1/s) visible only in headers;
  the client mirrors it and sleeps only when about to run dry.
- Request pages are 1-based, response pages 0-based.
- `POST /udi` requires at least one filter; `POST /fila/consulta` requires `filter.subfila`
  and returns the whole subqueue, ignoring pagination.
- Validation failures arrive as HTTP 500 and are mapped to typed exceptions by message.
- Dates are epoch milliseconds.
- `anvisa --version`; CI on Python 3.10–3.13 with a spec/model drift check; tag-driven
  PyPI release via trusted publishing.
