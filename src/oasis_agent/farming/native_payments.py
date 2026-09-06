"""Exclusive direct PosM native-currency increase/decrease/collect attribution.

Native (``address(0)``) settlement is not ERC-20 Transfer based. The owner
pays ETH into the PositionManager (root ``msg.value``), which settles it into
the PoolManager (``settle{value}``); refunds flow PoolManager -> owner directly
(``take``, an internal CALL with value). Attribution therefore combines:

- pinned ``eth_getBalance`` deltas for the owner (native side),
- family-aware receipt gas debit (Ethereum/OP L1 fee/Nitro all-in gas),
- trace CALL value conservation restricted to owner/PositionManager/PoolManager,
- the existing ERC-20 code/decimals/balanceOf/Transfer checks for the other
  currency (this covers WETH pools, where WETH is a plain ERC-20).

The transaction must be exclusive: one direct ``modifyLiquidities``, one
increase/decrease followed by two CLOSE_CURRENCY actions, no WRAP/UNWRAP, no
third-party ETH flows, no excess ``msg.value``. Anything else fails closed.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, replace
from typing import cast

from eth_abi.abi import encode

from ..evidence.rpc_validation import decode_exchange, rpc_quantity
from ..evidence.store import EvidenceStore
from ..models.chain import BlockSnapshot
from ..models.common import ZERO_ADDRESS, BlockRef, EvidenceRef, canonical_json, normalize_address
from ..models.finance import AssetPair, TokenAmount
from ..rpc.transport import RpcExchange
from ..uniswap_v4.events import MODIFY_LIQUIDITY_TOPIC
from .owner_payments import MODIFY, TRANSFER, UnsupportedOwnerPayment, _abi, _log, _transfer
from .position_ledger import PositionLedgerEntry, SettlementAttribution
from .position_settlement import PositionSettlementCapture, _mapping, _text
from .transaction_costs import FAMILIES

# Native decimals are 18 on every explicitly supported family (ethereum, op,
# nitro). Unknown chains fail closed at construction before any attribution.
NATIVE_DECIMALS = 18


def native_balance_requests(
    core: PositionSettlementCapture, owner: str
) -> list[tuple[str, list[object]]]:
    """Exact read-only witness sequence for one native + one ERC-20 currency."""
    owner = normalize_address(owner)
    block = core.header.ref
    if block.number == 0:
        raise ValueError("native payment needs a preceding block")
    currencies = (core.pool_key.currency0, core.pool_key.currency1)
    if sum(currency == ZERO_ADDRESS for currency in currencies) != 1:
        raise UnsupportedOwnerPayment("native attribution requires exactly one native currency")
    previous = {"blockHash": core.header.parent_hash, "requireCanonical": True}
    current = {"blockHash": block.block_hash, "requireCanonical": True}
    result: list[tuple[str, list[object]]] = [
        ("eth_chainId", []),
        ("eth_getBlockByNumber", [hex(block.number - 1), False]),
        ("eth_getBlockByNumber", [hex(block.number), False]),
    ]
    for token in currencies:
        if token == ZERO_ADDRESS:
            for pin in (previous, current):
                result.append(("eth_getBalance", [owner, pin]))
        else:
            for pin in (previous, current):
                result.append(("eth_getCode", [token, pin]))
            result.append(("eth_call", [{"to": token, "data": "0x313ce567"}, current]))
            for pin in (previous, current):
                result.append(
                    (
                        "eth_call",
                        [
                            {
                                "to": token,
                                "data": "0x70a08231" + encode(["address"], [owner]).hex(),
                            },
                            pin,
                        ],
                    )
                )
    erc20 = next(token for token in currencies if token != ZERO_ADDRESS)
    result.append(
        (
            "eth_getLogs",
            [
                {
                    "address": [erc20],
                    "fromBlock": hex(block.number),
                    "toBlock": hex(block.number),
                    "topics": [TRANSFER],
                }
            ],
        )
    )
    result.append(("eth_getBlockByNumber", [hex(block.number), False]))
    return result


def _gas_debit(receipt: dict[str, object], chain_id: int) -> int:
    """Native gas actually debited from the transaction sender's balance."""
    family = FAMILIES.get(chain_id)
    if family is None:
        raise UnsupportedOwnerPayment("unknown chain gas policy for native attribution")
    gas = rpc_quantity(receipt.get("gasUsed"))
    price = rpc_quantity(receipt.get("effectiveGasPrice"))
    if gas >= 1 << 64 or price >= 1 << 256:
        raise ValueError("gas/price width overflow")
    debit = gas * price
    if family == "op":
        # OP Stack charges L1 data fees on top of the L2 execution fee.
        debit += rpc_quantity(receipt.get("l1Fee"))
    return debit


def _native_value_frames(
    root: dict[str, object], owner: str, manager: str, posm: str
) -> tuple[int, int]:
    """(settle_in, take_out) native flows restricted to the exclusive policy.

    Allowed CALL frames carrying value: the root owner -> PositionManager
    payment (``settle_in``), one PositionManager -> PoolManager ``settle{value}``
    (``settle_in``), and one PoolManager -> owner ``take`` refund (``take_out``).
    WRAP/UNWRAP, sweeps, third parties, or excess value all fail closed.
    """
    settle_in = take_out = 0
    settle_seen = False
    stack: list[dict[str, object]] = [root]
    while stack:
        frame = stack.pop()
        stack.extend(_mapping(x) for x in cast(list[object], frame.get("calls", [])))
        if frame.get("type") != "CALL":
            continue
        value = rpc_quantity(frame.get("value", "0x0"))
        if value == 0:
            continue
        sender = normalize_address(_text(frame.get("from")))
        recipient = normalize_address(_text(frame.get("to")))
        if sender == owner and recipient == posm:
            if settle_in != 0:
                raise UnsupportedOwnerPayment("duplicate root native payment")
            settle_in = value
        elif sender == posm and recipient == manager:
            if settle_in == 0 or value != settle_in:
                raise UnsupportedOwnerPayment("native settle frame must match the root payment")
            settle_seen = True
        elif sender == manager and recipient == owner:
            if take_out != 0:
                raise UnsupportedOwnerPayment("duplicate native take frame")
            take_out = value
        else:
            raise UnsupportedOwnerPayment("third-party or wrapped native value flow")
    if settle_seen != (settle_in != 0):
        raise UnsupportedOwnerPayment("root native payment requires a matching settle frame")
    if settle_in and take_out:
        raise UnsupportedOwnerPayment("native settle and take in the same exclusive action")
    return settle_in, take_out


@dataclass(frozen=True, slots=True)
class NativeOwnerPaymentCapture:
    """Pinned owner balance, Transfer, and native value-flow evidence."""

    core: PositionSettlementCapture
    owner: str
    source: str
    exchanges: tuple[RpcExchange, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "owner", normalize_address(self.owner))
        object.__setattr__(self, "exchanges", tuple(self.exchanges))
        if not self.source or self.source != self.core.source:
            raise ValueError("native witness must identify the core observation provider")
        if self.owner == ZERO_ADDRESS:
            raise ValueError("owner cannot be zero")
        self.reconstruct()

    def reconstruct(self) -> AssetPair:
        core = self.core
        result = core.reconstruct()
        if result.caller_delta is None:
            raise UnsupportedOwnerPayment("complete trace required for native owner settlement")
        expected = native_balance_requests(core, self.owner)
        if (
            len(self.exchanges) != len(expected)
            or sum(len(x.request_payload) + len(x.response_payload) for x in self.exchanges)
            > 5_000_000
        ):
            raise ValueError("incomplete/oversized native balance evidence")
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
                raise ValueError("native balance evidence request/response mismatch")
            ids.add(exchange.request_id)
            results.append(response["result"])
        block = core.header.ref
        if rpc_quantity(results[0]) != block.chain_id:
            raise ValueError("native balance witness chain mismatch")
        previous = _mapping(results[1])
        previous_ref = BlockRef(
            block.chain_id,
            rpc_quantity(previous.get("number")),
            _text(previous.get("hash")),
            rpc_quantity(previous.get("timestamp")),
        )
        if (
            previous_ref.number != block.number - 1
            or previous_ref.block_hash != core.header.parent_hash
        ):
            raise ValueError("native balance opening header mismatch")
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
            if hdr != core.header:
                raise ValueError("native balance closing header mismatch/reorg")
        amounts: list[TokenAmount] = []
        native_delta = None
        offset = 3
        for token in (core.pool_key.currency0, core.pool_key.currency1):
            if token == ZERO_ADDRESS:
                before = rpc_quantity(results[offset])
                after = rpc_quantity(results[offset + 1])
                if before >= 1 << 256 or after >= 1 << 256:
                    raise ValueError("native balance width overflow")
                native_delta = after - before
                offset += 2
            else:
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
                offset += 5
        raw_logs = results[-2]
        if not isinstance(raw_logs, list):
            raise TypeError("complete block Transfer logs required")
        logs = tuple(_log(x) for x in raw_logs)
        indices = [x.log_index for x in logs]
        if indices != sorted(set(indices)):
            raise ValueError("native transfer logs must be unique and ordered")
        receipt = _mapping(decode_exchange(core.exchanges[2])[1]["result"])
        receipt_logs = receipt.get("logs")
        if not isinstance(receipt_logs, list):
            raise TypeError("receipt logs required")
        erc20 = next(
            token
            for token in (core.pool_key.currency0, core.pool_key.currency1)
            if token != ZERO_ADDRESS
        )
        currencies = {erc20}
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
                raise ValueError("native log block/token mismatch")
            token, sender, recipient, amount = _transfer(log)
            if self.owner not in (sender, recipient):
                continue
            if log.transaction_hash != core.entry.transaction_hash:
                raise UnsupportedOwnerPayment("owner has other token activity in the same block")
            owner_logs.append(log)
            sums[token] += (amount if recipient == self.owner else 0) - (
                amount if sender == self.owner else 0
            )
        expected_owner_logs = tuple(x for x in relevant_receipt if self.owner in _transfer(x)[1:3])
        if tuple(owner_logs) != expected_owner_logs:
            raise ValueError("receipt and full-block owner Transfer evidence disagree")
        for flow in amounts:
            if flow.token == erc20 and flow.amount != sums[erc20]:
                raise ValueError("owner token balance/Transfer residual")
        root = _mapping(decode_exchange(core.exchanges[3])[1]["result"])
        if root.get("type") != "CALL":
            raise ValueError("native transaction trace root must be CALL")
        settle_in, take_out = _native_value_frames(
            root, self.owner, core.pool_manager, core.entry.observation.sender
        )
        gas = _gas_debit(receipt, block.chain_id)
        native_amount = cast(int, native_delta)
        if native_amount + gas != take_out - settle_in:
            raise ValueError("owner native balance/gas/core residual")
        # The native balance delta includes the owner's gas debit; the
        # settlement amount is the balance delta net of gas.
        amounts.insert(
            0 if core.pool_key.currency0 == ZERO_ADDRESS else len(amounts),
            TokenAmount(ZERO_ADDRESS, NATIVE_DECIMALS, native_amount + gas),
        )
        return AssetPair(amounts[0], amounts[1])


def resolve_exclusive_native_payment(capture: NativeOwnerPaymentCapture) -> AssetPair:
    """Attribute the exclusive native+ERC-20 closure to the owner."""
    amounts = capture.reconstruct()
    core = capture.core
    result = core.reconstruct()
    if result.caller_delta is None:
        raise UnsupportedOwnerPayment("complete trace required for native owner settlement")
    receipt = _mapping(decode_exchange(core.exchanges[2])[1]["result"])
    root = _mapping(decode_exchange(core.exchanges[3])[1]["result"])
    if root.get("from") != capture.owner or root.get("to") != core.entry.observation.sender:
        raise UnsupportedOwnerPayment("payment must be direct owner to PositionManager")
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
    erc20 = next(
        token
        for token in (core.pool_key.currency0, core.pool_key.currency1)
        if token != ZERO_ADDRESS
    )
    transfers = [_transfer(x) for x in logs if x.address == erc20 and x.topics[:1] == (TRANSFER,)]
    for _, sender, recipient, _ in transfers:
        if {sender, recipient} != {capture.owner, core.pool_manager}:
            raise UnsupportedOwnerPayment("third-party payments/refunds need separate attribution")
    traced = []
    stack = [root]
    while stack:
        frame = stack.pop()
        stack.extend(_mapping(x) for x in cast(list[object], frame.get("calls", [])))
        if frame.get("type") != "CALL" or frame.get("to") != erc20:
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


def persist_native_owner_payment(
    store: EvidenceStore, capture: NativeOwnerPaymentCapture
) -> PositionLedgerEntry:
    amounts = resolve_exclusive_native_payment(capture)
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
        "native_owner_settlement",
        canonical_json(
            {
                "schema": "oasis.exclusive-native-owner-payment.v1",
                "entry": asdict(capture.core.entry),
                "owner": capture.owner,
                "amounts": asdict(amounts),
                "native_currency": ZERO_ADDRESS,
                "native_decimals": NATIVE_DECIMALS,
                "core_evidence": asdict(core_ref),
                "trust": "single_provider_research",
                "execution_eligible": False,
                "unresolved": [
                    "independent_provider_finality",
                    "token_runtime_attestation",
                    "all_cost_pnl",
                    "batch_native_mixed_transactions",
                ],
            }
        ),
        capture.core.header.ref,
        capture.source,
        request=envelope(False),
        raw_response=envelope(True),
        interpretation={
            "validation": "native_balance_gas_trace_reconciliation",
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
