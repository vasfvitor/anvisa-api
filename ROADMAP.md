# Roadmap

Where the library stands after 0.4.0 and what is worth doing next, in order. Every item that
touches the API follows the rule in CONTRIBUTING.md: it goes in with a recorded response, not a
guess.

## Status (2026-09-08)

All 32 endpoints the OpenAPI document had until 2026-10-01 are wrapped. What is left is
verification of claims that come only from ANVISA's examples, ergonomics, robustness, and the
27 endpoints ANVISA added since.

## New on the gateway (2026-10-01): 27 endpoints, 4 domains

The `drift` run of 2026-10-01 went red because the spec grew from 32 to 59 operations
(snapshot committed 2026-10-06). Unauthenticated probes answer 401, like the wrapped
endpoints, where the undeployed routes used to answer a Spring 404: they are live.

| Domain | Operations | Shape |
|---|---|---|
| `saude` (Produtos Saúde) | 4 | `POST /saude` paginated, `POST /saude/{numeroProcesso}` detail, PDF and Excel export per processo |
| `certificado` (Boas Práticas) | 5 | paginated search, detail by id, `status`, `certificacaoConcedidaPor`, download |
| `certificadoMedicamento` | 8 | same, plus `linhasCertificacao`, `formasFarmaceuticas`, `classesCertificacao` |
| `tabaco` | 10 | `POST /tabaco/tabacos` paginated, detail, `embalagem/{rotulo}`, six lookup lists, Excel and PDF downloads |

Wrapping them follows CONTRIBUTING.md: one recorded response per endpoint before any code, and
the `POST` searches need their filter keys discovered the way `udi` and `nomeTecnico` were.
Budget about 30 to 40 authenticated requests for the lot. The portal also gained two API
families, SAMMED (preços CMED) and SNCR (receitas), saved under `spec/portal/` and out of scope.

## 0.5.0: verify, harden, convenience

Verification session (one token, about 15 requests):

- **Sorting.** `sorting` is documented as a column → ASC/DESC map only because an array is
  rejected. Nobody has confirmed that sorting works or which column names are valid. Same for
  `column`/`order`. Record one sorted page per paginated endpoint and list the valid columns
  in the overlay.
- **Filter keys from ANVISA's examples.** `codigo` on `nomeTecnico`, and the `udi` keys beyond
  the five verified (`nomeComercial`, `udiDi`, `cnpjDetentora`, `codigoGmdn`, `nuRegistro`).
  Confirm each one or drop it from the command line.
- **Two assumed behaviors.** An empty sublista on `lista.download` (assumed to raise
  `EmptyExportError`, observed only on `fila.download`), and the success case of
  `udi.download_historico`, which only has the null-dereference 500 recorded. Find a device
  that is part of a snapshot, record the file.

Code:

- **`py.typed`.** The package is fully typed and does not say so. One empty file.
- **Retry with backoff** for 5xx from the gateway and connection errors, respecting the token
  bucket. Today a 502 or a timeout raises immediately. Keep validation 500s (the typed
  `RequestRejectedError`s) out of the retry path: they are deterministic.
- **Queue crawl.** `fila.iter_all()` walking área → grupo → subfila → queue, yielding rows tagged
  with their subfila, and the same for `lista`. Every script that wants the whole queue writes
  this loop by hand today (314 subfilas, 88 empty, on 2026-09-06).
- **Python 3.14** in the CI matrix.

## 0.6.0: async

`AsyncClient` on `httpx.AsyncClient`, sharing `Throttle`, `TokenAuth` and the domain classes.
Kept out of 0.5.0 because it changes the public shape of the package and deserves its own
release. The synchronous client stays.

## Open data (`anvisa.dados`)

Started 2026-10-06 with the two alimentos files, published daily as Parquet on GitHub Pages. The
TLS concern that kept this out of scope is gone: the chain verifies with httpx 0.28.1 and certifi
2026.07.22 (checked from a workstation and by `pytest -m live -k dados`). Next, each one catalog
entry in `anvisa/dados/catalog.py` plus one fixture (see CONTRIBUTING.md):

- **Saneantes** (`TA_CONSULTA_SANEANTES.CSV`, 25 MB). Its dates are `mm/dd/yyyy` (`06/21/2031`):
  set `timestamp_formats` on the entry; the 20% null guard catches a wrong guess. **Done
  2026-10-06**: `saneantes`, 144,384 rows, 3.7 MB. It took one parser change (a `"` followed by
  a line break inside a product name).
- **Petições de alimentos** (`dados/CICLO_ANALISE_PETICOES_*ALIMENTO.CSV`). **Done
  2026-10-06**: `peticoes_alimento` (finalized, 69,188 stage rows) and
  `peticoes_alimento_andamento` (open, 1,205). Open question: the files name no company, and the
  164 open new-registration processos are not in `alimentos` yet; check whether the Consultas
  Externas `fila` endpoints return a CNPJ for them before building anything on it.
- Two more alimentos files at the root, not added: `DADOS_ABERTOS_ALIMENTO.csv` (9.6 MB, another
  view of the products with `DT_FINALIZACAO_PROCESSO` and `ST_SITUACAO_REGISTRO`) is **UTF-8**,
  not Windows-1252, so it needs a per-dataset encoding first; worth adding only if it holds
  processos `TA_CONSULTA_ALIMENTOS.CSV` lacks. `CONSULTA_SITUCAO_FILA.csv` (2.5 MB) is every
  area × fila × categoria × assunto × situação combination, in text, with no codes.
- **Fiscalização** (`CONSULTAS/EMPRESA_FISCALIZACAO_PRODUTO/`). **Done 2026-10-06**:
  `produtos_irregulares` (98 MB of CSV, 79,986 rows, 1.1 MB of Parquet, 463 MiB peak memory to
  convert). Next in the same folder: `TA_CONSULTA_FUNCIONAMENTO_EMPRESA_NACIONAL.CSV` (AFE, 314
  MB: needs the streaming below, then a profile to decide whether the whole file is small
  enough as Parquet for a browser) and `TA_CONSULTA_CBPF.CSV` (8 MB, few food companies; it too
  has a requester CNPJ beside the inspected one).
- **Cosméticos** (`TA_CONSULTA_COSMETICOS.CSV`, 228 MB). `parse.normalize` holds a whole file in
  memory (fine at 34 MB); stream it before adding this one.
- Medicamentos, produtos para saúde, tabaco, cannabis: same folder, not profiled yet.
- **Keep the previous build live** for a day after a deploy (copy its `data/<id>/` into the new
  artifact), so a browser session open across a deploy never sees a 404. Today it re-reads the
  manifest instead.

## Later, if there is demand

- **Reading the spreadsheets.** Downloads return bytes. An optional extra (`anvisa[xlsx]`, on
  openpyxl) could turn the `.xlsx` exports (fila, lista, udi) into rows. The `.xls` exports
  (nomes técnicos, assuntos) would need xlrd; probably not worth it, the JSON siblings carry
  the same data.
- **Live smoke on a schedule.** The `drift` workflow watches the spec and the portal docs, not
  the API's behavior. A monthly `pytest -m live` in CI needs the credentials as a repository
  secret; decide whether that is acceptable before wiring it.

## Out of scope, by decision

- **The portal backend** (`consultas.anvisa.gov.br/api`: medicamentos, bulário, produtos para
  saúde, empresas, certificados, dossiê). Its routes are known, it needs
  `Referer: https://consultas.anvisa.gov.br/` and `Authorization: Guest`, and Cloudflare
  blocks Python's TLS fingerprint while letting curl through. Getting past that means
  disguising the client from a control the operator put there on purpose. Not doing it
  unless ANVISA allowlists a declared User-Agent. Produtos para saúde and certificados are on
  the official gateway since 2026-10-01 (see above); medicamentos, bulário and empresas are not.
- **SNGPC**, a separate service for pharmacies.
