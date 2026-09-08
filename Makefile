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

live:            ## 4 real requests; needs credentials
	$(PY) pytest -m live

lint:
	$(PY) ruff check . && $(PY) ruff format --check .
