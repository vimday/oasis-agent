.PHONY: install test lint format typecheck check report
PYTHON ?= python3
RUNNER ?= $(PYTHON)

install:
	$(PYTHON) -m pip install -e '.[dev]'

test:
	$(RUNNER) -m pytest

lint:
	$(PYTHON) -m compileall -q src tests scripts
	@if command -v ruff >/dev/null 2>&1; then ruff check src tests scripts; else echo 'ruff not installed; compile check passed'; fi

format:
	@if command -v ruff >/dev/null 2>&1; then ruff format src tests scripts; else echo 'ruff not installed'; fi

typecheck:
	@if command -v mypy >/dev/null 2>&1; then mypy src; else echo 'mypy not installed; typecheck skipped'; fi

check: lint typecheck test

report:
	PYTHONPATH=src $(PYTHON) scripts/analyze_benchmark.py --fixture --output reports/benchmark-hook-analysis.md
