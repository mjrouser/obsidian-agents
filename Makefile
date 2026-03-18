SHELL := /bin/bash

.PHONY: check test build

check:
	@if [ -x ./scripts/check.sh ]; then ./scripts/check.sh; else echo "No check script found"; fi

test:
	@PYTHONPATH=src python3 -m unittest discover -s tests -v

build:
	@python3 -m compileall src tests
