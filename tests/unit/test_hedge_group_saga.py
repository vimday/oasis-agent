import pytest

from oasis_agent.cross_venue.hedge_group import (
    AckTimeout,
    AuthoritativePositionMismatch,
    CancelOrderDirective,
    EmitFreezeNotice,
    FeedStaleOrDisconnected,
    FillConfirmed,
    FillLeg,
    FillRecord,
    HedgeAck,
    HedgeGroup,
    HedgeGroupState,
    HedgePolicy,
    PrimaryAck,
    ReconcileSnapshot,
    ReduceOnlyUnwindDirective,
    RequestUnwind,
    ReserveCapital,
    StartQuoting,
    SubmitHedgeOrder,
    TimerTick,
    apply_hedge_event,
)


def _policy(**overrides: int) -> HedgePolicy:
    defaults: dict[str, int] = {
        "max_child_order_base": 60,
        "min_hedge_notional_usd_micros": 100_000_000,  # $100
        "max_accumulate_wait_ms": 500,
        "max_unhedged_notional_ms": 2_000_000_000_000,  # $2000 * 1000 ms
        "base_decimals": 2,  # 100 base units = 1.00 unit
        "position_tolerance_base": 0,
        "max_snapshot_age_ms": 5_000,
    }
    defaults.update(overrides)
    return HedgePolicy(**defaults)


def _group(**overrides: object) -> HedgeGroup:
    defaults: dict[str, object] = {
        "group_id": "hg-eth-001",
        "instrument": "ETH-USD-PERP",
        "primary_venue": "lighter",
        "hedge_venue": "binance_usdm",
        "target_qty_base": 100,
    }
    defaults.update(overrides)
    return HedgeGroup(**defaults)  # type: ignore[arg-type]


def test_primary_ack_creates_no_hedge_and_duplicate_fill_is_strictly_idempotent() -> None:
    policy = _policy()
    group = _group()

    group, directives = apply_hedge_event(
        group,
        ReserveCapital(event_ms=100, reservation_id="res-1", reserved_usd_micros=5_000_000_000),
        policy,
    )
    assert group.state is HedgeGroupState.RESERVED
    assert directives == ()

    group, directives = apply_hedge_event(
        group, StartQuoting(event_ms=110, order_id="prim-1"), policy
    )
    assert group.state is HedgeGroupState.QUOTING
    assert directives == ()

    # PrimaryAck NEVER generates hedge demand or filled quantity.
    group, directives = apply_hedge_event(
        group, PrimaryAck(event_ms=120, order_id="prim-1", accepted_qty_base=100), policy
    )
    assert group.state is HedgeGroupState.PRIMARY_WORKING
    assert group.confirmed_primary_filled_base == 0
    assert directives == ()

    fill = FillRecord(
        venue="lighter",
        order_id="prim-1",
        fill_id="fill-1",
        leg=FillLeg.PRIMARY,
        qty_base=100,
        price_micros=2_000_000_000,  # $2,000
        timestamp_ms=200,
    )
    group, directives = apply_hedge_event(group, FillConfirmed(event_ms=200, fill=fill), policy)
    assert group.state is HedgeGroupState.HEDGING
    assert group.confirmed_primary_filled_base == 100
    assert group.pending_hedge_qty_base == 60  # bounded by max_child_order_base
    assert len(directives) == 1
    assert isinstance(directives[0], SubmitHedgeOrder)
    assert directives[0].qty_base == 60
    assert directives[0].reduce_only is False

    # Replay exact same FillConfirmed: strict no-op!
    replayed_group, replay_directives = apply_hedge_event(
        group, FillConfirmed(event_ms=250, fill=fill), policy
    )
    assert replayed_group == group
    assert replay_directives == ()


def test_partial_fills_accumulate_until_min_notional_or_wait_and_close_via_reconcile() -> None:
    # 100 base units = 1.00 ETH @ $2,000 -> 2 base units = $40 (< $100 min_hedge_notional)
    policy = _policy(max_child_order_base=50, min_hedge_notional_usd_micros=100_000_000)
    group = _group(target_qty_base=100)
    group, _ = apply_hedge_event(
        group,
        ReserveCapital(event_ms=0, reservation_id="r1", reserved_usd_micros=3_000_000_000),
        policy,
    )
    group, _ = apply_hedge_event(
        group, PrimaryAck(event_ms=10, order_id="p1", accepted_qty_base=100), policy
    )

    # Small partial fill: $40 < $100 min notional -> accumulates in PARTIALLY_FILLED, no hedge yet.
    small_fill = FillRecord(
        venue="lighter",
        order_id="p1",
        fill_id="f1",
        leg=FillLeg.PRIMARY,
        qty_base=2,
        price_micros=2_000_000_000,
        timestamp_ms=100,
    )
    group, directives = apply_hedge_event(
        group, FillConfirmed(event_ms=100, fill=small_fill), policy
    )
    assert group.state is HedgeGroupState.PARTIALLY_FILLED
    assert group.pending_hedge_qty_base == 0
    assert directives == ()

    # Wait exceeds max_accumulate_wait_ms (500 ms) -> TimerTick flushes bounded hedge order.
    group, directives = apply_hedge_event(group, TimerTick(event_ms=600), policy)
    assert group.state is HedgeGroupState.HEDGING
    assert len(directives) == 1
    assert isinstance(directives[0], SubmitHedgeOrder)
    assert directives[0].qty_base == 2
    client_oid = directives[0].client_order_id

    # HedgeAck does not settle position until hedge fill confirms.
    group, directives = apply_hedge_event(
        group,
        HedgeAck(event_ms=610, order_id="h-ord-1", client_order_id=client_oid, accepted_qty_base=2),
        policy,
    )
    assert group.state is HedgeGroupState.HEDGING
    assert directives == ()

    group, directives = apply_hedge_event(
        group,
        FillConfirmed(
            event_ms=620,
            fill=FillRecord(
                venue="binance_usdm",
                order_id="h-ord-1",
                fill_id="hf1",
                leg=FillLeg.HEDGE,
                qty_base=2,
                price_micros=2_001_000_000,
                timestamp_ms=620,
            ),
        ),
        policy,
    )
    assert group.state is HedgeGroupState.PARTIALLY_FILLED
    assert group.confirmed_hedge_filled_base == 2
    assert group.unhedged_started_ms is None

    # Remaining 98 primary fills at once -> emits max_child_order_base=50,
    # then emits 48 when the first child hedge fill confirms.
    group, directives = apply_hedge_event(
        group,
        FillConfirmed(
            event_ms=700,
            fill=FillRecord(
                venue="lighter",
                order_id="p1",
                fill_id="f2",
                leg=FillLeg.PRIMARY,
                qty_base=98,
                price_micros=2_000_000_000,
                timestamp_ms=700,
            ),
        ),
        policy,
    )
    assert len(directives) == 1
    assert isinstance(directives[0], SubmitHedgeOrder)
    assert directives[0].qty_base == 50

    group, directives = apply_hedge_event(
        group,
        FillConfirmed(
            event_ms=720,
            fill=FillRecord(
                venue="binance_usdm",
                order_id="h-ord-2",
                fill_id="hf2",
                leg=FillLeg.HEDGE,
                qty_base=50,
                price_micros=2_000_000_000,
                timestamp_ms=720,
            ),
        ),
        policy,
    )
    assert len(directives) == 1
    assert isinstance(directives[0], SubmitHedgeOrder)
    assert directives[0].qty_base == 48

    group, directives = apply_hedge_event(
        group,
        FillConfirmed(
            event_ms=740,
            fill=FillRecord(
                venue="binance_usdm",
                order_id="h-ord-3",
                fill_id="hf3",
                leg=FillLeg.HEDGE,
                qty_base=48,
                price_micros=2_000_000_000,
                timestamp_ms=740,
            ),
        ),
        policy,
    )
    assert group.state is HedgeGroupState.HEDGED
    assert group.confirmed_primary_filled_base == 100
    assert group.confirmed_hedge_filled_base == 100
    assert directives == ()

    # Reconcile HEDGED -> RECONCILING -> CLOSED
    group, directives = apply_hedge_event(
        group,
        ReconcileSnapshot(
            event_ms=800,
            reconciliation_version=1,
            authoritative_primary_base=100,
            authoritative_hedge_base=100,
            open_hedge_qty_base=0,
            final=False,
        ),
        policy,
    )
    assert group.state is HedgeGroupState.RECONCILING
    assert directives == ()

    group, directives = apply_hedge_event(
        group,
        ReconcileSnapshot(
            event_ms=850,
            reconciliation_version=2,
            authoritative_primary_base=100,
            authoritative_hedge_base=100,
            open_hedge_qty_base=0,
            final=True,
        ),
        policy,
    )
    assert group.state is HedgeGroupState.CLOSED
    assert directives == ()


def test_cbb_stale_data_phantom_delta_halts_and_allows_only_verified_reduce_only_unwind() -> None:
    policy = _policy(max_child_order_base=100)
    group = _group(target_qty_base=100)
    group, _ = apply_hedge_event(
        group,
        ReserveCapital(event_ms=0, reservation_id="r1", reserved_usd_micros=3_000_000_000),
        policy,
    )
    group, _ = apply_hedge_event(
        group, PrimaryAck(event_ms=10, order_id="p1", accepted_qty_base=100), policy
    )
    group, directives = apply_hedge_event(
        group,
        FillConfirmed(
            event_ms=20,
            fill=FillRecord(
                venue="lighter",
                order_id="p1",
                fill_id="f1",
                leg=FillLeg.PRIMARY,
                qty_base=50,
                price_micros=2_000_000_000,
                timestamp_ms=20,
            ),
        ),
        policy,
    )
    assert group.state is HedgeGroupState.HEDGING
    assert isinstance(directives[0], SubmitHedgeOrder)

    # CBB phantom-delta scenario: authoritative venue snapshot says primary=50, hedge=0,
    # OR stale snapshot arrives with mismatch / stale age.
    group, directives = apply_hedge_event(
        group,
        AuthoritativePositionMismatch(
            event_ms=100,
            authoritative_primary_base=50,
            authoritative_hedge_base=10,  # disagrees with confirmed_hedge_filled_base=0
            reconciliation_version=1,
            reason="cbb_stale_broker_snapshot",
        ),
        policy,
    )
    assert group.state is HedgeGroupState.HALTED
    assert len(directives) == 1
    assert isinstance(directives[0], EmitFreezeNotice)

    # Stale ReconcileSnapshot or another FillConfirmed CANNOT trigger SubmitHedgeOrder!
    group, directives = apply_hedge_event(
        group,
        ReconcileSnapshot(
            event_ms=110,
            reconciliation_version=1,
            authoritative_primary_base=100,
            authoritative_hedge_base=0,
            snapshot_age_ms=10_000,
            is_stale=True,
        ),
        policy,
    )
    assert group.state is HedgeGroupState.HALTED
    assert all(not isinstance(d, SubmitHedgeOrder) for d in directives)
    assert isinstance(directives[0], EmitFreezeNotice)

    group, directives = apply_hedge_event(
        group,
        FillConfirmed(
            event_ms=120,
            fill=FillRecord(
                venue="lighter",
                order_id="p1",
                fill_id="f2",
                leg=FillLeg.PRIMARY,
                qty_base=20,
                price_micros=2_000_000_000,
                timestamp_ms=120,
            ),
        ),
        policy,
    )
    assert group.state is HedgeGroupState.HALTED
    assert group.confirmed_primary_filled_base == 70  # truth updated
    assert len(directives) == 1
    assert isinstance(directives[0], EmitFreezeNotice)

    # Unverified RequestUnwind is blocked while halted from mismatch!
    group, directives = apply_hedge_event(
        group, RequestUnwind(event_ms=130, reason="blind_unwind"), policy
    )
    assert group.state is HedgeGroupState.HALTED
    assert len(directives) == 1
    assert isinstance(directives[0], EmitFreezeNotice)

    # Verified RequestUnwind emits ONLY ReduceOnlyUnwindDirective(reduce_only=True).
    group, directives = apply_hedge_event(
        group,
        RequestUnwind(
            event_ms=140,
            reason="verified_unwind",
            verified_authoritative_primary_base=50,
            verified_authoritative_hedge_base=10,
            reconciliation_version=2,
        ),
        policy,
    )
    assert group.state is HedgeGroupState.UNWINDING
    assert len(directives) == 2
    assert all(isinstance(d, ReduceOnlyUnwindDirective) for d in directives)
    for directive in directives:
        assert isinstance(directive, ReduceOnlyUnwindDirective)
        assert directive.reduce_only is True
    assert directives[0].qty_base == 50  # type: ignore[union-attr]
    assert directives[1].qty_base == 10  # type: ignore[union-attr]

    # Repeated RequestUnwind at same/stale reconciliation_version cannot re-fire correction!
    group_after_dup, dup_directives = apply_hedge_event(
        group,
        RequestUnwind(
            event_ms=150,
            reason="duplicate_stale_unwind",
            verified_authoritative_primary_base=50,
            verified_authoritative_hedge_base=10,
            reconciliation_version=2,
        ),
        policy,
    )
    assert group_after_dup.state is HedgeGroupState.UNWINDING
    assert group_after_dup.pending_unwind is True
    assert group_after_dup.last_unwind_reconciliation_version == 2
    assert group_after_dup.confirmed_primary_filled_base == group.confirmed_primary_filled_base
    assert group_after_dup.last_event_ms == 150
    assert dup_directives == ()

    # Reconciled zero residual closes the unwound saga.
    group, directives = apply_hedge_event(
        group,
        ReconcileSnapshot(
            event_ms=200,
            reconciliation_version=3,
            authoritative_primary_base=0,
            authoritative_hedge_base=0,
            open_hedge_qty_base=0,
            final=True,
        ),
        policy,
    )
    assert group.state is HedgeGroupState.CLOSED
    assert directives == ()


def test_ack_timeout_unknown_unhedged_budget_and_feed_stale_halting() -> None:
    policy = _policy(max_unhedged_notional_ms=100_000_000_000)  # $1000 * 100 ms
    group = _group()
    group, _ = apply_hedge_event(
        group,
        ReserveCapital(event_ms=0, reservation_id="r1", reserved_usd_micros=3_000_000_000),
        policy,
    )
    group, _ = apply_hedge_event(group, StartQuoting(event_ms=5, order_id="p1"), policy)

    # Missing ack -> UNKNOWN + EmitFreezeNotice, never presumed cancelled or filled.
    unknown_group, directives = apply_hedge_event(
        group, AckTimeout(event_ms=50, leg=FillLeg.PRIMARY, order_id="p1"), policy
    )
    assert unknown_group.state is HedgeGroupState.UNKNOWN
    assert len(directives) == 1
    assert isinstance(directives[0], EmitFreezeNotice)
    assert directives[0].target_state is HedgeGroupState.UNKNOWN

    # Clean cancel request before fills emits CancelOrderDirective.
    group, _ = apply_hedge_event(
        group, PrimaryAck(event_ms=10, order_id="p1", accepted_qty_base=100), policy
    )
    cancel_group, cancel_dirs = apply_hedge_event(
        group, RequestUnwind(event_ms=20, reason="operator_cancel"), policy
    )
    assert cancel_group.state is HedgeGroupState.CANCEL_PENDING
    assert len(cancel_dirs) == 1
    assert isinstance(cancel_dirs[0], CancelOrderDirective)

    # Unhedged notional-ms budget breach -> HALTED.
    group, _ = apply_hedge_event(
        group,
        FillConfirmed(
            event_ms=100,
            fill=FillRecord(
                venue="lighter",
                order_id="p1",
                fill_id="f1",
                leg=FillLeg.PRIMARY,
                qty_base=100,  # $2,000 notional
                price_micros=2_000_000_000,
                timestamp_ms=100,
            ),
        ),
        policy,
    )
    # 60 ms elapsed at $2,000 notional = 120_000_000_000 > 100_000_000_000 budget!
    halted_group, directives = apply_hedge_event(group, TimerTick(event_ms=160), policy)
    assert halted_group.state is HedgeGroupState.HALTED
    assert halted_group.halt_reason == "unhedged_notional_ms_budget_exceeded"
    assert len(directives) == 1
    assert isinstance(directives[0], EmitFreezeNotice)

    # FeedStaleOrDisconnected also halts immediately.
    fresh = _group()
    fresh, _ = apply_hedge_event(
        fresh,
        ReserveCapital(event_ms=0, reservation_id="r2", reserved_usd_micros=3_000_000_000),
        policy,
    )
    stale_group, stale_dirs = apply_hedge_event(
        fresh, FeedStaleOrDisconnected(event_ms=10, venue="binance_usdm"), policy
    )
    assert stale_group.state is HedgeGroupState.HALTED
    assert len(stale_dirs) == 1
    assert isinstance(stale_dirs[0], EmitFreezeNotice)
    with pytest.raises(ValueError, match="reduce_only=True"):
        ReduceOnlyUnwindDirective(
            group_id="g",
            venue="lighter",
            instrument="ETH",
            qty_base=10,
            reconciliation_version=1,
            reduce_only=False,  # type: ignore[arg-type]
        )
