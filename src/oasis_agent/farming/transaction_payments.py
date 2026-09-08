"""All-participant ERC-20 cashflow evidence for a complete transaction.

Receipt cashflows are reconciled to full-block balance and supply changes. They
are not silently assigned to NFTs: inter-position netting has no unique physical
payment allocation. Token semantics and independent finality remain research gates.

Scope and fail-closed boundaries:

- Only standard ERC-20 Transfer events (exactly three topics) scope tokens and
  accounts. A non-standard Transfer on the same token in the same block
  (topic0 == Transfer but topic count != 3) fails the whole reconstruction
  instead of being silently dropped; malformed or exotic token behavior must
  never produce a partial ledger.
- Reconciliation verifies both the per-account block balance delta and the
  total supply delta, so refunds and burns must agree with on-chain state
  changes in full-base-unit precision.
"""

from dataclasses import asdict, dataclass
from typing import cast

from eth_abi.abi import encode

from ..evidence.rpc_validation import decode_exchange, restore_exchanges, rpc_quantity
from ..evidence.store import EvidenceStore
from ..models.chain import BlockSnapshot
from ..models.common import ZERO_ADDRESS, BlockRef, EvidenceRef, canonical_json
from ..rpc.transport import RpcExchange
from .owner_payments import TRANSFER, _abi, _log, _transfer
from .position_settlement import PositionSettlementCapture, _mapping, _text


def _scope(core: PositionSettlementCapture) -> tuple[tuple[str, ...], tuple[str, ...]]:
    core.reconstruct()
    receipt = _mapping(decode_exchange(core.exchanges[2])[1]["result"])
    logs = [_log(x) for x in cast(list[object], receipt["logs"])]
    transfers = [_transfer(x) for x in logs if x.topics[:1] == (TRANSFER,) and len(x.topics) == 3]
    tokens = tuple(sorted({x[0] for x in transfers}))
    accounts = tuple(sorted({a for x in transfers for a in x[1:3]} - {ZERO_ADDRESS}))
    if not tokens or len(tokens) > 16 or len(accounts) > 64:
        raise ValueError("transaction token/participant scope empty or exceeds budget")
    return tokens, accounts


def transaction_payment_requests(core: PositionSettlementCapture) -> list[tuple[str, list[object]]]:
    tokens, accounts = _scope(core)
    b = core.header.ref
    if b.number == 0:
        raise ValueError("payment requires preceding block")
    pins = (
        {"blockHash": core.header.parent_hash, "requireCanonical": True},
        {"blockHash": b.block_hash, "requireCanonical": True},
    )
    specs: list[tuple[str, list[object]]] = [
        ("eth_chainId", []),
        ("eth_getBlockByNumber", [hex(b.number - 1), False]),
        ("eth_getBlockByNumber", [hex(b.number), False]),
    ]
    for token in tokens:
        specs += [("eth_getCode", [token, pin]) for pin in pins]
        specs += [("eth_call", [{"to": token, "data": "0x313ce567"}, pins[1]])]
        for data in [
            "0x18160ddd",
            *["0x70a08231" + encode(["address"], [a]).hex() for a in accounts],
        ]:
            specs += [("eth_call", [{"to": token, "data": data}, pin]) for pin in pins]
    specs += [
        (
            "eth_getLogs",
            [
                {
                    "address": list(tokens),
                    "fromBlock": hex(b.number),
                    "toBlock": hex(b.number),
                    "topics": [TRANSFER],
                }
            ],
        ),
        ("eth_getBlockByNumber", [hex(b.number), False]),
    ]
    return specs


@dataclass(frozen=True, slots=True)
class AccountCashflow:
    token: str
    decimals: int
    account: str
    paid: int
    received: int
    net: int
    block_balance_before: int
    block_balance_after: int


@dataclass(frozen=True, slots=True)
class TransactionPaymentCapture:
    core: PositionSettlementCapture
    exchanges: tuple[RpcExchange, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "exchanges", tuple(self.exchanges))
        self.reconstruct()

    def reconstruct(self) -> tuple[AccountCashflow, ...]:
        tokens, accounts = _scope(self.core)
        expected = transaction_payment_requests(self.core)
        if (
            len(expected) != len(self.exchanges)
            or sum(len(x.request_payload) + len(x.response_payload) for x in self.exchanges)
            > 32_000_000
        ):
            raise ValueError("incomplete or oversized transaction payment evidence")
        results = []
        ids = set()
        for x, (method, params) in zip(self.exchanges, expected, strict=True):
            actual, response = decode_exchange(x)
            if (
                x.method != method
                or canonical_json(actual) != canonical_json(params)
                or x.request_id in ids
                or "error" in response
            ):
                raise ValueError("transaction payment request/response mismatch")
            ids.add(x.request_id)
            results.append(response["result"])
        b = self.core.header.ref
        if rpc_quantity(results[0]) != b.chain_id:
            raise ValueError("payment chain mismatch")
        prior = _mapping(results[1])
        if (
            rpc_quantity(prior.get("number")) != b.number - 1
            or prior.get("hash") != self.core.header.parent_hash
        ):
            raise ValueError("payment preceding block mismatch")
        for i in (2, len(results) - 1):
            h = _mapping(results[i])
            header = BlockSnapshot(
                BlockRef(
                    b.chain_id,
                    rpc_quantity(h.get("number")),
                    _text(h.get("hash")),
                    rpc_quantity(h.get("timestamp")),
                ),
                _text(h.get("parentHash")),
            )
            if header != self.core.header:
                raise ValueError("payment block changed")
        raw_logs = results[-2]
        if not isinstance(raw_logs, list):
            raise TypeError("complete block Transfer logs required")
        logs = tuple(_log(x) for x in raw_logs)
        if [x.log_index for x in logs] != sorted({x.log_index for x in logs}):
            raise ValueError("unordered or duplicate transfer logs")
        receipt = _mapping(decode_exchange(self.core.exchanges[2])[1]["result"])
        transaction_logs = tuple(
            _log(x)
            for x in cast(list[object], receipt["logs"])
            if _log(x).address in tokens and _log(x).topics[:1] == (TRANSFER,)
        )
        if (
            tuple(x for x in logs if x.transaction_hash == self.core.entry.transaction_hash)
            != transaction_logs
        ):
            raise ValueError("receipt/full-block transfer mismatch")
        block_net = {(t, a): 0 for t in tokens for a in accounts}
        paid = dict(block_net)
        received = dict(block_net)
        supply = dict.fromkeys(tokens, 0)
        for log in logs:
            if (log.block_number, log.block_hash) != (
                b.number,
                b.block_hash,
            ) or log.address not in tokens:
                raise ValueError("transfer scope mismatch")
            token, sender, recipient, amount = _transfer(log)
            if sender == ZERO_ADDRESS:
                supply[token] += amount
            elif sender in accounts:
                block_net[token, sender] -= amount
            if recipient == ZERO_ADDRESS:
                supply[token] -= amount
            elif recipient in accounts:
                block_net[token, recipient] += amount
            if log.transaction_hash == self.core.entry.transaction_hash:
                if sender != ZERO_ADDRESS:
                    paid[token, sender] += amount
                if recipient != ZERO_ADDRESS:
                    received[token, recipient] += amount
        result = []
        offset = 3
        for token in tokens:
            code0, code1 = results[offset : offset + 2]
            if (
                code0 != code1
                or not isinstance(code0, str)
                or not code0.startswith("0x")
                or not bytes.fromhex(code0[2:])
            ):
                raise ValueError("token runtime missing or changed")
            decimals = cast(int, _abi(["uint8"], _text(results[offset + 2]))[0])
            supply0 = cast(int, _abi(["uint256"], _text(results[offset + 3]))[0])
            supply1 = cast(int, _abi(["uint256"], _text(results[offset + 4]))[0])
            if supply1 - supply0 != supply[token]:
                raise ValueError("token supply/Transfer residual")
            offset += 5
            for account in accounts:
                before = cast(int, _abi(["uint256"], _text(results[offset]))[0])
                after = cast(int, _abi(["uint256"], _text(results[offset + 1]))[0])
                offset += 2
                if after - before != block_net[token, account]:
                    raise ValueError("account balance/full-block Transfer residual")
                result.append(
                    AccountCashflow(
                        token,
                        decimals,
                        account,
                        paid[token, account],
                        received[token, account],
                        received[token, account] - paid[token, account],
                        before,
                        after,
                    )
                )
        return tuple(result)

    @property
    def payload(self) -> bytes:
        return canonical_json(
            {
                "schema": "oasis.transaction-payments.v1",
                "block": asdict(self.core.header.ref),
                "transaction_hash": self.core.entry.transaction_hash,
                "accounts": [asdict(x) for x in self.reconstruct()],
                "basis": "receipt_transfers_reconciled_to_block_balances_and_supply",
                "position_cash_allocation": "unresolved",
                "execution_eligible": False,
                "trust": "single_provider_research",
            }
        )


def persist_transaction_payments(
    store: EvidenceStore, capture: TransactionPaymentCapture
) -> EvidenceRef:
    """Retain exact wire data; replay still requires the verified core capture."""

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
        "transaction_payments",
        capture.payload,
        capture.core.header.ref,
        capture.core.source,
        request=envelope(False),
        raw_response=envelope(True),
        interpretation={
            "execution_eligible": False,
            "basis": "block_balance_supply_reconciliation",
        },
    )


def replay_transaction_payments(
    store: EvidenceStore, evidence: EvidenceRef, core: PositionSettlementCapture
) -> TransactionPaymentCapture:
    if (
        evidence.kind != "transaction_payments"
        or evidence.block != core.header.ref
        or evidence.source != core.source
    ):
        raise ValueError("transaction payment evidence scope mismatch")
    record = store.get_record(evidence)
    if record.request_payload is None:
        raise ValueError("payment evidence missing wire requests")
    result = TransactionPaymentCapture(
        core, restore_exchanges(record.request_payload, record.raw_response_payload)
    )
    if result.payload != record.payload:
        raise ValueError("payment interpretation differs from wire reconstruction")
    return result
