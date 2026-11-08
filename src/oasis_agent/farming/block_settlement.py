"""One account's native and ERC-20 balances across one block, explained by its logs.

For an account, a pinned block (number, hash, parent hash) and a small sorted set of
ERC-20 tokens, `settlement_requests` is the exact request sequence, every state read
pinned by EIP-1898 block hash:

- the chain id, the parent header by height (it must be the pinned parent hash) and
  the block header, read again at the end (it must not have moved);
- the account's native balance at the parent and at the block;
- per token: runtime code at both pins (identical and nonempty), `decimals()`, and
  `totalSupply()` and `balanceOf(account)` at both pins;
- every `Transfer` log of those tokens in the block (EIP-234 `blockHash` filter).

`reconstruct()` re-derives every value from the retained bytes. Each log must be a
canonical, non-removed `Transfer` log of a scoped token in this exact block, in
strictly increasing log index, with transaction positions non-decreasing in that
order and exactly one position per transaction hash. A log carrying the Transfer
topic that is not a standard ERC-20 Transfer (three topics, one ABI word) fails the
whole capture instead of being dropped.

The result states balances, Transfer flows and residuals. It does not decide whether
a residual is acceptable: a rebasing, reflection or event-less token leaves one, and
native movements carry no logs at all. Nothing here signs, holds a key or broadcasts.
"""

import hashlib
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from typing import cast

from eth_abi.exceptions import DecodingError
from eth_hash.auto import keccak

from ..evidence.exact_capture import (
    RecordedSession,
    decode_header,
    successful_results,
    validate_session_pair,
    wire_envelope,
)
from ..evidence.rpc_validation import restore_exchanges, rpc_quantity
from ..evidence.store import EvidenceStore
from ..models.chain import BlockSnapshot, LogRecord
from ..models.common import (
    ZERO_ADDRESS,
    EvidenceRef,
    canonical_json,
    normalize_address,
    normalize_hash32,
)
from ..rpc.transport import RpcExchange
from .account_inventory import balance_of_data
from .owner_payments import TRANSFER, _abi, _log, _transfer

SCHEMA = "oasis.account-block-settlement.v1"
KIND = "account_block_settlement"
DECIMALS = "0x313ce567"
TOTAL_SUPPLY = "0x18160ddd"
MAX_TOKENS = 4
MAX_LOGS = 20_000
MAX_BYTES = 32_000_000
_UINT256 = 1 << 256
_PER_TOKEN = 7  # code x2, decimals, totalSupply x2, balanceOf x2


def _pins(header: BlockSnapshot) -> tuple[dict[str, object], dict[str, object]]:
    """EIP-1898 pins: state before the block (its parent) and after it."""
    return (
        {"blockHash": header.parent_hash, "requireCanonical": True},
        {"blockHash": header.ref.block_hash, "requireCanonical": True},
    )


def _scope(account: str, tokens: Sequence[str]) -> tuple[str, tuple[str, ...]]:
    account = normalize_address(account)
    scoped = tuple(normalize_address(token) for token in tokens)
    if int(account, 16) == 0:
        raise ValueError("settlement account must be nonzero")
    if not scoped or len(scoped) > MAX_TOKENS or list(scoped) != sorted(set(scoped)):
        raise ValueError("settlement tokens must be a sorted, unique, bounded set")
    if any(int(token, 16) == 0 for token in scoped):
        raise ValueError("settlement token must be nonzero")
    return account, scoped


def settlement_requests(
    header: BlockSnapshot, account: str, tokens: Sequence[str]
) -> list[tuple[str, list[object]]]:
    """The exact request sequence; the writer and the replay share this one definition."""
    account, scoped = _scope(account, tokens)
    number = header.ref.number
    if number == 0:
        raise ValueError("settlement requires a preceding block")
    before, after = _pins(header)
    specs: list[tuple[str, list[object]]] = [
        ("eth_chainId", []),
        ("eth_getBlockByNumber", [hex(number - 1), False]),
        ("eth_getBlockByNumber", [hex(number), False]),
        ("eth_getBalance", [account, before]),
        ("eth_getBalance", [account, after]),
    ]
    for token in scoped:
        specs += [("eth_getCode", [token, pin]) for pin in (before, after)]
        specs.append(("eth_call", [{"to": token, "data": DECIMALS}, after]))
        for data in (TOTAL_SUPPLY, balance_of_data(account)):
            specs += [("eth_call", [{"to": token, "data": data}, pin]) for pin in (before, after)]
    logs_filter = {
        "address": list(scoped),
        "blockHash": header.ref.block_hash,
        "topics": [TRANSFER],
    }
    specs += [
        ("eth_getLogs", [logs_filter]),
        ("eth_getBlockByNumber", [hex(number), False]),
    ]
    return specs


def _uint(value: object, abi_type: str = "uint256") -> int:
    """One canonical ABI word (an empty result from a codeless address is refused)."""
    if not isinstance(value, str):
        raise ValueError("call result must be hex data")
    try:
        return cast(int, _abi([abi_type], value)[0])
    except DecodingError as error:
        raise ValueError("call result is not one canonical ABI word") from error


def _quantity(value: object) -> int:
    amount = rpc_quantity(value)
    if amount >= _UINT256:
        raise ValueError("balance exceeds uint256")
    return amount


def _runtime_hash(before: object, after: object) -> str:
    """keccak256 of a token runtime that exists at both pins and did not change."""
    if not isinstance(before, str) or before != after or not before.startswith("0x"):
        raise ValueError("token runtime missing or changed within the block")
    raw = bytes.fromhex(before[2:])
    if not raw or raw.hex() != before[2:].lower():
        raise ValueError("token runtime missing or noncanonical")
    return "0x" + keccak(raw).hex()


@dataclass(frozen=True, slots=True)
class TokenBalances:
    """One scoped token around the block: its pinned runtime, supply and account balance."""

    token: str
    runtime_hash: str
    decimals: int
    supply_before: int
    supply_after: int
    balance_before: int
    balance_after: int


@dataclass(frozen=True, slots=True)
class BlockSettlement:
    """Everything one capture proves; two providers must reconstruct the same value."""

    header: BlockSnapshot
    account: str
    native_before: int
    native_after: int
    tokens: tuple[TokenBalances, ...]
    logs: tuple[LogRecord, ...]  # every Transfer log of the scoped tokens in the block


def _block_logs(
    raw: object, header: BlockSnapshot, tokens: tuple[str, ...]
) -> tuple[LogRecord, ...]:
    """The complete scoped Transfer log list, in canonical order, or a refusal."""
    if not isinstance(raw, list) or len(raw) > MAX_LOGS:
        raise ValueError("block Transfer logs missing or beyond the bound")
    logs = tuple(_log(item) for item in raw)  # canonical fields; refuses removed logs
    if [log.log_index for log in logs] != sorted({log.log_index for log in logs}):
        raise ValueError("block Transfer logs unordered or duplicated")
    positions: dict[str, int] = {}
    owners: dict[int, str] = {}
    last = -1
    for log in logs:
        if (log.block_number, log.block_hash) != (header.ref.number, header.ref.block_hash):
            raise ValueError("Transfer log from another block")
        if log.address not in tokens or log.topics[:1] != (TRANSFER,):
            raise ValueError("log outside the requested token/topic filter")
        position = cast(int, log.transaction_index)  # _log requires it to be present
        if position < last:
            raise ValueError("log order contradicts transaction positions")
        last = position
        if positions.setdefault(log.transaction_hash, position) != position:
            raise ValueError("one transaction at two positions in the block")
        if owners.setdefault(position, log.transaction_hash) != log.transaction_hash:
            raise ValueError("two transactions at one position in the block")
        try:
            _transfer(log)  # a Transfer-topic log that is not a standard ERC-20 Transfer
        except (TypeError, ValueError, DecodingError) as error:
            raise ValueError("nonstandard Transfer log of a scoped token") from error
    return logs


@dataclass(frozen=True, slots=True)
class BlockSettlementCapture:
    """One provider's exact settlement exchanges; every field is re-derived from them."""

    source: str
    header: BlockSnapshot
    account: str
    tokens: tuple[str, ...]
    exchanges: tuple[RpcExchange, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.source, str) or not self.source.strip():
            raise ValueError("settlement source alias required")
        account, tokens = _scope(self.account, self.tokens)
        object.__setattr__(self, "account", account)
        object.__setattr__(self, "tokens", tokens)
        object.__setattr__(self, "exchanges", tuple(self.exchanges))
        self.reconstruct()

    def reconstruct(self) -> BlockSettlement:
        size = sum(len(x.request_payload) + len(x.response_payload) for x in self.exchanges)
        if size > MAX_BYTES:
            raise ValueError("settlement evidence exceeds its size bound")
        results = successful_results(
            self.exchanges, settlement_requests(self.header, self.account, self.tokens)
        )
        ref = self.header.ref
        if rpc_quantity(results[0]) != ref.chain_id:
            raise ValueError("settlement chain mismatch")
        parent = decode_header(results[1], chain_id=ref.chain_id)
        if parent.ref.number != ref.number - 1 or parent.ref.block_hash != self.header.parent_hash:
            raise ValueError("settlement parent block mismatch")
        for index in (2, len(results) - 1):
            if decode_header(results[index], chain_id=ref.chain_id) != self.header:
                raise ValueError("settlement block changed during capture")
        balances = []
        for i, token in enumerate(self.tokens):
            code0, code1, decimals, supply0, supply1, held0, held1 = results[
                5 + i * _PER_TOKEN : 5 + (i + 1) * _PER_TOKEN
            ]
            balances.append(
                TokenBalances(
                    token,
                    _runtime_hash(code0, code1),
                    _uint(decimals, "uint8"),
                    _uint(supply0),
                    _uint(supply1),
                    _uint(held0),
                    _uint(held1),
                )
            )
        return BlockSettlement(
            self.header,
            self.account,
            _quantity(results[3]),
            _quantity(results[4]),
            tuple(balances),
            _block_logs(results[-2], self.header, self.tokens),
        )


@dataclass(frozen=True, slots=True)
class TokenSettlement:
    """One token's balance change across the block and how much of it Transfer logs explain.

    `residual = balance delta - block Transfer net`: zero for a standard token, nonzero
    when balances moved without events (rebasing, reflection, nonstandard tokens).
    `supply_residual` is the same check for mints and burns. Neither is decided here.
    """

    token: str
    runtime_hash: str
    decimals: int
    balance_before: int
    balance_after: int
    balance_delta: int
    block_transfer_net: int
    residual: int
    supply_delta: int
    block_mint_minus_burn: int
    supply_residual: int
    transaction_received: int
    transaction_sent: int
    transaction_net: int
    transaction_logs: int  # this transaction's logs touching the account
    other_transaction_net: int
    other_transaction_logs: int  # other transactions' logs touching the account, any amount


@dataclass(frozen=True, slots=True)
class TransactionSettlement:
    """One transaction's movements for one account inside one reconciled block."""

    header: BlockSnapshot
    account: str
    transaction_hash: str
    transaction_index: int | None  # None: no scoped Transfer log of it in the block
    native_before: int
    native_after: int
    native_delta: int
    tokens: tuple[TokenSettlement, ...]


def transaction_settlement(value: BlockSettlement, transaction_hash: str) -> TransactionSettlement:
    """Split the block's scoped flows into this transaction and all other transactions."""
    tx = normalize_hash32(transaction_hash, "transaction hash")
    account = value.account
    rows: list[TokenSettlement] = []
    positions = {log.transaction_index for log in value.logs if log.transaction_hash == tx}
    for balances in value.tokens:
        block_net = mint_minus_burn = 0
        received = sent = count = others = 0
        for log in value.logs:
            if log.address != balances.token:
                continue
            _, sender, recipient, amount = _transfer(log)
            sender, recipient = normalize_address(sender), normalize_address(recipient)
            signed = (amount if recipient == account else 0) - (amount if sender == account else 0)
            block_net += signed
            if sender == ZERO_ADDRESS:
                mint_minus_burn += amount
            if recipient == ZERO_ADDRESS:
                mint_minus_burn -= amount
            if account not in (sender, recipient):
                continue
            # Counted by touch, not by amount: a zero or self transfer still touches it.
            if log.transaction_hash == tx:
                received += amount if recipient == account else 0
                sent += amount if sender == account else 0
                count += 1
            else:
                others += 1
        delta = balances.balance_after - balances.balance_before
        supply_delta = balances.supply_after - balances.supply_before
        rows.append(
            TokenSettlement(
                balances.token,
                balances.runtime_hash,
                balances.decimals,
                balances.balance_before,
                balances.balance_after,
                delta,
                block_net,
                delta - block_net,
                supply_delta,
                mint_minus_burn,
                supply_delta - mint_minus_burn,
                received,
                sent,
                received - sent,
                count,
                block_net - (received - sent),
                others,
            )
        )
    # _block_logs guarantees one position per transaction hash, so this is 0 or 1 values.
    (position,) = positions or {None}
    return TransactionSettlement(
        value.header,
        account,
        tx,
        position,
        value.native_before,
        value.native_after,
        value.native_after - value.native_before,
        tuple(rows),
    )


SessionFactory = Callable[[str], RecordedSession]


def _collect_one(
    session: RecordedSession, header: BlockSnapshot, account: str, tokens: tuple[str, ...]
) -> BlockSettlementCapture:
    transport, recorder, source = session
    for method, params in settlement_requests(header, account, tokens):
        transport.request(method, params)
    return BlockSettlementCapture(source, header, account, tokens, recorder.exchanges)


def collect_block_settlement_quorum(
    header: BlockSnapshot,
    account: str,
    tokens: Sequence[str],
    *,
    sources: tuple[str, str],
    sessions: SessionFactory,
) -> tuple[BlockSettlementCapture, BlockSettlementCapture]:
    """Two fresh provider sessions issue the same pinned sequence and must agree exactly.

    Agreement covers every balance, runtime hash, decimals, supply and the complete
    log list including each log's position, so an equal net from different rows is a
    disagreement. A failure or disagreement raises; nothing is retained here.
    """
    account, scoped = _scope(account, tokens)
    if len(sources) != 2 or sources[0] == sources[1]:
        raise ValueError("two distinct settlement provider sources required")
    first, second = sessions(sources[0]), sessions(sources[1])
    validate_session_pair(first, second)
    if (first[2], second[2]) != sources:
        raise ValueError("settlement sessions differ from the declared providers")
    captures = (
        _collect_one(first, header, account, scoped),
        _collect_one(second, header, account, scoped),
    )
    if captures[0].reconstruct() != captures[1].reconstruct():
        raise ValueError("providers disagree on the block settlement")
    return captures


def _payload(capture: BlockSettlementCapture) -> bytes:
    value = capture.reconstruct()
    return canonical_json(
        {
            "schema": SCHEMA,
            # Who observed it: two agreeing providers are still two distinct records.
            "source": capture.source,
            "block": asdict(value.header.ref),
            "parent_hash": value.header.parent_hash,
            "account": value.account,
            "native": {"before": str(value.native_before), "after": str(value.native_after)},
            "tokens": [
                {k: str(v) if isinstance(v, int) else v for k, v in asdict(t).items()}
                for t in value.tokens
            ],
            "transfer_logs": len(value.logs),
            "transfer_logs_sha256": hashlib.sha256(
                canonical_json([asdict(log) for log in value.logs])
            ).hexdigest(),
            "execution_eligible": False,
        }
    )


def persist_block_settlement(
    store: EvidenceStore,
    capture: BlockSettlementCapture,
    *,
    origin: dict[str, object] | None = None,
) -> EvidenceRef:
    """Retain exact wire bytes; `origin`, when given, is kept in the hash-bound interpretation."""
    return store.put(
        KIND,
        _payload(capture),
        capture.header.ref,
        capture.source,
        request=wire_envelope(capture.exchanges, response=False),
        raw_response=wire_envelope(capture.exchanges, response=True),
        interpretation={
            "scope": "one_account_native_and_scoped_erc20_across_one_block",
            "tokens": list(capture.tokens),
            "parent_hash": capture.header.parent_hash,
            "execution_eligible": False,
            **({} if origin is None else {"origin": origin}),
        },
    )


def replay_block_settlement(
    store: EvidenceStore,
    evidence: EvidenceRef,
    *,
    header: BlockSnapshot,
    account: str,
    tokens: Sequence[str],
) -> BlockSettlementCapture:
    """Rebuild one retained capture from its own wire bytes for an expected scope."""
    account, scoped = _scope(account, tokens)
    if evidence.kind != KIND or evidence.block != header.ref:
        raise ValueError("settlement evidence kind or block mismatch")
    record = store.get_record(evidence)
    if record.request_payload is None:
        raise ValueError("settlement evidence missing wire requests")
    capture = BlockSettlementCapture(
        evidence.source,
        header,
        account,
        scoped,
        restore_exchanges(record.request_payload, record.raw_response_payload),
    )
    if _payload(capture) != record.payload:
        raise ValueError("settlement payload differs from its wire reconstruction")
    return capture
