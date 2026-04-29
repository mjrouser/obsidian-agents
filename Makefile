SHELL := /bin/bash
PYTHON ?= ./.venv/bin/python

.PHONY: check test build audit

check:
	@if [ -x ./scripts/check.sh ]; then ./scripts/check.sh; else echo "No check script found"; fi

test:
	@PYTHONPATH=src $(PYTHON) -m unittest discover -s tests -v

build:
	@$(PYTHON) -m compileall src tests

audit:
	@mkdir -p .cache/pip-audit
	@$(PYTHON) -m pip_audit --cache-dir .cache/pip-audit -r requirements.lock
