"""Offline gas-oracle discovery from two exact raw RPC captures.

Discovery proposes fingerprints, never approves them. This module has no
transport, key, budget, journal, service-control or execution capability.
"""

import re
from collections.abc import Sequence
from dataclasses import asdict, replace
from typing import Any

from ..evidence.exact_capture import decode_header, successful_results, wire_envelope
from ..evidence.rpc_validation import restore_exchanges, rpc_quantity
from ..evidence.store import EvidenceStore
from ..models.chain import BlockSnapshot
from ..models.common import BlockRef, EvidenceRef, canonical_json, sha256_hex
from ..rpc.transport import RpcExchange
from .config import LiveConfig
from .gas_oracle import (
    IMPLEMENTATION_SLOT,
    LEGACY_PIN_NAMES,
    ORACLE,
    implementation_address,
    observed_gas_oracle_pins,
    require_gas_oracle_pins,
)

KIND = "live_gas_oracle_discovery"
INTERPRETATION = {"execution_eligible": False, "runtime_approved": False}


def _sources(sources: tuple[str, str]) -> None:
    if (
        len(sources) != 2
        or len(set(sources)) != 2
        or any(re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", source) is None for source in sources)
    ):
        raise ValueError("two declared distinct provider aliases required")


def _prefix_requests(block: BlockRef) -> list[tuple[str, list[object]]]:
    if block.chain_id != 8453 or block.timestamp is None:
        raise ValueError("gas-oracle discovery requires a timestamped Base block")
    pin = {"blockHash": block.block_hash, "requireCanonical": True}
    return [
        ("eth_chainId", []),
        ("eth_getBlockByNumber", [hex(block.number), False]),
        ("eth_getCode", [ORACLE, pin]),
        ("eth_getStorageAt", [ORACLE, IMPLEMENTATION_SLOT, pin]),
    ]


def discovery_requests(block: BlockRef, implementation: str) -> list[tuple[str, list[object]]]:
    """Six reads per source; derive the fifth read's address from the fourth reply.

    These templates do not resolve a head or make a request. The caller must
    independently authorize and budget acquisition, then retain its raw bytes.
    """
    calls = _prefix_requests(block)
    if (
        implementation_address("0x" + "00" * 12 + implementation.removeprefix("0x"))
        != implementation
    ):
        raise ValueError("canonical implementation address required")
    return [*calls, ("eth_getCode", [implementation, calls[2][1][1]]), calls[1]]


def _validate(
    block: BlockRef, exchanges: Sequence[RpcExchange]
) -> tuple[BlockSnapshot, dict[str, str]]:
    if len(exchanges) != 6 or any(type(e.attempt) is not int or e.attempt != 1 for e in exchanges):
        raise ValueError("exact six-read capture without retries required")
    # The prefix does not yet know the implementation. It is taken only from
    # the pinned storage response, never from a supplied summary or address.
    values = successful_results(exchanges[:4], _prefix_requests(block))
    implementation = implementation_address(values[3])
    values = successful_results(exchanges, discovery_requests(block, implementation))
    opening = decode_header(values[1], chain_id=block.chain_id)
    closing = decode_header(values[5], chain_id=block.chain_id)
    if rpc_quantity(values[0]) != block.chain_id or opening.ref != block or closing != opening:
        raise ValueError("gas-oracle discovery chain or header changed")
    return opening, observed_gas_oracle_pins(values[2], values[3], values[4])


def _payload(block: BlockRef, pins: dict[str, str]) -> bytes:
    return canonical_json(
        {
            "schema": "oasis.live-gas-oracle-discovery.v1",
            "block": asdict(block),
            "oracle": ORACLE,
            "implementation_slot": IMPLEMENTATION_SLOT,
            "observed_pins": pins,
            **INTERPRETATION,
        }
    )


def retain_discovery_quorum(
    store: EvidenceStore,
    block: BlockRef,
    captures: tuple[Sequence[RpcExchange], Sequence[RpcExchange]],
    *,
    sources: tuple[str, str],
) -> tuple[EvidenceRef, EvidenceRef]:
    """Validate both complete wire captures before the first evidence write."""
    _sources(sources)
    if len(captures) != 2:
        raise ValueError("two exact provider captures required")
    observations = [_validate(block, exchanges) for exchanges in captures]
    # Include the full agreed header, not just its declared hash. Parent hashes
    # must agree too, even if a hostile response reuses the requested hash.
    if observations[0] != observations[1]:
        raise ValueError("independent gas-oracle captures disagree")
    refs = [
        store.put(
            KIND,
            _payload(block, observed),
            block,
            source,
            request=wire_envelope(exchanges, response=False),
            raw_response=wire_envelope(exchanges, response=True),
            interpretation=INTERPRETATION,
        )
        for source, exchanges, (_, observed) in zip(sources, captures, observations, strict=True)
    ]
    return refs[0], refs[1]


def replay_discovery_quorum(
    store: EvidenceStore,
    refs: tuple[EvidenceRef, EvidenceRef],
    block: BlockRef,
    *,
    sources: tuple[str, str],
) -> dict[str, str]:
    """Reconstruct observed fingerprints from wire bytes, without acquisition."""
    _sources(sources)
    if len(refs) != 2 or tuple(ref.source for ref in refs) != sources:
        raise ValueError("gas-oracle discovery provider scope differs")
    observations = []
    for ref in refs:
        if ref.kind != KIND or ref.block != block:
            raise ValueError("gas-oracle discovery kind or block differs")
        record = store.get_record(ref)
        if record.request_payload is None:
            raise ValueError("gas-oracle discovery raw requests missing")
        exchanges = restore_exchanges(record.request_payload, record.raw_response_payload)
        observation = _validate(block, exchanges)
        _, observed = observation
        if record.payload != _payload(
            block, observed
        ) or record.interpretation_payload != canonical_json(INTERPRETATION):
            raise ValueError("gas-oracle discovery summary contradicts wire proof")
        observations.append(observation)
    if observations[0] != observations[1]:
        raise ValueError("independent gas-oracle captures disagree")
    return observations[0][1]


def candidate_configuration(
    store: EvidenceStore,
    refs: tuple[EvidenceRef, EvidenceRef],
    block: BlockRef,
    config: LiveConfig,
    *,
    sources: tuple[str, str],
    now: int,
) -> dict[str, Any]:
    """A fresh, strict-policy proposal, never a configuration write or release."""
    config.validate()
    if (
        config.chain_id != 8453
        or set(config.expected_pins) != LEGACY_PIN_NAMES
        or config.oracle_max_age_seconds != 1800
        or config.usdc_oracle_max_age_seconds != 1800
    ):
        raise ValueError("unchanged strict-feed legacy configuration required")
    if (
        type(now) is not int
        or block.timestamp is None
        or not block.timestamp <= now <= block.timestamp + 1800
    ):
        raise ValueError("current gas-oracle discovery block required")
    pins = replay_discovery_quorum(store, refs, block, sources=sources)
    candidate = replace(config, expected_pins=config.expected_pins | pins)
    candidate.validate()
    require_gas_oracle_pins(candidate.expected_pins)
    return {
        "schema": "oasis.live-gas-oracle-candidate.v1",
        **INTERPRETATION,
        "configuration_applied": False,
        "state_write_supported": False,
        "source_config_hash": sha256_hex(canonical_json(asdict(config))),
        "candidate_config_hash": sha256_hex(canonical_json(asdict(candidate))),
        "candidate_config": asdict(candidate),
        "block": asdict(block),
        "evidence": [asdict(ref) for ref in refs],
        "reviewed_at": now,
        "valid_until": block.timestamp + 1800,
        "limitations": [
            "provider_independence_is_declared_not_proven_by_aliases",
            "observed_runtime_is_not_source_verification_or_owner_approval",
            "historical_discovery_does_not_approve_future_execution",
            "fresh_complete_nav_and_simulation_still_required",
            "history_preserving_configuration_transition_still_required",
        ],
    }


__all__ = [
    "candidate_configuration",
    "discovery_requests",
    "replay_discovery_quorum",
    "retain_discovery_quorum",
]
