"""Successful bounded approval outcome plus gas-only owner cash, from raw quorum.

Only exact USDC-to-Permit2 and Permit2-to-configured-spender approvals qualify.
This cannot reconcile a swap, mint, burn or arbitrary successful transaction.
"""

import json
from dataclasses import asdict, dataclass
from typing import Any

from eth_abi.abi import decode, encode
from eth_hash.auto import keccak

from ..evidence.exact_capture import (
    decode_header,
    require_code_hash,
    successful_results,
    validate_session_pair,
    wire_envelope,
)
from ..evidence.rpc_validation import restore_exchanges
from ..evidence.store import EvidenceStore
from ..execution.account_capture import SessionFactory
from ..models.chain import BlockSnapshot
from ..models.common import (
    BlockRef,
    EvidenceRef,
    canonical_json,
    evidence_ref_from_json,
    sha256_hex,
)
from ..rpc.transport import RpcExchange
from .cash_interval import MAX_CASH_BLOCKS, TransferFreeInterval, capture_interval, gas_only_wallets
from .config import LiveConfig
from .intent import SubmittedTransaction
from .v4lp import erc20_approve_call, permit2_approve_call
from .wallet import WalletBalance, capture_wallet, replay_wallet, uint_word


def _validate_intent(
    config: LiveConfig, kind: str, intent: dict[str, Any], tx: SubmittedTransaction, start: BlockRef
) -> None:
    keys = {"amount"} if kind == "approve_usdc" else {"amount", "spender", "expiration"}
    if kind not in {"approve_usdc", "approve_permit2"} or set(intent) != keys:
        raise ValueError("only bounded approval intents qualify")
    amount = intent["amount"]
    if type(amount) is not int or not 0 < amount <= config.approval_usdc or amount >= 2**160:
        raise ValueError("approval amount exceeds configured bound")
    if kind == "approve_usdc":
        target, data = config.pool.currency1, erc20_approve_call(config.permit2, amount)
    else:
        spender, expiration = intent["spender"], intent["expiration"]
        if spender not in (config.position_manager, config.universal_router):
            raise ValueError("approval spender is not configured")
        if (
            start.timestamp is None
            or type(expiration) is not int
            or expiration != start.timestamp + config.approval_days * 86_400
            or not 0 < expiration < 2**48
        ):
            raise ValueError("approval expiration differs from decision policy")
        target, data = (
            config.permit2,
            permit2_approve_call(config.pool.currency1, spender, amount, expiration),
        )
    if tx.target != target or tx.value != 0 or tx.calldata_hash != "0x" + keccak(data).hex():
        raise ValueError("signed envelope is not the bounded approval")


def _requests(
    config: LiveConfig, kind: str, intent: dict[str, Any], start: BlockRef, end: BlockRef
) -> list[tuple[str, list[object]]]:
    a = {"blockHash": start.block_hash, "requireCanonical": True}
    b = {"blockHash": end.block_hash, "requireCanonical": True}
    if kind == "approve_usdc":
        data = "0xdd62ed3e" + encode(["address", "address"], [config.account, config.permit2]).hex()
        result: list[tuple[str, list[object]]] = [
            ("eth_call", [{"to": config.pool.currency1, "data": data}, b])
        ]
    else:
        data = (
            "0x"
            + (
                keccak(b"allowance(address,address,address)")[:4]
                + encode(
                    ["address"] * 3, [config.account, config.pool.currency1, intent["spender"]]
                )
            ).hex()
        )
        result = [
            ("eth_getCode", [config.permit2, a]),
            ("eth_getCode", [config.permit2, b]),
            ("eth_call", [{"to": config.permit2, "data": data}, a]),
            ("eth_call", [{"to": config.permit2, "data": data}, b]),
        ]
    result.append(("eth_getBlockByNumber", [hex(end.number), False]))
    return result


def _allowance(value: object) -> tuple[int, int, int]:
    if not isinstance(value, str) or not value.startswith("0x") or len(value) != 194:
        raise ValueError("Permit2 allowance is not three canonical words")
    amount, expiry, nonce = decode(["uint160", "uint48", "uint48"], bytes.fromhex(value[2:]))
    return int(amount), int(expiry), int(nonce)


@dataclass(frozen=True, slots=True)
class ApprovalCashCapture:
    kind: str
    intent: dict[str, Any]
    transaction: SubmittedTransaction
    opening: EvidenceRef
    closing: EvidenceRef
    cost: EvidenceRef
    headers: tuple[BlockSnapshot, ...]
    exchanges: tuple[RpcExchange, ...]

    def reconstruct(
        self, store: EvidenceStore, config: LiveConfig
    ) -> tuple[WalletBalance, int | None]:
        if self.opening.block is None:
            raise ValueError("approval opening block missing")
        _validate_intent(config, self.kind, self.intent, self.transaction, self.opening.block)
        a, b = gas_only_wallets(
            store,
            config,
            self.transaction,
            self.opening,
            self.closing,
            self.cost,
            expected_status=1,
        )
        split = 3 + 3 * len(self.headers)
        TransferFreeInterval(a.header, self.headers, self.exchanges[:split]).validate(
            config, b.header
        )
        if (
            sum(len(x.request_payload) + len(x.response_payload) for x in self.exchanges)
            > 5_000_000
        ):
            raise ValueError("approval cash byte budget exceeded")
        values = successful_results(
            self.exchanges[split:],
            _requests(config, self.kind, self.intent, a.header.ref, b.header.ref),
        )
        nonce = None
        if self.kind == "approve_usdc":
            if uint_word(values[0]) != self.intent["amount"]:
                raise ValueError("USDC approval outcome differs from intent")
        else:
            for code in values[:2]:
                require_code_hash(code, config.expected_pins["permit2"])
            before, after = _allowance(values[2]), _allowance(values[3])
            if (
                after[:2] != (self.intent["amount"], self.intent["expiration"])
                or before[2] != after[2]
                or b.header.ref.timestamp is None
                or after[1] <= b.header.ref.timestamp
            ):
                raise ValueError("Permit2 approval outcome or nonce differs")
            nonce = after[2]
        if decode_header(values[-1], chain_id=config.chain_id) != b.header:
            raise ValueError("approval closing block moved")
        return b, nonce

    def payload(self, store: EvidenceStore, config: LiveConfig) -> bytes:
        balance, nonce = self.reconstruct(store, config)
        return canonical_json(
            {
                "schema": "oasis.live-approval-cash.v1",
                "kind": self.kind,
                "intent": self.intent,
                "transaction": asdict(self.transaction),
                "opening": asdict(self.opening),
                "closing": asdict(self.closing),
                "cost": asdict(self.cost),
                "headers": [asdict(h) for h in self.headers],
                "balance": asdict(balance),
                "allowance_nonce": nonce,
                "config_hash": sha256_hex(canonical_json(asdict(config))),
                "execution_eligible": False,
            }
        )


def replay_approval_cash(
    store: EvidenceStore, ref: EvidenceRef, config: LiveConfig
) -> ApprovalCashCapture:
    record = store.get_record(ref)
    if ref.kind != "live_approval_cash" or record.request_payload is None:
        raise ValueError("approval cash scope missing")
    data = json.loads(record.payload)
    capture = ApprovalCashCapture(
        data["kind"],
        data["intent"],
        SubmittedTransaction(**data["transaction"]),
        evidence_ref_from_json(data["opening"]),
        evidence_ref_from_json(data["closing"]),
        evidence_ref_from_json(data["cost"]),
        tuple(BlockSnapshot(BlockRef(**h["ref"]), h["parent_hash"]) for h in data["headers"]),
        restore_exchanges(record.request_payload, record.raw_response_payload),
    )
    if (
        ref.source != capture.opening.source
        or ref.block != capture.closing.block
        or capture.payload(store, config) != record.payload
    ):
        raise ValueError("approval cash interpretation differs from raw proof")
    return capture


def reconcile_approval(
    store: EvidenceStore,
    config: LiveConfig,
    kind: str,
    intent: dict[str, Any],
    transaction: SubmittedTransaction,
    opening: tuple[EvidenceRef, ...],
    costs: tuple[EvidenceRef, ...],
    *,
    sources: tuple[str, str],
    sessions: SessionFactory,
) -> tuple[WalletBalance, tuple[EvidenceRef, EvidenceRef]]:
    if (
        len(opening) != 2
        or len(costs) != 2
        or tuple(r.source for r in opening) != sources
        or tuple(r.source for r in costs) != sources
        or costs[0].block is None
        or costs[0].block != costs[1].block
    ):
        raise ValueError("approval requires declared opening and cost pair")
    starts = tuple(replay_wallet(store, ref) for ref in opening)
    if starts[0].reconstruct() != starts[1].reconstruct():
        raise ValueError("approval opening providers disagree")
    start, end = starts[0].block, costs[0].block
    _validate_intent(config, kind, intent, transaction, start)
    if not 0 < end.number - start.number <= MAX_CASH_BLOCKS:
        raise ValueError("approval interval outside bound")
    closing = capture_wallet(store, config, end, sources=sources, sessions=sessions)
    pair = (sessions(sources[0]), sessions(sources[1]))
    validate_session_pair(*pair)
    if (pair[0][2], pair[1][2]) != sources:
        raise ValueError("approval aliases changed")
    captures = []
    for index, session in enumerate(pair):
        headers = capture_interval(config, start, end, session)
        for method, params in _requests(config, kind, intent, start, end):
            session[0].request(method, params)
        capture = ApprovalCashCapture(
            kind,
            dict(intent),
            transaction,
            opening[index],
            closing[index],
            costs[index],
            headers,
            session[1].exchanges,
        )
        capture.reconstruct(store, config)
        captures.append(capture)
    if (
        captures[0].reconstruct(store, config) != captures[1].reconstruct(store, config)
        or captures[0].headers != captures[1].headers
    ):
        raise ValueError("approval cash providers disagree")
    refs = [
        store.put(
            "live_approval_cash",
            c.payload(store, config),
            end,
            c.opening.source,
            request=wire_envelope(c.exchanges, response=False),
            raw_response=wire_envelope(c.exchanges, response=True),
            interpretation={"execution_eligible": False},
        )
        for c in captures
    ]
    for ref in refs:
        replay_approval_cash(store, ref, config)
    return captures[0].reconstruct(store, config)[0], (refs[0], refs[1])
