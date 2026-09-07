# `anvisa`

Open source client for ANVISA's official [Portal de APIs](https://api.anvisa.gov.br/),
specifically the **Consultas Externas** API: fila de análise, UDI de dispositivos médicos, termos GMDN,
nomes técnicos, listas e assuntos de peticionamento.

ANVISA publishes an OpenAPI document for this API, but it is wrong or silent about most of
what you need to call it successfully. This repository records what the API actually does and
ships a client that encodes it.

## What the spec doesn't tell you (all verified live, 2026-09-06)

| Behavior | Reality |
|---|---|
| User-Agent | Cloudflare answers **403** to curl's default UA, even for the spec URL. Send a descriptive one. |
| Auth | OAuth 2.0 client credentials (Keycloak, realm `externo`). Token lasts **1740 s** (the portal tutorial says 300), **no refresh token**, one role `CONSULTASEXTERNAS_LEITURA`. |
| Rate limit | Token bucket, **burst 25, refill 1/s**, reported only in `X-RateLimit-*` headers. Keyed by **source address**, not client id: the unauthenticated portal endpoints on the same gateway drain the same bucket. |
| Pagination | Requests are **1-based** (`page: 0` → error); responses are Spring `Page` objects, **0-based**. |
| Filters | `POST /udi` needs **at least one** `filter` key; `POST /fila/consulta` **and** `POST /lista/consulta` need `filter.subfila` (yes, also for listas). |
| Errors | Validation failures come back as **HTTP 500** with `{status, mensagem, data_hora, mensagem_detalhada}`. |
| `fila/consulta`, `lista/consulta` | Return the **whole** subqueue/sublista as an array (40, 35, 71 and 555 rows observed); `page`/`size` are ignored. A subfila with nothing queued is an empty-bodied **404**, which the client returns as `[]`. |
| Dates | Integer **epoch milliseconds**. |
| Filter keys | Verified live: `udi` accepts `nomeComercial` (substring), `udiDi` (exact), `cnpjDetentora`, `codigoGmdn`, `nuRegistro`; `nomeTecnico` accepts `nomeTecnico` (substring) and `categoriaProduto`; `termoGmdn` accepts `conteudo`. `POST /assunto/` is **broken** (500, body not bound). |
| Coverage | The spec has 32 endpoints. The portal's doc pages describe **35 more** (certificados, empresa nacional/internacional, dossiê, alimentos, produtos de saúde) on the same base path, but all seven probed answer a plain Spring **404**: documented, not deployed. The same datasets exist as bulk CSV on [`dados.anvisa.gov.br/dados/CONSULTAS/`](https://dados.anvisa.gov.br/dados/CONSULTAS/) (for example `TA_CONSULTA_PRODUTOS_IRREGULARES_RESULTADO.CSV`, refreshed on weekdays). |

## Layout

```
spec/       ANVISA's OpenAPI document (as published, only reformatted) + an OpenAPI Overlay
            with the corrections above + the resolved spec. Language-neutral source of truth.
            spec/portal/ holds the portal's own doc pages and menu, fetched by snapshot.py.
fixtures/   Real responses recorded from the API, with a manifest. Shared test fixtures.
packages/python/   The `anvisa` Python library and CLI.
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
anvisa lista areas && anvisa lista grupos 1 && anvisa lista sublistas 921
anvisa lista consulta 2141              # same shape as fila consulta
anvisa udi search --nome cateter --size 5
anvisa udi get 377
anvisa udi gmdn 47852
anvisa udi gmdn-search pacing           # GMDN terms by text (names are in Portuguese)
anvisa nome-tecnico search --size 50    # nomes técnicos with risk class
anvisa nome-tecnico categorias
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
(`MissingFilterError`, `InvalidPageError`, `MalformedRequestError`, `BlockedError`, and
others) instead of a bare 500.

### Development

```bash
cd packages/python && uv sync
uv run pytest             # fixture-only, no network
uv run pytest -m live     # 3 real requests; needs credentials
make spec && make models  # at the repo root; a non-empty git diff means the overlay drifted
make snapshot             # re-download the spec and portal docs; a diff means ANVISA changed them
```

## Scope

Covered: every JSON endpoint in the published spec, as the `fila`, `lista`, `udi`,
`nome_tecnico`, and `assunto` domains. The two XLS/XLSX download endpoints are not wrapped.
The domains the portal documents but the gateway does not serve yet (see the table) are
saved under `spec/portal/`; a workflow re-fetches them twice a month, so the day ANVISA deploys
them shows up as a diff. The SNGPC API (a
separate service for pharmacies) is out of scope.

Not affiliated with ANVISA. MIT.
