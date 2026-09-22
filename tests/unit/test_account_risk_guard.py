"""Signer-to-ledger boundary: independent reader binding, fail-closed admission, release."""

import json
from pathlib import Path

import pytest

from oasis_agent.execution.account_risk_guard import AccountRiskGuard
from oasis_agent.execution.lp_signer_sidecar import (
    LpSidecarConfig,
    LpSignerSidecar,
    NullSigningKeyProvider,
    SigningKeyProvider,
    sidecar_request_hash,
)
from oasis_agent.risk.account_risk import (
    AccountFrame,
    AssetBalance,
    GlobalRiskJournal,
    PortfolioFrame,
)
from tests.unit.test_lp_signer_sidecar import (
    ACCOUNT,
    KEY,
    MAX_FEE_CAP,
    OTHER_KEY,
    PRIORITY_CAP,
    RISK_ASSET,
    RISK_PER_ATTEMPT_USD_MICROS,
    RISK_POLICY,
    FixtureKeyProvider,
    make_policy,
    make_request,
)

BLOCK_HASH = "0x" + "cd" * 32


def fixed_clock() -> int:
    return 1_010


class Reader:
    """Deterministic whole-inventory reader; production readers stay operator-supplied."""

    def __init__(
        self,
        *,
        block_number: int = 101,
        block_hash: str = BLOCK_HASH,
        amount: int = RISK_PER_ATTEMPT_USD_MICROS,
    ) -> None:
        self.block_number = block_number
        self.block_hash = block_hash
        self.amount = amount

    def reconcile_and_price(self, request) -> tuple[PortfolioFrame, int]:  # type: ignore[no-untyped-def]
        signing = request.signing
        block = signing.current_block
        asset = AssetBalance(
            asset_id=RISK_ASSET,
            units=500_000_000,
            decimals=6,
            price_usd_micros_numerator=1_000_000,
            price_denominator=1,
            price_timestamp=block.timestamp,
            role="cash",
            evidence_hash="e" * 64,
        )
        frame = AccountFrame(
            chain_id=signing.intent.chain_id,
            account=signing.account,
            block_number=self.block_number,
            block_hash=self.block_hash,
            timestamp=block.timestamp,
            assets=(asset,),
            movements={RISK_ASSET: 0},
            external_flow_usd_micros=0,
            inventory_evidence_hash="f" * 64,
            unresolved_operations=0,
        )
        return PortfolioFrame(accounts=(frame,)), self.amount


def guard_for(
    tmp_path: Path, *, reader: Reader | None = None, name: str = "global-risk.db"
) -> AccountRiskGuard:
    return AccountRiskGuard(
        GlobalRiskJournal(tmp_path / name, RISK_POLICY),
        reader if reader is not None else Reader(),
        clock=fixed_clock,
    )


def sidecar_for(
    tmp_path: Path,
    *,
    guard: AccountRiskGuard | None,
    provider: SigningKeyProvider | None,
    name: str = "sidecar.db",
) -> LpSignerSidecar:
    return LpSignerSidecar(
        LpSidecarConfig(
            policy=make_policy(ACCOUNT),
            max_fee_per_gas=MAX_FEE_CAP,
            max_priority_fee_per_gas=PRIORITY_CAP,
            journal_path=tmp_path / name,
            provider=provider,
            risk_guard=guard,
        )
    )


def reservations(guard: AccountRiskGuard) -> list[str]:
    row = guard.journal.db.execute(
        "SELECT payload FROM global_events ORDER BY seq DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return []
    return [r["request_hash"] for r in json.loads(row[0])["state"]["reservations"]]


def test_guard_policy_digest_is_the_ledger_fingerprint(tmp_path: Path) -> None:
    guard = guard_for(tmp_path)
    assert guard.policy_digest == guard.journal.policy.digest
    assert len(guard.policy_digest) == 64


def test_guard_binds_the_reader_to_the_signing_block(tmp_path: Path) -> None:
    request = make_request(ACCOUNT)
    stale_number = guard_for(tmp_path, reader=Reader(block_number=102), name="a.db")
    with pytest.raises(ValueError, match="independent account block differs"):
        stale_number.reserve(request)
    stale_hash = guard_for(tmp_path, reader=Reader(block_hash="0x" + "ee" * 32), name="b.db")
    with pytest.raises(ValueError, match="independent account block differs"):
        stale_hash.reserve(request)
    assert reservations(stale_number) == []
    assert reservations(stale_hash) == []


def test_guard_reserve_binds_the_bare_request_digest_without_duplicating_it(
    tmp_path: Path,
) -> None:
    guard = guard_for(tmp_path)
    request = make_request(ACCOUNT)
    first = guard.reserve(request)
    second = guard.reserve(request)
    assert len(first) == 64 and len(second) == 64
    assert reservations(guard) == [sidecar_request_hash(request)[2:]]


def test_release_restores_capacity_for_a_new_attempt(tmp_path: Path) -> None:
    guard = guard_for(tmp_path, reader=Reader(amount=300_000_000))
    first = make_request(ACCOUNT, nonce=91)
    second = make_request(ACCOUNT, nonce=92)
    guard.reserve(first)
    assert reservations(guard) == [sidecar_request_hash(first)[2:]]
    with pytest.raises(ValueError, match="blocked admission"):
        guard.reserve(second)
    guard.release(first, reason="signing attempt produced no transaction")
    assert reservations(guard) == []
    guard.reserve(second)
    assert reservations(guard) == [sidecar_request_hash(second)[2:]]


def test_sidecar_reserves_capital_when_it_signs(tmp_path: Path) -> None:
    guard = guard_for(tmp_path)
    request = make_request(ACCOUNT)
    with sidecar_for(tmp_path, guard=guard, provider=FixtureKeyProvider(KEY)) as sidecar:
        decision = sidecar.handle(request)
    assert decision.outcome == "signed"
    assert reservations(guard) == [sidecar_request_hash(request)[2:]]


def test_sidecar_releases_capital_when_the_provider_fails(tmp_path: Path) -> None:
    guard = guard_for(tmp_path)
    with sidecar_for(tmp_path, guard=guard, provider=NullSigningKeyProvider()) as sidecar:
        decision = sidecar.handle(make_request(ACCOUNT))
    assert decision.outcome == "denied"
    assert "signing provider failed" in decision.reasons
    assert reservations(guard) == []


def test_sidecar_releases_capital_when_the_signature_recovers_elsewhere(tmp_path: Path) -> None:
    guard = guard_for(tmp_path)
    with sidecar_for(tmp_path, guard=guard, provider=FixtureKeyProvider(OTHER_KEY)) as sidecar:
        decision = sidecar.handle(make_request(ACCOUNT))
    assert decision.outcome == "denied"
    assert any("does not recover" in reason for reason in decision.reasons)
    assert reservations(guard) == []


def test_sidecar_denies_and_reserves_nothing_without_an_independent_guard(tmp_path: Path) -> None:
    with sidecar_for(tmp_path, guard=None, provider=FixtureKeyProvider(KEY)) as sidecar:
        decision = sidecar.handle(make_request(ACCOUNT))
    assert decision.outcome == "denied"
    assert "no independent account risk guard is configured" in decision.reasons
    assert decision.raw_transaction_hex is None


def test_guard_denial_beyond_the_capital_ceiling_stops_the_provider(tmp_path: Path) -> None:
    class CountingProvider(FixtureKeyProvider):
        calls = 0

        def sign(self, digest: bytes) -> tuple[int, int, int]:
            self.calls += 1
            return super().sign(digest)

    guard = guard_for(tmp_path, reader=Reader(amount=600_000_000))
    provider = CountingProvider(KEY)
    with sidecar_for(tmp_path, guard=guard, provider=provider) as sidecar:
        decision = sidecar.handle(make_request(ACCOUNT))
    assert decision.outcome == "denied"
    assert "independent account risk admission failed" in decision.reasons
    assert provider.calls == 0
    assert reservations(guard) == []


def test_a_latched_signer_stops_reserving_capital(tmp_path: Path) -> None:
    guard = guard_for(tmp_path)
    with sidecar_for(tmp_path, guard=guard, provider=NullSigningKeyProvider()) as sidecar:
        first = sidecar.handle(make_request(ACCOUNT, nonce=91))
        later = sidecar.handle(make_request(ACCOUNT, nonce=92))
    assert first.outcome == "denied"
    assert "signing provider failed" in first.reasons
    assert any("unresolved signing attempt" in reason for reason in later.reasons)
    assert reservations(guard) == []
