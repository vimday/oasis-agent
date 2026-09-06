.PHONY: install contract-install test lint format typecheck python-check contract-check check report
PYTHON ?= .venv/bin/python
RUNNER ?= $(PYTHON)

install:
	python3 -m venv .venv
	$(PYTHON) -m pip install --require-hashes -r requirements-dev.lock
	$(PYTHON) -m pip install --no-deps --no-build-isolation -e .

contract-install:
	cd contracts && npm ci --ignore-scripts --no-audit --no-fund

test:
	$(RUNNER) -m pytest

lint:
	$(PYTHON) -m compileall -q src tests scripts
	$(PYTHON) -m ruff check src tests scripts

format:
	$(PYTHON) -m ruff format --check src tests scripts

typecheck:
	$(PYTHON) -m mypy src

python-check: lint format typecheck test

contract-check:
	cd contracts && forge fmt --check
	cd contracts && forge build
	cd contracts && FOUNDRY_PROFILE=ci forge test
	cd contracts && forge lint

check: python-check contract-check

report:
	PYTHONPATH=src $(PYTHON) scripts/analyze_benchmark.py --fixture --output reports/benchmark-hook-analysis.md

security-check:
	$(PYTHON) -m pip_audit --require-hashes -r requirements-dev.lock
	$(PYTHON) -m bandit -r src scripts -ll
