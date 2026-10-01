"""Live admission reads: capture at the signing block, then fail closed.

The signing sidecar denies unless an independent admission boundary can price the
request against an account frame observed at the request's own block. These tests
drive that capture path with the two-provider fixtures: a fresh session pair per
call, an inception baseline that refuses a native declaration, a capture that must
advance the lineage, a request whose block the capture does not match, a
multi-chain manifest that one block cannot reconcile, and native declarations
outside the manifest. The last tests wire the real guard and the operator CLI, so
the boundary is exercised as the sidecar builds it rather than in isolation,
including the shadow window that a signing authority must present.
"""

import argparse
import time
from dataclasses import replace
from pathlib import Path

import pytest

from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.execution.account_capture import capture_observation, one_shot_sessions
from oasis_agent.execution.account_risk_guard import AccountRiskGuard
from oasis_agent.execution.live_account_reader import CaptureAccountRiskReader
from oasis_agent.execution.lp_signer_sidecar import (
    LpSidecarConfig,
    LpSignerSidecar,
    recover_signer,
)
from oasis_agent.models.common import BlockRef, canonical_json, normalize_address
from oasis_agent.risk.account_risk import GlobalRiskJournal, PortfolioFrame
from oasis_agent.risk.portfolio_manifest import PortfolioManifest
from oasis_agent.risk.shadow_acceptance import (
    CANARY_WINDOW_REQUIREMENTS,
    ObservationRun,
    ShadowRequirements,
    build_shadow_window,
)
from oasis_agent.rpc.budget import BudgetPolicy
from scripts.run_lp_signer_sidecar import _risk_guard
from tests.unit.test_account_risk_reader import (
    ACCOUNT,
    BLOCK_HASH,
    BLOCK_NUMBER,
    BLOCK_TWO,
    HASH_TWO,
    TIMESTAMP,
    InventoryFixture,
    baseline,
    bound_flow_fixture,
    chain_inventory,
    flow_session,
    inventory_session,
    later,
    manifest,
)
from tests.unit.test_lp_signer_sidecar import (
    KEY,
    MAX_FEE_CAP,
    PRIORITY_CAP,
    FixtureKeyProvider,
    make_policy,
    make_request,
)

URLS = {"rpc_a": "https://a.invalid", "rpc_b": "https://b.invalid"}
POLICIES = (BudgetPolicy("rpc_a", 32, 3000), BudgetPolicy("rpc_b", 32, 3000))


def inventory_pair(sources: tuple[str, str], fixture: InventoryFixture) -> tuple:
    """Two independent providers for one pinned inventory quorum."""
    return (
        inventory_session(sources[0], fixture),
        inventory_session(sources[1], replace(fixture, methods=[], block_calls=0)),
    )


def flow_pair(sources: tuple[str, str], fixture: InventoryFixture, logs: tuple = ()) -> tuple:
    return (
        flow_session(sources[0], bound_flow_fixture(fixture, incoming=logs)),
        flow_session(sources[1], bound_flow_fixture(fixture, incoming=logs)),
    )


def capture_factory(fixture: InventoryFixture) -> object:
    """Call 0 serves the inventory quorum; every later call serves one empty flow window."""
    calls = {"count": 0}

    def build(sources: tuple[str, str]) -> tuple:
        index = calls["count"]
        calls["count"] += 1
        return inventory_pair(sources, fixture) if index == 0 else flow_pair(sources, fixture)

    return build


def live_reader(
    store: EvidenceStore,
    fixture: InventoryFixture,
    *,
    previous: PortfolioFrame | None = None,
    native: dict[str, tuple[int, int]] | None = None,
) -> CaptureAccountRiskReader:
    return CaptureAccountRiskReader(
        store,
        manifest(fixture),
        sessions=capture_factory(fixture),  # type: ignore[arg-type]
        previous=lambda: previous,
        native=native,
    )


def at_block(number: int, block_hash: str, timestamp: int) -> object:
    """The same signing request, rebound to the block the fixture captures."""
    request = make_request(ACCOUNT)
    return replace(
        request,
        signing=replace(
            request.signing, current_block=BlockRef(4663, number, block_hash, timestamp)
        ),
    )


def test_one_shot_sessions_are_fresh_and_keep_the_declared_order(tmp_path) -> None:
    budget = tmp_path / "budget.db"
    first = one_shot_sessions(("rpc_a", "rpc_b"), urls=URLS, policies=POLICIES, budget=budget)
    second = one_shot_sessions(("rpc_a", "rpc_b"), urls=URLS, policies=POLICIES, budget=budget)
    assert [session[2] for session in first] == ["rpc_a", "rpc_b"]
    assert first[0][0] is not first[1][0]
    assert first[0][1] is not first[1][1]
    # An exact capture may not reuse a recorder, so every call is a new object.
    assert first[0][0] is not second[0][0]
    assert first[0][1] is not second[0][1]
    assert not first[0][1].exchanges


def test_one_shot_sessions_refuse_a_repeated_provider(tmp_path) -> None:
    with pytest.raises(ValueError, match="distinct fresh provider sessions required"):
        one_shot_sessions(
            ("rpc_a", "rpc_a"),
            urls=URLS,
            policies=POLICIES,
            budget=tmp_path / "budget.db",
        )


def test_an_inception_capture_refuses_a_native_declaration(tmp_path) -> None:
    """An inception baseline is a zero-flow statement; movement cannot be asserted."""
    store = EvidenceStore(tmp_path / "inception.db")
    fixture = InventoryFixture()
    try:
        with pytest.raises(ValueError, match="an inception baseline cannot declare native"):
            capture_observation(
                store,
                chain_inventory(fixture),
                block=BLOCK_NUMBER,
                previous=None,
                native_gas_units=1,
                non_trading_native_units=0,
                sessions=capture_factory(fixture),  # type: ignore[arg-type]
            )
    finally:
        store.close()


def test_a_capture_must_advance_the_previous_frame(tmp_path) -> None:
    store = EvidenceStore(tmp_path / "stale.db")
    fixture = InventoryFixture()
    try:
        prior = baseline(store, fixture)
        with pytest.raises(ValueError, match="does not advance the previous frame"):
            capture_observation(
                store,
                chain_inventory(fixture),
                block=prior.accounts[0].block_number,
                previous=prior.accounts[0],
                native_gas_units=0,
                non_trading_native_units=0,
                sessions=capture_factory(fixture),  # type: ignore[arg-type]
            )
    finally:
        store.close()


def test_the_live_reader_rebuilds_the_frame_at_the_signing_block(tmp_path) -> None:
    store = EvidenceStore(tmp_path / "inception-frame.db")
    fixture = InventoryFixture()
    try:
        reader = live_reader(store, fixture)
        frame, amount = reader.reconcile_and_price(make_request(ACCOUNT))
        account = frame.accounts[0]
        assert (account.key, account.block_number, account.block_hash) == (
            chain_inventory(fixture).key,
            BLOCK_NUMBER,
            BLOCK_HASH,
        )
        assert (account.unresolved_operations, amount) == (0, amount)
        assert amount > 0
    finally:
        store.close()


def test_the_live_reader_reconciles_along_one_lineage(tmp_path) -> None:
    """A later block is reconciled against the ledger's own previous frame."""
    store = EvidenceStore(tmp_path / "lineage.db")
    fixture = InventoryFixture()
    second = later(fixture)
    try:
        prior = baseline(store, fixture)
        reader = live_reader(
            store, second, previous=prior, native={chain_inventory(second).key: (0, 0)}
        )
        frame, amount = reader.reconcile_and_price(at_block(BLOCK_TWO, HASH_TWO, TIMESTAMP + 30))
        account = frame.accounts[0]
        assert (account.block_number, account.movements["native"]) == (BLOCK_TWO, 0)
        assert (account.unresolved_operations, account.non_trading_flow_usd_micros) == (0, 0)
        assert amount > 0
    finally:
        store.close()


def test_the_live_reader_refuses_a_capture_that_is_not_the_signing_block(tmp_path) -> None:
    """A provider that answers another height, or a same-height reorg, must refuse."""
    store = EvidenceStore(tmp_path / "binding.db")
    fixture = InventoryFixture()
    try:
        moved = live_reader(store, later(fixture))
        with pytest.raises(ValueError, match="RPC returned a different block number"):
            moved.reconcile_and_price(make_request(ACCOUNT))
        reorged = live_reader(store, replace(fixture, block_hash=HASH_TWO))
        with pytest.raises(ValueError, match="account block differs from the signing request"):
            reorged.reconcile_and_price(make_request(ACCOUNT))
    finally:
        store.close()


def test_the_live_reader_refuses_a_multi_chain_manifest(tmp_path) -> None:
    """One signing block cannot pin two chains, so the frame is refused, not mixed."""
    store = EvidenceStore(tmp_path / "multi-chain.db")
    fixture = InventoryFixture()
    other = replace(InventoryFixture(), chain_id=8453)
    try:
        with pytest.raises(ValueError, match="single-chain manifest"):
            CaptureAccountRiskReader(
                store,
                manifest(fixture, other),
                sessions=capture_factory(fixture),  # type: ignore[arg-type]
                previous=lambda: None,
            )
    finally:
        store.close()


@pytest.mark.parametrize(
    ("native", "expected"),
    [
        ({"8453:0x" + "00" * 20: (0, 0)}, "must name manifest accounts"),
        ({"4663:" + ACCOUNT: (0, -1)}, "nonnegative unit pairs"),
        ({"4663:" + ACCOUNT: (0, 0, 0)}, "nonnegative unit pairs"),
        ({"4663:" + ACCOUNT: [0, 0]}, "nonnegative unit pairs"),
        ({"4663:" + ACCOUNT: (True, 0)}, "nonnegative unit pairs"),
    ],
)
def test_the_live_reader_refuses_malformed_native_declarations(
    tmp_path, native: dict, expected: str
) -> None:
    store = EvidenceStore(tmp_path / "native.db")
    fixture = InventoryFixture()
    try:
        with pytest.raises(ValueError, match=expected):
            live_reader(store, fixture, native=native)  # type: ignore[arg-type]
    finally:
        store.close()


def test_the_live_reader_refuses_a_lineage_that_omits_the_signing_account(tmp_path) -> None:
    store = EvidenceStore(tmp_path / "orphan.db")
    fixture = InventoryFixture()
    try:
        reader = live_reader(store, fixture, previous=PortfolioFrame(accounts=()))
        with pytest.raises(ValueError, match="previous frame does not cover the signing account"):
            reader.reconcile_and_price(make_request(ACCOUNT))
    finally:
        store.close()


def test_the_live_guard_reserves_capacity_at_the_signing_block(tmp_path) -> None:
    """The real admission boundary, fed by live capture, reserves then releases."""
    store = EvidenceStore(tmp_path / "guard.db")
    fixture = InventoryFixture()
    declared = manifest(fixture)
    try:
        guard = AccountRiskGuard(
            GlobalRiskJournal(tmp_path / "ledger.db", declared.policy),
            live_reader(store, fixture),
            clock=lambda: TIMESTAMP,
        )
        request = make_request(ACCOUNT)
        assert len(guard.reserve(request)) == 64
        state = guard.journal.current_state()
        # Live capture must price exactly what retained-evidence replay prices.
        assert [reservation.usd_micros for reservation in state.reservations] == [3_400_001]
        assert state.frame is not None and state.frame.accounts[0].block_hash == BLOCK_HASH
        guard.release(request, reason="signing attempt produced no transaction")
        assert guard.journal.current_state().reservations == ()
    finally:
        store.close()


def test_the_sidecar_signs_only_through_live_admission(tmp_path) -> None:
    """End to end: live capture at the signing block, ledger admission, then signature."""
    store = EvidenceStore(tmp_path / "end-to-end.db")
    fixture = InventoryFixture()
    declared = manifest(fixture)
    guard = AccountRiskGuard(
        GlobalRiskJournal(tmp_path / "ledger.db", declared.policy),
        live_reader(store, fixture),
        clock=lambda: TIMESTAMP,
    )
    config = LpSidecarConfig(
        policy=make_policy(ACCOUNT),
        max_fee_per_gas=MAX_FEE_CAP,
        max_priority_fee_per_gas=PRIORITY_CAP,
        journal_path=tmp_path / "sidecar.db",
        provider=FixtureKeyProvider(KEY),
        risk_guard=guard,
    )
    try:
        with LpSignerSidecar(config) as sidecar:
            decision = sidecar.handle(make_request(ACCOUNT))
        assert decision.outcome == "signed"
        assert not decision.reasons
        raw = bytes.fromhex(decision.raw_transaction_hex or "")
        assert normalize_address(recover_signer(raw)) == ACCOUNT
        # The signature exists only because live admission reserved its worst case.
        assert [item.usd_micros for item in guard.journal.current_state().reservations] == [
            3_400_001
        ]
    finally:
        guard.journal.close()
        store.close()


def risk_args(**overrides: object) -> argparse.Namespace:
    names: dict[str, object] = {
        "risk_manifest": None,
        "risk_journal": None,
        "risk_store": None,
        "risk_budget": None,
        "risk_rpc": [],
        "risk_env_file": Path(".env"),
        "risk_native": None,
        "risk_interval_ms": 3000,
        "risk_max_requests": 256,
        "canary_window": None,
        "canary_cap_usd": None,
        "signer_socket": None,
        "signer_account": None,
    }
    names.update(overrides)
    return argparse.Namespace(**names)


AUTHORITY = FixtureKeyProvider(KEY)


def recent_runs(*, age_seconds: int = 120, seconds: int = 60) -> tuple[ObservationRun, ...]:
    """Three accepted runs spanning the repository minimum, ending `age_seconds` ago."""
    end = int(time.time()) - age_seconds
    return tuple(
        ObservationRun(
            seq=index + 1,
            block_number=BLOCK_NUMBER + index,
            block_hash=BLOCK_HASH,
            timestamp=end - 2 * seconds + index * seconds,
            state_digest=f"{index + 1:064x}",
            report_digest=f"{index + 101:064x}",
            cumulative_loss_usd_micros=0,
            unresolved_operations=0,
            halted=False,
        )
        for index in range(3)
    )


def write_manifest(path: Path, declared: PortfolioManifest) -> Path:
    path.write_bytes(canonical_json(declared.model_dump(mode="json")) + b"\n")
    return path


def write_window(
    path: Path,
    declared: PortfolioManifest,
    *,
    runs: tuple[ObservationRun, ...] | None = None,
    requirements: ShadowRequirements = CANARY_WINDOW_REQUIREMENTS,
    manifest_digest: str | None = None,
) -> Path:
    window = build_shadow_window(
        recent_runs() if runs is None else runs,
        account_key=declared.chains[0].key,
        manifest_digest=declared.digest if manifest_digest is None else manifest_digest,
        policy_digest=declared.policy.digest,
        requirements=requirements,
    )
    path.write_bytes(window.payload + b"\n")
    return path


def test_the_cli_needs_nothing_for_admission_and_refuses_half_of_it(tmp_path) -> None:
    assert _risk_guard(risk_args(), None, authority=None) is None
    with pytest.raises(ValueError, match="admission options require a risk manifest"):
        _risk_guard(risk_args(risk_rpc=["4663:rpc_a:rpc_b"]), None, authority=None)
    declared = manifest(InventoryFixture())
    manifest_path = write_manifest(tmp_path / "manifest.json", declared)
    with pytest.raises(ValueError, match="needs its retained evidence store"):
        _risk_guard(risk_args(risk_manifest=manifest_path), None, authority=None)
    store = EvidenceStore(tmp_path / "evidence.db")
    try:
        with pytest.raises(ValueError, match="needs its journal and budget ledger"):
            _risk_guard(
                risk_args(risk_manifest=manifest_path, risk_journal=tmp_path / "ledger.db"),
                store,
                authority=None,
            )
    finally:
        store.close()


def test_the_cli_refuses_admission_it_cannot_bind(tmp_path) -> None:
    """A swapped provider pair or a multi-chain manifest is refused, with no state written."""
    base = InventoryFixture()
    other = replace(InventoryFixture(), chain_id=8453)
    store = EvidenceStore(tmp_path / "evidence.db")
    env_file = tmp_path / ".env"
    env_file.write_text("rpc_a=https://a.invalid\nrpc_b=https://b.invalid\n")
    try:
        single = write_manifest(tmp_path / "single.json", manifest(base))
        with pytest.raises(ValueError, match="must match the declared manifest providers"):
            _risk_guard(
                risk_args(
                    risk_manifest=single,
                    risk_journal=tmp_path / "a.db",
                    risk_budget=tmp_path / "b.db",
                    risk_rpc=["4663:rpc_b:rpc_a"],
                    risk_env_file=env_file,
                ),
                store,
                authority=None,
            )
        multi = write_manifest(tmp_path / "multi.json", manifest(base, other))
        journal_path = tmp_path / "multi.db"
        with pytest.raises(ValueError, match="single-chain manifest"):
            _risk_guard(
                risk_args(
                    risk_manifest=multi,
                    risk_journal=journal_path,
                    risk_budget=tmp_path / "c.db",
                    risk_rpc=["4663:rpc_a:rpc_b", "8453:rpc_a:rpc_b"],
                    risk_env_file=env_file,
                ),
                store,
                authority=None,
            )
        # A reader the CLI refused must leave no open ledger and no admitted state.
        reopened = GlobalRiskJournal(journal_path, manifest(base, other).policy)
        try:
            state = reopened.current_state()
            assert (state.frame, state.reservations) == (None, ())
        finally:
            reopened.close()
    finally:
        store.close()


def test_the_cli_builds_the_guard_the_sidecar_will_use(tmp_path) -> None:
    declared = manifest(InventoryFixture())
    manifest_path = write_manifest(tmp_path / "manifest.json", declared)
    env_file = tmp_path / ".env"
    env_file.write_text("rpc_a=https://a.invalid\nrpc_b=https://b.invalid\n")
    store = EvidenceStore(tmp_path / "evidence.db")
    risk = None
    try:
        risk = _risk_guard(
            risk_args(
                risk_manifest=manifest_path,
                risk_journal=tmp_path / "ledger.db",
                risk_store=tmp_path / "evidence.db",
                risk_budget=tmp_path / "budget.db",
                risk_rpc=["4663:rpc_a:rpc_b"],
                risk_env_file=env_file,
            ),
            store,
            authority=None,
        )
        assert risk is not None
        guard, journal = risk
        assert guard.policy_digest == declared.policy.digest
        assert journal.policy == declared.policy
        assert journal.current_state().frame is None
    finally:
        if risk is not None:
            risk[1].close()
        store.close()


def test_a_signing_authority_cannot_start_without_a_shadow_window(tmp_path) -> None:
    """The path that can sign is unstartable without the canary precondition."""
    declared = manifest(InventoryFixture())
    manifest_path = write_manifest(tmp_path / "manifest.json", declared)
    env_file = tmp_path / ".env"
    env_file.write_text("rpc_a=https://a.invalid\nrpc_b=https://b.invalid\n")
    store = EvidenceStore(tmp_path / "evidence.db")
    journal_path = tmp_path / "ledger.db"
    try:
        with pytest.raises(ValueError, match="needs a shadow window and its canary cap"):
            _risk_guard(
                risk_args(
                    risk_manifest=manifest_path,
                    risk_journal=journal_path,
                    risk_budget=tmp_path / "budget.db",
                    risk_rpc=["4663:rpc_a:rpc_b"],
                    risk_env_file=env_file,
                ),
                store,
                authority=AUTHORITY,
            )
        # Half of the pair is still nothing, and no ledger was opened either way.
        with pytest.raises(ValueError, match="needs a shadow window and its canary cap"):
            _risk_guard(
                risk_args(
                    risk_manifest=manifest_path,
                    risk_journal=journal_path,
                    risk_budget=tmp_path / "budget.db",
                    risk_rpc=["4663:rpc_a:rpc_b"],
                    risk_env_file=env_file,
                    canary_cap_usd="50",
                ),
                store,
                authority=AUTHORITY,
            )
        # A window without the authority that would consume it is a configuration
        # error, never a silently ignored input.
        window_path = write_window(tmp_path / "window.json", declared)
        with pytest.raises(ValueError, match="only admitted with a signing authority"):
            _risk_guard(
                risk_args(
                    risk_manifest=manifest_path,
                    risk_journal=journal_path,
                    risk_budget=tmp_path / "budget.db",
                    risk_rpc=["4663:rpc_a:rpc_b"],
                    risk_env_file=env_file,
                    canary_window=window_path,
                    canary_cap_usd="50",
                ),
                store,
                authority=None,
            )
        assert not journal_path.exists()
    finally:
        store.close()


def test_the_cli_refuses_a_window_that_does_not_admit_the_cap(tmp_path) -> None:
    """A stale, foreign, altered or over-cap window is refused before any ledger."""
    declared = manifest(InventoryFixture())
    manifest_path = write_manifest(tmp_path / "manifest.json", declared)
    env_file = tmp_path / ".env"
    env_file.write_text("rpc_a=https://a.invalid\nrpc_b=https://b.invalid\n")
    store = EvidenceStore(tmp_path / "evidence.db")
    authority = AUTHORITY
    base = {
        "risk_manifest": manifest_path,
        "risk_journal": tmp_path / "ledger.db",
        "risk_budget": tmp_path / "budget.db",
        "risk_rpc": ["4663:rpc_a:rpc_b"],
        "risk_env_file": env_file,
        "signer_socket": Path("/private/tmp/oasis-signer.sock"),
        "signer_account": ACCOUNT,
    }
    try:
        # USD 300 exceeds the owner's USD 200 daily loss budget.
        window_path = write_window(tmp_path / "window.json", declared)
        with pytest.raises(ValueError, match="canary cap exceeds the daily loss budget"):
            _risk_guard(
                risk_args(**base, canary_window=window_path, canary_cap_usd="300"),
                store,
                authority=authority,
            )
        # A window observed under another manifest cannot describe this one.
        foreign = write_window(tmp_path / "foreign.json", declared, manifest_digest="b" * 64)
        with pytest.raises(ValueError, match="observed under a different manifest"):
            _risk_guard(
                risk_args(**base, canary_window=foreign, canary_cap_usd="50"),
                store,
                authority=authority,
            )
        # A window whose staleness allowance is wider than the repository minimum
        # would make this gate decorative.
        loose = write_window(
            tmp_path / "loose.json",
            declared,
            requirements=ShadowRequirements(
                min_runs=3,
                min_span_seconds=120,
                max_gap_seconds=300,
                max_staleness_seconds=86_400,
            ),
        )
        with pytest.raises(ValueError, match="weaker than the repository minimum"):
            _risk_guard(
                risk_args(**base, canary_window=loose, canary_cap_usd="50"),
                store,
                authority=authority,
            )
        # Runs older than the staleness allowance are refused too.
        stale = write_window(tmp_path / "stale.json", declared, runs=recent_runs(age_seconds=3600))
        with pytest.raises(ValueError, match="shadow window is stale"):
            _risk_guard(
                risk_args(**base, canary_window=stale, canary_cap_usd="50"),
                store,
                authority=authority,
            )
        # An edited record is not the record that was recorded: this one inflates
        # its own derived span, which the re-derivation refuses.
        altered = write_window(tmp_path / "altered.json", declared)
        retained = altered.read_bytes().replace(b'"span_seconds":120', b'"span_seconds":121')
        assert retained != altered.read_bytes()
        altered.write_bytes(retained)
        with pytest.raises(ValueError, match="altered after it was recorded"):
            _risk_guard(
                risk_args(**base, canary_window=altered, canary_cap_usd="50"),
                store,
                authority=authority,
            )
        assert not (tmp_path / "ledger.db").exists()
    finally:
        store.close()


def test_the_cli_starts_the_signing_path_on_an_admitted_window(tmp_path) -> None:
    declared = manifest(InventoryFixture())
    manifest_path = write_manifest(tmp_path / "manifest.json", declared)
    env_file = tmp_path / ".env"
    env_file.write_text("rpc_a=https://a.invalid\nrpc_b=https://b.invalid\n")
    store = EvidenceStore(tmp_path / "evidence.db")
    window_path = write_window(tmp_path / "window.json", declared)
    risk = None
    try:
        risk = _risk_guard(
            risk_args(
                risk_manifest=manifest_path,
                risk_journal=tmp_path / "ledger.db",
                risk_budget=tmp_path / "budget.db",
                risk_rpc=["4663:rpc_a:rpc_b"],
                risk_env_file=env_file,
                signer_socket=Path("/private/tmp/oasis-signer.sock"),
                signer_account=ACCOUNT,
                canary_window=window_path,
                canary_cap_usd="50",
            ),
            store,
            authority=AUTHORITY,
        )
        assert risk is not None
        guard, journal = risk
        assert guard.policy_digest == declared.policy.digest
        # The admission is a precondition, never a reservation.
        assert journal.current_state().reservations == ()
    finally:
        if risk is not None:
            risk[1].close()
        store.close()
