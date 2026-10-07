# `anvisa`

Open source client for ANVISA's official [Portal de APIs](https://api.anvisa.gov.br/),
specifically the **Consultas Externas** API: fila de análise, UDI de dispositivos médicos, termos GMDN,
nomes técnicos, listas e assuntos de peticionamento.

ANVISA publishes an OpenAPI document for this API, but it is wrong or silent about most of
what you need to call it successfully. This repository records what the API actually does and
ships a client that encodes it.

## What the spec doesn't tell you (all verified live, 2026-09-06 and 2026-09-08)

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
| Downloads | The spec says `200 OK` with no content and nothing about `Accept`. Every download answers **500 "Could not find acceptable representation"** to `Accept: application/json` (all but `GET /udi/{id}/download`); send `Accept: */*`. `Content-Type` is always `application/vnd.ms-excel` even when the bytes are OOXML or a zip. `POST /assunto/downloadAssuntoFormulario` takes a **bare JSON integer** body, not the declared `PaginationBuilder`, and returns the file with **no `Content-Type` and no `Content-Disposition`**. An empty subfila is a **500** here, not the empty 404 `fila/consulta` gives. |
| Filter keys | Verified live: `udi` accepts `nomeComercial` (substring), `udiDi` (exact), `cnpjDetentora`, `codigoGmdn`, `nuRegistro`; `nomeTecnico` accepts `nomeTecnico` (substring) and `categoriaProduto`; `termoGmdn` accepts `conteudo`. `POST /assunto/` is **broken** (500, body not bound). |
| Coverage | The spec had 32 endpoints until 2026-10-01, when ANVISA published **27 more** (produtos saúde, certificados de boas práticas, certificados de medicamentos, tabaco; snapshot of 2026-10-06). Unauthenticated probes answer **401** like the wrapped ones, so they are live; the client does not wrap them yet. The empresa nacional/internacional, dossiê and alimentos pages left the portal menu without ever being served. Alimentos and other product families exist as bulk CSV on [`dados.anvisa.gov.br/dados/CONSULTAS/`](https://dados.anvisa.gov.br/dados/CONSULTAS/) (for example `TA_CONSULTA_PRODUTOS_IRREGULARES_RESULTADO.CSV`, refreshed on weekdays); the alimentos files are republished here as Parquet, see [Dados abertos](#dados-abertos-parquet-on-github-pages). |

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
anvisa assunto servicos-associados 13497  # gov.br services behind a serviço code
anvisa --format json fila consulta 167 | jq length
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
```

```python
from anvisa import Client

with Client.from_env() as anvisa:
    for row in anvisa.fila.consulta(167):
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
uv run pytest -m live     # 5 real requests; the 4 API ones need credentials
make spec && make models  # at the repo root; a non-empty git diff means the overlay drifted
make snapshot             # re-download the spec and portal docs; a diff means ANVISA changed them
```

## Dados abertos: Parquet on GitHub Pages

The Consultas Externas API has no food products. ANVISA publishes them as bulk CSV on
[dados.anvisa.gov.br](https://dados.anvisa.gov.br/dados/CONSULTAS/PRODUTOS/), refreshed on
weekdays, with no CORS headers, so a browser cannot read them. A daily workflow here
(`.github/workflows/dados.yml`) converts them to typed, sorted Parquet and publishes them on
GitHub Pages, at <https://vasfvitor.github.io/anvisa-api/> once Pages is enabled:

| Table | Source | Rows (2026-10-05) | One row per |
|---|---|---|---|
| `alimentos` | `TA_CONSULTA_ALIMENTOS.CSV` | 66,681 | apresentação of a registered or notified food product, sorted by `nu_cnpj_empresa`, `nu_processo` |
| `alimentos_resultado` | `TA_CONSULTA_ALIMENTOS_RESULTADO.CSV` | 66,941 | apresentação detail (embalagem, tabela nutricional, alergênicos), sorted by `co_produto`, `co_seq_apresentacao_produto`; join on `co_seq_apresentacao_produto`, or fetch a whole product with `co_produto = alimentos.co_seq_produto` |

Column names are ANVISA's, lowercased, so the
[data dictionary](https://dados.anvisa.gov.br/dados/CONSULTAS/PRODUTOS/Documentacao_e_Dicionario_de_Dados_Regularizados_Alimentos.pdf)
applies. Timestamps are naive **Brasília local time**, as ANVISA writes them.

What the files don't tell you (verified on the 2026-10-05 files):

| Behavior | Reality |
|---|---|
| Encoding | **Windows-1252**, not Latin-1: `–` `’` `“ ”` `™` are bytes 0x96, 0x92, 0x93/0x94, 0x99. |
| Quoting | `;`-separated, text in `"..."`, but quotes **inside** a value are not escaped (`biscoito tipo "cookies"`; a value ending in `"` is written `...brilhante. ""`), and values contain LF and CRLF. DuckDB's strict reader refuses the file, its lenient one merges records, Python's `csv` splits two. A quote closes a field only before `;` or a line break; read that way, every record has the header's field count. |
| `DT_VENCIMENTO_REGISTRO` | Month and year, **`MMYYYY`** (`122029`), three rows `MM/YYYY`. Published as a DATE on the first of the month. Every notificação carries `122029`, a placeholder; registros expire 5, 10, 15 or 20 years after `DT_REGULARIZACAO`. |
| Text | `MARCAS` and `NO_PRODUTO` carry HTML entities (`L&apos;ANA MED`, `&quot;`, `&#8208;`), decoded on the way. Some values end in `\xa0\r\n`; every value is stripped. `ST_PRODUTO_ATIVO` is `S`/`N` plus one `X` (NULL in the BOOLEAN). |
| Server | ETag and Last-Modified on every file; `If-None-Match` answers **304**, so an unchanged day costs two empty responses. TLS verifies with certifi. |
| `NU_PROCESSO` | Digits only, but **not** a fixed length: 17 on 36,496 rows (all but 90 of the active ones), 13 on 29,647 old ones, **14 on 400** (the length of a CNPJ), a few shorter. Strip punctuation from user input and compare as text. |
| Registration numbers | `NU_REGISTRO_PRODUTO` always equals `NU_REGISTRO_NOTIFICACAO_PRODUTO` when present. For notificações the latter equals `NU_PROCESSO`; for registros it is the 9-digit registro. `NU_REGISTRO` is per apresentação: registro + 4-digit suffix (13 digits, like an old processo). |
| Products | Within one `CO_SEQ_PRODUTO`, processo, CNPJ, situação, name and brands never differ: it is a clean grouping unit (50,474 products, up to 64 apresentações each, p99 = 8). `alimentos_resultado.CO_PRODUTO` equals it on every joined row. |
| Detail coverage | 94 apresentações have no `alimentos_resultado` row, all active, regularized 2025-06 to 2026-10: ANVISA's detail export lags new notificações. |

`manifest.json` is the entry point (`schema_version` 1): for each table, its `path` (relative
to the manifest, under `data/<build_id>/`), rows, bytes, sha256, column names and types, sort
order, the source file's ETag and ANVISA's load time, and `nulls_added`, the count of values per
typed column that did not parse (the build fails if a column loses more than 20%). Data paths
are immutable: a new build gets a new directory and the previous one disappears with the next
deploy.

For a frontend on DuckDB-WASM (measured with `@duckdb/duckdb-wasm` 1.33 in Chrome, 2026-10-06):

- Fetch `manifest.json` (add `?t=<now>` to bypass the 10-minute Pages cache) and resolve each
  `path` against the manifest URL. A 404 on a data path means a deploy happened mid-session:
  re-read the manifest.
- Range reads need `db.open({filesystem: {reliableHeadRequests: true, allowFullHTTPReads: false,
  forceFullHTTPReads: false}})`; with the defaults the worker downloads the whole file on open.
- Only the **first** sort key prunes. `WHERE nu_cnpj_empresa = ?` reads the footer plus one row
  group (~650 KB); `WHERE nu_processo = ?` scans the file. `WHERE co_produto = ?` on
  `alimentos_resultado` prunes to one ~100 KB group. Use literals or VARCHAR parameters: an
  integer `BETWEEN ? AND ?` did not prune.
- Ranged responses are not reused by the browser cache, so a name search over Range costs more
  than the file (5.3 MB seen for a 3.3 MB file). For text search, `fetch()` the whole file once
  (immutable path, so it caches), `registerFileBuffer`, and query the buffer: every search after
  that costs nothing. Keep Range for the lookups that prune.
- Return dates as `strftime(...)` text: Arrow temporal units vary with the apache-arrow version.
- Python's `http.server` has neither Range nor CORS, so it does not reproduce the Pages code
  path locally; serve `dist/` with something that does both.

```sql
-- duckdb, anywhere: the path comes from manifest.json
SELECT no_produto, marcas, situacao_registro
FROM 'https://vasfvitor.github.io/anvisa-api/data/<build_id>/alimentos.parquet'
WHERE nu_cnpj_empresa = '40208221000174';
```

The same build runs locally (no credentials; DuckDB comes with the `dados` extra):

```bash
pip install 'anvisa[dados]'
anvisa dados list
anvisa dados build --out dist                     # dist/manifest.json, index.html, data/<id>/
anvisa dados build --out dist2 --skip-unchanged dist/manifest.json   # 304s → {"skipped": true}
```

## Scope

Covered: the 32 endpoints the spec had before 2026-10-01, as the `fila`, `lista`, `udi`,
`nome_tecnico`, and `assunto` domains. That includes the eight file downloads and
`servicosAssociados`, wrapped on 2026-09-08. From the open data, the two alimentos files
(`anvisa.dados`). Not yet covered: the 27 endpoints ANVISA added on 2026-10-01 (`saude`,
`certificado`, `certificadoMedicamento`, `tabaco`), recorded in `spec/` by the `drift` workflow
and listed in [ROADMAP.md](ROADMAP.md). The SNGPC, SAMMED and SNCR APIs (separate services) are
out of scope.

What comes next, and what was ruled out and why, is in [ROADMAP.md](ROADMAP.md).

Not affiliated with ANVISA. MIT.
