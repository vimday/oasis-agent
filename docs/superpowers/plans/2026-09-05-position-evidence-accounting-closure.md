# Position Evidence Binding and Accounting Closure Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the WP2 position-accounting subset by binding every position lifecycle event and settlement claim to verified block-range evidence, propagating unresolved ownership/settlement state without partial PnL, and verifying complete replay conservation inputs without adding LVR, reference-price, or counterfactual-routing functionality.

**Architecture:** Keep raw evidence ingestion (`evidence`/`rpc`) separate from deterministic position replay (`farming`). `PositionDatasetReplay` remains the boundary that consumes two `VerifiedBlockRange` objects; `PositionTokenLedger` remains the token-level lifecycle result; `PositionAccountingReplay` remains the fail-closed accounting result. Add only provenance and conservation validation needed to prevent a caller from attaching an unrelated receipt/trace or selectively valuing a partial ledger. Do not infer settlement amounts from `ModifyLiquidity`, ERC-20 logs, or a broad transaction when attribution is not proven.

**Tech Stack:** Python 3, `dataclasses` with frozen/slots models, `unittest` and `pytest`, `eth-abi`, existing `EvidenceRef`/`VerifiedBlockRange`/`BlockRangeDataset` types, Makefile quality gates, mypy strict, Ruff.

## Global Constraints

- Preserve all unrelated edits in the shared dirty worktree; do not reset, format, or rewrite concurrent files.
- Read and follow `docs/PROJECT_CHARTER.md`, `docs/strategies/ROBINHOOD_V4_AUTOLP_BLUEPRINT.md`, and `.claude/rules/evidence-and-operations.md` and `.claude/rules/financial-research.md`.
- Every decision-critical read is block-pinned; `latest` is never accepted for settlement evidence.
- All token amounts remain signed integer base units and are tracked independently as token0/token1; no binary float enters settlement or PnL.
- `ModifyLiquidity` is identity/liquidity evidence only; it does not prove token settlement amounts.
- Missing or partial settlement attribution remains unresolved and must yield `net_pnl1 is None`, not zero or a partial result.
- A resolved settlement requires evidence pinned to the exact event block and an explicit attribution mode.
- A quorum-trusted accounting result may only be built from provider-quorum input; single-provider data remains research-only.
- Do not implement or modify reference-price/LVR estimation, counterfactual routing, fee elasticity, hook deployment, signing, broadcasting, or live state changes in this plan.
- Before claiming completion, run the repository-required test, lint, format, type, and diff gates; report any gate that cannot run as failed/not passed.

---

### Task 1: Make position replay provenance explicit and reject mismatched verified datasets

**Files:**
- Modify: `src/oasis_agent/farming/position_dataset.py:15-114`
- Modify: `src/oasis_agent/farming/position_ledger.py:69-161`
- Test: `tests/unit/test_position_dataset.py`
- Test: `tests/unit/test_position_ledger.py`

**Interfaces:**
- Consumes: `VerifiedBlockRange.dataset`, `.evidence`, `.trust`, `.agreement`, and `.peer_evidence`; existing `PositionSnapshot`, `BlockRef`, and canonical event decoders.
- Produces: `PositionDatasetReplay` with a deterministic `trust` value and a `PositionTokenLedger` whose entries are bound to the exact verified block lineage and dataset scope.

- [ ] **Step 1: Add failing tests for evidence-to-dataset binding.**

Add tests showing that a position replay rejects each of these cases before decoding events:

```python
def test_rejects_core_evidence_that_does_not_match_dataset_content(tmp_path):
    opening, closing, core, nft = scenario()
    verified_core, verified_nft = verified_ranges(tmp_path, core, nft)
    forged = replace(
        verified_core,
        evidence=replace(verified_core.evidence, content_hash="f" * 64),
    )
    with pytest.raises(ValueError, match="content hash"):
        replay_position_datasets(
            opening,
            closing,
            pool_manager=CORE,
            core=forged,
            ownership=verified_nft,
        )


def test_rejects_single_provider_position_replay_for_confirmed_path(tmp_path):
    opening, closing, core, nft = scenario()
    verified_core, verified_nft = verified_ranges(tmp_path, core, nft)
    assert verified_core.trust is IngestionTrust.SINGLE_PROVIDER_RESEARCH
    result = replay_position_datasets(
        opening, closing, pool_manager=CORE, core=verified_core, ownership=verified_nft
    )
    assert result.trust is IngestionTrust.SINGLE_PROVIDER_RESEARCH
```

Use the existing fixture helpers and import `IngestionTrust`; do not turn single-provider research into a rejection because it is intentionally supported for unresolved research replay.

Add lineage/scope tests for:

```python
def test_rejects_closing_block_evidence_from_another_scope(tmp_path):
    opening, closing, core, nft = scenario()
    verified_core, verified_nft = verified_ranges(tmp_path, core, nft)
    altered = replace(
        verified_nft,
        dataset=replace(verified_nft.dataset, address=CORE),
    )
    with pytest.raises(ValueError, match="evidence|dataset"):
        replay_position_datasets(
            opening, closing, pool_manager=CORE, core=verified_core, ownership=altered
        )
```

The exact helper names may be added locally in the test file, but the tests must construct the forged object through `dataclasses.replace` and assert the error text identifies provenance/scope rather than an unrelated decode failure.

- [ ] **Step 2: Run the focused tests and verify the new tests fail for the intended missing guard.**

Run:

```bash
python3 -m pytest -q tests/unit/test_position_dataset.py tests/unit/test_position_ledger.py
```

Expected: existing tests pass; the new forged-provenance tests fail because replay currently trusts the supplied object after construction and does not revalidate all dataset/evidence relationships at the replay boundary.

- [ ] **Step 3: Implement the smallest provenance validation helper.**

In `position_dataset.py`, add a private function with this exact behavior:

```python
def _validate_verified_position_range(
    verified: VerifiedBlockRange,
    *,
    expected_address: str,
    expected_blocks: tuple[BlockSnapshot, ...],
) -> None:
    dataset = verified.dataset
    if dataset.address not in (None, expected_address):
        raise ValueError("position dataset emitter scope does not match the expected contract")
    if dataset.blocks != expected_blocks:
        raise ValueError("position datasets do not share one canonical block lineage")
    if verified.evidence.content_hash != dataset.content_hash:
        raise ValueError("verified position evidence content hash does not match dataset")
    if verified.evidence.source != dataset.source:
        raise ValueError("verified position evidence source does not match dataset")
    if verified.evidence.block != dataset.blocks[-1].ref:
        raise ValueError("verified position evidence is not pinned to the dataset closing block")
```

Import `BlockSnapshot` for the annotation. Call it for both `core` and `ownership` before event decoding, using the normalized PoolManager and PositionManager addresses. Preserve `VerifiedBlockRange.__post_init__` as the construction-time guard; this replay-boundary check protects against forged dataclass replacements and future deserialization paths.

In `position_ledger.py`, preserve the existing identity/liquidity checks and add a check that every resolved entry's `settlement_evidence` has the same chain ID and block hash as `entry.block`, not only equality through the current `EvidenceRef.block` comparison. Keep unresolved entries free of attribution/evidence.

- [ ] **Step 4: Run the focused tests and type/lint checks for touched files.**

Run:

```bash
python3 -m pytest -q tests/unit/test_position_dataset.py tests/unit/test_position_ledger.py
python3 -m ruff check src/oasis_agent/farming/position_dataset.py src/oasis_agent/farming/position_ledger.py tests/unit/test_position_dataset.py tests/unit/test_position_ledger.py
python3 -m ruff format --check src/oasis_agent/farming/position_dataset.py src/oasis_agent/farming/position_ledger.py tests/unit/test_position_dataset.py tests/unit/test_position_ledger.py
python3 -m mypy src/oasis_agent/farming/position_dataset.py src/oasis_agent/farming/position_ledger.py
```

Expected: all focused tests pass and all four commands exit zero.

- [ ] **Step 5: Review the diff for scope and secret leakage.**

Run:

```bash
git diff --check -- src/oasis_agent/farming/position_dataset.py src/oasis_agent/farming/position_ledger.py tests/unit/test_position_dataset.py tests/unit/test_position_ledger.py
git diff --stat -- src/oasis_agent/farming/position_dataset.py src/oasis_agent/farming/position_ledger.py tests/unit/test_position_dataset.py tests/unit/test_position_ledger.py
```

Confirm that no RPC credentials, signed transactions, or unrelated formatting changes appear.

---

### Task 2: Bind settlement attribution to the exact transaction evidence and prevent selective valuation

**Files:**
- Modify: `src/oasis_agent/farming/position_ledger.py:16-67`
- Modify: `src/oasis_agent/farming/accounting_replay.py:39-202`
- Modify: `src/oasis_agent/models/common.py:43-82` only if a narrowly scoped evidence identity helper is needed
- Test: `tests/unit/test_position_ledger.py`
- Test: `tests/unit/test_accounting_replay.py`

**Interfaces:**
- Consumes: `PositionLedgerEntry`, `EvidenceRef`, `ValuedLedgerEntry`, and complete `PositionTokenLedger.entries`.
- Produces: resolved entries whose evidence is bound to the entry block and transaction; unresolved entries that cannot be partially valued; `PositionAccountingReplay` with `COMPLETE`, `UNRESOLVED_SETTLEMENT`, or `UNRESOLVED_OWNERSHIP` status.

- [ ] **Step 1: Add failing tests for transaction-bound settlement evidence.**

Extend the existing ledger fixture with evidence carrying a transaction identity in its interpretation payload/hash only if the current evidence model supports it; otherwise add an explicit `transaction_hash` field to `PositionLedgerEntry`'s settlement binding only when it is already present in the retained evidence schema. The preferred minimal API is to add a required `settlement_transaction_hash: str | None` field to `PositionLedgerEntry`, defaulting to `None` for unresolved entries, and require it for resolved entries:

```python
def test_resolved_settlement_requires_the_event_transaction_hash(self):
    with self.assertRaisesRegex(ValueError, "transaction"):
        PositionLedgerEntry(
            self.event_block,
            "0x" + "77" * 32,
            1,
            2,
            self.observation(),
            -50,
            -80,
            SettlementAttribution.POSITION_TRACE,
            self.evidence(),
            settlement_transaction_hash="0x" + "88" * 32,
        )
```

If the repository's retained trace evidence already has a typed transaction field, use that existing field instead of adding a duplicate. Add the positive test with the entry transaction hash and the negative test with a different hash. The invariant is exact equality between the settlement attribution transaction and `entry.transaction_hash`; no block-only evidence can prove a settlement for a different transaction in the same block.

Add a selective-valuation regression test:

```python
def test_cannot_value_only_resolved_subset_of_complete_entries(self):
    first = self.entry(True)
    second = replace(self.entry(True), transaction_hash="0x" + "78" * 32, log_index=3)
    ledger = build_position_token_ledger(
        self.snapshot(self.b0, 100), self.snapshot(self.b2, 140), (first, second)
    )
    with self.assertRaisesRegex(ValueError, "exactly match"):
        replay_position_accounting(
            ledger,
            self.valuation(self.b0),
            self.valuation(self.b2),
            (ValuedLedgerEntry(first, self.valuation(self.b1)),),
        )
```

Add a test that an unresolved entry plus a resolved entry cannot be passed as a selectively valued subset and cannot produce a partial cumulative flow or PnL.

- [ ] **Step 2: Run the focused tests and verify the new tests fail.**

Run:

```bash
python3 -m pytest -q tests/unit/test_position_ledger.py tests/unit/test_accounting_replay.py
```

Expected: the transaction-binding test fails because the current entry model has no transaction-level settlement binding; selective valuation should either fail at construction or expose the precise missing guard.

- [ ] **Step 3: Implement transaction-level attribution with canonical hash validation.**

In `position_ledger.py`:

1. Add `settlement_transaction_hash: str | None = None` after `settlement_evidence` to preserve existing positional call compatibility.
2. Normalize it with `normalize_hash32` when present.
3. Reject it for unresolved entries.
4. Require it for resolved entries and require it to equal `self.transaction_hash`.
5. Keep `settlement_evidence.block == self.block` mandatory.
6. Keep attribution enum values limited to `POSITION_TRACE` and `EXCLUSIVE_TRANSACTION`.

The relevant invariant must be equivalent to:

```python
if self.owner_amount0 is None:
    if any(value is not None for value in (..., self.settlement_transaction_hash)):
        raise ValueError("unresolved settlement cannot claim attribution or evidence")
else:
    if (
        self.settlement_attribution is None
        or self.settlement_evidence is None
        or self.settlement_transaction_hash is None
    ):
        raise ValueError("resolved settlement requires attribution, transaction, and evidence")
    if self.settlement_transaction_hash != self.transaction_hash:
        raise ValueError("settlement evidence transaction does not match position event")
```

Update existing tests' resolved fixture entries to supply the same transaction hash. Do not infer it from `EvidenceRef.content_hash`; provenance hashes are not transaction identifiers.

In `accounting_replay.py`, retain the complete-entry tuple equality guard and make the unresolved branch explicit: if `ledger.settlement_complete` is false, reject any nonempty `valued_entries` and return `UNRESOLVED_SETTLEMENT` with both cumulative flow and PnL set to `None`.

- [ ] **Step 4: Run focused tests, then all position/accounting tests.**

Run:

```bash
python3 -m pytest -q tests/unit/test_position_ledger.py tests/unit/test_accounting_replay.py tests/unit/test_position_dataset.py
python3 -m ruff check src/oasis_agent/farming/position_ledger.py src/oasis_agent/farming/accounting_replay.py tests/unit/test_position_ledger.py tests/unit/test_accounting_replay.py
python3 -m ruff format --check src/oasis_agent/farming/position_ledger.py src/oasis_agent/farming/accounting_replay.py tests/unit/test_position_ledger.py tests/unit/test_accounting_replay.py
python3 -m mypy src/oasis_agent/farming/position_ledger.py src/oasis_agent/farming/accounting_replay.py
```

Expected: all tests and checks pass.

- [ ] **Step 5: Review the diff for compatibility and accounting semantics.**

Check every constructor call:

```bash
grep -R "PositionLedgerEntry(" -n src tests
```

Confirm all resolved fixtures carry the exact event transaction hash, unresolved fixtures carry no settlement metadata, and no code treats unresolved amounts as zero.

---

### Task 3: Add deterministic token-level reconciliation result and residual blocking

**Files:**
- Modify: `src/oasis_agent/farming/accounting_replay.py:49-202`
- Create: `src/oasis_agent/farming/reconciliation.py`
- Modify: `src/oasis_agent/farming/__init__.py`
- Test: `tests/unit/test_accounting_replay.py`
- Create: `tests/unit/test_position_reconciliation.py`

**Interfaces:**
- Consumes: independently signed integer token deltas from the opening/closing inventory and fully attributed position settlement entries.
- Produces: `TokenReconciliation`, `TokenResidual`, and `reconcile_position_tokens(...)`; nonzero or unknown residuals block `COMPLETE` accounting.

- [ ] **Step 1: Add failing tests for per-token residual handling.**

Create `tests/unit/test_position_reconciliation.py` with these tests:

```python
def test_zero_residual_closes_reconciliation():
    result = reconcile_position_tokens(
        opening_wallet0=1_000,
        closing_wallet0=900,
        opening_wallet1=2_000,
        closing_wallet1=2_080,
        owner_settlement0=-100,
        owner_settlement1=80,
        evidence=(settlement_evidence0, settlement_evidence1),
    )
    assert result.complete
    assert result.residual0 == 0
    assert result.residual1 == 0


def test_nonzero_token0_residual_blocks_completion():
    result = reconcile_position_tokens(
        opening_wallet0=1_000,
        closing_wallet0=901,
        opening_wallet1=2_000,
        closing_wallet1=2_080,
        owner_settlement0=-100,
        owner_settlement1=80,
        evidence=(settlement_evidence0, settlement_evidence1),
    )
    assert not result.complete
    assert result.residual0 == 1
    assert result.residual1 == 0
    assert result.status == ReconciliationStatus.UNRESOLVED_RESIDUAL


def test_unknown_settlement_does_not_become_zero():
    result = reconcile_position_tokens(
        opening_wallet0=1_000,
        closing_wallet0=900,
        opening_wallet1=2_000,
        closing_wallet1=2_080,
        owner_settlement0=None,
        owner_settlement1=None,
        evidence=(),
    )
    assert result.status == ReconciliationStatus.UNRESOLVED_SETTLEMENT
    assert result.residual0 is None
    assert result.residual1 is None
```

Use separate token0/token1 evidence references in the actual test fixture. Evidence is required for any supplied settlement total and must be block-pinned to the corresponding closing checkpoint; do not convert tokens into a common currency in this module.

- [ ] **Step 2: Run the new test and verify it fails because the reconciliation module does not exist.**

Run:

```bash
python3 -m pytest -q tests/unit/test_position_reconciliation.py
```

Expected: collection failure with an import error for `oasis_agent.farming.reconciliation`.

- [ ] **Step 3: Implement the minimal immutable reconciliation types.**

Create `reconciliation.py` with:

```python
class ReconciliationStatus(str, Enum):
    COMPLETE = "complete"
    UNRESOLVED_SETTLEMENT = "unresolved_settlement"
    UNRESOLVED_RESIDUAL = "unresolved_residual"

@dataclass(frozen=True, slots=True)
class TokenReconciliation:
    status: ReconciliationStatus
    residual0: int | None
    residual1: int | None
    evidence: tuple[EvidenceRef, ...]

    @property
    def complete(self) -> bool:
        return self.status is ReconciliationStatus.COMPLETE
```

Implement:

```python
def reconcile_position_tokens(
    *,
    opening_wallet0: int,
    closing_wallet0: int,
    opening_wallet1: int,
    closing_wallet1: int,
    owner_settlement0: int | None,
    owner_settlement1: int | None,
    evidence: tuple[EvidenceRef, ...],
) -> TokenReconciliation:
```

Validate all amounts as non-bool integers. If either settlement amount is `None`, require both to be `None`, return `UNRESOLVED_SETTLEMENT`, and set both residuals to `None`. If settlement amounts are present, require at least one evidence reference, require every evidence reference to be block-pinned, and compute independently:

```python
residual0 = closing_wallet0 - opening_wallet0 - owner_settlement0
residual1 = closing_wallet1 - opening_wallet1 - owner_settlement1
```

Return `COMPLETE` only when both residuals are zero; otherwise return `UNRESOLVED_RESIDUAL`. Do not accept an `external_cost1`, price, decimals conversion, or generic dictionary in this API.

Export the types from `farming/__init__.py` following existing package conventions.

- [ ] **Step 4: Integrate residual state into accounting without changing valuation scope.**

Add an optional keyword-only `token_reconciliation: TokenReconciliation | None = None` to `replay_position_accounting`. If provided and not complete, return a new `AccountingReplayStatus.UNRESOLVED_RESIDUAL` result with `cumulative_flow_value1=None` and `net_pnl1=None`; if absent, preserve current behavior for existing purely position-scoped tests. If the ledger is already unresolved, retain `UNRESOLVED_SETTLEMENT` precedence. If ownership continuity is unresolved, retain `UNRESOLVED_OWNERSHIP` precedence. Only a complete reconciliation can allow the existing complete accounting branch.

Update the status enum and `__all__` exports. The new status must never be treated as a numeric zero by reporting code.

- [ ] **Step 5: Add regression tests for precedence and no partial PnL.**

In `test_accounting_replay.py`, add:

```python
def test_nonzero_reconciliation_residual_blocks_pnl(self):
    ledger = build_position_token_ledger(
        self.snapshot(self.b0, 100), self.snapshot(self.b2, 100), ()
    )
    reconciliation = TokenReconciliation(
        ReconciliationStatus.UNRESOLVED_RESIDUAL, 1, 0, (self.evidence(self.b2, "receipt"),)
    )
    report = replay_position_accounting(
        ledger,
        self.valuation(self.b0),
        self.valuation(self.b2),
        (),
        token_reconciliation=reconciliation,
    )
    self.assertEqual(report.status, AccountingReplayStatus.UNRESOLVED_RESIDUAL)
    self.assertIsNone(report.net_pnl1)
```

Also test that an unresolved settlement remains `UNRESOLVED_SETTLEMENT` even when a residual object is supplied, and that a complete zero-residual object allows the existing complete result.

- [ ] **Step 6: Run focused reconciliation/accounting gates.**

Run:

```bash
python3 -m pytest -q tests/unit/test_position_reconciliation.py tests/unit/test_accounting_replay.py
python3 -m ruff check src/oasis_agent/farming/reconciliation.py src/oasis_agent/farming/accounting_replay.py src/oasis_agent/farming/__init__.py tests/unit/test_position_reconciliation.py tests/unit/test_accounting_replay.py
python3 -m ruff format --check src/oasis_agent/farming/reconciliation.py src/oasis_agent/farming/accounting_replay.py src/oasis_agent/farming/__init__.py tests/unit/test_position_reconciliation.py tests/unit/test_accounting_replay.py
python3 -m mypy src/oasis_agent/farming/reconciliation.py src/oasis_agent/farming/accounting_replay.py
```

Expected: all commands pass with no skipped tests.

---

### Task 4: Verify end-to-end replay behavior and update the verification record

**Files:**
- Modify: `tests/unit/test_position_dataset.py`
- Modify: `tests/unit/test_accounting_replay.py`
- Modify: `docs/verification/2026-09-04-position-accounting-foundation.md`
- Modify: `docs/verification/2026-09-05-evidence-binding-review.md` only if the new verification date/result belongs there; do not rewrite unrelated historical claims

**Interfaces:**
- Consumes: completed `replay_position_datasets`, `build_position_token_ledger`, `reconcile_position_tokens`, and `replay_position_accounting` contracts from Tasks 1–3.
- Produces: regression coverage proving raw core/NFT events, verified evidence, owner continuity, settlement attribution, and residual status cannot be mixed or silently downgraded.

- [ ] **Step 1: Add an end-to-end unresolved-path test.**

Extend the existing raw-dataset scenario so that it runs through:

```python
result = run_replay(tmp_path, *scenario())
assert result.trust is IngestionTrust.SINGLE_PROVIDER_RESEARCH
assert result.ledger.unresolved_settlements == 1
report = replay_position_accounting(
    result.ledger,
    opening_valuation,
    closing_valuation,
    (),
)
assert report.status is AccountingReplayStatus.UNRESOLVED_SETTLEMENT
assert report.net_pnl1 is None
```

The test must use the existing `PositionDatasetReplay` output rather than manually constructing a ledger, proving that raw `ModifyLiquidity` and NFT `Transfer` events still do not fabricate token settlement.

- [ ] **Step 2: Add an end-to-end resolved-path test with exact transaction binding.**

Construct a resolved `PositionLedgerEntry` for the same core event transaction with a synthetic, block-pinned settlement evidence reference and exact `settlement_transaction_hash`. Pass a complete zero-residual `TokenReconciliation` to `replay_position_accounting` and assert:

```python
assert report.status is AccountingReplayStatus.COMPLETE
assert report.cumulative_flow_value1 is not None
assert report.net_pnl1 is not None
```

The test must also prove a different transaction hash raises `ValueError` before accounting.

- [ ] **Step 3: Run the complete repository verification protocol.**

Prefer the project build system:

```bash
make check
```

Then run the exact required gates individually so failures are attributable:

```bash
python3 -m pytest -q
python3 -m ruff check src tests scripts
python3 -m ruff format --check src tests scripts
python3 -m mypy src
git diff --check
```

If the repository contains contract checks in the Makefile, run the relevant target as well:

```bash
make contract-check
```

Expected: every available command exits zero. If a concurrent unrelated edit causes a gate failure, preserve it, report the exact file/error, and do not claim the repository is green.

- [ ] **Step 4: Inspect the final diff for unsafe accounting defaults.**

Run:

```bash
git diff -- src/oasis_agent/farming tests/unit/test_position_dataset.py tests/unit/test_position_ledger.py tests/unit/test_accounting_replay.py tests/unit/test_position_reconciliation.py docs/verification
```

Verify manually that:

- no `None` settlement is converted to `0`;
- no partial ledger is valued;
- no evidence from another block, chain, source, dataset scope, or transaction is accepted;
- unresolved statuses remain visible to callers and reporting;
- no production signing/broadcast path was introduced;
- no reference-price/LVR or routing claims were added.

- [ ] **Step 5: Update the verification note with exact observed results.**

Add only verified facts to `docs/verification/2026-09-04-position-accounting-foundation.md` (or a new dated verification file if the project owner prefers): implementation boundary, focused test count, full gate commands/results, and explicit remaining blockers. State that this remains offline/research validation and that live two-provider evidence, pinned fork lifecycle, trace-backed settlement attribution, and production release gates remain unresolved unless actually demonstrated by tests/artifacts.

- [ ] **Step 6: Stop before commit and ask for authorization.**

Run:

```bash
git status --short
git diff --stat
```

Do not commit, deploy, sign, broadcast, or push. Report the exact tests/gates and residual risks, then ask: `代码已验证通过。是否创建 commit？`

---

## Self-Review Checklist

- **Spec coverage:** Task 1 covers verified dataset/evidence scope and block lineage; Task 2 covers transaction-bound attribution and selective valuation rejection; Task 3 covers independent token residuals and fail-closed accounting status; Task 4 covers end-to-end regression and all required gates. No task adds LVR, reference pricing, or routing.
- **Placeholder scan:** No `TODO`, `TBD`, or unspecified validation step is required; every new interface has concrete fields, status values, and commands.
- **Type consistency:** `AccountingReplayStatus.UNRESOLVED_RESIDUAL`, `TokenReconciliation`, `ReconciliationStatus`, and `reconcile_position_tokens` are defined in Task 3 before Task 4 uses them. `settlement_transaction_hash` is appended after existing optional fields to avoid breaking current positional constructors.
- **Safety consistency:** unresolved settlement, unresolved ownership, and nonzero residual all return `net_pnl1=None`; only exact evidence-bound complete inputs can produce a numeric PnL.
- **Scope consistency:** no task authorizes live chain activity or makes a profitability claim; all docs retain the project's current research-only boundary.
