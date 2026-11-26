# Common tasks. `make` alone lists them.
VENV   := .venv
PY     := $(VENV)/bin/python
PORT   ?= 8765
MODEL  ?= projects/examples/web-backend.yaml
UNTIL  ?= 300
SEED   ?= 1
BASE   ?=

.DEFAULT_GOAL := help
.PHONY: help setup run serve sim test lint format check health clean

help: ## list the targets
	@awk 'BEGIN {FS = ":.*## "} /^[a-z-]+:.*## / {printf "  make %-8s %s\n", $$1, $$2}' $(MAKEFILE_LIST)
	@echo "  variables: PORT=$(PORT) MODEL=$(MODEL) UNTIL=$(UNTIL) SEED=$(SEED) BASE=<rev>"

# marks a finished install; it reinstalls when a requirements file changes
STAMP  := $(VENV)/.installed

$(STAMP): requirements.txt requirements-dev.txt
	test -x $(PY) || python3 -m venv $(VENV)
	$(PY) -m pip install -q -r requirements-dev.txt
	@touch $(STAMP)

setup: $(STAMP) ## create .venv and install dependencies

run: $(STAMP) ## start the workbench with auto-reload (port 8765, or PORT=...)
	$(PY) -m server --reload --port $(PORT)

serve: $(STAMP) ## start the workbench without auto-reload
	$(PY) -m server --port $(PORT)

sim: $(STAMP) ## run MODEL headless until UNTIL with SEED
	$(PY) -m server.kernel $(MODEL) --until $(UNTIL) --seed $(SEED)

test: $(STAMP) ## run the tests
	$(PY) -m pytest -q

lint: $(STAMP) ## ruff check and format check
	$(PY) -m ruff check .
	$(PY) -m ruff format --check .

format: $(STAMP) ## fix lint and format Python files
	$(PY) -m ruff check --fix .
	$(PY) -m ruff format .

check: $(STAMP) ## everything CI runs (add BASE=main for aihealth delta)
	scripts/check.sh $(BASE)

health: ## aihealth delta of uncommitted changes
	aihealth delta

clean: ## remove caches (keeps .venv and runs.sqlite3)
	rm -rf .pytest_cache .ruff_cache
	find . -name __pycache__ -type d -not -path "./$(VENV)/*" -exec rm -rf {} +
