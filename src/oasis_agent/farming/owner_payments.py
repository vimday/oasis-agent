"""Research closure for exclusive, direct PosM ERC-20 increase/decrease/collect.

Amounts are actual observed owner balance changes, cross-checked with complete
block Transfer logs and successful token calls. Unsupported netting stays closed
to promotion. This is not token-code attestation or independent RPC finality.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, replace
from typing import cast

from eth_abi.abi import decode, encode
from eth_hash.auto import keccak

from ..evidence.rpc_validation import decode_exchange, rpc_quantity
from ..evidence.store import EvidenceStore
from ..models.chain import BlockSnapshot, LogRecord
from ..models.common import ZERO_ADDRESS, BlockRef, EvidenceRef, canonical_json, normalize_address
from ..models.finance import AssetPair, TokenAmount
from ..rpc.transport import RpcExchange
from ..uniswap_v4.events import MODIFY_LIQUIDITY_TOPIC
from .position_ledger import PositionLedgerEntry, SettlementAttribution
from .position_settlement import PositionSettlementCapture, _mapping, _text

TRANSFER = "0x" + keccak(b"Transfer(address,address,uint256)").hex()
MODIFY = keccak(b"modifyLiquidities(bytes,uint256)")[:4]


class UnsupportedOwnerPayment(ValueError):
    """Valid research data outside the supported exclusive-payment policy."""


def _abi(types: list[str], value: str) -> tuple[object, ...]:
    raw = bytes.fromhex(value.removeprefix("0x"))
    decoded = decode(types, raw)
    if encode(types, decoded) != raw:
        raise ValueError("noncanonical payment ABI")
    return cast(tuple[object, ...], decoded)


def _log(value: object) -> LogRecord:
    raw = _mapping(value)
    topics = raw.get("topics")
    if not isinstance(topics, list) or raw.get("removed", False) is not False:
        raise ValueError("invalid payment log topics/removal")
    return LogRecord(
        _text(raw.get("address")),
        tuple(_text(x) for x in topics),
        _text(raw.get("data")),
        rpc_quantity(raw.get("blockNumber")),
        _text(raw.get("transactionHash")),
        rpc_quantity(raw.get("logIndex")),
        _text(raw.get("blockHash")),
        rpc_quantity(raw.get("transactionIndex")),
    )


def _transfer(log: LogRecord) -> tuple[str, str, str, int]:
    if len(log.topics) != 3 or log.topics[0] != TRANSFER:
        raise ValueError("expected canonical ERC-20 Transfer")
    sender = _abi(["address"], log.topics[1])[0]
    recipient = _abi(["address"], log.topics[2])[0]
    amount = _abi(["uint256"], log.data)[0]
    return log.address, cast(str, sender), cast(str, recipient), cast(int, amount)


def balance_requests(core: PositionSettlementCapture, owner: str) -> list[tuple[str, list[object]]]:
    """Exact read-only witness sequence; use a fresh bounded recorder."""
    owner = normalize_address(owner)
    block = core.header.ref
    if block.number == 0:
        raise ValueError("payment needs a preceding block")
    previous = {"blockHash": core.header.parent_hash, "requireCanonical": True}
    current = {"blockHash": block.block_hash, "requireCanonical": True}
    result: list[tuple[str, list[object]]] = [
        ("eth_chainId", []),
        ("eth_getBlockByNumber", [hex(block.number - 1), False]),
        ("eth_getBlockByNumber", [hex(block.number), False]),
    ]
    for token in (core.pool_key.currency0, core.pool_key.currency1):
        for pin in (previous, current):
            result.append(("eth_getCode", [token, pin]))
        result.append(("eth_call", [{"to": token, "data": "0x313ce567"}, current]))
        for pin in (previous, current):
            result.append(
                (
                    "eth_call",
                    [{"to": token, "data": "0x70a08231" + encode(["address"], [owner]).hex()}, pin],
                )
            )
    result.append(
        (
            "eth_getLogs",
            [
                {
                    "address": [core.pool_key.currency0, core.pool_key.currency1],
                    "fromBlock": hex(block.number),
                    "toBlock": hex(block.number),
                    "topics": [TRANSFER],
                }
            ],
        )
    )
    result.append(("eth_getBlockByNumber", [hex(block.number), False]))
    return result


@dataclass(frozen=True, slots=True)
class OwnerBalanceCapture:
    core: PositionSettlementCapture
    owner: str
    source: str
    exchanges: tuple[RpcExchange, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "owner", normalize_address(self.owner))
        object.__setattr__(self, "exchanges", tuple(self.exchanges))
        if not self.source or self.source != self.core.source:
            raise ValueError("balance witness must identify the core observation provider")
        if self.owner == ZERO_ADDRESS:
            raise ValueError("owner cannot be zero")
        self.reconstruct()

    def reconstruct(self) -> AssetPair:
        self.core.reconstruct()
        expected = balance_requests(self.core, self.owner)
        if (
            len(self.exchanges) != len(expected)
            or sum(len(x.request_payload) + len(x.response_payload) for x in self.exchanges)
            > 5_000_000
        ):
            raise ValueError("incomplete/oversized balance evidence")
        results = []
        ids: set[int] = set()
        for exchange, (method, params) in zip(self.exchanges, expected, strict=True):
            actual, response = decode_exchange(exchange)
            if (
                exchange.method != method
                or canonical_json(actual) != canonical_json(params)
                or exchange.request_id in ids
                or "error" in response
            ):
                raise ValueError("balance evidence request/response mismatch")
            ids.add(exchange.request_id)
            results.append(response["result"])
        block = self.core.header.ref
        if rpc_quantity(results[0]) != block.chain_id:
            raise ValueError("balance witness chain mismatch")
        previous = _mapping(results[1])
        previous_ref = BlockRef(
            block.chain_id,
            rpc_quantity(previous.get("number")),
            _text(previous.get("hash")),
            rpc_quantity(previous.get("timestamp")),
        )
        if (
            previous_ref.number != block.number - 1
            or previous_ref.block_hash != self.core.header.parent_hash
        ):
            raise ValueError("balance opening header mismatch")
        for index in (2, len(results) - 1):
            current = _mapping(results[index])
            hdr = BlockSnapshot(
                BlockRef(
                    block.chain_id,
                    rpc_quantity(current.get("number")),
                    _text(current.get("hash")),
                    rpc_quantity(current.get("timestamp")),
                ),
                _text(current.get("parentHash")),
            )
            if hdr != self.core.header:
                raise ValueError("balance closing header mismatch/reorg")
        amounts = []
        for i, token in enumerate((self.core.pool_key.currency0, self.core.pool_key.currency1)):
            offset = 3 + 5 * i
            if token == ZERO_ADDRESS:
                raise UnsupportedOwnerPayment(
                    "native currency needs transaction gas/refund attribution"
                )
            code0, code1 = results[offset : offset + 2]
            if (
                code0 != code1
                or not isinstance(code0, str)
                or len(bytes.fromhex(code0.removeprefix("0x"))) == 0
            ):
                raise ValueError("token runtime missing or changed")
            decimals = cast(int, _abi(["uint8"], _text(results[offset + 2]))[0])
            before = cast(int, _abi(["uint256"], _text(results[offset + 3]))[0])
            after = cast(int, _abi(["uint256"], _text(results[offset + 4]))[0])
            amounts.append(TokenAmount(token, decimals, after - before))
        raw_logs = results[-2]
        if not isinstance(raw_logs, list):
            raise TypeError("complete block Transfer logs required")
        logs = tuple(_log(x) for x in raw_logs)
        indices = [x.log_index for x in logs]
        if indices != sorted(set(indices)):
            raise ValueError("balance transfer logs must be unique and ordered")
        receipt = _mapping(decode_exchange(self.core.exchanges[2])[1]["result"])
        receipt_logs = receipt.get("logs")
        if not isinstance(receipt_logs, list):
            raise TypeError("receipt logs required")
        currencies = {self.core.pool_key.currency0, self.core.pool_key.currency1}
        relevant_receipt = tuple(
            _log(x)
            for x in receipt_logs
            if _mapping(x).get("address") in currencies and _log(x).topics[:1] == (TRANSFER,)
        )
        owner_logs = []
        sums = {token: 0 for token in currencies}
        for log in logs:
            if log.address not in currencies or (log.block_number, log.block_hash) != (
                block.number,
                block.block_hash,
            ):
                raise ValueError("balance log block/token mismatch")
            token, sender, recipient, amount = _transfer(log)
            if self.owner not in (sender, recipient):
                continue
            if log.transaction_hash != self.core.entry.transaction_hash:
                raise UnsupportedOwnerPayment("owner has other token activity in the same block")
            owner_logs.append(log)
            sums[token] += (amount if recipient == self.owner else 0) - (
                amount if sender == self.owner else 0
            )
        expected_owner_logs = tuple(x for x in relevant_receipt if self.owner in _transfer(x)[1:3])
        if tuple(owner_logs) != expected_owner_logs:
            raise ValueError("receipt and full-block owner Transfer evidence disagree")
        if any(x.amount != sums[x.token] for x in amounts):
            raise ValueError("owner token balance/Transfer residual")
        return AssetPair(amounts[0], amounts[1])


def resolve_exclusive_owner_payment(capture: OwnerBalanceCapture) -> AssetPair:
    amounts = capture.reconstruct()
    core = capture.core
    result = core.reconstruct()
    if result.caller_delta is None:
        raise UnsupportedOwnerPayment("complete trace required for owner settlement")
    receipt = _mapping(decode_exchange(core.exchanges[2])[1]["result"])
    root = _mapping(decode_exchange(core.exchanges[3])[1]["result"])
    if root.get("from") != capture.owner or root.get("to") != core.entry.observation.sender:
        raise UnsupportedOwnerPayment("payment must be direct owner to PositionManager")
    if rpc_quantity(root.get("value", "0x0")) != 0:
        raise UnsupportedOwnerPayment("native value/refunds unsupported in ERC-20 closure")
    raw = bytes.fromhex(_text(root.get("input")).removeprefix("0x"))
    if raw[:4] != MODIFY:
        raise UnsupportedOwnerPayment("only direct modifyLiquidities supported")
    unlock, deadline = _abi(["bytes", "uint256"], raw[4:].hex())
    if cast(int, deadline) < cast(int, core.header.ref.timestamp):
        raise ValueError("expired action in successful receipt")
    actions, params = _abi(["bytes", "bytes[]"], cast(bytes, unlock).hex())
    if (
        actions not in (b"\x00\x12\x12", b"\x01\x12\x12")
        or len(cast(tuple[object, ...], params)) != 3
    ):
        raise UnsupportedOwnerPayment(
            "requires one increase/decrease followed by two CLOSE_CURRENCY actions"
        )
    params = cast(tuple[bytes, ...], params)
    token_id, liquidity, _, _, _ = _abi(
        ["uint256", "uint256", "uint128", "uint128", "bytes"], params[0].hex()
    )
    delta = cast(int, liquidity) * (-1 if actions[0] == 1 else 1)
    if token_id != result.token_id or delta != core.entry.observation.liquidity_delta:
        raise ValueError("action does not match NFT/core modification")
    for param, token in zip(
        params[1:], (core.pool_key.currency0, core.pool_key.currency1), strict=True
    ):
        if _abi(["address"], param.hex())[0] != token:
            raise ValueError("CLOSE_CURRENCY token mismatch")
    logs = tuple(_log(x) for x in cast(list[object], receipt["logs"]))
    if sum(x.topics[:1] == (MODIFY_LIQUIDITY_TOPIC,) for x in logs) != 1:
        raise UnsupportedOwnerPayment("mixed position transaction cannot use exclusive attribution")
    currencies = {core.pool_key.currency0, core.pool_key.currency1}
    transfers = [
        _transfer(x) for x in logs if x.address in currencies and x.topics[:1] == (TRANSFER,)
    ]
    for _, sender, recipient, _ in transfers:
        if {sender, recipient} != {capture.owner, core.pool_manager}:
            raise UnsupportedOwnerPayment("third-party payments/refunds need separate attribution")
    traced = []
    stack = [root]
    while stack:
        frame = stack.pop()
        stack.extend(_mapping(x) for x in cast(list[object], frame.get("calls", [])))
        if frame.get("type") != "CALL" or frame.get("to") not in currencies:
            continue
        data = bytes.fromhex(_text(frame.get("input")).removeprefix("0x"))
        if data[:4].hex() == "a9059cbb":
            call_recipient, call_amount = _abi(["address", "uint256"], data[4:].hex())
            call_sender = frame.get("from")
        elif data[:4].hex() == "23b872dd":
            call_sender, call_recipient, call_amount = _abi(
                ["address", "address", "uint256"], data[4:].hex()
            )
        else:
            continue
        if _abi(["bool"], _text(frame.get("output")))[0] is not True:
            raise ValueError("token transfer did not return true")
        traced.append((frame["to"], call_sender, call_recipient, call_amount))
    if Counter(traced) != Counter(transfers):
        raise ValueError("token call/receipt Transfer mismatch")
    if (amounts.amount0, amounts.amount1) != (
        result.caller_delta.amount0,
        result.caller_delta.amount1,
    ):
        raise ValueError("owner balance/core delta residual")
    return amounts


def persist_owner_payment(
    store: EvidenceStore, capture: OwnerBalanceCapture
) -> PositionLedgerEntry:
    amounts = resolve_exclusive_owner_payment(capture)
    from .position_settlement import persist_position_settlement_evidence

    core_ref = persist_position_settlement_evidence(store, capture.core)

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

    ref: EvidenceRef = store.put(
        "position_owner_settlement",
        canonical_json(
            {
                "schema": "oasis.exclusive-owner-payment.v1",
                "entry": asdict(capture.core.entry),
                "owner": capture.owner,
                "amounts": asdict(amounts),
                "core_evidence": asdict(core_ref),
                "trust": "single_provider_research",
                "execution_eligible": False,
                "unresolved": [
                    "independent_provider_finality",
                    "token_runtime_attestation",
                    "all_cost_pnl",
                ],
            }
        ),
        capture.core.header.ref,
        capture.source,
        request=envelope(False),
        raw_response=envelope(True),
        interpretation={
            "validation": "trace_transfer_balance_reconciliation",
            "execution_eligible": False,
        },
    )
    return replace(
        capture.core.entry,
        owner_amount0=amounts.amount0,
        owner_amount1=amounts.amount1,
        settlement_attribution=SettlementAttribution.EXCLUSIVE_TRANSACTION,
        settlement_evidence=ref,
    )
