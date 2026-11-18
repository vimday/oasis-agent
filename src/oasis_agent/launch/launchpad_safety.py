"""Fail-closed Launchpad Safety Gate (P1) for Base Denim anti-snipe curves and locks.

Models Clanker v4, Doppler Multicurve, Zora Creator, Flaunch FairLaunch, and Virtuals
curves using pure exact-integer arithmetic (basis points, USD micros, and millisecond
Denim timestamps). Any missing, stale, single-source, or mismatched evidence fails
closed to ``DecisionStatus.REJECTED``; pure anti-snipe decay/lock windows defer cleanly
to ``DecisionStatus.DEFER_UNTIL_DECAY`` with ``earliest_safe_entry_ms``.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from ..evidence.base_denim import BASE_CHAIN_ID, parse_header_timestamp_ms
from ..models.common import normalize_address, normalize_hash32

BASE_DENIM_BLOCK_TIME_MS: Final[int] = 200
CLANKER_V4_FEE_SCHEDULE_BPS: Final[tuple[int, ...]] = (8_000, 6_200, 4_400, 2_600, 800)
BPS_DENOMINATOR: Final[int] = 10_000


class LaunchpadArchetype(StrEnum):
    CLANKER_V4 = "clanker_v4"
    DOPPLER_MULTICURVE = "doppler_multicurve"
    ZORA_CREATOR = "zora_creator"
    FLAUNCH_FAIRLAUNCH = "flaunch_fairlaunch"
    VIRTUALS_CURVE = "virtuals_curve"
    UNKNOWN_CUSTOM = "unknown_custom"


class DecisionStatus(StrEnum):
    APPROVED = "APPROVED"
    DEFER_UNTIL_DECAY = "DEFER_UNTIL_DECAY"
    REJECTED = "REJECTED"


def _require_int(value: object, name: str) -> int:
    if type(value) is not int:
        raise TypeError(f"{name} must be an int, got {type(value).__name__}")
    return value


def _require_bps(value: object, name: str) -> int:
    val = _require_int(value, name)
    if not 0 <= val <= BPS_DENOMINATOR:
        raise ValueError(f"{name} must be in [0, 10000] bps")
    return val


def _validate_sha256(digest: str, name: str = "raw_sha256") -> str:
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(ch not in "0123456789abcdef" for ch in digest)
    ):
        raise ValueError(f"{name} must be a lowercase 64-char SHA-256 hex digest")
    return digest


def compute_linear_decay_fee_bps(
    start_fee_bps: int,
    end_fee_bps: int,
    genesis_ts_ms: int,
    current_ts_ms: int,
    duration_ms: int,
) -> int:
    """Compute conservative integer linear-decay fee in bps clamped to ``[end, start]``.

    Returns ``start_fee_bps`` when ``current_ts_ms < genesis_ts_ms``. Subtracting the
    integer-floor reduction guarantees the returned bps never underestimates the fee.
    """
    _require_bps(start_fee_bps, "start_fee_bps")
    _require_bps(end_fee_bps, "end_fee_bps")
    if end_fee_bps > start_fee_bps:
        raise ValueError("end_fee_bps must not exceed start_fee_bps")
    _require_int(genesis_ts_ms, "genesis_ts_ms")
    _require_int(current_ts_ms, "current_ts_ms")
    _require_int(duration_ms, "duration_ms")
    if genesis_ts_ms < 0 or current_ts_ms < 0 or duration_ms < 0:
        raise ValueError("timestamps and duration_ms must be non-negative")

    if current_ts_ms < genesis_ts_ms:
        return start_fee_bps
    elapsed_ms = current_ts_ms - genesis_ts_ms
    if duration_ms == 0 or elapsed_ms >= duration_ms:
        return end_fee_bps
    reduction_bps = ((start_fee_bps - end_fee_bps) * elapsed_ms) // duration_ms
    fee_bps = start_fee_bps - reduction_bps
    return max(end_fee_bps, min(start_fee_bps, fee_bps))


def compute_stepped_auction_fee_bps(
    fee_schedule_bps: tuple[int, ...],
    round_blocks: int,
    blocks_elapsed: int,
    normal_fee_bps: int,
) -> int:
    """Compute Clanker v4 style stepped-auction fee in bps across discrete block rounds."""
    if not isinstance(fee_schedule_bps, tuple) or not fee_schedule_bps:
        raise ValueError("fee_schedule_bps must be a non-empty tuple")
    for idx, fee in enumerate(fee_schedule_bps):
        _require_bps(fee, f"fee_schedule_bps[{idx}]")
    _require_int(round_blocks, "round_blocks")
    if round_blocks <= 0:
        raise ValueError("round_blocks must be positive")
    _require_int(blocks_elapsed, "blocks_elapsed")
    _require_bps(normal_fee_bps, "normal_fee_bps")

    if blocks_elapsed < 0:
        return max(fee_schedule_bps[0], normal_fee_bps)
    round_index = blocks_elapsed // round_blocks
    if round_index < len(fee_schedule_bps):
        return max(fee_schedule_bps[round_index], normal_fee_bps)
    return normal_fee_bps


def earliest_safe_entry_ms(
    *,
    max_buy_tax_bps: int,
    unlock_timestamp_ms: int | None = None,
    genesis_timestamp_ms: int | None = None,
    decay_start_fee_bps: int | None = None,
    decay_end_fee_bps: int | None = None,
    decay_duration_ms: int | None = None,
    auction_fee_schedule_bps: tuple[int, ...] | None = None,
    auction_round_blocks: int | None = None,
    normal_fee_bps: int | None = None,
    block_time_ms: int = BASE_DENIM_BLOCK_TIME_MS,
    slot_step_ms: int = BASE_DENIM_BLOCK_TIME_MS,
) -> int:
    """Return the exact Denim millisecond timestamp where fee <= max and lock expired."""
    _require_bps(max_buy_tax_bps, "max_buy_tax_bps")
    _require_int(slot_step_ms, "slot_step_ms")
    if slot_step_ms <= 0:
        raise ValueError("slot_step_ms must be positive")
    _require_int(block_time_ms, "block_time_ms")
    if block_time_ms <= 0:
        raise ValueError("block_time_ms must be positive")

    candidates: list[int] = []
    if unlock_timestamp_ms is not None:
        _require_int(unlock_timestamp_ms, "unlock_timestamp_ms")
        if unlock_timestamp_ms < 0:
            raise ValueError("unlock_timestamp_ms must be non-negative")
        candidates.append(unlock_timestamp_ms)

    linear_decay_spec = (decay_start_fee_bps, decay_end_fee_bps, decay_duration_ms)
    linear_fields = (genesis_timestamp_ms, *linear_decay_spec)
    if any(field is not None for field in linear_decay_spec):
        if any(field is None for field in linear_fields):
            raise ValueError("incomplete linear decay parameters")
        assert (
            genesis_timestamp_ms is not None
            and decay_start_fee_bps is not None
            and decay_end_fee_bps is not None
            and decay_duration_ms is not None
        )
        _require_int(genesis_timestamp_ms, "genesis_timestamp_ms")
        _require_bps(decay_start_fee_bps, "decay_start_fee_bps")
        _require_bps(decay_end_fee_bps, "decay_end_fee_bps")
        _require_int(decay_duration_ms, "decay_duration_ms")
        if genesis_timestamp_ms < 0 or decay_duration_ms < 0:
            raise ValueError("genesis_timestamp_ms and decay_duration_ms must be non-negative")
        if decay_end_fee_bps > decay_start_fee_bps:
            raise ValueError("decay_end_fee_bps must not exceed decay_start_fee_bps")
        if decay_end_fee_bps > max_buy_tax_bps:
            raise ValueError("decay_end_fee_bps exceeds max_buy_tax_bps")
        if decay_start_fee_bps <= max_buy_tax_bps or decay_duration_ms == 0:
            candidates.append(genesis_timestamp_ms)
        else:
            delta = decay_start_fee_bps - decay_end_fee_bps
            needed = decay_start_fee_bps - max_buy_tax_bps
            elapsed_ms = (needed * decay_duration_ms + delta - 1) // delta
            candidates.append(genesis_timestamp_ms + elapsed_ms)

    auction_fields = (
        genesis_timestamp_ms,
        auction_fee_schedule_bps,
        auction_round_blocks,
        normal_fee_bps,
    )
    if any(field is not None for field in (auction_fee_schedule_bps, auction_round_blocks)):
        if any(field is None for field in auction_fields):
            raise ValueError("incomplete stepped auction parameters")
        assert (
            genesis_timestamp_ms is not None
            and auction_fee_schedule_bps is not None
            and auction_round_blocks is not None
            and normal_fee_bps is not None
        )
        _require_int(genesis_timestamp_ms, "genesis_timestamp_ms")
        _require_int(auction_round_blocks, "auction_round_blocks")
        _require_bps(normal_fee_bps, "normal_fee_bps")
        if auction_round_blocks <= 0:
            raise ValueError("auction_round_blocks must be positive")
        if normal_fee_bps > max_buy_tax_bps:
            raise ValueError("normal_fee_bps exceeds max_buy_tax_bps")
        safe_round: int | None = None
        for idx, fee in enumerate(auction_fee_schedule_bps):
            _require_bps(fee, f"auction_fee_schedule_bps[{idx}]")
            if max(fee, normal_fee_bps) <= max_buy_tax_bps:
                safe_round = idx
                break
        if safe_round is None:
            safe_round = len(auction_fee_schedule_bps)
        candidates.append(genesis_timestamp_ms + safe_round * auction_round_blocks * block_time_ms)

    if not candidates:
        raise ValueError("at least one lock or decay schedule is required")

    raw_ts_ms = max(candidates)
    if slot_step_ms == 1:
        return raw_ts_ms
    return ((raw_ts_ms + slot_step_ms - 1) // slot_step_ms) * slot_step_ms


@dataclass(frozen=True, slots=True)
class LaunchpadSafetyPolicy:
    """Explicit owner-approved thresholds for launchpad entry safety gating."""

    max_buy_tax_bps: int = 200
    max_sell_tax_bps: int = 200
    max_round_trip_loss_bps: int = 600
    min_exit_liquidity_usd_micros: int = 25_000_000_000
    max_creator_snipe_bps: int = 500
    max_cluster_bps: int = 1_500
    max_age_ms: int = 5_000
    require_dual_source: bool = True
    allow_unknown_archetype: bool = False
    slot_step_ms: int = BASE_DENIM_BLOCK_TIME_MS

    def __post_init__(self) -> None:
        _require_bps(self.max_buy_tax_bps, "max_buy_tax_bps")
        _require_bps(self.max_sell_tax_bps, "max_sell_tax_bps")
        _require_bps(self.max_round_trip_loss_bps, "max_round_trip_loss_bps")
        _require_bps(self.max_creator_snipe_bps, "max_creator_snipe_bps")
        _require_bps(self.max_cluster_bps, "max_cluster_bps")
        _require_int(self.min_exit_liquidity_usd_micros, "min_exit_liquidity_usd_micros")
        _require_int(self.max_age_ms, "max_age_ms")
        _require_int(self.slot_step_ms, "slot_step_ms")
        if self.min_exit_liquidity_usd_micros <= 0:
            raise ValueError("min_exit_liquidity_usd_micros must be positive")
        if self.max_age_ms <= 0:
            raise ValueError("max_age_ms must be positive")
        if self.slot_step_ms <= 0:
            raise ValueError("slot_step_ms must be positive")
        if type(self.require_dual_source) is not bool:
            raise TypeError("require_dual_source must be a bool")
        if type(self.allow_unknown_archetype) is not bool:
            raise TypeError("allow_unknown_archetype must be a bool")


@dataclass(frozen=True, slots=True)
class LaunchpadSafetyEvidence:
    """Block-pinned, dual-source evidence snapshot for a launchpad candidate."""

    chain_id: int
    token: str
    pool: str
    archetype: LaunchpadArchetype
    block_number: int
    block_hash: str
    block_timestamp_s: int
    block_timestamp_ms: int | None
    observed_at_ms: int
    source_count: int
    buy_tax_bps: int | None
    sell_tax_bps: int | None
    sell_enabled: bool | None
    unlock_timestamp_ms: int | None
    genesis_timestamp_ms: int | None
    decay_start_fee_bps: int | None
    decay_end_fee_bps: int | None
    decay_duration_ms: int | None
    simulated_buy_in: int | None
    simulated_buy_out: int | None
    simulated_sell_out: int | None
    exit_liquidity_usd_micros: int | None
    creator_same_block_buy_bps: int | None
    bundled_cluster_bps: int | None
    creator_prior_rug_count: int | None
    raw_sha256: str
    auction_fee_schedule_bps: tuple[int, ...] | None = None
    auction_round_blocks: int | None = None
    genesis_block_number: int | None = None
    normal_fee_bps: int | None = None

    def __post_init__(self) -> None:
        _require_int(self.chain_id, "chain_id")
        if self.chain_id <= 0:
            raise ValueError("chain_id must be positive")
        token_norm = normalize_address(self.token)
        pool_norm = normalize_address(self.pool)
        if int(token_norm, 16) == 0 or int(pool_norm, 16) == 0:
            raise ValueError("token and pool addresses must be non-zero")
        object.__setattr__(self, "token", token_norm)
        object.__setattr__(self, "pool", pool_norm)
        if not isinstance(self.archetype, LaunchpadArchetype):
            raise TypeError("archetype must be LaunchpadArchetype")
        _require_int(self.block_number, "block_number")
        if self.block_number < 0:
            raise ValueError("block_number must be non-negative")
        object.__setattr__(self, "block_hash", normalize_hash32(self.block_hash, "block_hash"))
        _require_int(self.block_timestamp_s, "block_timestamp_s")
        if self.block_timestamp_s < 0:
            raise ValueError("block_timestamp_s must be non-negative")
        if self.block_timestamp_ms is not None:
            _require_int(self.block_timestamp_ms, "block_timestamp_ms")
            if self.block_timestamp_ms < 0:
                raise ValueError("block_timestamp_ms must be non-negative")
        _require_int(self.observed_at_ms, "observed_at_ms")
        if self.observed_at_ms <= 0:
            raise ValueError("observed_at_ms must be positive")
        _require_int(self.source_count, "source_count")
        if self.source_count < 0:
            raise ValueError("source_count must be non-negative")
        if self.sell_enabled is not None and type(self.sell_enabled) is not bool:
            raise TypeError("sell_enabled must be bool or None")
        for field_name in (
            "buy_tax_bps",
            "sell_tax_bps",
            "unlock_timestamp_ms",
            "genesis_timestamp_ms",
            "decay_start_fee_bps",
            "decay_end_fee_bps",
            "decay_duration_ms",
            "simulated_buy_in",
            "simulated_buy_out",
            "simulated_sell_out",
            "exit_liquidity_usd_micros",
            "creator_same_block_buy_bps",
            "bundled_cluster_bps",
            "creator_prior_rug_count",
            "auction_round_blocks",
            "genesis_block_number",
            "normal_fee_bps",
        ):
            val = getattr(self, field_name)
            if val is not None:
                _require_int(val, field_name)
        if self.auction_fee_schedule_bps is not None:
            if not isinstance(self.auction_fee_schedule_bps, tuple):
                raise TypeError("auction_fee_schedule_bps must be a tuple or None")
            for idx, item in enumerate(self.auction_fee_schedule_bps):
                _require_int(item, f"auction_fee_schedule_bps[{idx}]")
        _validate_sha256(self.raw_sha256)


@dataclass(frozen=True, slots=True)
class LaunchpadSafetyDecision:
    """Outcome of the fail-closed launchpad safety gate."""

    status: DecisionStatus
    earliest_safe_entry_ms: int | None
    effective_buy_tax_bps: int | None
    schedule_fee_bps: int | None
    round_trip_loss_bps: int | None
    reasons: tuple[str, ...]
    raw_sha256: str


def _validate_denim_timestamps(
    evidence: LaunchpadSafetyEvidence, hard_blockers: list[str]
) -> int | None:
    ms = evidence.block_timestamp_ms
    if evidence.chain_id == BASE_CHAIN_ID:
        header: dict[str, object] = {"timestamp": hex(evidence.block_timestamp_s)}
        if ms is not None:
            header["timestampMs"] = hex(ms)
        try:
            validated = parse_header_timestamp_ms(header, require_denim=True)
        except ValueError as exc:
            hard_blockers.append(str(exc))
            return None
        return validated
    if ms is None:
        hard_blockers.append("block_timestamp_ms_missing")
        return None
    if ms // 1000 != evidence.block_timestamp_s:
        hard_blockers.append("block_timestamp_ms_mismatch")
        return None
    return ms


def _compute_schedule_fees(
    evidence: LaunchpadSafetyEvidence,
    block_ts_ms: int | None,
    hard_blockers: list[str],
) -> tuple[int | None, bool, bool]:
    """Return ``(schedule_fee_bps, has_linear, has_stepped)``; appends blockers on invalid."""
    linear_parts = (
        evidence.genesis_timestamp_ms,
        evidence.decay_start_fee_bps,
        evidence.decay_end_fee_bps,
        evidence.decay_duration_ms,
    )
    has_any_linear = any(part is not None for part in linear_parts)
    has_all_linear = all(part is not None for part in linear_parts)

    auction_parts = (
        evidence.auction_fee_schedule_bps,
        evidence.auction_round_blocks,
        evidence.genesis_block_number,
        evidence.normal_fee_bps,
    )
    has_any_auction = any(part is not None for part in auction_parts)
    has_all_auction = all(part is not None for part in auction_parts)

    if has_any_linear and not has_all_linear:
        hard_blockers.append("incomplete_linear_decay_schedule")
    if has_any_auction and not has_all_auction:
        hard_blockers.append("incomplete_stepped_auction_schedule")

    if (
        evidence.archetype
        in {
            LaunchpadArchetype.DOPPLER_MULTICURVE,
            LaunchpadArchetype.ZORA_CREATOR,
            LaunchpadArchetype.VIRTUALS_CURVE,
        }
        and not has_all_linear
    ):
        hard_blockers.append("missing_decay_schedule")
    if (
        evidence.archetype is LaunchpadArchetype.CLANKER_V4
        and not has_all_linear
        and not has_all_auction
    ):
        hard_blockers.append("missing_clanker_schedule")
    if (
        evidence.archetype is LaunchpadArchetype.FLAUNCH_FAIRLAUNCH
        and evidence.unlock_timestamp_ms is None
    ):
        hard_blockers.append("missing_unlock_timestamp")

    schedule_fees: list[int] = []
    if has_all_linear and block_ts_ms is not None:
        assert (
            evidence.decay_start_fee_bps is not None
            and evidence.decay_end_fee_bps is not None
            and evidence.genesis_timestamp_ms is not None
            and evidence.decay_duration_ms is not None
        )
        try:
            linear_fee = compute_linear_decay_fee_bps(
                evidence.decay_start_fee_bps,
                evidence.decay_end_fee_bps,
                evidence.genesis_timestamp_ms,
                block_ts_ms,
                evidence.decay_duration_ms,
            )
            schedule_fees.append(linear_fee)
        except ValueError:
            hard_blockers.append("invalid_linear_decay_schedule")

    if has_all_auction:
        assert (
            evidence.auction_fee_schedule_bps is not None
            and evidence.auction_round_blocks is not None
            and evidence.genesis_block_number is not None
            and evidence.normal_fee_bps is not None
        )
        try:
            blocks_elapsed = evidence.block_number - evidence.genesis_block_number
            auction_fee = compute_stepped_auction_fee_bps(
                evidence.auction_fee_schedule_bps,
                evidence.auction_round_blocks,
                blocks_elapsed,
                evidence.normal_fee_bps,
            )
            schedule_fees.append(auction_fee)
        except ValueError:
            hard_blockers.append("invalid_stepped_auction_schedule")

    return (
        max(schedule_fees) if schedule_fees else None,
        has_all_linear,
        has_all_auction,
    )


def _compute_safe_entry_for_defer(
    evidence: LaunchpadSafetyEvidence,
    policy: LaunchpadSafetyPolicy,
    *,
    has_linear: bool,
    has_auction: bool,
    block_ts_ms: int,
    hard_blockers: list[str],
) -> int | None:
    try:
        if has_linear or has_auction:
            auction_genesis_ms = evidence.genesis_timestamp_ms
            if (
                not has_linear
                and has_auction
                and auction_genesis_ms is None
                and evidence.genesis_block_number is not None
            ):
                blocks_delta = evidence.genesis_block_number - evidence.block_number
                auction_genesis_ms = max(0, block_ts_ms + blocks_delta * BASE_DENIM_BLOCK_TIME_MS)
            return earliest_safe_entry_ms(
                max_buy_tax_bps=policy.max_buy_tax_bps,
                unlock_timestamp_ms=evidence.unlock_timestamp_ms,
                genesis_timestamp_ms=auction_genesis_ms,
                decay_start_fee_bps=evidence.decay_start_fee_bps if has_linear else None,
                decay_end_fee_bps=evidence.decay_end_fee_bps if has_linear else None,
                decay_duration_ms=evidence.decay_duration_ms if has_linear else None,
                auction_fee_schedule_bps=(
                    evidence.auction_fee_schedule_bps if has_auction else None
                ),
                auction_round_blocks=evidence.auction_round_blocks if has_auction else None,
                normal_fee_bps=evidence.normal_fee_bps if has_auction else None,
                slot_step_ms=policy.slot_step_ms,
            )
        if evidence.unlock_timestamp_ms is not None:
            return earliest_safe_entry_ms(
                max_buy_tax_bps=policy.max_buy_tax_bps,
                unlock_timestamp_ms=evidence.unlock_timestamp_ms,
                slot_step_ms=policy.slot_step_ms,
            )
    except ValueError:
        hard_blockers.append("decay_schedule_never_safe")
        return None
    hard_blockers.append("missing_defer_schedule")
    return None


def evaluate_launchpad_safety(
    evidence: LaunchpadSafetyEvidence,
    policy: LaunchpadSafetyPolicy | None = None,
) -> LaunchpadSafetyDecision:
    """Evaluate launchpad safety strictly and fail closed on any missing/violated invariant."""
    if not isinstance(evidence, LaunchpadSafetyEvidence):
        raise TypeError("evidence must be LaunchpadSafetyEvidence")
    active_policy = policy if policy is not None else LaunchpadSafetyPolicy()
    if not isinstance(active_policy, LaunchpadSafetyPolicy):
        raise TypeError("policy must be LaunchpadSafetyPolicy")

    hard_blockers: list[str] = []
    defer_blockers: list[str] = []

    if (
        evidence.archetype is LaunchpadArchetype.UNKNOWN_CUSTOM
        and not active_policy.allow_unknown_archetype
    ):
        hard_blockers.append("unknown_launchpad_archetype")

    block_ts_ms = _validate_denim_timestamps(evidence, hard_blockers)

    if active_policy.require_dual_source and evidence.source_count < 2:
        hard_blockers.append("insufficient_sources")
    elif evidence.source_count < 1:
        hard_blockers.append("missing_source")

    if block_ts_ms is not None:
        age_ms = evidence.observed_at_ms - block_ts_ms
        if age_ms < 0:
            hard_blockers.append("future_block_timestamp")
        elif age_ms > active_policy.max_age_ms:
            hard_blockers.append("stale_evidence")

    mandatory_fields: tuple[tuple[str, object], ...] = (
        ("buy_tax_bps", evidence.buy_tax_bps),
        ("sell_tax_bps", evidence.sell_tax_bps),
        ("sell_enabled", evidence.sell_enabled),
        ("simulated_buy_in", evidence.simulated_buy_in),
        ("simulated_buy_out", evidence.simulated_buy_out),
        ("simulated_sell_out", evidence.simulated_sell_out),
        ("exit_liquidity_usd_micros", evidence.exit_liquidity_usd_micros),
        ("creator_same_block_buy_bps", evidence.creator_same_block_buy_bps),
        ("bundled_cluster_bps", evidence.bundled_cluster_bps),
        ("creator_prior_rug_count", evidence.creator_prior_rug_count),
    )
    for field_name, field_value in mandatory_fields:
        if field_value is None:
            hard_blockers.append(f"missing_{field_name}")

    schedule_fee_bps, has_linear, has_auction = _compute_schedule_fees(
        evidence, block_ts_ms, hard_blockers
    )

    in_lock_window = False
    if evidence.unlock_timestamp_ms is not None and block_ts_ms is not None:
        if evidence.unlock_timestamp_ms < 0:
            hard_blockers.append("invalid_unlock_timestamp")
        elif block_ts_ms < evidence.unlock_timestamp_ms:
            in_lock_window = True
            defer_blockers.append("lock_window_active")

    effective_buy_tax_bps: int | None = None
    if evidence.buy_tax_bps is not None:
        if not 0 <= evidence.buy_tax_bps <= BPS_DENOMINATOR:
            hard_blockers.append("invalid_buy_tax_bps")
        else:
            effective_buy_tax_bps = (
                max(evidence.buy_tax_bps, schedule_fee_bps)
                if schedule_fee_bps is not None
                else evidence.buy_tax_bps
            )
            if effective_buy_tax_bps > active_policy.max_buy_tax_bps:
                if (
                    schedule_fee_bps is not None
                    and schedule_fee_bps > active_policy.max_buy_tax_bps
                    and evidence.buy_tax_bps <= schedule_fee_bps
                ):
                    defer_blockers.append("active_decay_fee")
                else:
                    hard_blockers.append("excessive_buy_tax")

    if evidence.sell_tax_bps is not None:
        if not 0 <= evidence.sell_tax_bps <= BPS_DENOMINATOR:
            hard_blockers.append("invalid_sell_tax_bps")
        elif evidence.sell_tax_bps > active_policy.max_sell_tax_bps:
            hard_blockers.append("excessive_sell_tax")

    if evidence.sell_enabled is False and not in_lock_window:
        hard_blockers.append("sell_disabled")

    round_trip_loss_bps: int | None = None
    if (
        evidence.simulated_buy_in is not None
        and evidence.simulated_buy_out is not None
        and evidence.simulated_sell_out is not None
    ):
        if (
            evidence.simulated_buy_in <= 0
            or evidence.simulated_buy_out <= 0
            or evidence.simulated_sell_out < 0
        ):
            hard_blockers.append("invalid_round_trip_simulation")
        elif evidence.simulated_sell_out == 0:
            if not (in_lock_window and evidence.sell_enabled is False):
                hard_blockers.append("zero_simulated_sell_out")
        else:
            loss = evidence.simulated_buy_in - evidence.simulated_sell_out
            round_trip_loss_bps = (
                0 if loss <= 0 else -(-loss * BPS_DENOMINATOR // evidence.simulated_buy_in)
            )
            if round_trip_loss_bps > active_policy.max_round_trip_loss_bps:
                # Active buy-tax decay can inflate the immediate simulation loss; strip only
                # the scheduled buy-tax decay delta down to end fee and verify sellability.
                decay_explained = False
                if (
                    schedule_fee_bps is not None
                    and schedule_fee_bps > active_policy.max_buy_tax_bps
                    and effective_buy_tax_bps is not None
                    and effective_buy_tax_bps < BPS_DENOMINATOR
                    and evidence.buy_tax_bps is not None
                    and evidence.buy_tax_bps <= schedule_fee_bps
                ):
                    post_decay_buy_fee = (
                        evidence.decay_end_fee_bps
                        if has_linear and evidence.decay_end_fee_bps is not None
                        else evidence.normal_fee_bps
                        if has_auction and evidence.normal_fee_bps is not None
                        else active_policy.max_buy_tax_bps
                    )
                    if post_decay_buy_fee <= active_policy.max_buy_tax_bps:
                        projected_sell_out = (
                            evidence.simulated_sell_out * (BPS_DENOMINATOR - post_decay_buy_fee)
                        ) // (BPS_DENOMINATOR - effective_buy_tax_bps)
                        proj_loss = evidence.simulated_buy_in - projected_sell_out
                        proj_loss_bps = (
                            0
                            if proj_loss <= 0
                            else -(-proj_loss * BPS_DENOMINATOR // evidence.simulated_buy_in)
                        )
                        if proj_loss_bps <= active_policy.max_round_trip_loss_bps:
                            decay_explained = True
                if not decay_explained:
                    hard_blockers.append("excessive_round_trip_loss")

    if evidence.exit_liquidity_usd_micros is not None:
        if evidence.exit_liquidity_usd_micros < 0:
            hard_blockers.append("invalid_exit_liquidity")
        elif evidence.exit_liquidity_usd_micros < active_policy.min_exit_liquidity_usd_micros:
            hard_blockers.append("insufficient_exit_liquidity")

    if evidence.creator_same_block_buy_bps is not None:
        if not 0 <= evidence.creator_same_block_buy_bps <= BPS_DENOMINATOR:
            hard_blockers.append("invalid_creator_snipe")
        elif evidence.creator_same_block_buy_bps > active_policy.max_creator_snipe_bps:
            hard_blockers.append("excessive_creator_snipe")

    if evidence.bundled_cluster_bps is not None:
        if not 0 <= evidence.bundled_cluster_bps <= BPS_DENOMINATOR:
            hard_blockers.append("invalid_bundled_cluster")
        elif evidence.bundled_cluster_bps > active_policy.max_cluster_bps:
            hard_blockers.append("excessive_bundled_cluster")

    if evidence.creator_prior_rug_count is not None and evidence.creator_prior_rug_count != 0:
        hard_blockers.append("creator_prior_rug")

    if not hard_blockers and defer_blockers and block_ts_ms is not None:
        safe_ts_ms = _compute_safe_entry_for_defer(
            evidence,
            active_policy,
            has_linear=has_linear,
            has_auction=has_auction,
            block_ts_ms=block_ts_ms,
            hard_blockers=hard_blockers,
        )
        if not hard_blockers and safe_ts_ms is not None:
            return LaunchpadSafetyDecision(
                status=DecisionStatus.DEFER_UNTIL_DECAY,
                earliest_safe_entry_ms=safe_ts_ms,
                effective_buy_tax_bps=effective_buy_tax_bps,
                schedule_fee_bps=schedule_fee_bps,
                round_trip_loss_bps=round_trip_loss_bps,
                reasons=tuple(defer_blockers),
                raw_sha256=evidence.raw_sha256,
            )

    if hard_blockers:
        return LaunchpadSafetyDecision(
            status=DecisionStatus.REJECTED,
            earliest_safe_entry_ms=None,
            effective_buy_tax_bps=effective_buy_tax_bps,
            schedule_fee_bps=schedule_fee_bps,
            round_trip_loss_bps=round_trip_loss_bps,
            reasons=(*hard_blockers, *defer_blockers),
            raw_sha256=evidence.raw_sha256,
        )

    return LaunchpadSafetyDecision(
        status=DecisionStatus.APPROVED,
        earliest_safe_entry_ms=block_ts_ms,
        effective_buy_tax_bps=effective_buy_tax_bps,
        schedule_fee_bps=schedule_fee_bps,
        round_trip_loss_bps=round_trip_loss_bps,
        reasons=(),
        raw_sha256=evidence.raw_sha256,
    )
