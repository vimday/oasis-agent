"""Retained independent full-envelope simulation and nonce admission proofs."""

from collections.abc import Sequence
from dataclasses import asdict
from typing import Any

from eth_abi.abi import decode

from ..evidence.exact_capture import (
    decode_header,
    successful_results,
    validate_session_pair,
    wire_envelope,
)
from ..evidence.rpc_validation import restore_exchanges, rpc_quantity
from ..evidence.store import EvidenceStore
from ..models.chain import BlockSnapshot
from ..models.common import (
    BlockRef,
    EvidenceRef,
    canonical_json,
    normalize_address,
    normalize_hex,
    sha256_hex,
)
from ..rpc.transport import RpcExchange
from .acquisition import acquire_pair
from .chain import calldata
from .config import LiveConfig
from .gas_oracle import (
    IMPLEMENTATION_SLOT,
    ORACLE,
    require_gas_oracle_pins,
    validate_gas_oracle_proof,
)
from .receipts import RetainedReceiptCostReader


def _requests(
    c: LiveConfig,
    block: BlockRef,
    tx: dict[str, Any],
    *,
    nonce: int,
    gas: int,
    maximum: int,
) -> list[tuple[str, list[object]]]:
    c.validate()
    implementation = require_gas_oracle_pins(c.expected_pins)
    if block.chain_id != c.chain_id or block.timestamp is None:
        raise ValueError("simulation requires the configured chain and a timestamped block")
    if any(type(v) is not int or not 0 <= v < 2**256 for v in (nonce, gas, maximum)):
        raise ValueError("simulation bounds must be canonical integers")
    if not 0 < gas <= 1_500_000 or not 0 < maximum <= c.max_fee_per_gas_wei:
        raise ValueError("simulation bounds exceed policy")
    if set(tx) != {
        "from",
        "to",
        "data",
        "value",
        "gas",
        "maxFeePerGas",
        "maxPriorityFeePerGas",
        "nonce",
    }:
        raise ValueError("exact simulation transaction fields required")
    if (
        tx["from"] != c.account
        or normalize_address(tx["to"]) != tx["to"]
        or tx["to"] not in (c.pool.currency1, c.permit2, c.position_manager, c.universal_router)
        or normalize_hex(tx["data"]) != tx["data"]
        or len(tx["data"]) < 10
        or tx["nonce"] != hex(nonce)
        or tx["gas"] != hex(gas)
        or tx["maxFeePerGas"] != hex(maximum)
        or not 0 <= rpc_quantity(tx["maxPriorityFeePerGas"]) <= min(maximum, c.priority_fee_wei)
        or rpc_quantity(tx["value"]) >= 2**256
    ):
        raise ValueError("simulation envelope differs from caller bounds or policy")
    pin = {"blockHash": block.block_hash, "requireCanonical": True}
    # OP's Fjord upper-bound API includes signature/compression overhead itself.
    # This is an observed bound at this block, not a promise about future L1 prices.
    size = len(bytes.fromhex(tx["data"][2:])) + 256
    calls: list[tuple[str, list[object]]] = [
        ("eth_chainId", []),
        ("eth_getBlockByNumber", [hex(block.number), False]),
        ("eth_getTransactionCount", [c.account, pin]),
        ("eth_getTransactionCount", [c.account, "latest"]),
        ("eth_getTransactionCount", [c.account, "pending"]),
        ("eth_call", [tx, pin]),
        ("eth_estimateGas", [tx, pin]),
        ("eth_getCode", [ORACLE, pin]),
        (
            "eth_call",
            [
                {
                    "to": ORACLE,
                    "data": "0x"
                    + calldata("getL1FeeUpperBound(uint256)", ["uint256"], [size]).hex(),
                },
                pin,
            ],
        ),
        (
            "eth_call",
            [
                {
                    "to": ORACLE,
                    "data": "0x" + calldata("getOperatorFee(uint256)", ["uint256"], [gas]).hex(),
                },
                pin,
            ],
        ),
        ("eth_getTransactionCount", [c.account, "pending"]),
        ("eth_getStorageAt", [ORACLE, IMPLEMENTATION_SLOT, pin]),
        ("eth_getCode", [implementation, pin]),
        ("eth_getBlockByNumber", [hex(block.number), False]),
    ]
    return calls


def _validate_opening(
    c: LiveConfig, block: BlockRef, values: tuple[object, ...], nonce: int
) -> BlockSnapshot:
    header = decode_header(values[1], chain_id=c.chain_id)
    if (
        rpc_quantity(values[0]) != c.chain_id
        or header.ref != block
        or any(rpc_quantity(value) != nonce for value in values[2:5])
    ):
        raise ValueError("simulation chain, block or nonce changed")
    return header


def _validate(
    c: LiveConfig,
    block: BlockRef,
    tx: dict[str, Any],
    exchanges: Sequence[RpcExchange],
    *,
    nonce: int,
    gas: int,
    maximum: int,
) -> tuple[object, ...]:
    calls = _requests(c, block, tx, nonce=nonce, gas=gas, maximum=maximum)
    values = successful_results(exchanges, calls)
    header = _validate_opening(c, block, values, nonce)
    if (
        decode_header(values[-1], chain_id=c.chain_id) != header
        or rpc_quantity(values[10]) != nonce
    ):
        raise ValueError("simulation chain, block or nonce changed")
    expected_return = "0x" + f"{1:064x}" if tx["to"] == c.pool.currency1 else "0x"
    if values[5] != expected_return or not 0 < rpc_quantity(values[6]) <= gas:
        raise ValueError("simulation failed or exceeds envelope")
    validate_gas_oracle_proof(c.expected_pins, values[7], values[11], values[12])
    extras = []
    for raw in values[8:10]:
        if not isinstance(raw, str) or len(raw) != 66 or not raw.startswith("0x"):
            raise ValueError("canonical Base fee estimate missing")
        extras.append(int(decode(["uint256"], bytes.fromhex(raw[2:]))[0]))
    if gas * maximum + sum(extras) > c.gas_reserve_wei:
        raise ValueError("observed complete gas bound exceeds reserve")
    return values


def _payload(
    c: LiveConfig,
    block: BlockRef,
    tx: dict[str, Any],
    values: tuple[object, ...],
    *,
    nonce: int,
    gas: int,
    maximum: int,
) -> bytes:
    return canonical_json(
        {
            "schema": "oasis.live-authority-simulation.v3",
            "block": asdict(block),
            "config_hash": sha256_hex(canonical_json(asdict(c))),
            "transaction": tx,
            "nonce": nonce,
            "gas": gas,
            "maximum": maximum,
            "gas_oracle_pins": validate_gas_oracle_proof(
                c.expected_pins, values[7], values[11], values[12]
            ),
            "results": values,
            "execution_eligible": False,
        }
    )


def replay_simulation(
    store: EvidenceStore,
    ref: EvidenceRef,
    c: LiveConfig,
    block: BlockRef,
    tx: dict[str, Any],
    *,
    nonce: int,
    gas: int,
    maximum: int,
) -> tuple[object, ...]:
    """Re-derive an explicitly scoped simulation from retained wire bytes only."""
    if ref.kind != "live_authority_simulation" or ref.block != block:
        raise ValueError("simulation evidence kind or block differs")
    record = store.get_record(ref)
    if record.request_payload is None:
        raise ValueError("simulation raw requests missing")
    exchanges = restore_exchanges(record.request_payload, record.raw_response_payload)
    values = _validate(c, block, tx, exchanges, nonce=nonce, gas=gas, maximum=maximum)
    if record.payload != _payload(
        c, block, tx, values, nonce=nonce, gas=gas, maximum=maximum
    ) or record.interpretation_payload != canonical_json({"execution_eligible": False}):
        raise ValueError("simulation interpretation contradicts raw proof or scope")
    return values


def replay_simulation_quorum(
    store: EvidenceStore,
    refs: tuple[EvidenceRef, EvidenceRef],
    c: LiveConfig,
    block: BlockRef,
    tx: dict[str, Any],
    *,
    sources: tuple[str, str],
    nonce: int,
    gas: int,
    maximum: int,
) -> tuple[object, ...]:
    if len(refs) != 2 or len(set(sources)) != 2 or tuple(r.source for r in refs) != sources:
        raise ValueError("simulation requires the declared independent provider pair")
    values = [
        replay_simulation(store, r, c, block, tx, nonce=nonce, gas=gas, maximum=maximum)
        for r in refs
    ]
    if values[0] != values[1]:
        raise ValueError("independent raw simulations disagree")
    return values[0]


def simulate(
    reader: RetainedReceiptCostReader,
    c: LiveConfig,
    block: BlockRef,
    tx: dict[str, Any],
    *,
    nonce: int,
    gas: int,
    maximum: int,
) -> tuple[EvidenceRef, EvidenceRef]:
    if reader.chain_id != c.chain_id or reader.account != c.account:
        raise ValueError("simulation reader scope differs")
    calls = _requests(c, block, tx, nonce=nonce, gas=gas, maximum=maximum)
    pair = (reader.sessions(reader.sources[0]), reader.sessions(reader.sources[1]))
    validate_session_pair(*pair)
    if (pair[0][2], pair[1][2]) != reader.sources:
        raise ValueError("simulation provider aliases changed")
    opening = calls[:5]

    def verify_opening() -> None:
        results = [successful_results(session[1].exchanges, opening) for session in pair]
        for values in results:
            _validate_opening(c, block, values, nonce)
        if results[0] != results[1]:
            raise ValueError("independent raw simulations disagree")

    acquire_pair(pair, calls, checkpoint=(len(opening), verify_opening))
    results = [
        _validate(c, block, tx, session[1].exchanges, nonce=nonce, gas=gas, maximum=maximum)
        for session in pair
    ]
    if results[0] != results[1]:
        raise ValueError("independent raw simulations disagree")
    refs = tuple(
        reader.store.put(
            "live_authority_simulation",
            _payload(c, block, tx, values, nonce=nonce, gas=gas, maximum=maximum),
            block,
            session[2],
            request=wire_envelope(session[1].exchanges, response=False),
            raw_response=wire_envelope(session[1].exchanges, response=True),
            interpretation={"execution_eligible": False},
        )
        for session, values in zip(pair, results, strict=True)
    )
    quorum = (refs[0], refs[1])
    replay_simulation_quorum(
        reader.store,
        quorum,
        c,
        block,
        tx,
        sources=reader.sources,
        nonce=nonce,
        gas=gas,
        maximum=maximum,
    )
    return quorum
