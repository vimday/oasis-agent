"""Read-only attribution of Base discovery, whole simulation, and child coverage."""

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import pairwise

from ..evidence.rpc_validation import rpc_quantity, strict_integer, strict_list, strict_object
from ..models.common import normalize_hash32
from .cycle_plan import address
from .cycle_protocols import data_bytes, topic

BASE_CHAIN_ID = 8453
WETH = "0x4200000000000000000000000000000000000006"
TRANSFER = topic("Transfer(address,address,uint256)")
TOP_CAP = 100_000
COHORTS = {"four_pool": 4, "address_58": 58, "mixed_66": 66}


def _hash(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("expected a block hash")
    return normalize_hash32(value)


def _integer(value: object, label: str) -> int:
    out = strict_integer(value, label)
    if not 0 <= out < 1 << 256:
        raise ValueError(f"invalid {label}")
    return out


@dataclass(frozen=True, slots=True)
class SimulatedCall:
    gas_used: int
    base_fee_wei_per_gas: int
    success: bool
    return_data: bytes
    weth_delta: int


@dataclass(frozen=True, slots=True)
class _HeadTiming:
    block: int
    block_hash: str
    relation: str
    received_ns: int
    state_ready_ns: int
    discovery_finished_ns: int


def _arrival_deadlines(timings: Sequence[_HeadTiming]) -> dict[str, object]:
    pairs: list[dict[str, object]] = []
    missing_child: list[int] = []
    for current, successor in pairwise(timings):
        if successor.relation != "direct_child":
            missing_child.append(current.block)
            continue
        pairs.append(
            {
                "block": current.block,
                "block_hash": current.block_hash,
                "child_block": successor.block,
                "child_hash": successor.block_hash,
                "head_to_child_receive_ns": successor.received_ns - current.received_ns,
                "child_receive_minus_state_ready_ns": (
                    successor.received_ns - current.state_ready_ns
                ),
                "child_receive_minus_discovery_finished_ns": (
                    successor.received_ns - current.discovery_finished_ns
                ),
            }
        )
    missing_child.append(timings[-1].block)
    compared = tuple(
        (current, successor)
        for current, successor in pairwise(timings)
        if successor.relation == "direct_child"
    )
    return {
        "clock_scope": "local_public_websocket_receive_monotonic",
        "witnessed_direct_child_pairs": len(pairs),
        "state_ready_before_child_observed": sum(
            current.state_ready_ns < successor.received_ns for current, successor in compared
        ),
        "discovery_finished_before_child_observed": sum(
            current.discovery_finished_ns < successor.received_ns for current, successor in compared
        ),
        "blocks_without_observed_direct_child": missing_child,
        "pairs": pairs,
    }


def _transfer_address(value: object) -> str:
    word = data_bytes(value)
    if len(word) != 32 or word[:12] != bytes(12):
        raise ValueError("Transfer topic is not a canonical address")
    return "0x" + word[12:].hex()


def decode_base_simulation(
    result: object, *, parent_number: int, parent_hash: str, recipient: str, gas_limit: int
) -> SimulatedCall:
    """Decode one eth_simulateV1 call built on a verified parent block.

    Only canonical WETH Transfer movements for the fixed recipient are observed.
    The caller must independently bind the frozen calldata and know the envelope's
    native payment and supported asset semantics before constructing a net value.
    """
    recipient = address(recipient)
    (block_value,) = strict_list(result, "simulated blocks")
    block = strict_object(block_value, "simulated block")
    if (
        _hash(block.get("parentHash")) != _hash(parent_hash)
        or rpc_quantity(block.get("number")) != parent_number + 1
    ):
        raise ValueError("simulated block is not built on the requested exact state")
    (call_value,) = strict_list(block.get("calls"), "simulated calls")
    call = strict_object(call_value, "simulated call")
    status = rpc_quantity(call.get("status"))
    gas = rpc_quantity(call.get("gasUsed"))
    if (
        status not in (0, 1)
        or not 0 < gas <= gas_limit
        or gas != rpc_quantity(block.get("gasUsed"))
    ):
        raise ValueError("invalid simulated status or gas usage")
    if status == 1 and call.get("error") is not None:
        raise ValueError("successful simulated call has an execution error")
    logs = strict_list(call.get("logs"), "simulated logs")
    if len(logs) > 4096 or (status == 0 and logs):
        raise ValueError("invalid committed simulation logs")
    delta = 0
    for item in logs:
        log = strict_object(item, "simulation log")
        if address(log.get("address")) != WETH:
            continue
        topics = strict_list(log.get("topics"), "simulation topics")
        if topics and topics[0] == TRANSFER:
            if len(topics) != 3 or len(raw := data_bytes(log.get("data"))) != 32:
                raise ValueError("malformed WETH Transfer")
            source, target = map(_transfer_address, topics[1:])
            delta += ((target == recipient) - (source == recipient)) * int.from_bytes(raw, "big")
    return SimulatedCall(
        gas,
        rpc_quantity(block.get("baseFeePerGas")),
        bool(status),
        data_bytes(call.get("returnData")),
        delta,
    )


def _cohort_counts(mixed: dict[str, object]) -> dict[str, tuple[int, int | None]]:
    cohorts = strict_object(mixed.get("cohorts"), "shadow cohorts")
    if set(cohorts) != set(COHORTS):
        raise ValueError("incomplete Base cohort coverage")
    out = {}
    for label, count in COHORTS.items():
        cohort = strict_object(cohorts[label], "shadow cohort")
        if _integer(cohort.get("pools"), "cohort pool count") != count:
            raise ValueError("Base shadow pool roster disagrees")
        anchors = [
            strict_object(raw, "anchor") for raw in strict_list(cohort.get("anchors"), "anchors")
        ]
        if len(anchors) != 2 or len({address(anchor.get("base_token")) for anchor in anchors}) != 2:
            raise ValueError("Base shadow anchors are not distinct")
        candidates = sum(
            _integer(anchor.get("model_candidates"), "candidates") for anchor in anchors
        )
        residuals = [anchor.get("sizing_residuals") for anchor in anchors]
        unresolved = (
            sum(_integer(raw, "sizing residuals") for raw in residuals)
            if None not in residuals
            else None
        )
        out[label] = candidates, unresolved
    return out


def summarize_base_shadow(
    records: Sequence[object], *, arrival_deadlines: bool = False
) -> dict[str, object]:
    """Preserve each sampled head, candidate model amounts, and explicit missing stages."""
    if not 1 <= len(records) <= 64:
        raise ValueError("Base survival requires 1 to 64 captured heads")
    frames = []
    timings = []
    counts: Counter[str] = Counter()
    previous_hash: str | None = None
    previous_number = 0
    previous_receive = 0
    candidates = 0
    for index, raw in enumerate(records, 1):
        row = strict_object(raw, "shadow row")
        if (
            row.get("chain_id") != BASE_CHAIN_ID
            or row.get("execution_eligible") is not False
            or _integer(row.get("seq"), "record sequence") != index
        ):
            raise ValueError("invalid campaign scope or sequence")
        number = _integer(row.get("block_number"), "block number")
        block_hash = _hash(row.get("block_hash"))
        lifecycle = strict_object(row.get("shadow_cycle_lifecycle"), "campaign lifecycle")
        mixed = strict_object(lifecycle.get("mixed_base_state"), "mixed Base state")
        stream = strict_object(mixed.get("newheads_observation"), "stream observation")
        head = strict_object(stream.get("head"), "stream head")
        parent = _hash(head.get("parent_hash"))
        if (
            _hash(head.get("block_hash")) != block_hash
            or _integer(head.get("height"), "head number") != number
        ):
            raise ValueError("campaign row disagrees with its stream header")
        clock = _integer(stream.get("received_monotonic_ns"), "head receive")
        state = _integer(mixed.get("state_ready_monotonic_ns"), "mixed state ready")
        ready = _integer(lifecycle.get("state_ready_monotonic_ns"), "state ready")
        discovered = _integer(
            lifecycle.get("discovery_finished_monotonic_ns"), "discovery finished"
        )
        if not 0 < clock <= state <= ready <= discovered or clock < previous_receive:
            raise ValueError("invalid live discovery chronology")
        previous_receive = clock
        queue = None
        if (start := mixed.get("state_capture_started_monotonic_ns")) is not None:
            dispatch = _integer(start, "state capture started")
            if not clock <= dispatch <= state:
                raise ValueError("invalid state dispatch chronology")
            queue = dispatch - clock
        relation = "first"
        if previous_hash is not None:
            if number <= previous_number:
                raise ValueError("campaign heights must advance")
            relation = "gap" if number > previous_number + 1 else "direct_child"
            if relation == "direct_child" and parent != previous_hash:
                raise ValueError("campaign parent hash conflict")
        counts[relation] += 1
        if arrival_deadlines:
            timings.append(_HeadTiming(number, block_hash, relation, clock, state, discovered))
        previous_number, previous_hash = number, block_hash
        cohort_counts = _cohort_counts(mixed)
        dispositions = [
            strict_object(item, "candidate disposition")
            for item in strict_list(
                row.get("atomic_candidate_dispositions"), "normal candidate dispositions"
            )
        ]
        candidates += len(dispositions)
        if candidates > TOP_CAP or len(dispositions) != _integer(
            row.get("rust_raw_candidates_count"), "candidate count"
        ):
            raise ValueError("normal ranking coverage disagrees")
        if any(item.get("whole_transaction_status") != "not_simulated" for item in dispositions):
            raise ValueError("unknown whole-transaction evidence schema")
        frames.append(
            {
                "block": number,
                "block_hash": block_hash,
                "parent_hash": parent,
                "relation": relation,
                "head_to_state_ns": state - clock,
                "head_to_discovery_ns": discovered - clock,
                "queue_before_state_ns": queue,
                "cohorts": {
                    label: {"model_candidates": c, "unresolved_sizing_intervals": n}
                    for label, (c, n) in cohort_counts.items()
                },
                "normal_ranked_candidates": dispositions,
                "whole_transaction_simulations": 0,
                "prior_fixed_route_model_reprice": lifecycle.get("previous_block_reprice"),
                "same_envelope_child_simulations": 0,
            }
        )
    return {
        "schema": "oasis.base-shadow-survival.v1",
        "heads": frames,
        "head_relations": dict(counts),
        "normal_discovery_candidates": candidates,
        "whole_transaction_simulations": 0,
        "same_envelope_child_simulations": 0,
        "execution_eligible": False,
        "positive_survival_rate": None,
        **({"arrival_deadlines": _arrival_deadlines(timings)} if arrival_deadlines else {}),
    }
