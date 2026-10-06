# Roadmap

Where the library stands after 0.4.0 and what is worth doing next, in order. Every item that
touches the API follows the rule in CONTRIBUTING.md: it goes in with a recorded response, not a
guess.

## Status (2026-09-08)

All 32 endpoints of the published OpenAPI document are wrapped. What is left is verification of
claims that come only from ANVISA's examples, ergonomics, and robustness.

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
  set `timestamp_formats` on the entry; the 20% null guard catches a wrong guess.
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
  unless ANVISA allowlists a declared User-Agent. Revisit if the official gateway deploys
  these domains: the `drift` workflow will show them as a diff under `spec/portal/`.
- **SNGPC**, a separate service for pharmacies.
