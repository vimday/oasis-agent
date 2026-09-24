"""Cross-chain reconciled-account risk ledger: fail-closed admission and loss latching."""

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from oasis_agent.risk.account_risk import (
    AccountFrame,
    AssetBalance,
    Commitment,
    GlobalRiskJournal,
    GlobalRiskPolicy,
    PortfolioFrame,
    RiskState,
    reconcile,
)

DAY = timezone(timedelta(hours=8))
USD = "usd"
GOLD = "gold"
CHAIN_A = 4663
CHAIN_B = 8453
ACCOUNT_A = "0x" + "a1" * 20
ACCOUNT_B = "0x" + "b2" * 20
NOON = int(datetime(2027, 3, 4, 12, 0, tzinfo=DAY).timestamp())
MIDNIGHT = int(datetime(2027, 3, 5, 0, 0, tzinfo=DAY).timestamp())
LIMIT_TOTAL = 500_000_000
LIMIT_DAILY = 200_000_000
LIMIT_CUMULATIVE = 500_000_000


def asset(
    units: int,
    *,
    asset_id: str = USD,
    role: str = "cash",
    price_timestamp: int = NOON,
) -> AssetBalance:
    """One whole unit of this asset is priced at exactly one USD, so `units` is USD micros."""
    return AssetBalance(
        asset_id=asset_id,
        units=units,
        decimals=6,
        price_usd_micros_numerator=1_000_000,
        price_denominator=1,
        price_timestamp=price_timestamp,
        role=role,
        evidence_hash="e" * 64,
    )


def make_frame(
    *,
    chain_id: int = CHAIN_A,
    account: str = ACCOUNT_A,
    block_number: int = 100,
    timestamp: int = NOON,
    assets: tuple[AssetBalance, ...] | None = None,
    movements: dict[str, int] | None = None,
    non_trading_flow_usd_micros: int = 0,
    unresolved_operations: int = 0,
) -> AccountFrame:
    assets = assets if assets is not None else (asset(100_000_000, price_timestamp=timestamp),)
    return AccountFrame(
        chain_id=chain_id,
        account=account,
        block_number=block_number,
        block_hash="0x" + f"{block_number:064x}",
        timestamp=timestamp,
        assets=assets,
        movements=movements if movements is not None else {a.asset_id: 0 for a in assets},
        non_trading_flow_usd_micros=non_trading_flow_usd_micros,
        inventory_evidence_hash="f" * 64,
        unresolved_operations=unresolved_operations,
    )


def make_policy(
    *,
    inventory: dict[str, tuple[str, ...]] | None = None,
    total: int = LIMIT_TOTAL,
    daily: int = LIMIT_DAILY,
    cumulative: int = LIMIT_CUMULATIVE,
    max_age_seconds: int = 600,
    max_skew_seconds: int = 60,
) -> GlobalRiskPolicy:
    return GlobalRiskPolicy(
        inventory=inventory if inventory is not None else {f"{CHAIN_A}:{ACCOUNT_A}": (USD,)},
        total_capital_usd_micros=total,
        daily_loss_usd_micros=daily,
        cumulative_loss_usd_micros=cumulative,
        max_age_seconds=max_age_seconds,
        max_skew_seconds=max_skew_seconds,
    )


def commitment(amount: int, *, request: str = "1" * 64, key: str | None = None) -> Commitment:
    return Commitment(
        request_hash=request,
        account_key=key if key is not None else f"{CHAIN_A}:{ACCOUNT_A}",
        usd_micros=amount,
    )


def make_journal(tmp_path: Path, policy: GlobalRiskPolicy | None = None) -> GlobalRiskJournal:
    return GlobalRiskJournal(tmp_path / "risk.db", policy if policy is not None else make_policy())


def state_of(journal: GlobalRiskJournal) -> dict:
    row = journal.db.execute(
        "SELECT payload FROM global_events ORDER BY seq DESC LIMIT 1"
    ).fetchone()
    return json.loads(row[0])["state"]


def test_policy_requires_complete_inventory_and_ordered_limits() -> None:
    with pytest.raises(ValueError, match="complete inventory and ordered loss limits"):
        make_policy(inventory={})
    with pytest.raises(ValueError, match="complete inventory and ordered loss limits"):
        make_policy(daily=LIMIT_CUMULATIVE + 1)
    with pytest.raises(ValueError, match="unique policy asset inventory"):
        make_policy(inventory={f"{CHAIN_A}:{ACCOUNT_A}": (USD, USD)})


def test_asset_value_rounds_down_for_cash_and_away_from_zero_for_debt() -> None:
    def priced(role: str) -> int:
        return AssetBalance(
            asset_id=USD,
            units=1,
            decimals=0,
            price_usd_micros_numerator=1,
            price_denominator=3,
            price_timestamp=NOON,
            role=role,
            evidence_hash="e" * 64,
        ).value

    assert priced("cash") == 0
    assert priced("debt") == -1


def test_frame_rejects_incomplete_or_duplicated_inventory() -> None:
    with pytest.raises(ValueError, match="unique complete asset inventory"):
        make_frame(assets=())
    with pytest.raises(ValueError, match="unique complete asset inventory"):
        make_frame(assets=(asset(1), asset(2)))
    with pytest.raises(ValueError, match="movement reconciliation"):
        make_frame(assets=(asset(1), asset(1, asset_id=GOLD)), movements={USD: 0})
    with pytest.raises(ValueError, match="nonzero account"):
        make_frame(account="0x" + "0" * 40)


def test_reconcile_requires_integer_clock_and_complete_cross_chain_coverage() -> None:
    policy = make_policy(
        inventory={f"{CHAIN_A}:{ACCOUNT_A}": (USD,), f"{CHAIN_B}:{ACCOUNT_B}": (USD,)}
    )
    with pytest.raises(ValueError, match="integer wall clock"):
        reconcile(
            policy,
            RiskState(),
            PortfolioFrame(accounts=(make_frame(),)),
            float(NOON),
        )
    partial = PortfolioFrame(accounts=(make_frame(),))
    with pytest.raises(ValueError, match="complete unique cross-chain account coverage"):
        reconcile(policy, RiskState(), partial, NOON)
    with pytest.raises(ValueError, match="complete unique cross-chain account coverage"):
        reconcile(policy, RiskState(), PortfolioFrame(accounts=(make_frame(), make_frame())), NOON)


def test_reconcile_rejects_skew_staleness_and_unresolved_operations() -> None:
    two_chains = make_policy(
        inventory={f"{CHAIN_A}:{ACCOUNT_A}": (USD,), f"{CHAIN_B}:{ACCOUNT_B}": (USD,)}
    )
    skewed = PortfolioFrame(
        accounts=(
            make_frame(),
            make_frame(
                chain_id=CHAIN_B,
                account=ACCOUNT_B,
                block_number=200,
                timestamp=NOON + two_chains.max_skew_seconds + 1,
            ),
        )
    )
    with pytest.raises(ValueError, match="cross-chain snapshot skew"):
        reconcile(two_chains, RiskState(), skewed, NOON + two_chains.max_skew_seconds + 1)

    single = make_policy()
    with pytest.raises(ValueError, match="unresolved or stale account state"):
        reconcile(
            single,
            RiskState(),
            PortfolioFrame(accounts=(make_frame(unresolved_operations=1),)),
            NOON,
        )
    with pytest.raises(ValueError, match="unresolved or stale account state"):
        reconcile(single, RiskState(), PortfolioFrame(accounts=(make_frame(),)), NOON + 601)
    future_price = PortfolioFrame(
        accounts=(make_frame(assets=(asset(100_000_000, price_timestamp=NOON + 300),)),)
    )
    with pytest.raises(ValueError, match="stale or future valuation"):
        reconcile(single, RiskState(), future_price, NOON)


def test_reconcile_rejects_asset_set_and_role_changes() -> None:
    policy = make_policy(inventory={f"{CHAIN_A}:{ACCOUNT_A}": (USD, GOLD)})
    first = PortfolioFrame(accounts=(make_frame(assets=(asset(0), asset(0, asset_id=GOLD))),))
    state = reconcile(policy, RiskState(), first, NOON)
    missing = PortfolioFrame(
        accounts=(
            make_frame(
                block_number=101,
                timestamp=NOON + 1,
                assets=(asset(0, price_timestamp=NOON + 1),),
                movements={USD: 0},
            ),
        )
    )
    with pytest.raises(ValueError, match="asset inventory mismatch"):
        reconcile(policy, state, missing, NOON + 1)
    changed_role = PortfolioFrame(
        accounts=(
            make_frame(
                block_number=101,
                timestamp=NOON + 1,
                assets=(
                    asset(0, price_timestamp=NOON + 1),
                    asset(0, asset_id=GOLD, role="investment", price_timestamp=NOON + 1),
                ),
                movements={USD: 0, GOLD: 0},
            ),
        )
    )
    with pytest.raises(ValueError, match="asset units or role changed"):
        reconcile(policy, state, changed_role, NOON + 1)


def test_reconcile_requires_exact_per_asset_movement_reconciliation() -> None:
    policy = make_policy()
    state = reconcile(policy, RiskState(), PortfolioFrame(accounts=(make_frame(),)), NOON)
    drifted = PortfolioFrame(
        accounts=(
            make_frame(
                block_number=101,
                timestamp=NOON + 1,
                assets=(asset(90_000_000, price_timestamp=NOON + 1),),
                movements={USD: 0},
            ),
        )
    )
    with pytest.raises(ValueError, match="unreconciled per-asset residual"):
        reconcile(policy, state, drifted, NOON + 1)
    regressed = PortfolioFrame(
        accounts=(make_frame(block_number=100, assets=(asset(0),), movements={USD: 0}),)
    )
    with pytest.raises(ValueError, match="regressed or reorged"):
        reconcile(policy, state, regressed, NOON)


def test_gains_in_one_account_never_offset_a_loss_in_another() -> None:
    policy = make_policy(
        inventory={f"{CHAIN_A}:{ACCOUNT_A}": (USD,), f"{CHAIN_B}:{ACCOUNT_B}": (USD,)}
    )
    start = PortfolioFrame(
        accounts=(
            make_frame(assets=(asset(100_000_000),)),
            make_frame(
                chain_id=CHAIN_B,
                account=ACCOUNT_B,
                block_number=200,
                timestamp=NOON + 10,
            ),
        )
    )
    state = reconcile(policy, RiskState(), start, NOON + 10)
    moved = PortfolioFrame(
        accounts=(
            make_frame(
                block_number=101,
                timestamp=NOON + 1,
                assets=(asset(70_000_000, price_timestamp=NOON + 1),),
                movements={USD: -30_000_000},
            ),
            make_frame(
                chain_id=CHAIN_B,
                account=ACCOUNT_B,
                block_number=201,
                timestamp=NOON + 11,
                assets=(asset(200_000_000, price_timestamp=NOON + 11),),
                movements={USD: 100_000_000},
            ),
        )
    )
    state = reconcile(policy, state, moved, NOON + 11)
    assert state.cumulative_loss == 30_000_000
    assert state.daily_losses == {"2027-03-04": 30_000_000}
    assert state.halted is False


def test_non_trading_flow_is_not_a_loss_but_unexplained_drop_is() -> None:
    policy = make_policy()
    state = reconcile(policy, RiskState(), PortfolioFrame(accounts=(make_frame(),)), NOON)
    withdrawn = PortfolioFrame(
        accounts=(
            make_frame(
                block_number=101,
                timestamp=NOON + 1,
                assets=(asset(70_000_000, price_timestamp=NOON + 1),),
                movements={USD: -30_000_000},
                non_trading_flow_usd_micros=-30_000_000,
            ),
        )
    )
    state = reconcile(policy, state, withdrawn, NOON + 1)
    assert state.cumulative_loss == 0
    burned = PortfolioFrame(
        accounts=(
            make_frame(
                block_number=102,
                timestamp=NOON + 2,
                assets=(asset(60_000_000, price_timestamp=NOON + 2),),
                movements={USD: -10_000_000},
            ),
        )
    )
    state = reconcile(policy, state, burned, NOON + 2)
    assert state.cumulative_loss == 10_000_000


def test_daily_limit_latches_the_halt_and_cumulative_loss_accumulates() -> None:
    policy = make_policy(daily=300_000_000)
    state = reconcile(
        policy,
        RiskState(),
        PortfolioFrame(accounts=(make_frame(assets=(asset(300_000_000),)),)),
        NOON,
    )
    half = PortfolioFrame(
        accounts=(
            make_frame(
                block_number=101,
                timestamp=NOON + 1,
                assets=(asset(100_000_000, price_timestamp=NOON + 1),),
                movements={USD: -200_000_000},
            ),
        )
    )
    state = reconcile(policy, state, half, NOON + 1)
    assert state.cumulative_loss == 200_000_000
    assert state.halted is False
    limit = PortfolioFrame(
        accounts=(
            make_frame(
                block_number=102,
                timestamp=NOON + 2,
                assets=(asset(0, price_timestamp=NOON + 2),),
                movements={USD: -100_000_000},
            ),
        )
    )
    state = reconcile(policy, state, limit, NOON + 2)
    assert state.daily_losses["2027-03-04"] == policy.daily_loss_usd_micros
    assert state.cumulative_loss < policy.cumulative_loss_usd_micros
    assert state.halted is True
    assert reconcile(policy, state, limit, NOON + 2).halted is True


def test_cumulative_limit_latches_the_halt_across_days_without_a_daily_breach() -> None:
    policy = make_policy(daily=200_000_000, cumulative=200_000_000)
    state = reconcile(
        policy,
        RiskState(),
        PortfolioFrame(accounts=(make_frame(assets=(asset(300_000_000),)),)),
        NOON,
    )
    first = PortfolioFrame(
        accounts=(
            make_frame(
                block_number=101,
                timestamp=NOON + 1,
                assets=(asset(200_000_000, price_timestamp=NOON + 1),),
                movements={USD: -100_000_000},
            ),
        )
    )
    state = reconcile(policy, state, first, NOON + 1)
    assert state.daily_losses["2027-03-04"] < policy.daily_loss_usd_micros
    assert state.cumulative_loss < policy.cumulative_loss_usd_micros
    assert state.halted is False
    midnight = PortfolioFrame(
        accounts=(
            make_frame(
                block_number=102,
                timestamp=MIDNIGHT,
                assets=(asset(200_000_000, price_timestamp=MIDNIGHT),),
                movements={USD: 0},
            ),
        )
    )
    state = reconcile(policy, state, midnight, MIDNIGHT)
    second = PortfolioFrame(
        accounts=(
            make_frame(
                block_number=103,
                timestamp=MIDNIGHT + 1,
                assets=(asset(100_000_000, price_timestamp=MIDNIGHT + 1),),
                movements={USD: -100_000_000},
            ),
        )
    )
    state = reconcile(policy, state, second, MIDNIGHT + 1)
    assert state.cumulative_loss == policy.cumulative_loss_usd_micros
    assert state.daily_losses["2027-03-05"] < policy.daily_loss_usd_micros
    assert state.halted is True


def test_day_boundary_requires_an_exact_midnight_reconciliation() -> None:
    policy = make_policy()
    state = reconcile(policy, RiskState(), PortfolioFrame(accounts=(make_frame(),)), NOON)
    late = PortfolioFrame(
        accounts=(
            make_frame(
                block_number=101,
                timestamp=MIDNIGHT + 60,
                assets=(asset(90_000_000, price_timestamp=MIDNIGHT + 60),),
                movements={USD: -10_000_000},
            ),
        )
    )
    with pytest.raises(ValueError, match="missing day-boundary reconciliation"):
        reconcile(policy, state, late, MIDNIGHT + 60)
    midnight = PortfolioFrame(
        accounts=(
            make_frame(
                block_number=101,
                timestamp=MIDNIGHT,
                assets=(asset(90_000_000, price_timestamp=MIDNIGHT),),
                movements={USD: -10_000_000},
            ),
        )
    )
    state = reconcile(policy, state, midnight, MIDNIGHT)
    assert state.daily_losses == {"2027-03-04": 10_000_000}
    next_midnight = PortfolioFrame(
        accounts=(
            make_frame(
                block_number=102,
                timestamp=MIDNIGHT + 86_400,
                assets=(asset(80_000_000, price_timestamp=MIDNIGHT + 86_400),),
                movements={USD: -10_000_000},
            ),
        )
    )
    state = reconcile(policy, state, next_midnight, MIDNIGHT + 86_400)
    assert state.daily_losses == {"2027-03-04": 10_000_000, "2027-03-05": 10_000_000}


def test_admission_counts_deployed_investment_toward_the_ceiling(tmp_path: Path) -> None:
    journal = make_journal(tmp_path)
    deployed = PortfolioFrame(
        accounts=(make_frame(assets=(asset(450_000_000, role="investment"),)),)
    )
    with pytest.raises(ValueError, match="blocked admission"):
        journal.admit(deployed, commitment(100_000_000, request="2" * 64), now=NOON)
    assert state_of(journal)["reservations"] == []


def test_admission_counts_pending_commitments_toward_the_ceiling(tmp_path: Path) -> None:
    journal = make_journal(tmp_path)
    held = PortfolioFrame(accounts=(make_frame(assets=(asset(900_000_000),)),))
    journal.admit(held, commitment(300_000_000, request="2" * 64), now=NOON)
    journal.admit(held, commitment(200_000_000, request="3" * 64), now=NOON)
    assert len(state_of(journal)["reservations"]) == 2
    with pytest.raises(ValueError, match="blocked admission"):
        journal.admit(held, commitment(1, request="4" * 64), now=NOON)


def test_admission_replay_is_idempotent_and_changed_replay_is_rejected(tmp_path: Path) -> None:
    journal = make_journal(tmp_path)
    frame = PortfolioFrame(accounts=(make_frame(),))
    journal.admit(frame, commitment(100_000_000), now=NOON)
    journal.admit(frame, commitment(100_000_000), now=NOON)
    assert len(state_of(journal)["reservations"]) == 1
    with pytest.raises(ValueError, match="replay changed commitment"):
        journal.admit(frame, commitment(200_000_000), now=NOON)


def test_admission_rejects_an_account_outside_the_policy(tmp_path: Path) -> None:
    journal = make_journal(tmp_path)
    frame = PortfolioFrame(accounts=(make_frame(),))
    with pytest.raises(ValueError, match="commitment account outside policy"):
        journal.admit(frame, commitment(1, key=f"{CHAIN_B}:{ACCOUNT_B}"), now=NOON)


def test_release_restores_capacity_and_is_idempotent(tmp_path: Path) -> None:
    journal = make_journal(tmp_path)
    frame = PortfolioFrame(accounts=(make_frame(),))
    journal.admit(frame, commitment(300_000_000, request="2" * 64), now=NOON)
    with pytest.raises(ValueError, match="blocked admission"):
        journal.admit(frame, commitment(300_000_000, request="3" * 64), now=NOON)
    journal.release("2" * 64, reason="signing attempt produced no transaction")
    assert state_of(journal)["reservations"] == []
    journal.admit(frame, commitment(300_000_000, request="3" * 64), now=NOON)
    assert [r["request_hash"] for r in state_of(journal)["reservations"]] == ["3" * 64]
    tail = journal.release("9" * 64, reason="signing attempt produced no transaction")
    assert len(tail) == 64
    assert [r["request_hash"] for r in state_of(journal)["reservations"]] == ["3" * 64]


def test_release_rejects_malformed_identity_or_reason(tmp_path: Path) -> None:
    journal = make_journal(tmp_path)
    with pytest.raises(ValueError, match="canonical request hash"):
        journal.release("0x" + "1" * 64, reason="ok")
    with pytest.raises(ValueError, match="bounded ascii release reason"):
        journal.release("1" * 64, reason="")
    with pytest.raises(ValueError, match="bounded ascii release reason"):
        journal.release("1" * 64, reason="release\nforged")


def test_a_loss_denial_still_persists_the_latch_across_restarts(tmp_path: Path) -> None:
    policy = make_policy(daily=50_000_000)
    journal = make_journal(tmp_path, policy)
    journal.admit(
        PortfolioFrame(accounts=(make_frame(),)), commitment(1, request="2" * 64), now=NOON
    )
    lost = PortfolioFrame(
        accounts=(
            make_frame(
                block_number=101,
                timestamp=NOON + 1,
                assets=(asset(0, price_timestamp=NOON + 1),),
                movements={USD: -100_000_000},
            ),
        )
    )
    with pytest.raises(ValueError, match="blocked admission"):
        journal.admit(lost, commitment(1, request="3" * 64), now=NOON + 1)
    restarted = GlobalRiskJournal(tmp_path / "risk.db", policy)
    state = state_of(restarted)
    assert state["cumulative_loss"] == 100_000_000
    assert state["halted"] is True
    assert [r["request_hash"] for r in state["reservations"]] == ["2" * 64]
    with pytest.raises(ValueError, match="blocked admission"):
        restarted.admit(lost, commitment(1, request="4" * 64), now=NOON + 1)


def test_journal_detects_tampering_and_policy_change(tmp_path: Path) -> None:
    policy = make_policy()
    journal = make_journal(tmp_path, policy)
    journal.admit(PortfolioFrame(accounts=(make_frame(),)), commitment(1), now=NOON)
    connection = sqlite3.connect(tmp_path / "risk.db")
    payload, _ = connection.execute(
        "SELECT payload, digest FROM global_events WHERE seq = 1"
    ).fetchone()
    connection.execute(
        "UPDATE global_events SET payload = ? WHERE seq = 1",
        (payload.replace(b'"halted":false', b'"halted":true'),),
    )
    connection.commit()
    connection.close()
    with pytest.raises(ValueError, match="global risk journal integrity failure"):
        GlobalRiskJournal(tmp_path / "risk.db", policy)
    with pytest.raises(ValueError, match="global risk policy changed"):
        GlobalRiskJournal(tmp_path / "risk.db", make_policy(total=LIMIT_TOTAL + 1))


def test_journal_rejects_events_without_a_recorded_policy(tmp_path: Path) -> None:
    policy = make_policy()
    journal = make_journal(tmp_path, policy)
    journal.admit(PortfolioFrame(accounts=(make_frame(),)), commitment(1), now=NOON)
    connection = sqlite3.connect(tmp_path / "risk.db")
    connection.execute("DELETE FROM global_policy")
    connection.commit()
    connection.close()
    with pytest.raises(ValueError, match="missing global policy"):
        GlobalRiskJournal(tmp_path / "risk.db", policy)


def test_admission_state_is_append_only_and_hash_linked(tmp_path: Path) -> None:
    journal = make_journal(tmp_path)
    frame = PortfolioFrame(accounts=(make_frame(),))
    journal.admit(frame, commitment(1, request="2" * 64), now=NOON)
    journal.release("2" * 64, reason="signing attempt produced no transaction")
    rows = journal.db.execute(
        "SELECT seq, payload, digest FROM global_events ORDER BY seq"
    ).fetchall()
    assert [row[0] for row in rows] == [1, 2]
    previous = "0" * 64
    for _, payload, digest in rows:
        assert json.loads(payload)["previous"] == previous
        assert hashlib.sha256(payload).hexdigest() == digest
        previous = digest


def test_current_state_is_a_read_only_baseline_without_write_semantics(tmp_path: Path) -> None:
    journal = make_journal(tmp_path)
    assert journal.current_state() == RiskState()
    frame = PortfolioFrame(accounts=(make_frame(),))
    journal.admit(frame, commitment(300_000_000, request="2" * 64), now=NOON)
    first = journal.current_state()
    second = journal.current_state()
    assert first == second
    assert first.frame == frame
    assert (first.halted, first.cumulative_loss) == (False, 0)
    assert [r.request_hash for r in first.reservations] == ["2" * 64]
    events = journal.db.execute("SELECT count(*) FROM global_events").fetchone()[0]
    assert events == 1  # reading never appends

    # The retained frame is a legitimate prior baseline for the next observation.
    lost = PortfolioFrame(
        accounts=(
            make_frame(
                block_number=101,
                timestamp=NOON + 1,
                assets=(asset(0, price_timestamp=NOON + 1),),
                movements={USD: -100_000_000},
            ),
        )
    )
    state = reconcile(journal.policy, first, lost, now=NOON + 1)
    assert state.cumulative_loss == 100_000_000
    assert journal.db.execute("SELECT count(*) FROM global_events").fetchone()[0] == 1


def test_current_state_fails_closed_on_a_tampered_chain(tmp_path: Path) -> None:
    policy = make_policy()
    journal = make_journal(tmp_path, policy)
    journal.admit(PortfolioFrame(accounts=(make_frame(),)), commitment(1), now=NOON)
    connection = sqlite3.connect(tmp_path / "risk.db")
    payload, _ = connection.execute(
        "SELECT payload, digest FROM global_events WHERE seq = 1"
    ).fetchone()
    connection.execute(
        "UPDATE global_events SET payload = ? WHERE seq = 1",
        (payload.replace(b'"halted":false', b'"halted":true'),),
    )
    connection.commit()
    connection.close()
    with pytest.raises(ValueError, match="global risk journal integrity failure"):
        journal.current_state()
