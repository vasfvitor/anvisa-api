PY := cd packages/python && uv run

.PHONY: snapshot spec models test live lint

snapshot:        ## re-download ANVISA's spec + portal docs into spec/ (no credentials)
	python3 spec/snapshot.py

spec:            ## apply the overlay -> spec/consultas-externas.resolved.json
	$(PY) python ../../spec/apply_overlay.py ../../spec/consultas-externas.overlay.yaml

models: spec     ## regenerate packages/python/src/anvisa/models.py
	$(PY) python scripts/gen_models.py

test:
	$(PY) pytest

live:            ## 6 real requests (4 need credentials, 1 token, 1 HEAD on dados.anvisa.gov.br)
	$(PY) pytest -m live

lint:
	$(PY) ruff check . && uv run ruff format --check .   # one cd: the shell stays in packages/python
