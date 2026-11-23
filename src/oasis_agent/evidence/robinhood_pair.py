"""Arrival-order replay of already decoded Robinhood Nitro capture frames."""

from collections import defaultdict
from dataclasses import dataclass, replace
from typing import cast

from ..arbitrage.cycle_protocols import abi, data_bytes, topic
from .public_receipts import ObservedReceipt
from .robinhood_nitro import FeedReplay, NitroHintJournal, RpcAnchor

MAX_ARRIVAL_MESSAGES = 4096
POOL_MANAGER = "0x8366a39cc670b4001a1121b8f6a443a643e40951"
SWAP_TOPIC = topic("Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)")
SWAP_TYPES = ("int128", "int128", "uint160", "uint128", "int24", "uint24")


@dataclass(frozen=True, slots=True)
class FeedArrival:
    source: str
    frame: int
    received_ns: int
    replay: FeedReplay


def replay_arrivals(
    journal: NitroHintJournal,
    arrivals: tuple[FeedArrival, ...],
    anchors: tuple[RpcAnchor, ...] = (),
    *,
    omit_primary: tuple[int, int] | None = None,
) -> dict[str, object]:
    """Release only the contiguous prefix, replaying each received frame atomically."""
    primary = [
        message.sequence
        for row in arrivals
        if row.source == "primary"
        for message in row.replay.messages
    ]
    if not primary or not any(row.source == "delayed" for row in arrivals):
        raise ValueError("Robinhood replay requires both retained feed arms")
    if sum(len(row.replay.messages) for row in arrivals) > MAX_ARRIVAL_MESSAGES:
        raise ValueError("too many Robinhood messages in paired replay")
    if any(row.source not in {"primary", "delayed"} for row in arrivals):
        raise ValueError("unknown Robinhood replay source")
    omitted = set()
    if omit_primary is not None:
        first, last = omit_primary
        omitted = {seq for seq in primary if first <= seq <= last}
        if not primary[0] < first <= last < max(primary) or len(omitted) != last - first + 1:
            raise ValueError("omission must be a fully retained interior primary range")

    ordered = sorted(
        arrivals, key=lambda row: (row.received_ns, row.source != "primary", row.frame)
    )
    start = primary[0]
    previous = journal.ingest((), start=start, limit=1)
    already_released_before_replay = cast(int, previous["next_sequence"])
    anchor_sequences = {anchor.sequence for anchor in anchors}
    anchors_released_at: dict[int, int] = {}
    gap_started = None
    gaps = []
    counts = {"added": 0, "duplicates": 0, "before_start": 0, "conflicts": 0}
    for arrival in ordered:
        messages = tuple(
            msg
            for msg in arrival.replay.messages
            if arrival.source != "primary" or msg.sequence not in omitted
        )
        if not messages:
            continue
        replay = replace(arrival.replay, messages=messages)
        current = journal.ingest((replay,), limit=1)
        for key, count in cast(dict[str, int], current["ingest_counts"]).items():
            counts[key] += count
        if current["status"] == "invalidated":
            break
        prior_cursor = cast(int, previous["next_sequence"])
        next_cursor = cast(int, current["next_sequence"])
        for seq in anchor_sequences:
            if prior_cursor <= seq < next_cursor:
                anchors_released_at[seq] = arrival.received_ns
        if current["status"] == "waiting_for_gap" and gap_started is None:
            gap_started = {
                "first_missing_range": current["first_missing_range"],
                "started_ns": arrival.received_ns,
                "source_at_detection": arrival.source,
            }
        elif current["status"] == "contiguous" and gap_started is not None:
            started = cast(int, gap_started["started_ns"])
            gaps.append(
                {
                    **gap_started,
                    "recovered_by": arrival.source,
                    "recovered_ns": arrival.received_ns,
                    "observed_recovery_ns": arrival.received_ns - started,
                    "released_through": current["contiguous_through"],
                }
            )
            gap_started = None
        previous = current

    final = journal.ingest((), anchors)
    if final["status"] == "invalidated":
        anchors_released_at.clear()
    return {
        "omitted_primary_sequences": sorted(omitted),
        "recovered_gaps": gaps,
        "pending_gap": gap_started,
        "anchor_prefix_release_ns": anchors_released_at,
        "capture_absolute_clock": "capture_monotonic_ns",
        "journal_next_sequence_before_replay": already_released_before_replay,
        "ingest_counts": counts,
        "continuity": final,
    }


def receipt_swap_hints(receipt: ObservedReceipt) -> dict[str, object] | None:
    """Read exact event amounts; currencies and funding remain unresolved without PoolKeys."""
    swaps = []
    producers: dict[tuple[str, int], list[tuple[int, str]]] = defaultdict(list)
    consumers: dict[tuple[str, int], list[tuple[int, str]]] = defaultdict(list)
    for log in receipt.logs:
        if log.address != POOL_MANAGER or not log.topics or log.topics[0] != SWAP_TOPIC:
            continue
        if len(log.topics) != 3 or log.topics[2][2:26] != "0" * 24:
            raise ValueError("malformed Robinhood PoolManager Swap topics")
        caller = "0x" + log.topics[2][26:]
        pool_id = log.topics[1]
        d0, d1, sqrt_price_x96, liquidity, tick, fee = abi(SWAP_TYPES, data_bytes(log.data))
        swaps.append(
            {
                "log_index": log.log_index,
                "pool_id": pool_id,
                "caller": caller,
                "signed_amounts_currency_0_1": [d0, d1],
                "sqrt_price_x96": sqrt_price_x96,
                "liquidity": liquidity,
                "tick": tick,
                "event_fee_pips": fee,
            }
        )
        if d0 * d1 < 0:
            producers[caller, max(d0, d1)].append((log.log_index, pool_id))
            consumers[caller, -min(d0, d1)].append((log.log_index, pool_id))
    if not swaps:
        return None
    exact_amount_hints = []
    for (caller, amount), outgoing in producers.items():
        incoming = consumers.get((caller, amount), [])
        if len(incoming) == len(outgoing) == 1 and incoming[0][1] != outgoing[0][1]:
            exact_amount_hints.append(
                {
                    "caller": caller,
                    "amount_raw": amount,
                    "source_log": outgoing[0][0],
                    "target_log": incoming[0][0],
                    "asset": None,
                }
            )
    return {
        "tx_hash": receipt.transaction_hash,
        "tx_index": receipt.transaction_index,
        "swaps": swaps,
        "exact_amount_hints": exact_amount_hints,
        "funding_verified": False,
        "quote_state_verified": False,
        "gas_used": receipt.gas_used,
        "effective_gas_price": receipt.effective_gas_price,
        "receipt_gas_cost_wei": receipt.gas_used * receipt.effective_gas_price,
    }
