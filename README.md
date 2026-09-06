# anvisa

Open-source client for ANVISA's official [Portal de APIs](https://api.anvisa.gov.br/) —
the **Consultas Externas** API: fila de análise, UDI de dispositivos médicos, termos GMDN,
nomes técnicos, listas e assuntos de peticionamento.

ANVISA publishes an OpenAPI document for this API, but it is wrong or silent about most of
what you need to call it successfully. This repository records what the API actually does and
ships a client that encodes it.

## What the spec doesn't tell you (all verified live, 2026-09-06)

| Behavior | Reality |
|---|---|
| User-Agent | Cloudflare answers **403** to curl's default UA, even for the spec URL. Send a descriptive one. |
| Auth | OAuth2 client credentials (Keycloak, realm `externo`). Token lasts **1740 s**, **no refresh token**. |
| Rate limit | Token bucket per client: **burst 25, refill 1/s**, reported only in `X-RateLimit-*` headers. |
| Pagination | Requests are **1-based** (`page: 0` → error); responses are Spring `Page` objects, **0-based**. |
| Filters | `POST /udi` needs **at least one** `filter` key; `POST /fila/consulta` needs `filter.subfila`. |
| Errors | Validation failures come back as **HTTP 500** with `{status, mensagem, data_hora, mensagem_detalhada}`. |
| `fila/consulta` | Returns the **whole** subqueue as an array; `page`/`size` are ignored. |
| Dates | Integer **epoch milliseconds**. |

## Layout

```
spec/       ANVISA's OpenAPI document (untouched) + an OpenAPI Overlay with the corrections
            above + the resolved spec. Language-neutral source of truth.
fixtures/   Real responses recorded from the API, with a manifest. Shared test fixtures.
packages/python/   The `anvisa` Python library and CLI.
notes/      Research log.
```

The overlay follows the [OpenAPI Overlay Specification 1.0](https://spec.openapis.org/overlay/v1.0.0.html),
so any overlay tool can apply it. `spec/apply_overlay.py` is the ~50-line applier used here.
Models are generated from the resolved spec; everything else is hand-written and small.

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
anvisa fila consulta 167                # the queue, in order, with protocol numbers
anvisa udi search --nome cateter --size 5
anvisa udi get 377
anvisa udi gmdn 47852
anvisa assunto lista --busca bioequival   # petition subject codes
anvisa assunto get 10013                  # documents, forms, legal basis, fees by size
anvisa --format json fila consulta 167 | jq length
```

```python
from anvisa import Client

with Client.from_env() as anvisa:
    for row in anvisa.fila.consulta(167):
        print(row.nuOrdem, row.numeroProcessoFormatado, row.dsAssunto)

    page = anvisa.udi.search(nomeComercial="cateter", size=50)
    for device in anvisa.udi.iter_search(nomeComercial="cateter"):   # every page, throttled
        print(device.udiDi, device.nomeComercial)
```

The client sends a User-Agent, caches the token and renews it before expiry, mirrors the
gateway's token bucket so a loop never hits 429, and raises typed exceptions
(`MissingFilterError`, `InvalidPageError`, `MalformedRequestError`, `BlockedError`, ...)
instead of a bare 500.

### Development

```bash
cd packages/python && uv sync
uv run pytest             # fixture-only, no network
uv run pytest -m live     # 3 real requests; needs credentials
make spec && make models  # at the repo root; a non-empty git diff means ANVISA changed the spec
```

## Scope

Covered: the **fila**, **udi** and **assunto** domains. The other tags in the spec (listas,
nomes técnicos, downloads) are generated as models but have no client methods yet. The
SNGPC API (a separate service for pharmacies) is out of scope.

Not affiliated with ANVISA. MIT.
