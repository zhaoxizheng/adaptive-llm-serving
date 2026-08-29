PYTHON ?= python
CONFIG ?= configs/week01.yaml

.PHONY: install install-dev check-env smoke benchmark report test lint

install:
	$(PYTHON) -m pip install -r requirements.txt

install-dev:
	$(PYTHON) -m pip install -r requirements-dev.txt

check-env:
	$(PYTHON) -m scripts.check_env --output results/week01/environment.json

smoke:
	$(PYTHON) -m src.generate --config $(CONFIG) --output-tokens 32

benchmark:
	$(PYTHON) -m src.benchmark_kv_cache --config $(CONFIG)

report:
	$(PYTHON) -m src.analyze_week01 --input results/week01/raw/kv_cache.csv --output-dir results/week01/figures

test:
	$(PYTHON) -m pytest -q

lint:
	$(PYTHON) -m ruff check src scripts tests

