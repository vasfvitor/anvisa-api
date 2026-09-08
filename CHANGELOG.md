# Changelog

## 0.4.0 (unreleased)

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
