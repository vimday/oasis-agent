"""Distributed two-leg HedgeGroup execution saga and stale-state defenses."""

from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Literal

from ..models.common import canonical_json


class HedgeGroupState(StrEnum):
    PROPOSED = "proposed"
    RESERVED = "reserved"
    QUOTING = "quoting"
    PRIMARY_WORKING = "primary_working"
    PARTIALLY_FILLED = "partially_filled"
    HEDGE_REQUIRED = "hedge_required"
    HEDGING = "hedging"
    HEDGED = "hedged"
    RECONCILING = "reconciling"
    CLOSED = "closed"
    CANCEL_PENDING = "cancel_pending"
    UNWINDING = "unwinding"
    UNKNOWN = "unknown"
    HALTED = "halted"


class FillLeg(StrEnum):
    PRIMARY = "primary"
    HEDGE = "hedge"


def _require_nonempty_str(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")
    return value


def _require_int(value: object, name: str) -> int:
    if type(value) is not int:
        raise TypeError(f"{name} must be an integer")
    return value


def _require_nonneg_int(value: object, name: str) -> int:
    parsed = _require_int(value, name)
    if parsed < 0:
        raise ValueError(f"{name} must be nonnegative")
    return parsed


def _require_pos_int(value: object, name: str) -> int:
    parsed = _require_int(value, name)
    if parsed <= 0:
        raise ValueError(f"{name} must be positive")
    return parsed


@dataclass(frozen=True, slots=True)
class FillRecord:
    """Immutable confirmed execution fill keyed by (venue, order_id, fill_id)."""

    venue: str
    order_id: str
    fill_id: str
    leg: FillLeg | str
    qty_base: int
    price_micros: int
    timestamp_ms: int
    fee_usd_micros: int = 0

    def __post_init__(self) -> None:
        _require_nonempty_str(self.venue, "venue")
        _require_nonempty_str(self.order_id, "order_id")
        _require_nonempty_str(self.fill_id, "fill_id")
        if not isinstance(self.leg, FillLeg):
            object.__setattr__(self, "leg", FillLeg(self.leg))
        _require_pos_int(self.qty_base, "qty_base")
        _require_pos_int(self.price_micros, "price_micros")
        _require_nonneg_int(self.timestamp_ms, "timestamp_ms")
        _require_int(self.fee_usd_micros, "fee_usd_micros")

    @property
    def dedup_key(self) -> str:
        """Collision-free canonical identifier for (venue, order_id, fill_id)."""
        return canonical_json([self.venue, self.order_id, self.fill_id]).decode("utf-8")


@dataclass(frozen=True, slots=True)
class HedgePolicy:
    """Deterministic risk, accumulation, and child-sizing limits for a HedgeGroup."""

    max_child_order_base: int
    min_hedge_notional_usd_micros: int
    max_accumulate_wait_ms: int
    max_unhedged_notional_ms: int
    base_decimals: int = 18
    position_tolerance_base: int = 0
    max_snapshot_age_ms: int = 5_000
    max_gross_position_base: int | None = None

    def __post_init__(self) -> None:
        _require_pos_int(self.max_child_order_base, "max_child_order_base")
        _require_nonneg_int(self.min_hedge_notional_usd_micros, "min_hedge_notional_usd_micros")
        _require_nonneg_int(self.max_accumulate_wait_ms, "max_accumulate_wait_ms")
        _require_pos_int(self.max_unhedged_notional_ms, "max_unhedged_notional_ms")
        decimals = _require_nonneg_int(self.base_decimals, "base_decimals")
        if decimals > 77:
            raise ValueError("base_decimals cannot exceed 77")
        _require_nonneg_int(self.position_tolerance_base, "position_tolerance_base")
        _require_pos_int(self.max_snapshot_age_ms, "max_snapshot_age_ms")
        if self.max_gross_position_base is not None:
            _require_pos_int(self.max_gross_position_base, "max_gross_position_base")


@dataclass(frozen=True, slots=True)
class HedgeGroup:
    """Immutable distributed saga state linking primary and hedge legs."""

    group_id: str
    instrument: str
    primary_venue: str
    hedge_venue: str
    target_qty_base: int
    state: HedgeGroupState = HedgeGroupState.PROPOSED
    confirmed_primary_filled_base: int = 0
    confirmed_hedge_filled_base: int = 0
    pending_hedge_qty_base: int = 0
    seen_fill_keys: frozenset[str] = frozenset()
    primary_avg_price_micros: int = 0
    hedge_avg_price_micros: int = 0
    unhedged_started_ms: int | None = None
    unhedged_notional_ms_accumulated: int = 0
    last_event_ms: int = 0
    reconciliation_version: int = 0
    halt_reason: str | None = None
    primary_order_id: str | None = None
    pending_unwind: bool = False
    last_unwind_reconciliation_version: int | None = None
    next_hedge_seq: int = 0

    def __post_init__(self) -> None:
        _require_nonempty_str(self.group_id, "group_id")
        _require_nonempty_str(self.instrument, "instrument")
        _require_nonempty_str(self.primary_venue, "primary_venue")
        _require_nonempty_str(self.hedge_venue, "hedge_venue")
        _require_pos_int(self.target_qty_base, "target_qty_base")
        if not isinstance(self.state, HedgeGroupState):
            object.__setattr__(self, "state", HedgeGroupState(self.state))
        _require_nonneg_int(self.confirmed_primary_filled_base, "confirmed_primary_filled_base")
        _require_nonneg_int(self.confirmed_hedge_filled_base, "confirmed_hedge_filled_base")
        _require_nonneg_int(self.pending_hedge_qty_base, "pending_hedge_qty_base")
        if not isinstance(self.seen_fill_keys, frozenset):
            raise TypeError("seen_fill_keys must be a frozenset")
        _require_nonneg_int(self.primary_avg_price_micros, "primary_avg_price_micros")
        _require_nonneg_int(self.hedge_avg_price_micros, "hedge_avg_price_micros")
        if self.unhedged_started_ms is not None:
            _require_nonneg_int(self.unhedged_started_ms, "unhedged_started_ms")
        _require_nonneg_int(
            self.unhedged_notional_ms_accumulated, "unhedged_notional_ms_accumulated"
        )
        _require_nonneg_int(self.last_event_ms, "last_event_ms")
        _require_nonneg_int(self.reconciliation_version, "reconciliation_version")
        if type(self.pending_unwind) is not bool:
            raise TypeError("pending_unwind must be a bool")
        if self.last_unwind_reconciliation_version is not None:
            _require_nonneg_int(
                self.last_unwind_reconciliation_version, "last_unwind_reconciliation_version"
            )
        _require_nonneg_int(self.next_hedge_seq, "next_hedge_seq")


# --- Events ---


@dataclass(frozen=True, slots=True)
class ReserveCapital:
    event_ms: int
    reservation_id: str
    reserved_usd_micros: int

    def __post_init__(self) -> None:
        _require_nonneg_int(self.event_ms, "event_ms")
        _require_nonempty_str(self.reservation_id, "reservation_id")
        _require_pos_int(self.reserved_usd_micros, "reserved_usd_micros")


@dataclass(frozen=True, slots=True)
class StartQuoting:
    event_ms: int
    order_id: str

    def __post_init__(self) -> None:
        _require_nonneg_int(self.event_ms, "event_ms")
        _require_nonempty_str(self.order_id, "order_id")


@dataclass(frozen=True, slots=True)
class PrimaryAck:
    event_ms: int
    order_id: str
    accepted_qty_base: int

    def __post_init__(self) -> None:
        _require_nonneg_int(self.event_ms, "event_ms")
        _require_nonempty_str(self.order_id, "order_id")
        _require_pos_int(self.accepted_qty_base, "accepted_qty_base")


@dataclass(frozen=True, slots=True)
class AckTimeout:
    event_ms: int
    leg: FillLeg | str
    order_id: str

    def __post_init__(self) -> None:
        _require_nonneg_int(self.event_ms, "event_ms")
        if not isinstance(self.leg, FillLeg):
            object.__setattr__(self, "leg", FillLeg(self.leg))
        _require_nonempty_str(self.order_id, "order_id")


@dataclass(frozen=True, slots=True)
class FillConfirmed:
    event_ms: int
    fill: FillRecord

    def __post_init__(self) -> None:
        _require_nonneg_int(self.event_ms, "event_ms")
        if not isinstance(self.fill, FillRecord):
            raise TypeError("fill must be a FillRecord")


@dataclass(frozen=True, slots=True)
class HedgeAck:
    event_ms: int
    order_id: str
    client_order_id: str
    accepted_qty_base: int

    def __post_init__(self) -> None:
        _require_nonneg_int(self.event_ms, "event_ms")
        _require_nonempty_str(self.order_id, "order_id")
        _require_nonempty_str(self.client_order_id, "client_order_id")
        _require_pos_int(self.accepted_qty_base, "accepted_qty_base")


@dataclass(frozen=True, slots=True)
class AuthoritativePositionMismatch:
    event_ms: int
    authoritative_primary_base: int
    authoritative_hedge_base: int
    reconciliation_version: int
    source: str = "reconciler"
    reason: str = "position_mismatch"

    def __post_init__(self) -> None:
        _require_nonneg_int(self.event_ms, "event_ms")
        _require_nonneg_int(self.authoritative_primary_base, "authoritative_primary_base")
        _require_nonneg_int(self.authoritative_hedge_base, "authoritative_hedge_base")
        _require_nonneg_int(self.reconciliation_version, "reconciliation_version")
        _require_nonempty_str(self.source, "source")
        _require_nonempty_str(self.reason, "reason")


@dataclass(frozen=True, slots=True)
class FeedStaleOrDisconnected:
    event_ms: int
    venue: str
    reason: str = "feed_stale_or_disconnected"

    def __post_init__(self) -> None:
        _require_nonneg_int(self.event_ms, "event_ms")
        _require_nonempty_str(self.venue, "venue")
        _require_nonempty_str(self.reason, "reason")


@dataclass(frozen=True, slots=True)
class ReconcileSnapshot:
    event_ms: int
    reconciliation_version: int
    authoritative_primary_base: int
    authoritative_hedge_base: int
    snapshot_age_ms: int = 0
    is_stale: bool = False
    open_hedge_qty_base: int = 0
    final: bool = False

    def __post_init__(self) -> None:
        _require_nonneg_int(self.event_ms, "event_ms")
        _require_nonneg_int(self.reconciliation_version, "reconciliation_version")
        _require_nonneg_int(self.authoritative_primary_base, "authoritative_primary_base")
        _require_nonneg_int(self.authoritative_hedge_base, "authoritative_hedge_base")
        _require_nonneg_int(self.snapshot_age_ms, "snapshot_age_ms")
        if type(self.is_stale) is not bool:
            raise TypeError("is_stale must be a bool")
        _require_nonneg_int(self.open_hedge_qty_base, "open_hedge_qty_base")
        if type(self.final) is not bool:
            raise TypeError("final must be a bool")


@dataclass(frozen=True, slots=True)
class RequestUnwind:
    event_ms: int
    reason: str = "risk_unwind"
    verified_authoritative_primary_base: int | None = None
    verified_authoritative_hedge_base: int | None = None
    reconciliation_version: int | None = None

    def __post_init__(self) -> None:
        _require_nonneg_int(self.event_ms, "event_ms")
        _require_nonempty_str(self.reason, "reason")
        if self.verified_authoritative_primary_base is not None:
            _require_nonneg_int(
                self.verified_authoritative_primary_base, "verified_authoritative_primary_base"
            )
        if self.verified_authoritative_hedge_base is not None:
            _require_nonneg_int(
                self.verified_authoritative_hedge_base, "verified_authoritative_hedge_base"
            )
        if self.reconciliation_version is not None:
            _require_nonneg_int(self.reconciliation_version, "reconciliation_version")


@dataclass(frozen=True, slots=True)
class TimerTick:
    event_ms: int

    def __post_init__(self) -> None:
        _require_nonneg_int(self.event_ms, "event_ms")


HedgeEvent = (
    ReserveCapital
    | StartQuoting
    | PrimaryAck
    | AckTimeout
    | FillConfirmed
    | HedgeAck
    | AuthoritativePositionMismatch
    | FeedStaleOrDisconnected
    | ReconcileSnapshot
    | RequestUnwind
    | TimerTick
)


# --- Directives ---


@dataclass(frozen=True, slots=True)
class SubmitHedgeOrder:
    group_id: str
    venue: str
    instrument: str
    client_order_id: str
    qty_base: int
    reduce_only: Literal[False] = False

    def __post_init__(self) -> None:
        _require_pos_int(self.qty_base, "qty_base")
        if self.reduce_only is not False:
            raise ValueError("SubmitHedgeOrder must not set reduce_only=True")


@dataclass(frozen=True, slots=True)
class EmitFreezeNotice:
    group_id: str
    reason: str
    target_state: HedgeGroupState


@dataclass(frozen=True, slots=True)
class ReduceOnlyUnwindDirective:
    group_id: str
    venue: str
    instrument: str
    qty_base: int
    reconciliation_version: int
    reduce_only: Literal[True] = True

    def __post_init__(self) -> None:
        _require_pos_int(self.qty_base, "qty_base")
        _require_nonneg_int(self.reconciliation_version, "reconciliation_version")
        if self.reduce_only is not True:
            raise ValueError("ReduceOnlyUnwindDirective requires reduce_only=True")


@dataclass(frozen=True, slots=True)
class CancelOrderDirective:
    group_id: str
    venue: str
    order_id: str
    reason: str


CoordinatorDirective = (
    SubmitHedgeOrder | EmitFreezeNotice | ReduceOnlyUnwindDirective | CancelOrderDirective
)


_FROZEN_STATES: frozenset[HedgeGroupState] = frozenset(
    {
        HedgeGroupState.UNKNOWN,
        HedgeGroupState.HALTED,
        HedgeGroupState.CANCEL_PENDING,
        HedgeGroupState.UNWINDING,
        HedgeGroupState.CLOSED,
    }
)
_ACTIVE_FILL_STATES: frozenset[HedgeGroupState] = frozenset(
    {
        HedgeGroupState.RESERVED,
        HedgeGroupState.QUOTING,
        HedgeGroupState.PRIMARY_WORKING,
        HedgeGroupState.PARTIALLY_FILLED,
        HedgeGroupState.HEDGE_REQUIRED,
        HedgeGroupState.HEDGING,
        HedgeGroupState.HEDGED,
    }
)
_MISMATCH_HALT_PREFIXES: tuple[str, ...] = (
    "authoritative_position_mismatch",
    "reconcile_position_mismatch",
    "stale_reconcile_snapshot",
    "stale_reconciliation_version",
)


def _reference_price_micros(group: HedgeGroup) -> int:
    if group.primary_avg_price_micros > 0:
        return group.primary_avg_price_micros
    return group.hedge_avg_price_micros


def _unhedged_notional_usd_micros(group: HedgeGroup, policy: HedgePolicy) -> int:
    unhedged_base = abs(group.confirmed_primary_filled_base - group.confirmed_hedge_filled_base)
    if unhedged_base == 0:
        return 0
    ref_price = _reference_price_micros(group)
    if ref_price <= 0:
        return 0
    scale = int(10**policy.base_decimals)
    return (unhedged_base * ref_price + scale - 1) // scale


def _advance_exposure_clock(group: HedgeGroup, event_ms: int, policy: HedgePolicy) -> HedgeGroup:
    dt_ms = max(0, event_ms - group.last_event_ms)
    added = _unhedged_notional_usd_micros(group, policy) * dt_ms
    return replace(
        group,
        unhedged_notional_ms_accumulated=group.unhedged_notional_ms_accumulated + added,
        last_event_ms=max(group.last_event_ms, event_ms),
    )


def _halt(group: HedgeGroup, reason: str) -> tuple[HedgeGroup, tuple[CoordinatorDirective, ...]]:
    kept_reason = group.halt_reason if group.state is HedgeGroupState.HALTED else reason
    final_reason = kept_reason or reason
    halted = replace(
        group,
        state=HedgeGroupState.HALTED,
        halt_reason=final_reason,
    )
    return halted, (
        EmitFreezeNotice(
            group_id=group.group_id,
            reason=final_reason,
            target_state=HedgeGroupState.HALTED,
        ),
    )


def _weighted_avg_price(old_qty: int, old_avg: int, add_qty: int, add_price: int) -> int:
    new_qty = old_qty + add_qty
    if new_qty == 0:
        return 0
    return (old_avg * old_qty + add_price * add_qty) // new_qty


def _maybe_emit_hedge(
    group: HedgeGroup, event_ms: int, policy: HedgePolicy, ref_price_micros: int
) -> tuple[HedgeGroup, tuple[CoordinatorDirective, ...]]:
    if group.unhedged_notional_ms_accumulated > policy.max_unhedged_notional_ms:
        return _halt(group, "unhedged_notional_ms_budget_exceeded")

    delta = (
        group.confirmed_primary_filled_base
        - group.confirmed_hedge_filled_base
        - group.pending_hedge_qty_base
    )
    if delta <= 0:
        if group.pending_hedge_qty_base > 0:
            return replace(group, state=HedgeGroupState.HEDGING), ()
        if group.confirmed_primary_filled_base == group.confirmed_hedge_filled_base:
            next_state = (
                HedgeGroupState.HEDGED
                if group.confirmed_primary_filled_base == group.target_qty_base
                else HedgeGroupState.PARTIALLY_FILLED
            )
            return replace(group, state=next_state, unhedged_started_ms=None), ()
        return group, ()

    scale = 10**policy.base_decimals
    delta_notional_usd_micros = (delta * ref_price_micros) // scale
    elapsed_ms = (
        max(0, event_ms - group.unhedged_started_ms) if group.unhedged_started_ms is not None else 0
    )

    if (
        delta_notional_usd_micros >= policy.min_hedge_notional_usd_micros
        or elapsed_ms >= policy.max_accumulate_wait_ms
    ):
        child_qty = min(delta, policy.max_child_order_base)
        client_order_id = f"{group.group_id}:hedge:{group.next_hedge_seq}"
        updated = replace(
            group,
            state=HedgeGroupState.HEDGING,
            pending_hedge_qty_base=group.pending_hedge_qty_base + child_qty,
            next_hedge_seq=group.next_hedge_seq + 1,
        )
        directive = SubmitHedgeOrder(
            group_id=group.group_id,
            venue=group.hedge_venue,
            instrument=group.instrument,
            client_order_id=client_order_id,
            qty_base=child_qty,
        )
        return updated, (directive,)

    waiting_state = (
        HedgeGroupState.HEDGING
        if group.pending_hedge_qty_base > 0
        else (
            HedgeGroupState.HEDGE_REQUIRED
            if group.confirmed_primary_filled_base == group.target_qty_base
            else HedgeGroupState.PARTIALLY_FILLED
        )
    )
    return replace(group, state=waiting_state), ()


def _apply_fill_confirmed(
    group: HedgeGroup, event: FillConfirmed, policy: HedgePolicy
) -> tuple[HedgeGroup, tuple[CoordinatorDirective, ...]]:
    fill = event.fill
    dedup_key = fill.dedup_key
    if dedup_key in group.seen_fill_keys:
        return group, ()

    if fill.leg is FillLeg.PRIMARY and fill.venue != group.primary_venue:
        return _halt(_advance_exposure_clock(group, event.event_ms, policy), "fill_venue_mismatch")
    if fill.leg is FillLeg.HEDGE and fill.venue != group.hedge_venue:
        return _halt(_advance_exposure_clock(group, event.event_ms, policy), "fill_venue_mismatch")

    advanced = _advance_exposure_clock(group, event.event_ms, policy)
    seen = advanced.seen_fill_keys | frozenset({dedup_key})

    if fill.leg is FillLeg.PRIMARY:
        new_primary = advanced.confirmed_primary_filled_base + fill.qty_base
        new_avg = _weighted_avg_price(
            advanced.confirmed_primary_filled_base,
            advanced.primary_avg_price_micros,
            fill.qty_base,
            fill.price_micros,
        )
        started = advanced.unhedged_started_ms
        if started is None and new_primary > advanced.confirmed_hedge_filled_base:
            started = event.event_ms
        updated = replace(
            advanced,
            confirmed_primary_filled_base=new_primary,
            primary_avg_price_micros=new_avg,
            seen_fill_keys=seen,
            unhedged_started_ms=started,
        )
        if advanced.state not in _ACTIVE_FILL_STATES:
            if advanced.state is HedgeGroupState.UNKNOWN:
                return updated, (
                    EmitFreezeNotice(
                        group_id=updated.group_id,
                        reason="fill_in_unknown_state",
                        target_state=HedgeGroupState.UNKNOWN,
                    ),
                )
            reason = advanced.halt_reason or f"fill_in_{advanced.state.value}_state"
            if advanced.state is HedgeGroupState.HALTED:
                return updated, (
                    EmitFreezeNotice(
                        group_id=updated.group_id,
                        reason=reason,
                        target_state=HedgeGroupState.HALTED,
                    ),
                )
            return _halt(updated, reason)

        max_gross = (
            policy.max_gross_position_base
            if policy.max_gross_position_base is not None
            else advanced.target_qty_base
        )
        if new_primary > max_gross or new_primary > advanced.target_qty_base:
            return _halt(updated, "primary_overfill")
        return _maybe_emit_hedge(updated, event.event_ms, policy, fill.price_micros)

    new_hedge = advanced.confirmed_hedge_filled_base + fill.qty_base
    new_pending = max(0, advanced.pending_hedge_qty_base - fill.qty_base)
    new_avg = _weighted_avg_price(
        advanced.confirmed_hedge_filled_base,
        advanced.hedge_avg_price_micros,
        fill.qty_base,
        fill.price_micros,
    )
    new_started = (
        None
        if new_hedge == advanced.confirmed_primary_filled_base
        else advanced.unhedged_started_ms
    )
    updated = replace(
        advanced,
        confirmed_hedge_filled_base=new_hedge,
        pending_hedge_qty_base=new_pending,
        hedge_avg_price_micros=new_avg,
        seen_fill_keys=seen,
        unhedged_started_ms=new_started,
    )
    if advanced.state not in _ACTIVE_FILL_STATES:
        if advanced.state is HedgeGroupState.UNKNOWN:
            return updated, (
                EmitFreezeNotice(
                    group_id=updated.group_id,
                    reason="hedge_fill_in_unknown_state",
                    target_state=HedgeGroupState.UNKNOWN,
                ),
            )
        if advanced.state in (HedgeGroupState.UNWINDING, HedgeGroupState.CLOSED):
            return updated, ()
        return _halt(updated, advanced.halt_reason or "hedge_fill_while_halted")

    if new_hedge > advanced.confirmed_primary_filled_base + policy.position_tolerance_base:
        return _halt(updated, "hedge_overfill_exceeds_primary")

    ref_price = _reference_price_micros(updated) or fill.price_micros
    return _maybe_emit_hedge(updated, event.event_ms, policy, ref_price)


def _apply_reconcile_snapshot(
    group: HedgeGroup, event: ReconcileSnapshot, policy: HedgePolicy
) -> tuple[HedgeGroup, tuple[CoordinatorDirective, ...]]:
    advanced = _advance_exposure_clock(group, event.event_ms, policy)
    if (
        event.is_stale
        or event.snapshot_age_ms > policy.max_snapshot_age_ms
        or event.reconciliation_version < advanced.reconciliation_version
    ):
        return _halt(advanced, "stale_reconcile_snapshot")

    ver_updated = replace(advanced, reconciliation_version=event.reconciliation_version)
    if ver_updated.state is HedgeGroupState.UNWINDING:
        if (
            event.authoritative_primary_base == 0
            and event.authoritative_hedge_base == 0
            and event.open_hedge_qty_base == 0
        ):
            closed = replace(
                ver_updated,
                state=HedgeGroupState.CLOSED,
                confirmed_primary_filled_base=0,
                confirmed_hedge_filled_base=0,
                pending_hedge_qty_base=0,
                unhedged_started_ms=None,
                pending_unwind=False,
            )
            return closed, ()
        if (
            event.authoritative_primary_base > ver_updated.confirmed_primary_filled_base
            or event.authoritative_hedge_base > ver_updated.confirmed_hedge_filled_base
        ):
            return _halt(ver_updated, "reconcile_exposure_grew_during_unwind")
        return replace(
            ver_updated,
            confirmed_primary_filled_base=event.authoritative_primary_base,
            confirmed_hedge_filled_base=event.authoritative_hedge_base,
            pending_hedge_qty_base=event.open_hedge_qty_base,
        ), ()

    primary_diff = abs(event.authoritative_primary_base - ver_updated.confirmed_primary_filled_base)
    hedge_diff = abs(event.authoritative_hedge_base - ver_updated.confirmed_hedge_filled_base)
    if max(primary_diff, hedge_diff) > policy.position_tolerance_base:
        return _halt(ver_updated, "reconcile_position_mismatch")

    if ver_updated.state is HedgeGroupState.HALTED:
        if (
            event.final
            and event.authoritative_primary_base == 0
            and event.authoritative_hedge_base == 0
            and event.open_hedge_qty_base == 0
        ):
            return replace(ver_updated, state=HedgeGroupState.CLOSED, unhedged_started_ms=None), ()
        return _halt(ver_updated, ver_updated.halt_reason or "halted_awaiting_unwind")

    if ver_updated.state is HedgeGroupState.UNKNOWN:
        if (
            event.authoritative_primary_base == 0
            and event.authoritative_hedge_base == 0
            and event.open_hedge_qty_base == 0
        ):
            return replace(ver_updated, state=HedgeGroupState.CLOSED, unhedged_started_ms=None), ()
        if (
            abs(event.authoritative_primary_base - event.authoritative_hedge_base)
            <= policy.position_tolerance_base
            and event.authoritative_primary_base == ver_updated.target_qty_base
            and event.open_hedge_qty_base == 0
        ):
            target = HedgeGroupState.CLOSED if event.final else HedgeGroupState.RECONCILING
            return replace(ver_updated, state=target, unhedged_started_ms=None), ()
        return _halt(ver_updated, "unknown_resolved_with_unhedged_exposure")

    if ver_updated.state in (HedgeGroupState.HEDGED, HedgeGroupState.RECONCILING) and (
        event.open_hedge_qty_base == 0
        and abs(event.authoritative_primary_base - event.authoritative_hedge_base)
        <= policy.position_tolerance_base
    ):
        next_state = (
            HedgeGroupState.CLOSED
            if event.final or ver_updated.state is HedgeGroupState.RECONCILING
            else HedgeGroupState.RECONCILING
        )
        return replace(ver_updated, state=next_state, unhedged_started_ms=None), ()

    return ver_updated, ()


def _apply_request_unwind(
    group: HedgeGroup, event: RequestUnwind, policy: HedgePolicy
) -> tuple[HedgeGroup, tuple[CoordinatorDirective, ...]]:
    advanced = _advance_exposure_clock(group, event.event_ms, policy)
    if advanced.state is HedgeGroupState.CLOSED:
        return advanced, ()

    if (
        advanced.state is HedgeGroupState.UNWINDING
        and advanced.pending_unwind
        and (
            event.reconciliation_version is None
            or (
                advanced.last_unwind_reconciliation_version is not None
                and event.reconciliation_version <= advanced.last_unwind_reconciliation_version
            )
        )
    ):
        return advanced, ()

    if (
        advanced.state
        in (
            HedgeGroupState.PROPOSED,
            HedgeGroupState.RESERVED,
            HedgeGroupState.QUOTING,
            HedgeGroupState.PRIMARY_WORKING,
        )
        and advanced.confirmed_primary_filled_base == 0
        and advanced.confirmed_hedge_filled_base == 0
        and advanced.pending_hedge_qty_base == 0
    ):
        if advanced.primary_order_id is not None and advanced.state in (
            HedgeGroupState.QUOTING,
            HedgeGroupState.PRIMARY_WORKING,
        ):
            return replace(advanced, state=HedgeGroupState.CANCEL_PENDING), (
                CancelOrderDirective(
                    group_id=advanced.group_id,
                    venue=advanced.primary_venue,
                    order_id=advanced.primary_order_id,
                    reason=event.reason,
                ),
            )
        return replace(advanced, state=HedgeGroupState.CLOSED), ()

    requires_verified = advanced.state is HedgeGroupState.UNKNOWN or (
        advanced.halt_reason is not None
        and advanced.halt_reason.startswith(_MISMATCH_HALT_PREFIXES)
    )
    has_verified = (
        event.verified_authoritative_primary_base is not None
        and event.verified_authoritative_hedge_base is not None
        and event.reconciliation_version is not None
    )

    if requires_verified and not has_verified:
        return advanced, (
            EmitFreezeNotice(
                group_id=advanced.group_id,
                reason="unwind_requires_fresh_authoritative_verification",
                target_state=advanced.state,
            ),
        )

    if has_verified:
        assert event.reconciliation_version is not None
        assert event.verified_authoritative_primary_base is not None
        assert event.verified_authoritative_hedge_base is not None
        if event.reconciliation_version < advanced.reconciliation_version or (
            advanced.last_unwind_reconciliation_version is not None
            and event.reconciliation_version <= advanced.last_unwind_reconciliation_version
        ):
            return advanced, (
                EmitFreezeNotice(
                    group_id=advanced.group_id,
                    reason="stale_unwind_reconciliation_version",
                    target_state=advanced.state,
                ),
            )
        unwind_primary = event.verified_authoritative_primary_base
        unwind_hedge = event.verified_authoritative_hedge_base
        unwind_ver = event.reconciliation_version
    else:
        unwind_primary = advanced.confirmed_primary_filled_base
        unwind_hedge = advanced.confirmed_hedge_filled_base
        unwind_ver = advanced.reconciliation_version

    directives: list[CoordinatorDirective] = []
    if unwind_primary > 0:
        directives.append(
            ReduceOnlyUnwindDirective(
                group_id=advanced.group_id,
                venue=advanced.primary_venue,
                instrument=advanced.instrument,
                qty_base=unwind_primary,
                reconciliation_version=unwind_ver,
            )
        )
    if unwind_hedge > 0:
        directives.append(
            ReduceOnlyUnwindDirective(
                group_id=advanced.group_id,
                venue=advanced.hedge_venue,
                instrument=advanced.instrument,
                qty_base=unwind_hedge,
                reconciliation_version=unwind_ver,
            )
        )

    if not directives:
        return replace(
            advanced,
            state=HedgeGroupState.CLOSED,
            pending_unwind=False,
            unhedged_started_ms=None,
            reconciliation_version=max(advanced.reconciliation_version, unwind_ver),
        ), ()

    return (
        replace(
            advanced,
            state=HedgeGroupState.UNWINDING,
            pending_unwind=True,
            reconciliation_version=max(advanced.reconciliation_version, unwind_ver),
            last_unwind_reconciliation_version=unwind_ver,
        ),
        tuple(directives),
    )


def apply_hedge_event(
    group: HedgeGroup, event: HedgeEvent, policy: HedgePolicy
) -> tuple[HedgeGroup, tuple[CoordinatorDirective, ...]]:
    """Pure state transition for a distributed two-leg HedgeGroup saga."""
    if not isinstance(group, HedgeGroup):
        raise TypeError("group must be a HedgeGroup")
    if not isinstance(policy, HedgePolicy):
        raise TypeError("policy must be a HedgePolicy")

    if isinstance(event, FillConfirmed):
        return _apply_fill_confirmed(group, event, policy)

    if isinstance(event, ReserveCapital):
        advanced = _advance_exposure_clock(group, event.event_ms, policy)
        if advanced.state is HedgeGroupState.PROPOSED:
            return replace(advanced, state=HedgeGroupState.RESERVED), ()
        if advanced.state is HedgeGroupState.RESERVED:
            return advanced, ()
        if advanced.state in _FROZEN_STATES:
            return advanced, (
                EmitFreezeNotice(
                    group_id=advanced.group_id,
                    reason=advanced.halt_reason or f"reserve_blocked_in_{advanced.state.value}",
                    target_state=advanced.state,
                ),
            )
        return advanced, ()

    if isinstance(event, StartQuoting):
        advanced = _advance_exposure_clock(group, event.event_ms, policy)
        if advanced.state is HedgeGroupState.RESERVED:
            return replace(
                advanced, state=HedgeGroupState.QUOTING, primary_order_id=event.order_id
            ), ()
        if (
            advanced.state is HedgeGroupState.QUOTING
            and advanced.primary_order_id == event.order_id
        ):
            return advanced, ()
        if advanced.state in _FROZEN_STATES:
            return advanced, (
                EmitFreezeNotice(
                    group_id=advanced.group_id,
                    reason=advanced.halt_reason or f"quote_blocked_in_{advanced.state.value}",
                    target_state=advanced.state,
                ),
            )
        return _halt(advanced, "invalid_start_quoting_state")

    if isinstance(event, PrimaryAck):
        advanced = _advance_exposure_clock(group, event.event_ms, policy)
        if advanced.state in _FROZEN_STATES:
            if advanced.state in (HedgeGroupState.HALTED, HedgeGroupState.UNKNOWN):
                return advanced, (
                    EmitFreezeNotice(
                        group_id=advanced.group_id,
                        reason=advanced.halt_reason
                        or f"primary_ack_ignored_in_{advanced.state.value}",
                        target_state=advanced.state,
                    ),
                )
            return advanced, ()
        if advanced.primary_order_id is not None and advanced.primary_order_id != event.order_id:
            return _halt(advanced, "unexpected_primary_order_id")
        if advanced.state in (HedgeGroupState.RESERVED, HedgeGroupState.QUOTING):
            return replace(
                advanced,
                state=HedgeGroupState.PRIMARY_WORKING,
                primary_order_id=event.order_id,
            ), ()
        # Duplicate/late ack never creates hedge demand and never regresses filled states.
        return replace(advanced, primary_order_id=advanced.primary_order_id or event.order_id), ()

    if isinstance(event, HedgeAck):
        advanced = _advance_exposure_clock(group, event.event_ms, policy)
        if advanced.unhedged_notional_ms_accumulated > policy.max_unhedged_notional_ms:
            return _halt(advanced, "unhedged_notional_ms_budget_exceeded")
        if advanced.state in (HedgeGroupState.HALTED, HedgeGroupState.UNKNOWN):
            return advanced, (
                EmitFreezeNotice(
                    group_id=advanced.group_id,
                    reason=advanced.halt_reason or f"hedge_ack_in_{advanced.state.value}",
                    target_state=advanced.state,
                ),
            )
        return advanced, ()

    if isinstance(event, AckTimeout):
        advanced = _advance_exposure_clock(group, event.event_ms, policy)
        if advanced.state in (HedgeGroupState.HALTED, HedgeGroupState.CLOSED):
            return advanced, ()
        reason = f"ack_timeout:{FillLeg(event.leg).value}:{event.order_id}"
        unknown_group = replace(
            advanced,
            state=HedgeGroupState.UNKNOWN,
            halt_reason=advanced.halt_reason or reason,
        )
        return unknown_group, (
            EmitFreezeNotice(
                group_id=unknown_group.group_id,
                reason=reason,
                target_state=HedgeGroupState.UNKNOWN,
            ),
        )

    if isinstance(event, FeedStaleOrDisconnected):
        advanced = _advance_exposure_clock(group, event.event_ms, policy)
        if advanced.state is HedgeGroupState.CLOSED:
            return advanced, ()
        return _halt(advanced, f"feed_stale_or_disconnected:{event.venue}:{event.reason}")

    if isinstance(event, AuthoritativePositionMismatch):
        advanced = _advance_exposure_clock(group, event.event_ms, policy)
        if advanced.state is HedgeGroupState.CLOSED:
            return advanced, ()
        if event.reconciliation_version < advanced.reconciliation_version:
            return _halt(advanced, "stale_reconciliation_version")
        primary_diff = abs(
            event.authoritative_primary_base - advanced.confirmed_primary_filled_base
        )
        hedge_diff = abs(event.authoritative_hedge_base - advanced.confirmed_hedge_filled_base)
        ver_group = replace(
            advanced,
            reconciliation_version=max(
                advanced.reconciliation_version, event.reconciliation_version
            ),
        )
        if max(primary_diff, hedge_diff) > policy.position_tolerance_base:
            return _halt(ver_group, f"authoritative_position_mismatch:{event.reason}")
        return ver_group, ()

    if isinstance(event, ReconcileSnapshot):
        return _apply_reconcile_snapshot(group, event, policy)

    if isinstance(event, RequestUnwind):
        return _apply_request_unwind(group, event, policy)

    if isinstance(event, TimerTick):
        advanced = _advance_exposure_clock(group, event.event_ms, policy)
        if advanced.state in _FROZEN_STATES:
            return advanced, ()
        if advanced.unhedged_notional_ms_accumulated > policy.max_unhedged_notional_ms:
            return _halt(advanced, "unhedged_notional_ms_budget_exceeded")
        ref_price = _reference_price_micros(advanced)
        if ref_price > 0:
            return _maybe_emit_hedge(advanced, event.event_ms, policy, ref_price)
        return advanced, ()

    raise TypeError(f"unsupported hedge event: {type(event).__name__}")


__all__ = [
    "AckTimeout",
    "AuthoritativePositionMismatch",
    "CancelOrderDirective",
    "CoordinatorDirective",
    "EmitFreezeNotice",
    "FeedStaleOrDisconnected",
    "FillConfirmed",
    "FillLeg",
    "FillRecord",
    "HedgeAck",
    "HedgeEvent",
    "HedgeGroup",
    "HedgeGroupState",
    "HedgePolicy",
    "PrimaryAck",
    "ReconcileSnapshot",
    "ReduceOnlyUnwindDirective",
    "RequestUnwind",
    "ReserveCapital",
    "StartQuoting",
    "SubmitHedgeOrder",
    "TimerTick",
    "apply_hedge_event",
]
