PYTHON ?= python
CONFIG ?= configs/week01.yaml

.PHONY: install install-dev check-env prepare-model smoke benchmark report run-week01 verify test lint

install:
	$(PYTHON) -m pip install -r requirements.txt

install-dev:
	$(PYTHON) -m pip install -r requirements-dev.txt

check-env:
	$(PYTHON) -m scripts.check_env --config $(CONFIG)

prepare-model:
	$(PYTHON) -m scripts.prepare_week01_model --config $(CONFIG)

smoke:
	$(PYTHON) -m src.generate --config $(CONFIG) --output-tokens 32

benchmark:
	$(PYTHON) -m src.benchmark_kv_cache --config $(CONFIG)

report:
	$(PYTHON) -m src.analyze_week01 --config $(CONFIG)

run-week01:
	PYTHON=$(PYTHON) CONFIG=$(CONFIG) bash scripts/run_week01.sh

verify:
	$(PYTHON) -m scripts.verify_week01 --config $(CONFIG)

test:
	$(PYTHON) -m pytest -q

lint:
	$(PYTHON) -m ruff check src scripts tests

