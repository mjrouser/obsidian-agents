SHELL := /bin/bash
PYTHON ?= ./.venv/bin/python

.PHONY: check lint format-check format test build audit

check: lint format-check
	@if [ -x ./scripts/check.sh ]; then ./scripts/check.sh; else echo "No check script found"; fi

lint:
	@PYTHONPATH=src $(PYTHON) -m ruff check --no-cache src tests scripts

format-check:
	@$(PYTHON) -m ruff format --check --no-cache src tests scripts

format:
	@$(PYTHON) -m ruff format --no-cache src tests scripts

test:
	@PYTHONPATH=src $(PYTHON) -m unittest discover -s tests -v

build:
	@$(PYTHON) -m compileall src tests

audit:
	@mkdir -p .cache/pip-audit
	@$(PYTHON) -m pip_audit --cache-dir .cache/pip-audit -r requirements.lock
