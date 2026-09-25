"""Two-source, exact-envelope LP eth_call simulation with retained raw proofs."""

import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime

from eth_hash.auto import keccak

from ..evidence.rpc_capture import BoundedExchangeRecorder
from ..evidence.rpc_validation import decode_exchange, rpc_quantity
from ..evidence.store import EvidenceStore
from ..models.chain import BlockSnapshot
from ..models.common import EvidenceRef, normalize_address, normalize_hex
from ..rpc.client import RpcClient
from ..rpc.transport import JsonRpcTransport, RpcExchange
from .lp_preparation import LpSimulationPlan
from .lp_signer_policy import calldata_hash, simulation_payload
from .lp_state_machine import RuntimeRole

POSITION_MANAGER_SELECTOR = keccak(b"POSITION_MANAGER()")[:4]
POOL_MANAGER_SELECTOR = keccak(b"POOL_MANAGER()")[:4]
SIMULATION_KIND = "pinned-simulation"


@dataclass(frozen=True, slots=True)
class LpSimulationResult:
    plan_hash: str
    evidence: tuple[EvidenceRef, EvidenceRef]

    @property
    def execution_eligible(self) -> bool:
        return False


def simulate_lp_plan(
    store: EvidenceStore,
    plan: LpSimulationPlan,
    primary: tuple[JsonRpcTransport, BoundedExchangeRecorder, str],
    secondary: tuple[JsonRpcTransport, BoundedExchangeRecorder, str],
) -> LpSimulationResult:
    """Only fresh sessions; complete raw reply replay precedes evidence retention."""
    intent = plan.intent
    pins = intent.runtime_pins
    pins_by_role = {pin.role: pin for pin in pins}
    if (
        set(pins_by_role) != set(RuntimeRole)
        or pins_by_role[RuntimeRole.EXECUTOR].address != intent.target
        or calldata_hash(plan.calldata) != intent.calldata_hash
    ):
        raise ValueError("role-bound runtime pins or calldata do not match plan")
    block = plan.position_evidence[0].block
    if (
        block is None
        or block.chain_id != intent.chain_id
        or block.number != intent.state_block
        or block.block_hash != intent.state_hash
        or plan.position_evidence[1].block != block
    ):
        raise ValueError("plan simulation block mismatch")
    if primary[0] is secondary[0] or primary[1] is secondary[1] or primary[2] == secondary[2]:
        raise ValueError("independent simulation sessions required")
    for session in (primary, secondary):
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", session[2]) or session[1].exchanges:
            raise ValueError("fresh recorder and public source alias required")
    pin_arg = {"blockHash": block.block_hash, "requireCanonical": True}
    call = {
        "from": plan.account,
        "to": intent.target,
        "value": "0x0",
        "gas": hex(intent.max_gas_units),
        "data": "0x" + plan.calldata.hex(),
    }
    payload = simulation_payload(
        block=block,
        account=plan.account,
        target=intent.target,
        value=0,
        gas_limit=intent.max_gas_units,
        calldata_digest=intent.calldata_hash,
        succeeded=True,
        runtime_pins=intent.runtime_pins,
    )

    getter_calls = (
        (
            RuntimeRole.POSITION_MANAGER,
            {"to": intent.target, "data": "0x" + POSITION_MANAGER_SELECTOR.hex()},
        ),
        (
            RuntimeRole.POOL_MANAGER,
            {"to": intent.target, "data": "0x" + POOL_MANAGER_SELECTOR.hex()},
        ),
    )

    def decode_getter_address(result: object) -> str:
        if not isinstance(result, str):
            raise ValueError("executor dependency getter returned non-hex data")
        value = normalize_hex(result)
        if len(value) != 66 or value[2:26] != "0" * 24:
            raise ValueError("executor dependency getter returned noncanonical address")
        return normalize_address("0x" + value[-40:])

    def capture(
        session: tuple[JsonRpcTransport, BoundedExchangeRecorder, str],
    ) -> tuple[BlockSnapshot, tuple[RpcExchange, ...]]:
        transport, recorder, _ = session
        rpc = RpcClient(transport, expected_chain_id=intent.chain_id)
        header = rpc.get_block_snapshot(block.number)
        if header.ref != block:
            raise ValueError("simulation opening header mismatch")
        for pin in pins:
            code = rpc.get_code(pin.address, block)
            if code == "0x" or "0x" + keccak(bytes.fromhex(code[2:])).hex() != pin.code_hash:
                raise ValueError("simulation runtime mismatch")
        for role, getter_call in getter_calls:
            result = transport.request("eth_call", [getter_call, pin_arg])
            if decode_getter_address(result) != pins_by_role[role].address:
                raise ValueError("executor immutable dependency mismatch")
        if transport.request("eth_call", [call, pin_arg]) != "0x":
            raise ValueError("executor simulation returned unexpected data")
        if rpc.get_block_snapshot(block.number) != header:
            raise ValueError("simulation reorg")
        exchanges = recorder.exchanges
        # Reconstruct exact successful requests and results instead of trusting
        # parsed transport values or caller-supplied success flags.
        expected = [
            ("eth_chainId", []),
            ("eth_getBlockByNumber", [hex(block.number), False]),
            *(("eth_getCode", [pin.address, pin_arg]) for pin in pins),
            *(("eth_call", [getter_call, pin_arg]) for _, getter_call in getter_calls),
            ("eth_call", [call, pin_arg]),
            ("eth_getBlockByNumber", [hex(block.number), False]),
        ]
        if len(exchanges) != len(expected):
            raise ValueError("simulation capture is incomplete or contains extra calls")
        seen = set()
        for exchange, (method, params) in zip(exchanges, expected, strict=True):
            actual_params, response = decode_exchange(exchange)
            if (
                exchange.request_id in seen
                or exchange.method != method
                or actual_params != params
                or "error" in response
            ):
                raise ValueError("simulation raw proof mismatch")
            result = response["result"]
            if method == "eth_chainId":
                if rpc_quantity(result) != block.chain_id:
                    raise ValueError("raw simulation chain mismatch")
            elif method == "eth_getBlockByNumber":
                if (
                    not isinstance(result, dict)
                    or rpc_quantity(result.get("number")) != block.number
                    or result.get("hash") != block.block_hash
                    or result.get("parentHash") != header.parent_hash
                    or rpc_quantity(result.get("timestamp")) != block.timestamp
                ):
                    raise ValueError("raw simulation header mismatch")
            elif method == "eth_getCode":
                runtime = next(pin for pin in pins if pin.address == params[0])
                if (
                    not isinstance(result, str)
                    or result == "0x"
                    or "0x" + keccak(bytes.fromhex(normalize_hex(result)[2:])).hex()
                    != runtime.code_hash
                ):
                    raise ValueError("raw simulation code mismatch")
            elif method == "eth_call":
                getter = next(
                    (
                        (role, getter_call)
                        for role, getter_call in getter_calls
                        if params[0] == getter_call
                    ),
                    None,
                )
                if getter is not None:
                    if decode_getter_address(result) != pins_by_role[getter[0]].address:
                        raise ValueError("raw executor dependency getter mismatch")
                elif params[0] != call or result != "0x":
                    raise ValueError("raw executor simulation result mismatch")
            seen.add(exchange.request_id)
        return header, exchanges

    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="lp-simulation") as workers:
        first, second = workers.submit(capture, primary), workers.submit(capture, secondary)
        captures = (first.result(), second.result())
    if captures[0][0] != captures[1][0]:
        raise ValueError("simulation provider header disagreement")
    retrieved_at = datetime.now(UTC).isoformat()

    def retain(exchanges: tuple[RpcExchange, ...], source: str) -> EvidenceRef:
        return store.put(
            SIMULATION_KIND,
            payload,
            block,
            source,
            request={
                "encoding": "hex_exact_bytes",
                "exchanges": [
                    {
                        "method": e.method,
                        "request_id": e.request_id,
                        "attempt": e.attempt,
                        "payload": e.request_payload.hex(),
                    }
                    for e in exchanges
                ],
            },
            raw_response={
                "encoding": "hex_exact_bytes",
                "exchanges": [
                    {
                        "method": e.method,
                        "request_id": e.request_id,
                        "attempt": e.attempt,
                        "payload": e.response_payload.hex(),
                    }
                    for e in exchanges
                ],
            },
            interpretation=payload,
            retrieved_at=retrieved_at,
            schema_version=2,
        )

    return LpSimulationResult(
        intent.intent_hash,
        (
            retain(captures[0][1], primary[2]),
            retain(captures[1][1], secondary[2]),
        ),
    )
