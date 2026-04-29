SHELL := /bin/bash
PYTHON ?= ./.venv/bin/python

.PHONY: check test build

check:
	@if [ -x ./scripts/check.sh ]; then ./scripts/check.sh; else echo "No check script found"; fi

test:
	@PYTHONPATH=src $(PYTHON) -m unittest discover -s tests -v

build:
	@$(PYTHON) -m compileall src tests
