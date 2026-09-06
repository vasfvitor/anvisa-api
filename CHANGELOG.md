# Changelog

## Unreleased

- `client.assunto`: petition subject codes (`lista`, `detalhe`) and their catalogs, from
  responses recorded on 2026-09-06.
- CI (pytest on Python 3.10–3.13, ruff, spec/model drift check) and a tag-driven PyPI
  release workflow using trusted publishing.

## 0.1.0 — 2026-09-06

First release. Python client and CLI for the `fila` (queue of analysis) and `udi` (medical
device identification) domains of ANVISA's Consultas Externas API, built on ANVISA's OpenAPI
document plus an overlay recording the behavior observed live:

- Cloudflare rejects curl's default User-Agent; a descriptive one is sent.
- OAuth2 client-credentials tokens last 1740 s with no refresh token; renewed before expiry
  and once after a 401.
- Rate limit is a per-client token bucket (burst 25, refill 1/s) visible only in headers;
  the client mirrors it and sleeps only when about to run dry.
- Request pages are 1-based, response pages 0-based.
- `POST /udi` requires at least one filter; `POST /fila/consulta` requires `filter.subfila`
  and returns the whole subqueue, ignoring pagination.
- Validation failures arrive as HTTP 500 and are mapped to typed exceptions by message.
- Dates are epoch milliseconds.
