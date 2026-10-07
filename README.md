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
weekdays, with no CORS headers, so a browser cannot read them; the analysis cycle of every
alimentos petição sits at the root of the [same server](https://dados.anvisa.gov.br/dados/), and
the measures behind the portal's "Consulta de produtos irregulares", for every area, in
`CONSULTAS/EMPRESA_FISCALIZACAO_PRODUTO/`. A daily workflow here
(`.github/workflows/dados.yml`) converts them to typed, sorted Parquet and publishes them on
GitHub Pages, at <https://vasfvitor.github.io/anvisa-api/> once Pages is enabled:

| Table | Source | Rows (file of) | One row per |
|---|---|---|---|
| `alimentos` | `TA_CONSULTA_ALIMENTOS.CSV` | 66,681 (10-05) | apresentação of a registered or notified food product, sorted by `nu_cnpj_empresa`, `nu_processo` |
| `alimentos_resultado` | `TA_CONSULTA_ALIMENTOS_RESULTADO.CSV` | 66,941 (10-05) | apresentação detail (embalagem, tabela nutricional, alergênicos), sorted by `co_produto`, `co_seq_apresentacao_produto`; join on `co_seq_apresentacao_produto`, or fetch a whole product with `co_produto = alimentos.co_seq_produto` |
| `saneantes` | `TA_CONSULTA_SANEANTES.CSV` | 144,384 (10-05) | registered or notified saneante (`is_registrado`), one row per processo; sorted by `nu_cnpj_empresa`, `nu_processo`. No detail file. |
| `peticoes_alimento` | `CICLO_ANALISE_PETICOES_ALIMENTO.CSV` | 69,188 (10-06) | stage (fila, análise, exigência, finalização…) of an alimentos petição finalized at least once: 20,518 petições since 1998, sorted by `num_processo_peticao`, `num_expediente_peticao`, `ordem_ocorre_grupo_etapa_asc` |
| `peticoes_alimento_andamento` | `CICLO_ANALISE_PETICOES_ANDAMENTO_ALIMENTO.CSV` | 1,205 (10-06) | stage of a petição **never finalized**, what is in análise today (336 petições); same columns minus the two finalization dates, same sort |
| `produtos_irregulares` | `TA_CONSULTA_PRODUTOS_IRREGULARES_RESULTADO.CSV` | 79,986 (10-05) | product × ação (suspensão, proibição, recolhimento, apreensão, interdição, inutilização) × atividade of a fiscalização dossiê, every area (Alimento: 8,743 rows, 722 dossiês); sorted by `co_tipo_produto` (6 = Alimento), `nu_cnpj_empresa_investigada`, `co_seq_dossie_investig_med` |

Column names are ANVISA's, lowercased, so the
[data dictionary](https://dados.anvisa.gov.br/dados/CONSULTAS/PRODUTOS/Documentacao_e_Dicionario_de_Dados_Regularizados_Alimentos.pdf)
applies. Timestamps are naive **Brasília local time**, as ANVISA writes them.

What the files don't tell you (verified on the 2026-10-05 files):

| Behavior | Reality |
|---|---|
| Encoding | **Windows-1252**, not Latin-1: `–` `’` `“ ”` `™` are bytes 0x96, 0x92, 0x93/0x94, 0x99. |
| Quoting | `;`-separated, text in `"..."`, but quotes **inside** a value are not escaped (`biscoito tipo "cookies"`; a value ending in `"` is written `...brilhante. ""`), and values contain LF and CRLF. DuckDB's strict reader refuses the file, its lenient one merges records, Python's `csv` splits two. A quote closes a field only before `;` or a line break, and (saneantes: `AMBIENTE "AS MENINAS"` + CRLF CRLF + `SPRAY` inside one name) a line break can only close the record's last field; read that way, every record of every file has the header's field count. |
| Dates | `dd/mm/yyyy HH:MM:SS` in the alimentos files and in `peticoes_alimento_andamento`, **`mm/dd/yyyy HH:MM:SS`** in saneantes (`06/21/2031`), `peticoes_alimento` and `produtos_irregulares`. One order per dataset in the catalog, never both: given both, `12/08/2026` would quietly parse as whichever comes first. With one, the wrong order leaves every date with a day above 12 unparsed (65% of them in the open file) and the 20% null guard fails the build. Saneantes expiries run to the year 3033. |
| Booleans | `S`/`N` in alimentos (`ST_PRODUTO_ATIVO`, plus one `X`), `1`/`0` in saneantes (`IS_REGISTRADO`). |
| `DT_VENCIMENTO_REGISTRO` | Month and year, **`MMYYYY`** (`122029`), three rows `MM/YYYY`. Published as a DATE on the first of the month. Every notificação carries `122029`, a placeholder; registros expire 5, 10, 15 or 20 years after `DT_REGULARIZACAO`. |
| Text | `MARCAS` and `NO_PRODUTO` carry HTML entities (`L&apos;ANA MED`, `&quot;`, `&#8208;`), as do the irregular products' `PRODUTO`, `PRODUTOS_CONCATENADOS` and `NO_EMPRESA_INVESTIGADA`; decoded on the way. Some values end in `\xa0\r\n`; every value is stripped. `ST_PRODUTO_ATIVO` is `S`/`N` plus one `X` (NULL in the BOOLEAN). |
| Server | ETag and Last-Modified on every file; `If-None-Match` answers **304**, so an unchanged day costs two empty responses. TLS verifies with certifi. |
| `NU_PROCESSO` | Digits only, but **not** a fixed length: 17 on 36,496 rows (all but 90 of the active ones), 13 on 29,647 old ones, **14 on 400** (the length of a CNPJ), a few shorter. Strip punctuation from user input and compare as text. |
| Registration numbers | `NU_REGISTRO_PRODUTO` always equals `NU_REGISTRO_NOTIFICACAO_PRODUTO` when present. For notificações the latter equals `NU_PROCESSO`; for registros it is the 9-digit registro. `NU_REGISTRO` is per apresentação: registro + 4-digit suffix (13 digits, like an old processo). |
| Products | Within one `CO_SEQ_PRODUTO`, processo, CNPJ, situação, name and brands never differ: it is a clean grouping unit (50,474 products, up to 64 apresentações each, p99 = 8). `alimentos_resultado.CO_PRODUTO` equals it on every joined row. |
| Detail coverage | 94 apresentações have no `alimentos_resultado` row, all active, regularized 2025-06 to 2026-10: ANVISA's detail export lags new notificações. |
| Petition files | The two are **disjoint** (no expediente in both): finalized at least once, or never. The finalized file's header starts with `#` (`#NUM_EXPEDIENTE_PETICAO`), dropped on read. **Neither names the company**: join `num_processo_peticao` to `alimentos.nu_processo`, which finds 72% of the finalized file's processos, 163 of the 172 open petições of type Petição, and **none** of the 164 open of type Processo (new registration and evaluation requests: the product does not exist yet). Every open petição also has one `Todos` row (`ordem_ocorre_grupo_etapa_asc` = 0, no end date, starting the day its first stage did): leave it out when adding up stage durations. One assunto ends in `&#8203,`, an entity whose `;` the export turned into `,`; kept as written, and `cod_assunto_peticao` maps one to one to the text. |
| Irregular products | **`nu_cnpj` is who filed the dossiê, not the company acted against**: ANVISA itself (`03112386000111`) on 1,698 of 4,916 dossiês, a marketplace on some, the company itself on others (4,636 of the 8,743 Alimento rows carry the same CNPJ in both columns). The company is `nu_cnpj_empresa_investigada`, which is not always a CNPJ: 14 digits on 35,029 rows, a CPF's 11 on 2,334, text on most of the rest (`DESCONHECIDO`, `Desconhecido`, `Não se aplica`, `NA`…), empty on 31,658. Only 1,667 of the 8,693 Alimento rows that name one join `alimentos.nu_cnpj_empresa`, and only 8 of 1,580 product names match a regularized one: most food measures target products and companies with no registration. `registro` is empty on every Alimento row; `produtos_concatenados` is cut at 4,000 characters (use `produto`); 38 rows equal another once a trailing space is stripped (`HARVONI` / `HARVONI `); `dt_publicacao_medida` is the dossiê's latest `dt_publicacao`. |
| Completeness | ANVISA's own panel (`consultas.anvisa.gov.br/#/alimentos/`) shows nothing these two files lack: product page and apresentação page compared by hand on 2026-10-06. |

`manifest.json` is the entry point (`schema_version` 1): for each table, its `path` (relative
to the manifest, under `data/<build_id>/`), rows, bytes, sha256, column names and types, sort
order, the source file's ETag and ANVISA's load time, and `nulls_added`, the count of values per
typed column that did not parse (the build fails if a column loses more than 20%). Data paths
are immutable: a new build gets a new directory and the previous one disappears with the next
deploy.

For a frontend on DuckDB-WASM (what the first one, `anvisa-dash`, learned on 2026-10-06 with
`@duckdb/duckdb-wasm` 1.33):

- Fetch `manifest.json` (add `?t=<now>` to bypass the 10-minute Pages cache) and resolve each
  `path` against the manifest URL. A 404 on a data path means a deploy happened mid-session:
  re-read the manifest.
- **Download whole files; do not rely on HTTP Range on GitHub Pages.** The files are small (3.7,
  3.3, 1.7, 1.2 and 1.1 MB, and 21 KB) on purpose. Range reads worked in Chrome but: Pages answers `HEAD` + `Range`
  with 200, which breaks duckdb-wasm's `reliableHeadRequests`; Firefox's synchronous XHR with
  `Range` fails with a NetworkError; full responses are gzip-encoded and ranged ones are not; and
  in Chrome a ranged `fetch` **poisons the cache**, so the next plain `fetch` of the same URL
  returns a 1-byte body. `fetch()` each file, check `length === bytes` and that it ends in
  `PAR1` (refetch with `cache: "reload"` otherwise), `registerFileBuffer`, `CREATE VIEW`. After
  that every query costs no network.
- The sort orders and 2,048-row groups still matter for anyone reading over HTTP with native
  DuckDB (`read_parquet('https://...')`): only the **first** sort key prunes, so CNPJ and
  `co_produto` lookups read one row group, a processo lookup scans.
- Return dates as `strftime(...)` text: Arrow temporal units vary with the apache-arrow version.
- To reproduce the Pages code path locally, serve `dist/` with something that does CORS (and
  Range, if you test it); Python's `http.server` does neither.

```sql
-- duckdb, anywhere: the path comes from manifest.json
SELECT no_produto, marcas, situacao_registro
FROM 'https://vasfvitor.github.io/anvisa-api/data/<build_id>/alimentos.parquet'
WHERE nu_cnpj_empresa = '40208221000174';

-- what one company has in análise today, on products it already has (see "Petition files")
SELECT num_processo_peticao, desc_assunto_peticao, desc_situacao_atual_peticao,
       strftime(data_ini_ocorrencia_grp_etapa, '%Y-%m-%d') AS desde
FROM peticoes_alimento_andamento
WHERE desc_grupo_etapa_ciclo_analise = 'Todos'
  AND num_processo_peticao IN (SELECT nu_processo FROM alimentos WHERE nu_cnpj_empresa = ?)
ORDER BY desde;

-- fiscalização measures against one company since 2024 (see "Irregular products")
SELECT ds_acao_fiscalizacao, count(DISTINCT co_seq_dossie_investig_med) AS dossies,
       strftime(max(dt_publicacao), '%Y-%m-%d') AS ultima
FROM produtos_irregulares
WHERE nu_cnpj_empresa_investigada = ? AND dt_publicacao >= DATE '2024-01-01'
GROUP BY 1 ORDER BY 2 DESC;
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
`servicosAssociados`, wrapped on 2026-09-08. From the open data, the two alimentos files and
saneantes (`anvisa.dados`). Not yet covered: the 27 endpoints ANVISA added on 2026-10-01 (`saude`,
`certificado`, `certificadoMedicamento`, `tabaco`), recorded in `spec/` by the `drift` workflow
and listed in [ROADMAP.md](ROADMAP.md). The SNGPC, SAMMED and SNCR APIs (separate services) are
out of scope.

What comes next, and what was ruled out and why, is in [ROADMAP.md](ROADMAP.md).

Not affiliated with ANVISA. MIT.
