SHELL := /bin/bash
PYTHON ?= ./.venv/bin/python

.PHONY: check lint typecheck format-check format test smoke build audit

check: lint typecheck format-check
	@if [ -x ./scripts/check.sh ]; then ./scripts/check.sh; else echo "No check script found"; fi

lint:
	@PYTHONPATH=src $(PYTHON) -m ruff check --no-cache src tests scripts

typecheck:
	@PYTHONPATH=src $(PYTHON) -m mypy

format-check:
	@$(PYTHON) -m ruff format --check --no-cache src tests scripts

format:
	@$(PYTHON) -m ruff format --no-cache src tests scripts

test:
	@PYTHONPATH=src $(PYTHON) -m unittest discover -s tests -v

smoke:
	@$(PYTHON) scripts/smoke_cli.py

build:
	@$(PYTHON) -m compileall src tests

audit:
	@mkdir -p .cache/pip-audit
	@$(PYTHON) -m pip_audit --cache-dir .cache/pip-audit -r requirements.lock
