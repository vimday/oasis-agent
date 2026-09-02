# Robinhood Uni v4 Research and Execution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a reproducible Oasis Agent foundation that analyzes Robinhood Chain Uniswap v4 pools/hooks, measures risk-adjusted LP net yield, researches arbitrage, and enforces a default-disabled transaction execution path.

**Architecture:** Use a typed Python package with separated RPC/evidence, EVM analysis, Uniswap v4 decoding, farming analytics, arbitrage research, risk policy, execution state machine, and reporting layers. Every chain-derived result is block-pinned and content-hashed; execution accepts only validated, simulated, allowlisted opportunities and defaults to observe/simulate.

**Tech Stack:** Python 3.12+, `uv`, `pyproject.toml`, `pytest`, `ruff`, `mypy`, `pydantic`, `httpx`, `web3.py`, `eth-abi`, `eth-utils`, SQLite, `Decimal`/integer arithmetic.

## Global Constraints

- The benchmark is Pool ID `0x374531f25cfb1b92f6d03b946598d53dd934fba6e138bcece153a52113aa73a5` and hook `0x1191Ac2561817686EFE4296B37BA5a5419Eaa880` on chain ID `4663`.
- The PoolManager allowlist includes `0x8366a39cc670b4001a1121b8f6a443a643e40951` only for the benchmark configuration.
- Default execution mode is `observe`; no default command may sign or broadcast a transaction.
- No private key, signing material, or RPC secret may be stored in source, fixtures, logs, or checked-in configuration.
- No exploit payload, unauthorized asset extraction, sandwiching, malicious ordering, or undisclosed/trader-specific fee implementation is included.
- Static selector database matches and bytecode control-flow interpretations are labeled as `confirmed`, `inferred`, or `unresolved`.
- Settlement decisions use integer arithmetic or explicit `Decimal`; binary floating point is not used for token, fee, PnL, or risk calculations.
- Every persisted evidence item includes chain ID, block number, block hash, retrieval timestamp, source endpoint, and content hash.
- Every task ends with its focused test command and a commit; commit messages end with `Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>`.

## File Map

Create the following focused files:

```text
pyproject.toml
Makefile
.gitignore
src/oasis_agent/__init__.py
src/oasis_agent/cli.py
src/oasis_agent/config.py
src/oasis_agent/errors.py
src/oasis_agent/models/{common,chain,evm,v4,finance,execution}.py
src/oasis_agent/rpc/{client,transport}.py
src/oasis_agent/evidence/store.py
src/oasis_agent/evm/{bytecode,selectors,permissions,features}.py
src/oasis_agent/uniswap_v4/{pool_id,events,delta,fees}.py
src/oasis_agent/farming/{metrics,backtest}.py
src/oasis_agent/arbitrage/{models,scanner}.py
src/oasis_agent/risk/policy.py
src/oasis_agent/execution/state_machine.py
src/oasis_agent/reporting/render.py
configs/robinhood.example.toml
reports/benchmark-hook-analysis.md
scripts/analyze_benchmark.py
scripts/build_sample_evidence.py
tests/unit/... 
tests/integration/...
```

## Task 1: Repository and Quality Gate

**Files:**
- Create: `.gitignore`, `pyproject.toml`, `Makefile`, `src/oasis_agent/__init__.py`, `tests/conftest.py`
- Test: `tests/unit/test_project_bootstrap.py`

**Interfaces:**
- Produces installable package metadata and the commands `make test`, `make lint`, `make typecheck`, `make check`.

- [ ] **Step 1: Initialize Git and write the failing bootstrap test.**

```bash
git init
git branch -M main
mkdir -p src/oasis_agent tests/unit
```

```python
# tests/unit/test_project_bootstrap.py
def test_package_imports():
    import oasis_agent
    assert oasis_agent.__version__
```

- [ ] **Step 2: Run the focused test and verify it fails because the package is not installed.**

Run: `python -m pytest tests/unit/test_project_bootstrap.py -q`
Expected: FAIL with an import/packaging error.

- [ ] **Step 3: Add package metadata and quality commands.**

`pyproject.toml` must define Python `>=3.12`, a `src` layout, runtime dependencies listed in the design, optional dev dependencies for pytest/ruff/mypy, pytest discovery, strict mypy, and ruff rules. `Makefile` must call `uv run pytest`, `uv run ruff check`, `uv run ruff format --check`, and `uv run mypy src`. `.gitignore` must exclude `.venv`, caches, SQLite evidence, local configs, keys, and generated reports except the checked-in benchmark report.

- [ ] **Step 4: Run the bootstrap test and all formatting checks.**

Run: `uv sync && make check`
Expected: PASS; if `uv` is unavailable, create a documented local virtual environment only in `.venv` and run the equivalent package commands.

- [ ] **Step 5: Commit the bootstrap.**

```bash
git add .gitignore pyproject.toml Makefile src tests
git commit -m "chore: initialize typed research project"
```

## Task 2: Typed Models and Configuration

**Files:**
- Create: `src/oasis_agent/errors.py`, `src/oasis_agent/models/common.py`, `src/oasis_agent/models/chain.py`, `src/oasis_agent/models/evm.py`, `src/oasis_agent/models/v4.py`, `src/oasis_agent/models/finance.py`, `src/oasis_agent/models/execution.py`, `src/oasis_agent/config.py`, `configs/robinhood.example.toml`
- Test: `tests/unit/test_models.py`, `tests/unit/test_config.py`

**Interfaces:**
- `EvidenceRef`, `BlockRef`, `Address`, `HexData`, `PoolKey`, `HookPermissions`, `FeeObservation`, `SwapObservation`, `LpPerformance`, `ArbitrageOpportunity`, `RiskDecision`, and `ExecutionMode` are immutable validated models.
- `load_config(path: Path, environ: Mapping[str, str]) -> AppConfig` rejects missing chain ID, RPC URL, or PoolManager allowlist.

- [ ] **Step 1: Write tests for checksum/address, PoolKey validation, execution default, and secret rejection.**

```python
def test_config_defaults_to_observe(tmp_path):
    cfg = load_config(tmp_path / "config.toml", {"OASIS_RPC_URL": "https://example.invalid"})
    assert cfg.execution.mode is ExecutionMode.OBSERVE

def test_config_rejects_private_key(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('private_key = "0xdeadbeef"')
    with pytest.raises(ConfigurationError):
        load_config(path, {})

def test_pool_key_requires_uint24_fee():
    with pytest.raises(ValidationError):
        PoolKey(..., fee=16_777_216)
```

- [ ] **Step 2: Run the model tests and verify failure.**

Run: `python -m pytest tests/unit/test_models.py tests/unit/test_config.py -q`
Expected: FAIL because models and loader do not exist.

- [ ] **Step 3: Implement immutable Pydantic models and strict configuration.**

Use `ConfigDict(frozen=True, extra="forbid")`; validate 20-byte addresses, nonnegative block numbers, `uint24` fee bounds, tick-spacing bounds, explicit decimal strings, and execution enum. Read TOML using the standard library. Permit RPC URL through environment or example config, but reject any key named `private_key`, `mnemonic`, `secret`, or `rpc_token` in checked-in config.

- [ ] **Step 4: Run focused tests and type checks.**

Run: `python -m pytest tests/unit/test_models.py tests/unit/test_config.py -q && make typecheck`
Expected: PASS.

- [ ] **Step 5: Commit typed contracts.**

```bash
git add src/oasis_agent/errors.py src/oasis_agent/models src/oasis_agent/config.py configs/robinhood.example.toml tests/unit/test_models.py tests/unit/test_config.py
git commit -m "feat: add typed chain and strategy contracts"
```

## Task 3: RPC Transport and Evidence Store

**Files:**
- Create: `src/oasis_agent/rpc/transport.py`, `src/oasis_agent/rpc/client.py`, `src/oasis_agent/evidence/store.py`
- Test: `tests/unit/test_rpc.py`, `tests/unit/test_evidence.py`

**Interfaces:**
- `RpcTransport.request(method: str, params: list[object]) -> object` validates JSON-RPC IDs and errors.
- `RpcClient.chain_id() -> int`, `get_code(address, block) -> HexData`, `call(to, data, block) -> HexData`, `get_logs(filter) -> list[LogRecord]`, and `get_receipt(tx_hash) -> Receipt`.
- `EvidenceStore.put(kind, payload, block_ref, source) -> EvidenceRef` and `get(ref) -> bytes`.

- [ ] **Step 1: Write deterministic fake-transport and evidence-hash tests.**

```python
def test_rpc_rejects_json_rpc_error():
    transport = FakeTransport({"error": {"code": -32000, "message": "boom"}})
    with pytest.raises(RpcError, match="boom"):
        transport.request("eth_chainId", [])

def test_evidence_is_content_addressed(tmp_path):
    store = EvidenceStore(tmp_path / "evidence.sqlite3")
    ref1 = store.put("code", b"abc", block_ref, "test")
    ref2 = store.put("code", b"abc", block_ref, "test")
    assert ref1.content_hash == ref2.content_hash
```

- [ ] **Step 2: Run focused tests and verify failure.**

Run: `python -m pytest tests/unit/test_rpc.py tests/unit/test_evidence.py -q`
Expected: FAIL because transport and store are absent.

- [ ] **Step 3: Implement bounded HTTP JSON-RPC transport and SQLite evidence.**

Use `httpx.Client` with explicit timeout, bounded retries for 429/5xx, request IDs, and no logging of headers named authorization/token. Validate chain ID before any non-health request. Store canonical JSON or bytes, metadata, and SHA-256 content hash in SQLite with unique `(kind, content_hash, block_hash)`.

- [ ] **Step 4: Run focused tests including retry and persistence behavior.**

Run: `python -m pytest tests/unit/test_rpc.py tests/unit/test_evidence.py -q && make lint`
Expected: PASS.

- [ ] **Step 5: Commit data layer.**

```bash
git add src/oasis_agent/rpc src/oasis_agent/evidence tests/unit/test_rpc.py tests/unit/test_evidence.py
git commit -m "feat: add block-pinned rpc and evidence store"
```

## Task 4: EVM Bytecode, Selectors, and Hook Permissions

**Files:**
- Create: `src/oasis_agent/evm/bytecode.py`, `src/oasis_agent/evm/selectors.py`, `src/oasis_agent/evm/permissions.py`, `src/oasis_agent/evm/features.py`
- Test: `tests/unit/test_evm_analysis.py`

**Interfaces:**
- `extract_cbor_metadata(bytecode: bytes) -> MetadataResult`.
- `extract_push4_selectors(bytecode: bytes) -> tuple[SelectorObservation, ...]` skips PUSH payload bytes.
- `decode_hook_permissions(address: str) -> HookPermissions`.
- `scan_features(bytecode: bytes) -> FeatureSummary` reports opcode counts, hard-coded addresses, storage operations, external calls, and confidence-tagged observations.

- [ ] **Step 1: Write tests using synthetic bytecode and the benchmark address.**

```python
def test_selector_scanner_does_not_scan_push_payloads():
    bytecode = bytes.fromhex("63deadbeef6360abcdef")
    assert [x.selector for x in extract_push4_selectors(bytecode)] == ["0xdeadbeef"]

def test_benchmark_hook_permissions():
    permissions = decode_hook_permissions("0x1191Ac2561817686EFE4296B37BA5a5419Eaa880")
    assert permissions.before_swap is True
    assert permissions.after_swap is False
    assert permissions.before_swap_returns_delta is False
```

- [ ] **Step 2: Run the analysis test and verify failure.**

Run: `python -m pytest tests/unit/test_evm_analysis.py -q`
Expected: FAIL because analysis functions are absent.

- [ ] **Step 3: Implement a bounds-safe EVM byte scanner.**

Treat PUSH immediate bytes as data, parse Solidity CBOR length from the final two bytes, decode IPFS/Bzz fields when valid, count `SLOAD/SSTORE/CALL/STATICCALL/DELEGATECALL/CREATE/SELFDESTRUCT`, and emit only confidence-tagged observations. Decode the v4 permission mask from the low 14 address bits using named boolean fields. Do not query external selector databases from production code.

- [ ] **Step 4: Run tests against saved benchmark bytecode fixture.**

Store only the public bytecode hash and a fixture under `tests/fixtures` with provenance. Run: `python -m pytest tests/unit/test_evm_analysis.py -q && make typecheck`
Expected: PASS.

- [ ] **Step 5: Commit EVM analyzer.**

```bash
git add src/oasis_agent/evm tests/unit/test_evm_analysis.py tests/fixtures
git commit -m "feat: add safe evm and hook permission analysis"
```

## Task 5: Uniswap v4 Pool IDs, Events, Fees, and Deltas

**Files:**
- Create: `src/oasis_agent/uniswap_v4/pool_id.py`, `src/oasis_agent/uniswap_v4/events.py`, `src/oasis_agent/uniswap_v4/delta.py`, `src/oasis_agent/uniswap_v4/fees.py`
- Test: `tests/unit/test_v4.py`

**Interfaces:**
- `pool_id_from_key(pool_key: PoolKey) -> bytes32` uses canonical ABI encoding and keccak.
- `decode_pool_initialized(log: LogRecord) -> PoolInitialized`.
- `decode_swap(log: LogRecord) -> SwapObservation`.
- `decode_balance_delta(value: bytes32) -> BalanceDelta` with signed int128 components.
- `effective_fee(input_fee: int, protocol_fee: int) -> Decimal` and `validate_fee_override(value: int) -> int`.

- [ ] **Step 1: Write tests for known PoolManager event data, signed delta edges, and fee flags.**

```python
def test_delta_decodes_negative_int128():
    value = ((1 << 128) - 7) << 128 | 9
    assert decode_balance_delta(value.to_bytes(32, "big")).amount0 == -7
    assert decode_balance_delta(value.to_bytes(32, "big")).amount1 == 9

def test_override_fee_rejects_flag_only_value():
    with pytest.raises(FeeEncodingError):
        validate_fee_override(0x400000)
```

- [ ] **Step 2: Run focused tests and verify failure.**

Run: `python -m pytest tests/unit/test_v4.py -q`
Expected: FAIL because v4 decoders are absent.

- [ ] **Step 3: Implement canonical encoding and strict event decoders.**

Use `eth_abi`/`eth_utils.keccak`, validate topic count and data lengths, decode signed two’s-complement int128, preserve raw log evidence, distinguish `DYNAMIC_FEE_FLAG` and `OVERRIDE_FEE_FLAG`, and reject fee values outside the supported uint24 domain or without a valid fee payload.

- [ ] **Step 4: Run v4 tests and property-style boundary cases.**

Run: `python -m pytest tests/unit/test_v4.py -q && make check`
Expected: PASS.

- [ ] **Step 5: Commit v4 primitives.**

```bash
git add src/oasis_agent/uniswap_v4 tests/unit/test_v4.py
git commit -m "feat: decode uniswap v4 pool and accounting primitives"
```

## Task 6: Benchmark Acquisition and Reproducible Hook Report

**Files:**
- Create: `scripts/analyze_benchmark.py`, `scripts/build_sample_evidence.py`, `src/oasis_agent/reporting/render.py`, `reports/benchmark-hook-analysis.md`
- Test: `tests/integration/test_benchmark_report.py`

**Interfaces:**
- `analyze_benchmark(client: RpcClient, store: EvidenceStore, block: int | None) -> BenchmarkReport`.
- `render_markdown(report: BenchmarkReport) -> str`.

- [ ] **Step 1: Write an offline integration test with deterministic evidence fixtures.**

```python
def test_benchmark_report_contains_evidence_levels(sample_benchmark):
    report = analyze_benchmark(sample_benchmark.client, sample_benchmark.store, block=52_707_515)
    text = render_markdown(report)
    assert "confirmed" in text
    assert "inferred" in text
    assert "unresolved" in text
    assert "0x1191Ac2561817686EFE4296B37BA5a5419Eaa880" in text
```

- [ ] **Step 2: Run the integration test and verify failure.**

Run: `python -m pytest tests/integration/test_benchmark_report.py -q`
Expected: FAIL because acquisition and rendering do not exist.

- [ ] **Step 3: Implement block-pinned benchmark acquisition.**

Fetch chain ID, hook code, owner/registrar/PoolManager/default fee getters, PoolInitialized and Swap logs, and receipts through the evidence layer. Validate all addresses against configuration. Build the report with bytecode hash, permissions, selectors, observed event fields, and explicit limitations. Any unsupported trace or unavailable source is recorded as unresolved rather than hidden.

- [ ] **Step 4: Render and regenerate the checked-in report offline.**

Run: `python scripts/build_sample_evidence.py && python scripts/analyze_benchmark.py --fixture --output reports/benchmark-hook-analysis.md && python -m pytest tests/integration/test_benchmark_report.py -q`
Expected: PASS and deterministic output hash on repeated runs.

- [ ] **Step 5: Commit benchmark report.**

```bash
git add scripts src/oasis_agent/reporting reports/benchmark-hook-analysis.md tests/integration/test_benchmark_report.py
git commit -m "feat: add reproducible benchmark hook report"
```

## Task 7: Accounting Reconciliation and LP Net-Yield Metrics

**Files:**
- Create: `src/oasis_agent/farming/metrics.py`, `src/oasis_agent/farming/backtest.py`
- Test: `tests/unit/test_farming.py`

**Interfaces:**
- `reconcile_swap(components: AccountingComponents) -> ReconciliationResult` requires all tracked deltas to close.
- `calculate_lp_net_yield(snapshot_start, snapshot_end, costs: CostModel) -> LpPerformance`.
- `backtest_strategy(observations, strategy, costs) -> BacktestResult` separates calibration/evaluation periods.

- [ ] **Step 1: Write tests for closed/open accounting and net-yield subtraction.**

```python
def test_reconciliation_rejects_unexplained_hook_delta():
    components = AccountingComponents(..., hook_delta=Decimal("1"), unexplained=Decimal("0"))
    with pytest.raises(AccountingError):
        reconcile_swap(components)

def test_lp_yield_includes_il_gas_and_rebalance_costs(sample_snapshots):
    result = calculate_lp_net_yield(sample_snapshots.start, sample_snapshots.end, costs)
    assert result.net_yield == Decimal("0.042")
```

- [ ] **Step 2: Run farming tests and verify failure.**

Run: `python -m pytest tests/unit/test_farming.py -q`
Expected: FAIL because metrics are absent.

- [ ] **Step 3: Implement integer/Decimal accounting and benchmark backtest.**

Require trader input/output, PoolManager delta, pool delta, protocol fee, LP fee, hook delta, and ERC-20 transfer delta. Fail closed on any unexplained residual. Compute fee income, impermanent loss using documented relative-price inputs, gas, rebalancing, adverse-selection and inventory costs. Keep calibration and evaluation observations separate and return scenario ranges rather than false precision.

- [ ] **Step 4: Run farming tests and deterministic backtest.**

Run: `python -m pytest tests/unit/test_farming.py -q && make check`
Expected: PASS.

- [ ] **Step 5: Commit farming analytics.**

```bash
git add src/oasis_agent/farming tests/unit/test_farming.py
git commit -m "feat: add reconciled lp net-yield analytics"
```

## Task 8: Transparent Dynamic Fee Strategy and Local Adversarial Fixture

**Files:**
- Create: `src/oasis_agent/uniswap_v4/fees.py` additions, `src/oasis_agent/farming/backtest.py` additions, `tests/fixtures/adversarial_fee.py`
- Test: `tests/unit/test_dynamic_fee.py`

**Interfaces:**
- `TransparentFeeStrategy.quote(state: PoolState, direction: SwapDirection) -> FeeQuote`.
- `FeeQuote` exposes `effective_fee`, `min_fee`, `max_fee`, `rule_version`, and deterministic inputs.
- `AdversarialUndisclosedFeeFixture` is local-only and cannot implement `Signer` or `Broadcaster` interfaces.

- [ ] **Step 1: Write tests for fee bounds, bounded changes, reproducibility, and fixture isolation.**

```python
def test_transparent_fee_is_bounded_and_reproducible(pool_state):
    a = strategy.quote(pool_state, SwapDirection.ZERO_FOR_ONE)
    b = strategy.quote(pool_state, SwapDirection.ZERO_FOR_ONE)
    assert a == b
    assert a.min_fee <= a.effective_fee <= a.max_fee

def test_adversarial_fixture_has_no_broadcast_capability():
    assert not hasattr(AdversarialUndisclosedFeeFixture, "broadcast")
```

- [ ] **Step 2: Run dynamic-fee tests and verify failure.**

Run: `python -m pytest tests/unit/test_dynamic_fee.py -q`
Expected: FAIL because strategy and fixture are absent.

- [ ] **Step 3: Implement the transparent strategy.**

Use volatility, inventory imbalance, adverse-selection estimate, and rebalancing incentive as explicit inputs. Clamp to immutable configured bounds and a per-block change limit. Expose a pure quote method and emit a serializable rule version. Keep the adversarial fee fixture in tests only, with synthetic traders and no signer/broadcaster dependency.

- [ ] **Step 4: Run tests and verify no hidden strategy reaches execution imports.**

Run: `python -m pytest tests/unit/test_dynamic_fee.py -q && python -m mypy src`
Expected: PASS.

- [ ] **Step 5: Commit fee strategy.**

```bash
git add src/oasis_agent/uniswap_v4/fees.py src/oasis_agent/farming/backtest.py tests/fixtures/adversarial_fee.py tests/unit/test_dynamic_fee.py
git commit -m "feat: add bounded transparent fee strategy"
```

## Task 9: Arbitrage Models, Scanner, and Risk Policy

**Files:**
- Create: `src/oasis_agent/arbitrage/models.py`, `src/oasis_agent/arbitrage/scanner.py`, `src/oasis_agent/risk/policy.py`
- Test: `tests/unit/test_arbitrage.py`, `tests/unit/test_risk.py`

**Interfaces:**
- `scan_opportunities(snapshot: MarketSnapshot, constraints: RiskLimits) -> tuple[ArbitrageOpportunity, ...]`.
- `evaluate_opportunity(opportunity: ArbitrageOpportunity, evidence: EvidenceBundle, policy: RiskPolicy) -> RiskDecision`.

- [ ] **Step 1: Write tests for net-profit calculation, expiry, allowlists, fee bounds, and unresolved accounting.**

```python
def test_scanner_excludes_negative_net_profit(route):
    assert scan_opportunities(route.snapshot, route.constraints) == ()

def test_policy_blocks_unresolved_dynamic_fee(opportunity, evidence):
    decision = policy.evaluate(opportunity, evidence)
    assert decision.level is RiskLevel.BLOCKED
    assert "dynamic fee" in decision.reason.lower()
```

- [ ] **Step 2: Run focused tests and verify failure.**

Run: `python -m pytest tests/unit/test_arbitrage.py tests/unit/test_risk.py -q`
Expected: FAIL because scanner and policy are absent.

- [ ] **Step 3: Implement immutable opportunity records and fail-closed risk evaluation.**

Compute expected and worst-case profit after fee/gas/slippage/price-impact bounds. Require expiry block, confidence, allowlisted targets, pinned bytecode hashes, successful simulation evidence, and closed accounting. Return structured reasons; never silently downgrade a missing field.

- [ ] **Step 4: Run tests and property boundary cases.**

Run: `python -m pytest tests/unit/test_arbitrage.py tests/unit/test_risk.py -q && make check`
Expected: PASS.

- [ ] **Step 5: Commit research and policy layer.**

```bash
git add src/oasis_agent/arbitrage src/oasis_agent/risk tests/unit/test_arbitrage.py tests/unit/test_risk.py
git commit -m "feat: add arbitrage research and fail-closed risk policy"
```

## Task 10: Guarded Execution State Machine

**Files:**
- Create: `src/oasis_agent/execution/state_machine.py`, `src/oasis_agent/execution/signer.py`, `src/oasis_agent/execution/broadcaster.py`
- Test: `tests/unit/test_execution.py`

**Interfaces:**
- `ExecutionStateMachine.transition(event: ExecutionEvent) -> ExecutionRecord`.
- `DryRunSigner.sign(unsigned_tx) -> SignedTransaction` raises if configured with a private key.
- `Broadcaster.broadcast(signed_tx)` is never constructed by observe/simulate commands.

- [ ] **Step 1: Write tests proving default observe, simulation-only behavior, and all blocking gates.**

```python
def test_default_mode_never_constructs_broadcaster():
    app = build_app(test_config_without_execution)
    app.run("scan")
    assert broadcaster_factory.constructed is False

def test_expired_opportunity_cannot_be_approved(opportunity):
    with pytest.raises(ExecutionBlockedError, match="expired"):
        machine.transition(Approve(opportunity=opportunity.expired()))
```

- [ ] **Step 2: Run execution tests and verify failure.**

Run: `python -m pytest tests/unit/test_execution.py -q`
Expected: FAIL because state machine and signer interfaces are absent.

- [ ] **Step 3: Implement explicit transitions and dry-run signer.**

Permit only `observe -> quote -> simulate -> risk_check -> approve -> sign -> broadcast -> verify`. Require explicit execution mode, chain/target/token allowlists, bytecode pinning, successful simulation, bounded capital/gas/slippage/fee, unexpired opportunity, nonce match, and reconciliation evidence. Make `signer` and `broadcaster` dependency-injected; default factories return no-op/dry-run implementations. Reject private-key config at startup.

- [ ] **Step 4: Run execution tests and inspect import/runtime safety.**

Run: `python -m pytest tests/unit/test_execution.py -q && python -m pytest -q`
Expected: PASS; no test sends an RPC transaction.

- [ ] **Step 5: Commit execution gates.**

```bash
git add src/oasis_agent/execution tests/unit/test_execution.py
git commit -m "feat: add default-disabled execution state machine"
```

## Task 11: CLI, Make Targets, and End-to-End Offline Verification

**Files:**
- Create: `src/oasis_agent/cli.py`, `tests/integration/test_cli.py`
- Modify: `Makefile`, `pyproject.toml`

**Interfaces:**
- CLI commands: `analyze-hook --fixture`, `report-benchmark --fixture`, `backtest --fixture`, `scan --fixture`, `simulate --fixture`.
- No CLI command has a default broadcast/execute mode.

- [ ] **Step 1: Write CLI tests for deterministic output and safe defaults.**

```python
def test_analyze_hook_fixture_outputs_json(runner):
    result = runner.invoke(cli, ["analyze-hook", "--fixture"])
    assert result.exit_code == 0
    assert '"permissions"' in result.stdout

def test_execute_is_rejected_without_explicit_mode(runner):
    result = runner.invoke(cli, ["execute"])
    assert result.exit_code != 0
```

- [ ] **Step 2: Run CLI tests and verify failure.**

Run: `python -m pytest tests/integration/test_cli.py -q`
Expected: FAIL because CLI is absent.

- [ ] **Step 3: Implement commands with structured JSON and Markdown output.**

Use `argparse` or the selected CLI dependency; ensure fixture commands use only local evidence. Add `make test`, `make lint`, `make typecheck`, `make check`, and `make report`. The command parser must make `execute` fail unless explicit configuration, a signer, and policy requirements are provided.

- [ ] **Step 4: Run the complete offline suite.**

Run: `make check && make report && git diff --check`
Expected: PASS; report output is deterministic.

- [ ] **Step 5: Commit CLI and integration wiring.**

```bash
git add src/oasis_agent/cli.py tests/integration/test_cli.py Makefile pyproject.toml reports
git commit -m "feat: add reproducible research cli"
```

## Task 12: Final Verification and Documentation Commit

**Files:**
- Modify: `docs/superpowers/specs/2026-09-03-robinhood-v4-research-execution-design.md` status from `Draft for user review` to `Approved and implemented in milestones` only after all tests pass.
- Create: `docs/verification/2026-09-03-initial-verification.md`

**Interfaces:**
- Verification record contains exact commands, versions, commit hashes, fixture hashes, and pass/fail output summaries.

- [ ] **Step 1: Run the complete project gate from a clean environment.**

```bash
uv sync --locked
make check
make report
python -m oasis_agent.cli analyze-hook --fixture
python -m oasis_agent.cli report-benchmark --fixture
```

Expected: all commands exit 0; no command broadcasts a transaction.

- [ ] **Step 2: Run a safety grep over tracked files.**

```bash
git grep -nE 'PRIVATE_KEY|private_key|mnemonic|eth_sendRawTransaction|send_transaction' -- ':!docs/verification/*'
```

Expected: only intentionally guarded interface names or test assertions appear; no secret values or automatic broadcast path appears in defaults.

- [ ] **Step 3: Record verification evidence.**

Write exact package/tool versions, test count, coverage, report hash, and any blocked network-dependent work. Do not claim mainnet execution or profitability unless an actual pinned dataset and out-of-sample result support it.

- [ ] **Step 4: Update design status and commit verification.**

```bash
git add docs/superpowers/specs/2026-09-03-robinhood-v4-research-execution-design.md docs/verification
git commit -m "docs: record initial verification evidence"
```

- [ ] **Step 5: Final status report.**

Report completed milestones, exact verification output, unresolved benchmark questions, and that execution remains disabled by default. If any gate fails, report `DONE_WITH_CONCERNS` rather than claiming completion.

## Coverage Check

- Design architecture and package boundaries: Tasks 1-3.
- Evidence and block-pinned RPC: Task 3.
- EVM metadata/selectors/permissions/features: Task 4.
- PoolKey/events/deltas/fees: Task 5.
- Benchmark evidence and report: Task 6.
- LP net yield and reconciliation: Task 7.
- Transparent fee strategy and local adversarial fixture: Task 8.
- Arbitrage scanner and risk gates: Task 9.
- Guarded execution: Task 10.
- CLI and project quality commands: Task 11.
- Verification and evidence: Task 12.

## Plan Self-Review

- No `TBD`, `TODO`, or vague “handle appropriately” implementation steps remain.
- Interfaces are named before downstream use.
- Every task has a failing test, a focused command, a minimal implementation boundary, and a commit.
- The plan does not treat an unverified hook as exploitable or allow hidden fees into a deployable path.
- Network-dependent data acquisition is separated from deterministic fixtures.
- The primary LP net-yield objective is measured net of stated costs and separated into calibration/evaluation periods.
