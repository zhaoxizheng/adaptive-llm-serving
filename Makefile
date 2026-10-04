PYTHON ?= python
CONFIG ?= configs/week01.yaml
WEEK02_CONFIG ?= configs/week02.yaml
WEEK03_CONFIG ?= configs/week03.yaml
WEEK03_SIMULATION_ROOT ?= results/week03-simulation
WEEK03_SMOKE_ROOT ?= results/week03-smoke
WEEK04_CONFIG ?= configs/week04.yaml
VLLM_PYTHON ?= .venv-vllm/bin/python

.PHONY: install install-dev check-env prepare-model smoke benchmark report run-week01 verify \
	run-week02 verify-week02 calibrate-week03 simulate-week03 smoke-week03 run-week03 verify-week03 \
	plan-week04 run-week04 analyze-week04 audit-template-week04 verify-week04 test lint

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

run-week02:
	PYTHON=$(PYTHON) CONFIG=$(WEEK02_CONFIG) bash scripts/run_week02.sh

verify-week02:
	$(PYTHON) -m scripts.verify_week02 --config $(WEEK02_CONFIG)

calibrate-week03:
	$(PYTHON) -m src.week03_calibration --week3-config $(WEEK03_CONFIG) --week2-config $(WEEK02_CONFIG)

simulate-week03:
	PYTHON=$(PYTHON) CONFIG=$(WEEK03_CONFIG) PROFILE=primary BACKEND=fake OUTPUT_ROOT=$(WEEK03_SIMULATION_ROOT) RUN_LOG=$(WEEK03_SIMULATION_ROOT)/logs/week03.log bash scripts/run_week03.sh

smoke-week03: calibrate-week03
	PYTHON=$(PYTHON) CONFIG=$(WEEK03_CONFIG) PROFILE=smoke BACKEND=hf OUTPUT_ROOT=$(WEEK03_SMOKE_ROOT) RUN_LOG=$(WEEK03_SMOKE_ROOT)/logs/week03.log bash scripts/run_week03.sh

run-week03: calibrate-week03
	PYTHON=$(PYTHON) CONFIG=$(WEEK03_CONFIG) PROFILE=primary BACKEND=hf bash scripts/run_week03.sh

verify-week03:
	$(PYTHON) -m scripts.verify_week03 --config $(WEEK03_CONFIG)

plan-week04:
	CONTRACT_PYTHON=$(PYTHON) CONFIG=$(WEEK04_CONFIG) bash scripts/run_week04.sh --plan

run-week04:
	PYTHON=$(VLLM_PYTHON) VLLM_PYTHON=$(VLLM_PYTHON) CONFIG=$(WEEK04_CONFIG) bash scripts/run_week04.sh --gpu

analyze-week04:
	ANALYSIS_PYTHON=$(PYTHON) CONFIG=$(WEEK04_CONFIG) bash scripts/run_week04.sh --analyze

audit-template-week04:
	ANALYSIS_PYTHON=$(PYTHON) CONFIG=$(WEEK04_CONFIG) bash scripts/run_week04.sh --audit-template

verify-week04:
	ANALYSIS_PYTHON=$(PYTHON) CONFIG=$(WEEK04_CONFIG) bash scripts/run_week04.sh --verify

test:
	$(PYTHON) -m pytest -q

lint:
	$(PYTHON) -m ruff check src scripts tests

