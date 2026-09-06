"""Receipt/callTracer evidence for core position deltas, not owner cash settlement.

A PoolManager return includes principal, fees and potentially hook deltas. The
caller is PositionManager, not the NFT owner. Never promote these amounts into
owner_amount or net PnL without a separate payment/recipient reconciliation.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import cast

from eth_abi.abi import decode, encode
from eth_hash.auto import keccak

from ..evidence.rpc_capture import BoundedExchangeRecorder
from ..evidence.rpc_validation import decode_exchange, rpc_quantity
from ..evidence.store import EvidenceStore
from ..models.chain import BlockSnapshot, LogRecord
from ..models.common import EvidenceRef, canonical_json, normalize_address, normalize_hex
from ..models.v4 import BalanceDelta, PoolKey
from ..rpc.transport import JsonRpcTransport, RpcExchange
from ..uniswap_v4.delta import decode_balance_delta
from ..uniswap_v4.events import MODIFY_LIQUIDITY_TOPIC, decode_modify_liquidity
from .position_ledger import PositionLedgerEntry

MODIFY_TYPES = ["(address,address,uint24,int24,address)", "(int24,int24,int256,bytes32)", "bytes"]
MODIFY_SELECTOR = keccak(
    b"modifyLiquidity((address,address,uint24,int24,address),(int24,int24,int256,bytes32),bytes)"
)[:4]


def _text(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("expected a string in settlement evidence")
    return value


def _mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise TypeError("expected an object in settlement evidence")
    return cast(dict[str, object], value)


def _requests(
    header: BlockSnapshot, pool_manager: str, entry: PositionLedgerEntry
) -> list[tuple[str, list[object]]]:
    block = header.ref
    pin = {"blockHash": block.block_hash, "requireCanonical": True}
    return [
        ("eth_chainId", []),
        ("eth_getBlockByNumber", [hex(block.number), False]),
        ("eth_getTransactionReceipt", [entry.transaction_hash]),
        (
            "debug_traceTransaction",
            [
                entry.transaction_hash,
                {
                    "tracer": "callTracer",
                    "timeout": "10s",
                    "tracerConfig": {"onlyTopCall": False},
                },
            ],
        ),
        ("eth_getCode", [pool_manager, pin]),
        ("eth_getCode", [entry.observation.sender, pin]),
        ("eth_getBlockByNumber", [hex(block.number), False]),
    ]


@dataclass(frozen=True, slots=True)
class PositionCoreSettlement:
    token_id: int
    caller_delta: BalanceDelta | None
    fees_accrued: BalanceDelta | None
    execution_gas_native_base_units: int
    pool_manager_code_hash: str
    position_manager_code_hash: str


@dataclass(frozen=True, slots=True)
class PositionSettlementCapture:
    source: str
    header: BlockSnapshot
    pool_manager: str
    pool_key: PoolKey
    entry: PositionLedgerEntry
    exchanges: tuple[RpcExchange, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.source, str) or not self.source.strip():
            raise ValueError("settlement source alias required")
        object.__setattr__(self, "pool_manager", normalize_address(self.pool_manager))
        object.__setattr__(self, "exchanges", tuple(self.exchanges))
        self.reconstruct()

    def reconstruct(self) -> PositionCoreSettlement:
        from ..uniswap_v4.pool_id import pool_id_from_key

        entry, block = self.entry, self.header.ref
        if block != entry.block or entry.settlement_resolved:
            raise ValueError("capture requires the exact unresolved ledger entry block")
        if pool_id_from_key(self.pool_key) != entry.observation.pool_id:
            raise ValueError("settlement PoolKey does not match entry")
        expected = _requests(self.header, self.pool_manager, entry)
        if (
            sum(len(x.request_payload) + len(x.response_payload) for x in self.exchanges)
            > 5_000_000
        ):
            raise ValueError("settlement evidence exceeds bounded parser byte capacity")
        if len(self.exchanges) != len(expected):
            raise ValueError("settlement capture requires the complete bounded RPC sequence")
        values: list[object] = []
        ids: set[int] = set()
        for exchange, (method, params) in zip(self.exchanges, expected, strict=True):
            actual, response = decode_exchange(exchange)
            # canonical JSON comparison distinguishes false from 0.
            if (
                exchange.method != method
                or canonical_json(actual) != canonical_json(params)
                or exchange.request_id in ids
            ):
                raise ValueError("settlement request scope/order/identity mismatch")
            ids.add(exchange.request_id)
            if "error" in response:
                if method != "debug_traceTransaction":
                    raise ValueError("required settlement RPC evidence is unavailable")
                values.append(None)
            else:
                if response["result"] is None:
                    raise ValueError("null settlement RPC result is not usable evidence")
                values.append(response["result"])
        if rpc_quantity(values[0]) != block.chain_id:
            raise ValueError("settlement chain mismatch")
        for value in (values[1], values[6]):
            hdr = _mapping(value)
            if (
                rpc_quantity(hdr.get("number")) != block.number
                or hdr.get("hash") != block.block_hash
                or hdr.get("parentHash") != self.header.parent_hash
                or rpc_quantity(hdr.get("timestamp")) != block.timestamp
            ):
                raise ValueError("settlement header changed or mismatched")
        codes = [bytes.fromhex(normalize_hex(_text(value))[2:]) for value in values[4:6]]
        if any(not code for code in codes):
            raise ValueError("settlement contract code is missing")
        receipt = _mapping(values[2])
        self._bind_transaction(receipt)
        if rpc_quantity(receipt.get("status")) != 1:
            raise ValueError("position event requires a successful receipt")
        gas = rpc_quantity(receipt.get("gasUsed")) * rpc_quantity(receipt.get("effectiveGasPrice"))
        logs = receipt.get("logs")
        if not isinstance(logs, list):
            raise TypeError("complete receipt logs required")
        matches: list[LogRecord] = []
        last_index = -1
        for value in logs:
            raw = _mapping(value)
            self._bind_transaction(raw)
            index = rpc_quantity(raw.get("logIndex"))
            if index <= last_index or raw.get("removed", False) is not False:
                raise ValueError("receipt logs must be ordered, unique and not removed")
            last_index = index
            topics = raw.get("topics")
            if not isinstance(topics, list):
                raise TypeError("receipt topics required")
            log = LogRecord(
                normalize_address(_text(raw.get("address"))),
                tuple(_text(topic) for topic in topics),
                normalize_hex(_text(raw.get("data"))),
                block.number,
                entry.transaction_hash,
                index,
                block.block_hash,
                entry.transaction_index,
            )
            if log.address == self.pool_manager and log.topics[:1] == (MODIFY_LIQUIDITY_TOPIC,):
                observation = decode_modify_liquidity(log)
                if (observation.sender, observation.pool_id, observation.salt) == (
                    entry.observation.sender,
                    entry.observation.pool_id,
                    entry.observation.salt,
                ):
                    matches.append(log)
        if (
            len(matches) != 1
            or matches[0].log_index != entry.log_index
            or decode_modify_liquidity(matches[0]) != entry.observation
        ):
            raise ValueError("receipt does not uniquely identify the position modification")
        caller = fees = None
        if values[3] is not None:
            caller, fees = self._trace_deltas(_mapping(values[3]), receipt)
        return PositionCoreSettlement(
            int(entry.observation.salt, 16),
            caller,
            fees,
            gas,
            "0x" + keccak(codes[0]).hex(),
            "0x" + keccak(codes[1]).hex(),
        )

    def _bind_transaction(self, raw: dict[str, object]) -> None:
        entry = self.entry
        if (
            raw.get("transactionHash") != entry.transaction_hash
            or raw.get("blockHash") != entry.block.block_hash
            or rpc_quantity(raw.get("blockNumber")) != entry.block.number
            or rpc_quantity(raw.get("transactionIndex")) != entry.transaction_index
        ):
            raise ValueError("settlement transaction/block/index mismatch")

    def _trace_deltas(
        self, root: dict[str, object], receipt: dict[str, object]
    ) -> tuple[BalanceDelta, BalanceDelta]:
        if root.get("type") != "CALL":
            raise ValueError("transaction trace root must be CALL")
        for name in ("from", "to"):
            if normalize_address(_text(root.get(name))) != normalize_address(
                _text(receipt.get(name))
            ):
                raise ValueError("trace root differs from receipt")
        candidates: list[bytes] = []
        stack = [(root, 0)]
        visited = 0
        key, obs = self.pool_key, self.entry.observation
        expected_key = (key.currency0, key.currency1, key.fee, key.tick_spacing, key.hooks)
        expected_params = (
            obs.tick_lower,
            obs.tick_upper,
            obs.liquidity_delta,
            bytes.fromhex(obs.salt[2:]),
        )
        while stack:
            frame, depth = stack.pop()
            visited += 1
            if visited > 10_000 or depth > 128:
                raise ValueError("trace exceeds bounded parser capacity")
            if "error" in frame or "revertReason" in frame:
                raise ValueError("reverted trace frames are unsupported for settlement attribution")
            kind = frame.get("type")
            if kind not in {"CALL", "STATICCALL", "DELEGATECALL", "CREATE", "CREATE2"}:
                raise ValueError("unsupported trace call type")
            target = normalize_address(_text(frame.get("to")))
            sender = normalize_address(_text(frame.get("from")))
            data = bytes.fromhex(normalize_hex(_text(frame.get("input")))[2:])
            # Geth frame.from is the calling execution address, not Solidity
            # msg.sender. DELEGATECALL keeps that address as execution context;
            # its code target must never become the caller of its child CALLs.
            context = sender if kind == "DELEGATECALL" else target
            children = frame.get("calls", [])
            if not isinstance(children, list):
                raise TypeError("invalid trace children")
            for child in reversed(children):
                child_map = _mapping(child)
                if normalize_address(_text(child_map.get("from"))) != context:
                    raise ValueError("trace child caller differs from parent")
                stack.append((child_map, depth + 1))
            if target != self.pool_manager or data[:4] != MODIFY_SELECTOR:
                continue
            if kind != "CALL":
                raise ValueError("modification must be a CALL")
            try:
                decoded = decode(MODIFY_TYPES, data[4:])
                if encode(MODIFY_TYPES, decoded) != data[4:]:
                    raise ValueError("noncanonical modifyLiquidity calldata")
            except Exception as exc:
                raise ValueError("invalid modifyLiquidity calldata") from exc
            if decoded[0] == expected_key and decoded[1] == expected_params:
                if sender != obs.sender:
                    raise ValueError("trace modification caller mismatch")
                output = bytes.fromhex(normalize_hex(_text(frame.get("output")))[2:])
                if len(output) != 64:
                    raise ValueError("modifyLiquidity must return exactly two BalanceDelta words")
                candidates.append(output)
        if len(candidates) != 1:
            raise ValueError("trace does not uniquely identify the position action")
        caller = decode_balance_delta(candidates[0][:32])
        fees = decode_balance_delta(candidates[0][32:])
        if fees.amount0 < 0 or fees.amount1 < 0:
            raise ValueError("accrued fees cannot be negative")
        return caller, fees

    @property
    def payload(self) -> bytes:
        result = self.reconstruct()
        unresolved = [
            "owner_payment_and_recipient",
            "token_balance_reconciliation",
            "chain_total_gas_and_cost_allocation",
            "deployment_identity_and_finality",
        ]
        if result.caller_delta is None:
            unresolved.append("trace_unavailable")
        return canonical_json(
            {
                "schema": "oasis.position-core-settlement.v1",
                "header": asdict(self.header),
                "pool_manager": self.pool_manager,
                "pool_key": asdict(self.pool_key),
                "entry": asdict(self.entry),
                "core_settlement": asdict(result),
                "unresolved": unresolved,
                "trust": "single_provider_research",
                "execution_eligible": False,
                "settlement_complete": False,
                "owner_amount0": None,
                "owner_amount1": None,
                "net_pnl": None,
            }
        )


def collect_position_settlement(
    transport: JsonRpcTransport,
    recorder: BoundedExchangeRecorder,
    *,
    source: str,
    header: BlockSnapshot,
    pool_manager: str,
    pool_key: PoolKey,
    entry: PositionLedgerEntry,
) -> PositionSettlementCapture:
    """Read through a fresh transport configured with this recorder, without signing."""
    from ..errors import RpcError

    if recorder.exchanges:
        raise ValueError("settlement collection requires a fresh recorded session")
    manager = normalize_address(pool_manager)
    for method, params in _requests(header, manager, entry):
        try:
            transport.request(method, params)
        except RpcError:
            if method != "debug_traceTransaction":
                raise
            # Only a retained, well-formed RPC error can become trace_unavailable.
            # A transport failure leaves incomplete coverage and is rejected below.
    return PositionSettlementCapture(source, header, manager, pool_key, entry, recorder.exchanges)


def persist_position_settlement_evidence(
    store: EvidenceStore,
    capture: PositionSettlementCapture,
    *,
    retrieved_at: str | None = None,
) -> EvidenceRef:
    payload = capture.payload

    def envelope(response: bool) -> dict[str, object]:
        return {
            "encoding": "hex_exact_bytes",
            "exchanges": [
                {
                    "method": x.method,
                    "request_id": x.request_id,
                    "attempt": x.attempt,
                    "payload": (x.response_payload if response else x.request_payload).hex(),
                }
                for x in capture.exchanges
            ],
        }

    return store.put(
        "position_core_settlement",
        payload,
        capture.header.ref,
        capture.source,
        request=envelope(False),
        raw_response=envelope(True),
        interpretation={
            "schema": "oasis.position-core-settlement-evidence.v1",
            "validation": "raw_rpc_reconstruction",
            "execution_eligible": False,
        },
        retrieved_at=retrieved_at,
        schema_version=1,
    )


def replay_position_settlement_evidence(
    store: EvidenceStore,
    evidence: EvidenceRef,
    *,
    header: BlockSnapshot,
    pool_manager: str,
    pool_key: PoolKey,
    entry: PositionLedgerEntry,
) -> PositionSettlementCapture:
    """Revalidate retained wire bytes against an explicitly supplied event scope."""
    from ..evidence.rpc_validation import restore_exchanges

    if (
        evidence.kind != "position_core_settlement"
        or evidence.schema_version != 1
        or evidence.block != header.ref
    ):
        raise ValueError("retained settlement evidence has wrong kind/schema/block")
    record = store.get_record(evidence)
    if record.request_payload is None:
        raise ValueError("retained settlement evidence is missing raw requests")
    capture = PositionSettlementCapture(
        evidence.source,
        header,
        pool_manager,
        pool_key,
        entry,
        restore_exchanges(record.request_payload, record.raw_response_payload),
    )
    if capture.payload != record.payload:
        raise ValueError("stored settlement interpretation differs from raw reconstruction")
    return capture
