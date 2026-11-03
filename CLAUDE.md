# anvisa

Python client for ANVISA's Consultas Externas API plus `anvisa.dados`, which turns ANVISA's
open-data CSVs into Parquet on GitHub Pages. GitHub repo `vasfvitor/anvisa-api`, PyPI name
`anvisa`. Layout, recording rules and the dataset recipe are in `CONTRIBUTING.md`; what is next
is in `ROADMAP.md`. An untracked `NOTES.local.md` holds the running to-do list when present.

## Rules

- **Recorded response first.** No client method or dataset entry without a fixture cut from a
  real response (`CONTRIBUTING.md`, "Recording a new response" and "Adding an open-data
  dataset"). Verified source quirks live in `README.md` (open-data table) and
  `fixtures/dados/manifest.json`; read them before touching a parser.
- **Go easy on the API.** The gateway's bucket is shared per source address with the portal's
  unauthenticated endpoints: burst 25, refill 1/s, token lasts 29 min. `make snapshot` alone
  spends about 14 requests. Batch a handful of requests per run and state the count.
- **`tests/test_spec.py`** asserts how many operations are wrapped and how many are not, against
  the resolved spec. Wrapping an endpoint means updating that count.
- **Releases**: bump `__version__` (pyproject reads it), update `CHANGELOG.md`, then the
  user pushes a signed tag `vX.Y.Z`; `release.yml` publishes to PyPI by trusted publishing.
  Never push or tag from a session.
- **Before every commit** run `git status --short` and `git add` any `??` file the change
  needs: `git commit -a` never stages new files (0.5.0 shipped without `tests/test_spec.py`).
- Commit on your own when a unit of work is complete (one subject per commit, no attribution
  trailers); do not ask first. Pushing is the user's.
- **`RUNBOOK.md`** (Portuguese, for the user, not for an agent) explains every way a workflow here
  turns red. Adding or changing an alarm (`::error::`, `::warning::`, an `exit 1` or a raise that
  ends a workflow) or an error message means updating its entry in the same commit, quoting the
  message exactly as the log prints it.

## Sibling repos

- `anvisa-feeds` (`~/code/anvisa-feeds`): daily fila snapshots to Atom feeds, pins `anvisa`
  from PyPI. A breaking rename here means a follow-up there.
- `anvisa-dash` (`~/code/anvisa-dash`): DuckDB-WASM frontend over the Pages Parquet. It
  downloads whole files: Pages gzips `application/octet-stream` and applies a Range to the
  gzipped bytes, and Firefox before 148 does not send `Accept-Encoding: identity` on Range
  requests, so ranged reads break there. Keep each Parquet about 15 MB or less, or publish
  `busca` files beside it (`dados/busca.py`; cosméticos does). The tokenizer there is a
  contract with the site: `fixtures/dados/tokens.json` is tested on both sides, and so is
  `fixtures/dados/chaves.json`, the manufacturer key (`derivar.chave_sql`).

## Open data

Daily `.github/workflows/dados.yml` converts the CSVs and deploys `manifest.json` plus Parquet
to GitHub Pages (`https://vasfvitor.github.io/anvisa-api/manifest.json`). Adding a dataset is
mechanical once the real file is profiled; an Opus agent did cosméticos from a single brief, so
delegate the next one the same way. Files above about 200 MB need `parse.stream`; DuckDB's
`memory_limit` and `temp_directory` are set in `convert.py`. The Biblioteca Digital blocks
automated access, so `dcb` is built from the copy in `fontes/dcb/` (`--fontes`), replaced by hand
on each new version (`CONTRIBUTING.md`); do not work around the 403. That list and what derives
from it are CC BY-NC-SA 4.0, stated per table in the manifest. The eleven derived tables
(`produtos`, `empresas`, `medidas`, `cosmeticos_por_empresa`, `alimentos_por_produto`,
`medicamentos_por_produto`, `produtos_saude_por_produto`, `alimentos_fabricacao`,
`certificados`, `produtos_saude_fabricacao`, `substancias_dcb`) live in
`dados/derivar.py` and are rebuilt every build with order-stable
aggregates only; column descriptions and table `notes` are Portuguese, in `dados/descricoes.py` and the
catalog, and feed the manifest and the Pages `dicionario.html`. Data-side changes go in
`CHANGELOG.dados.md`, by build, not only in the package CHANGELOG.
