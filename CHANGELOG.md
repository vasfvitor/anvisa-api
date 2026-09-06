# Changelog

## Unreleased

- `spec/portal/` snapshots of the portal's documentation pages plus a twice-monthly `drift` workflow.
  The pages document 35 endpoints missing from the OpenAPI spec; four probed on 2026-09-06
  (`GET /empresa/{cnpj}`, `POST /consulta/saude`, `GET /empresa/tipoEmpresa`,
  `GET /certificado/status`) return a plain 404, so they are documented but not deployed.
- Overlay notes from the research handoff, namely the JWT role `CONSULTASEXTERNAS_LEITURA`, the
  filter-before-page validation order, the portal tutorial's wrong `expires_in`, and a second
  recorded queue (subfila 161, 35 rows) alongside 167 (40) and a 555-row lista: no size cap.
- Rate limit corrected: the bucket is shared per source address with the portal's
  unauthenticated endpoints, not per client id.
- `client.lista` (`areas`, `grupos`, `sublistas`, `consulta`) and `client.nome_tecnico`
  (`search`, `iter_search`, `categorias`), with the commands `anvisa lista` and
  `anvisa nome-tecnico`.
  Recorded live on 2026-09-06 (4 requests): `POST /lista/consulta` requires the filter key
  `subfila`, not `sublista`, and returns the whole sublista unpaginated like `fila/consulta`.

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
