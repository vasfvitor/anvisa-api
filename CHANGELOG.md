# Changelog

## Unreleased

- `Download.save("exports/")` and `udi.download_snapshot(..., "exports/")` created a *file*
  named `exports` when the directory did not exist yet (`Path` drops the trailing slash before
  `is_dir()` is asked). `download.target_path` now treats a trailing separator as a directory,
  as the docstring and README always said. Found in a code review.
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

- `spec/snapshot.py` normalizes the OpenAPI documents before writing them: `responses` and
  `components.schemas` sorted by key, Keycloak `nonce` stripped from the OAuth 2.0 URLs. The 2026-09-15
  `drift` run went red on exactly that noise. Snapshots, `resolved.json` and `models.py` are
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
