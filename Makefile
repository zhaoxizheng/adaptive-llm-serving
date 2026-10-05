PYTHON ?= python
CONFIG ?= configs/week01.yaml
WEEK02_CONFIG ?= configs/week02.yaml
WEEK03_CONFIG ?= configs/week03.yaml
WEEK03_SIMULATION_ROOT ?= results/week03-simulation
WEEK03_SMOKE_ROOT ?= results/week03-smoke
WEEK04_CONFIG ?= configs/week04.yaml
VLLM_PYTHON ?= .venv-vllm/bin/python
WEEK05_CONFIG ?= configs/week05.yaml
WEEK06_CONFIG ?= configs/week06.yaml
WEEK07_CONFIG ?= configs/week07.yaml
WEEK08_CONFIG ?= configs/week08.yaml
WEEK09_CONFIG ?= configs/week09.yaml
WEEK10_CONFIG ?= configs/week10.yaml
WEEK11_CONFIG ?= configs/week11-profiler.yaml
WEEK12_CONFIG ?= configs/week12-nsys.yaml
WEEK13_CONFIG ?= configs/week13-prefix.yaml
WEEK14_CONFIG ?= configs/week14-optimization.yaml
WEEK15_CONFIG ?= configs/week15-multireplica.yaml
WEEK16_CONFIG ?= configs/week16-l7-matrix.yaml
WEEK06_PHASE ?= representation

.PHONY: install install-dev check-env prepare-model smoke benchmark report run-week01 verify \
	run-week02 verify-week02 calibrate-week03 simulate-week03 smoke-week03 run-week03 verify-week03 \
	plan-week04 run-week04 analyze-week04 audit-template-week04 verify-week04 test lint \
	plan-week05 run-week05 analyze-week05 plan-week06 run-week06 analyze-week06 \
	plan-week07 run-week07 plan-week08 run-week08 \
	plan-week09 run-week09 plan-week10 run-week10 plan-week11 run-week11 plan-week12 run-week12

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

smoke-week03:
	PYTHON=$(PYTHON) CONFIG=$(WEEK03_CONFIG) WEEK02_CONFIG=$(WEEK02_CONFIG) CALIBRATE=1 PROFILE=smoke BACKEND=hf OUTPUT_ROOT=$(WEEK03_SMOKE_ROOT) RUN_LOG=$(WEEK03_SMOKE_ROOT)/logs/week03.log bash scripts/run_week03.sh

run-week03:
	PYTHON=$(PYTHON) CONFIG=$(WEEK03_CONFIG) WEEK02_CONFIG=$(WEEK02_CONFIG) CALIBRATE=1 PROFILE=primary BACKEND=hf bash scripts/run_week03.sh

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

plan-week05:
	$(PYTHON) -m scripts.run_week05 --config $(WEEK05_CONFIG) --plan

run-week05:
	$(VLLM_PYTHON) -m scripts.run_week05 --config $(WEEK05_CONFIG) --run

analyze-week05:
	$(PYTHON) -m src.analyze_week05

plan-week06:
	$(PYTHON) -m scripts.benchmark_week06 --config $(WEEK06_CONFIG) --phase $(WEEK06_PHASE) --plan

run-week06:
	$(VLLM_PYTHON) -m scripts.benchmark_week06 --config $(WEEK06_CONFIG) --phase $(WEEK06_PHASE) --run

analyze-week06:
	$(PYTHON) -m src.analyze_week06

plan-week07:
	$(PYTHON) -m scripts.run_source_study --config $(WEEK07_CONFIG) --plan

run-week07:
	$(VLLM_PYTHON) -m scripts.run_source_study --config $(WEEK07_CONFIG) --run

plan-week08:
	$(PYTHON) -m scripts.run_source_study --config $(WEEK08_CONFIG) --plan

run-week08:
	$(VLLM_PYTHON) -m scripts.run_source_study --config $(WEEK08_CONFIG) --run

plan-week09 plan-week10 plan-week11 plan-week12:
	$(PYTHON) -m scripts.run_deep_study --config $(WEEK$(patsubst plan-week%,%,$@)_CONFIG) --plan

run-week09 run-week10 run-week11 run-week12:
	$(VLLM_PYTHON) -m scripts.run_deep_study --config $(WEEK$(patsubst run-week%,%,$@)_CONFIG) --run

test:
	$(PYTHON) -m pytest -q

lint:
	$(PYTHON) -m ruff check src scripts tests

.PHONY: plan-week13 plan-week14 plan-week15 plan-week16 run-week13 run-week14

plan-week13:
	$(PYTHON) -m scripts.run_kernel_study --plan
	$(PYTHON) -m scripts.run_serving_experiment --config $(WEEK13_CONFIG) --plan

plan-week14:
	$(PYTHON) -m scripts.run_serving_experiment --config $(WEEK14_CONFIG) --plan
	$(PYTHON) -m scripts.run_serving_experiment --config configs/week14-tp.yaml --plan

plan-week15 plan-week16:
	$(PYTHON) -m scripts.run_cluster_study --config $(WEEK$(patsubst plan-week%,%,$@)_CONFIG) --plan

run-week13 run-week14:
	$(VLLM_PYTHON) -m scripts.run_serving_experiment --config $(WEEK$(patsubst run-week%,%,$@)_CONFIG) --run


# Weeks 17-20: offline plans; runtime operations are explicit script flags.
WEEK17_CONFIG ?= configs/week17-inferencepool.yaml
WEEK18_CONFIG ?= configs/week18-routing.yaml
WEEK19_CONFIG ?= configs/week19-resource-audit.yaml
WEEK20_CONFIG ?= configs/week20-autoscaling.yaml

.PHONY: plan-week17 plan-week18 plan-week19 plan-week20 test-platform

plan-week17 plan-week18 plan-week19 plan-week20:
	$(PYTHON) -m scripts.run_platform_study --config $(WEEK$(patsubst plan-week%,%,$@)_CONFIG) --plan

test-platform:
	$(PYTHON) -m pytest -q tests/test_platform_contract.py tests/test_platform_analysis.py tests/test_platform_runner.py
