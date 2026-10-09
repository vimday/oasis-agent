.PHONY: install lock contract-install test lint format typecheck python-check frontend-check contract-check check report run-server
PYTHON ?= .venv/bin/python
RUNNER ?= $(PYTHON)

# uv owns the environment: uv.lock is the source of truth; requirements*.lock
# are hashed exports kept for pip-audit and must never be edited by hand.
install:
	uv sync --frozen --all-extras --python 3.12

lock:
	uv lock --python 3.12
	uv export --frozen --no-emit-project --no-header --format requirements.txt -o requirements.lock
	uv export --frozen --all-extras --no-emit-project --no-header --format requirements.txt -o requirements-dev.lock

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

frontend-check:
	$(PYTHON) -m scripts.check_dashboard

check: python-check frontend-check contract-check

report:
	PYTHONPATH=src $(PYTHON) scripts/analyze_benchmark.py --fixture --output reports/benchmark-hook-analysis.md

run-server:
	mkdir -p data
	$(PYTHON) scripts/run_server.py --config configs/server.local.toml

security-check:
	$(PYTHON) -m pip_audit --require-hashes -r requirements-dev.lock
	$(PYTHON) -m bandit -r src scripts -ll
	$(PYTHON) -m scripts.check_secrets

contract-security-check:
	cd contracts && VIRTUAL_ENV="$(abspath .venv)" ../.venv/bin/slither . --filter-paths node_modules --exclude-dependencies --fail-low

# Offline fixed pipeline; exit 3 means retained inputs are incomplete, not trading approval.
WORKFLOW ?= configs/workflow.current.json
RUN_OUTPUT ?= reports/runs/current
.PHONY: run verify-run
run:
	$(PYTHON) -m scripts.run_lp_research --workflow "$(WORKFLOW)" --output "$(RUN_OUTPUT)" --resume

verify-run: check run

# Live LP loop (Base ETH/USDC v4). `make live` expects the signer daemon on SIGNER_SOCKET.
SIGNER_SOCKET ?= $(HOME)/.oasis/signer.sock
.PHONY: shadow live live-report live-stop
shadow:
	$(PYTHON) scripts/run_live_lp.py --mode shadow
live:
	$(PYTHON) scripts/run_live_lp.py --mode live --signer-socket "$(SIGNER_SOCKET)"
live-report:
	$(PYTHON) scripts/run_live_lp.py --mode $(or $(MODE),live) --report
live-stop:
	touch data/live/STOP
