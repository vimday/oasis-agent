"""Retained independent full-envelope simulation and nonce admission proofs."""

from dataclasses import asdict
from typing import Any

from eth_abi.abi import decode
from eth_hash.auto import keccak

from ..evidence.exact_capture import (
    decode_header,
    successful_results,
    validate_session_pair,
    wire_envelope,
)
from ..evidence.rpc_validation import rpc_quantity
from ..models.common import BlockRef, canonical_json, sha256_hex
from .acquisition import acquire_pair
from .chain import calldata
from .config import LiveConfig
from .receipts import RetainedReceiptCostReader

ORACLE = "0x420000000000000000000000000000000000000f"


def simulate(
    reader: RetainedReceiptCostReader,
    c: LiveConfig,
    block: BlockRef,
    tx: dict[str, Any],
    *,
    nonce: int,
    gas: int,
    maximum: int,
) -> tuple[str, str]:
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
        ("eth_getBlockByNumber", [hex(block.number), False]),
    ]
    pair = (reader.sessions(reader.sources[0]), reader.sessions(reader.sources[1]))
    validate_session_pair(*pair)
    if (pair[0][2], pair[1][2]) != reader.sources:
        raise ValueError("simulation provider aliases changed")
    acquire_pair(pair, calls)
    results = []
    for session in pair:
        values = successful_results(session[1].exchanges, calls)
        if (
            rpc_quantity(values[0]) != c.chain_id
            or decode_header(values[1], chain_id=c.chain_id).ref != block
            or decode_header(values[-1], chain_id=c.chain_id).ref != block
            or any(rpc_quantity(values[i]) != nonce for i in (2, 3, 4, 10))
        ):
            raise ValueError("simulation chain, block or nonce changed")
        expected_return = "0x" + f"{1:064x}" if tx["to"] == c.pool.currency1 else "0x"
        if values[5] != expected_return or not 0 < rpc_quantity(values[6]) <= gas:
            raise ValueError("simulation failed or exceeds envelope")
        code = values[7]
        if not isinstance(code, str) or not code.startswith("0x") or not bytes.fromhex(code[2:]):
            raise ValueError("Base fee oracle code missing")
        extras = []
        for raw in values[8:10]:
            if not isinstance(raw, str) or len(raw) != 66 or not raw.startswith("0x"):
                raise ValueError("canonical Base fee estimate missing")
            extras.append(int(decode(["uint256"], bytes.fromhex(raw[2:]))[0]))
        if gas * maximum + sum(extras) > c.gas_reserve_wei:
            raise ValueError("observed complete gas bound exceeds reserve")
        results.append(values)
    if results[0] != results[1]:
        raise ValueError("independent raw simulations disagree")
    refs = []
    for session, values in zip(pair, results, strict=True):
        payload = canonical_json(
            {
                "schema": "oasis.live-authority-simulation.v1",
                "block": asdict(block),
                "config_hash": sha256_hex(canonical_json(asdict(c))),
                "transaction": tx,
                "gas_oracle_runtime_hash": "0x" + keccak(bytes.fromhex(str(values[7])[2:])).hex(),
                "results": values,
                "execution_eligible": False,
            }
        )
        ref = reader.store.put(
            "live_authority_simulation",
            payload,
            block,
            session[2],
            request=wire_envelope(session[1].exchanges, response=False),
            raw_response=wire_envelope(session[1].exchanges, response=True),
            interpretation={"execution_eligible": False},
        )
        if reader.store.get_record(ref).payload != payload:
            raise ValueError("simulation retention failed")
        refs.append(ref.content_hash)
    return refs[0], refs[1]
