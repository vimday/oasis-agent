"""Two-provider pinned account inventory: native, declared ERC-20 and declared positions.

Raw capture, offline replay and nothing else. Balances, position ownership and
USD feed answers are re-derived from exact retained RPC bytes at one canonical
block, so a manifest that disagrees with the chain fails closed. A declared
position contributes one unit exactly when `ownerOf` names the reconciled
account, which makes ownership a captured fact rather than an assumption. This is
not NAV, not settlement and never an execution input by itself.
"""

import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from typing import cast

from ..evidence.exact_capture import (
    RecordedSession,
    decode_header,
    require_code_hash,
    successful_results,
    validate_session_pair,
    wire_envelope,
)
from ..evidence.rpc_validation import restore_exchanges, rpc_quantity, strict_json_object
from ..evidence.store import EvidenceStore
from ..models.chain import BlockSnapshot
from ..models.common import EvidenceRef, canonical_json, normalize_address
from ..risk.portfolio_manifest import ChainInventory
from ..rpc.client import RpcClient
from ..rpc.transport import RpcExchange
from .owner_payments import _abi
from .position_settlement import _text
from .price_evidence import selector

DECIMALS_SELECTOR = selector("decimals()")
BALANCE_OF_SELECTOR = selector("balanceOf(address)")
OWNER_OF_SELECTOR = selector("ownerOf(uint256)")
FEED_SIGNATURES = ("decimals()", "description()", "latestRoundData()")
_SCHEMA = "oasis.account-inventory.v1"


def balance_of_data(account: str) -> str:
    return BALANCE_OF_SELECTOR + account[2:].rjust(64, "0")


def owner_of_data(token_id: int) -> str:
    if type(token_id) is not int or not 0 < token_id < 2**256:
        raise ValueError("position token id must be a bounded unsigned integer")
    return OWNER_OF_SELECTOR + f"{token_id:064x}"


@dataclass(frozen=True, slots=True)
class AssetUnits:
    asset_id: str
    units: int

    def __post_init__(self) -> None:
        if not self.asset_id or type(self.units) is not int or not 0 <= self.units < 2**256:
            raise ValueError("canonical uint256 asset balance required")


@dataclass(frozen=True, slots=True)
class FeedAnswer:
    """One complete, fresh oracle round pinned to the inventory block."""

    asset_id: str
    answer: int
    decimals: int
    round_id: int
    updated_at: int

    def __post_init__(self) -> None:
        if self.answer <= 0 or not 0 <= self.decimals <= 36:
            raise ValueError("positive feed answer and bounded decimals required")
        if self.round_id <= 0 or self.updated_at < 0:
            raise ValueError("complete oracle round identity required")

    @property
    def usd_micros_numerator(self) -> int:
        return self.answer * 1_000_000

    @property
    def price_denominator(self) -> int:
        value: int = 10**self.decimals
        return value


def _header_json(header: BlockSnapshot) -> dict[str, object]:
    ref = header.ref
    assert ref.timestamp is not None
    return {
        "number": hex(ref.number),
        "hash": ref.block_hash,
        "timestamp": hex(ref.timestamp),
        "parentHash": header.parent_hash,
    }


def _expected(entry: ChainInventory, header: BlockSnapshot) -> list[tuple[str, list[object]]]:
    block = header.ref
    pin: dict[str, object] = {"blockHash": block.block_hash, "requireCanonical": True}
    expected: list[tuple[str, list[object]]] = [
        ("eth_chainId", []),
        ("eth_getBlockByNumber", [hex(block.number), False]),
        ("eth_getBalance", [entry.account, pin]),
    ]
    for asset in entry.erc20_assets:
        expected.extend(
            [
                ("eth_getCode", [asset.address, pin]),
                ("eth_call", [{"to": asset.address, "data": DECIMALS_SELECTOR}, pin]),
                ("eth_call", [{"to": asset.address, "data": balance_of_data(entry.account)}, pin]),
            ]
        )
    for position in entry.positions:
        owner_call = {"to": position.position_manager, "data": owner_of_data(position.token_id)}
        expected.extend(
            [("eth_getCode", [position.position_manager, pin]), ("eth_call", [owner_call, pin])]
        )
    for asset in entry.assets:
        expected.append(("eth_getCode", [asset.feed.address, pin]))
        expected.extend(
            ("eth_call", [{"to": asset.feed.address, "data": selector(name)}, pin])
            for name in FEED_SIGNATURES
        )
    expected.append(("eth_getBlockByNumber", [hex(block.number), False]))
    return expected


def _feed_round(raw: str, *, block_timestamp: int, max_age_seconds: int) -> tuple[int, int, int]:
    """Validate one complete, fresh oracle round and return (answer, round, updated)."""
    round_id, answer, started, updated, answered = _abi(
        ["uint80", "int256", "uint256", "uint256", "uint80"], raw
    )
    r, a, s, u, ar = (
        int(value)
        for value in cast(
            tuple[int, int, int, int, int], (round_id, answer, started, updated, answered)
        )
    )
    if r == 0 or a <= 0 or not 0 < s <= u or ar < r:
        raise ValueError("invalid, stale, future or incomplete oracle round")
    if not 0 <= block_timestamp - u <= max_age_seconds:
        raise ValueError("stale or future inventory valuation")
    return a, r, u


@dataclass(frozen=True, slots=True)
class AccountInventoryCapture:
    """One provider's exact bytes for a manifest account at one block."""

    source: str
    header: BlockSnapshot
    entry: ChainInventory
    exchanges: tuple[RpcExchange, ...]
    units: tuple[AssetUnits, ...] = field(init=False, repr=False, default=())
    feeds: tuple[FeedAnswer, ...] = field(init=False, repr=False, default=())

    def __post_init__(self) -> None:
        object.__setattr__(self, "exchanges", tuple(self.exchanges))
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", self.source):
            raise ValueError("inventory provider name is invalid")
        units, feeds = self.reconstruct()
        object.__setattr__(self, "units", units)
        object.__setattr__(self, "feeds", feeds)

    @property
    def payload(self) -> bytes:
        return canonical_json(
            {
                "schema": _SCHEMA,
                "source": self.source,
                "header": _header_json(self.header),
                "chain_id": self.entry.chain_id,
                "account": self.entry.account,
                "units": [asdict(unit) for unit in self.units],
                "feeds": [asdict(feed) for feed in self.feeds],
            }
        )

    def reconstruct(self) -> tuple[tuple[AssetUnits, ...], tuple[FeedAnswer, ...]]:
        entry = self.entry
        ref = self.header.ref
        if ref.timestamp is None or ref.chain_id != entry.chain_id:
            raise ValueError("inventory block must be timestamped on the manifest chain")
        results = successful_results(self.exchanges, _expected(entry, self.header))
        if rpc_quantity(results[0]) != entry.chain_id:
            raise ValueError("inventory provider chain mismatch")
        if decode_header(results[1], chain_id=entry.chain_id) != self.header:
            raise ValueError("inventory opening header mismatch")
        cursor = 2
        units = [AssetUnits(entry.native.asset_id, rpc_quantity(results[cursor]))]
        cursor += 1
        for asset in entry.erc20_assets:
            assert asset.runtime_keccak is not None
            require_code_hash(results[cursor], asset.runtime_keccak)
            cursor += 1
            decimals = cast(int, _abi(["uint8"], _text(results[cursor]))[0])
            cursor += 1
            if decimals != asset.decimals:
                raise ValueError("inventory token decimals mismatch")
            balance = cast(int, _abi(["uint256"], _text(results[cursor]))[0])
            units.append(AssetUnits(asset.asset_id, int(balance)))
            cursor += 1
        for position in entry.positions:
            require_code_hash(results[cursor], position.position_manager_runtime_keccak)
            cursor += 1
            owner = normalize_address(cast(str, _abi(["address"], _text(results[cursor]))[0]))
            cursor += 1
            units.append(AssetUnits(position.asset_id, 1 if owner == entry.account else 0))
        feeds: list[FeedAnswer] = []
        for asset in entry.assets:
            require_code_hash(results[cursor], asset.feed.runtime_keccak)
            cursor += 1
            feed_decimals = cast(int, _abi(["uint8"], _text(results[cursor]))[0])
            cursor += 1
            if not 0 <= feed_decimals <= 36:
                raise ValueError("inventory feed decimals out of range")
            if _abi(["string"], _text(results[cursor]))[0] != asset.feed.description:
                raise ValueError("inventory feed description mismatch")
            cursor += 1
            answer, round_id, updated_at = _feed_round(
                _text(results[cursor]),
                block_timestamp=ref.timestamp,
                max_age_seconds=asset.feed.max_age_seconds,
            )
            cursor += 1
            feeds.append(FeedAnswer(asset.asset_id, answer, feed_decimals, round_id, updated_at))
        if decode_header(results[cursor], chain_id=entry.chain_id) != self.header:
            raise ValueError("inventory closing header mismatch")
        return tuple(units), tuple(feeds)


@dataclass(frozen=True, slots=True)
class AccountInventoryQuorum:
    left: AccountInventoryCapture
    right: AccountInventoryCapture

    def __post_init__(self) -> None:
        if self.left.source == self.right.source or self.left.entry != self.right.entry:
            raise ValueError("inventory quorum requires distinct providers of one manifest entry")
        if self.left.header != self.right.header:
            raise ValueError("inventory providers disagree on the block")
        if self.left.units != self.right.units or self.left.feeds != self.right.feeds:
            raise ValueError("inventory providers disagree on balances or valuations")

    @property
    def header(self) -> BlockSnapshot:
        return self.left.header

    @property
    def units(self) -> tuple[AssetUnits, ...]:
        return self.left.units

    @property
    def feeds(self) -> tuple[FeedAnswer, ...]:
        return self.left.feeds


def _collect_one(
    session: RecordedSession, *, entry: ChainInventory, block_number: int
) -> AccountInventoryCapture:
    transport, recorder, source = session
    client = RpcClient(transport, expected_chain_id=entry.chain_id)
    opening = client.get_block_snapshot(block_number)
    if opening.ref.number != block_number:
        raise ValueError("inventory opened the wrong block")
    pin: dict[str, object] = {"blockHash": opening.ref.block_hash, "requireCanonical": True}
    transport.request("eth_getBalance", [entry.account, pin])
    for asset in entry.erc20_assets:
        transport.request("eth_getCode", [asset.address, pin])
        transport.request("eth_call", [{"to": asset.address, "data": DECIMALS_SELECTOR}, pin])
        transport.request(
            "eth_call", [{"to": asset.address, "data": balance_of_data(entry.account)}, pin]
        )
    for position in entry.positions:
        transport.request("eth_getCode", [position.position_manager, pin])
        transport.request(
            "eth_call",
            [{"to": position.position_manager, "data": owner_of_data(position.token_id)}, pin],
        )
    for asset in entry.assets:
        transport.request("eth_getCode", [asset.feed.address, pin])
        for name in FEED_SIGNATURES:
            transport.request("eth_call", [{"to": asset.feed.address, "data": selector(name)}, pin])
    if client.get_block_snapshot(block_number) != opening:
        raise ValueError("inventory block changed during collection")
    return AccountInventoryCapture(source, opening, entry, recorder.exchanges)


def collect_account_inventory_quorum(
    primary: RecordedSession,
    secondary: RecordedSession,
    *,
    entry: ChainInventory,
    block_number: int,
) -> AccountInventoryQuorum:
    validate_session_pair(primary, secondary)
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="account-inventory") as workers:
        left = workers.submit(_collect_one, primary, entry=entry, block_number=block_number)
        right = workers.submit(_collect_one, secondary, entry=entry, block_number=block_number)
        return AccountInventoryQuorum(left.result(), right.result())


def _persist_capture(
    store: EvidenceStore, capture: AccountInventoryCapture, *, retrieved_at: str | None
) -> EvidenceRef:
    return store.put(
        "account_inventory",
        capture.payload,
        capture.header.ref,
        capture.source,
        request=wire_envelope(capture.exchanges, response=False),
        raw_response=wire_envelope(capture.exchanges, response=True),
        interpretation={
            "schema": "oasis.account-inventory-evidence.v1",
            "execution_eligible": False,
        },
        retrieved_at=retrieved_at,
        schema_version=1,
    )


def persist_account_inventory_evidence(
    store: EvidenceStore,
    quorum: AccountInventoryQuorum,
    *,
    retrieved_at: str | None = None,
) -> tuple[EvidenceRef, EvidenceRef]:
    if (
        quorum.left.header != quorum.right.header
        or quorum.left.units != quorum.right.units
        or quorum.left.feeds != quorum.right.feeds
    ):
        raise ValueError("inventory quorum changed before persistence")
    return (
        _persist_capture(store, quorum.left, retrieved_at=retrieved_at),
        _persist_capture(store, quorum.right, retrieved_at=retrieved_at),
    )


def _bound_header(payload: bytes, entry: ChainInventory) -> BlockSnapshot:
    data = strict_json_object(payload)
    if (
        set(data) != {"schema", "source", "header", "chain_id", "account", "units", "feeds"}
        or data["schema"] != _SCHEMA
    ):
        raise ValueError("inventory payload has invalid fields or schema")
    if type(data["chain_id"]) is not int or data["chain_id"] != entry.chain_id:
        raise ValueError("inventory payload chain does not match the manifest")
    if data["account"] != entry.account:
        raise ValueError("inventory payload account does not match the manifest")
    return decode_header(data["header"], chain_id=entry.chain_id)


def replay_account_inventory_evidence(
    store: EvidenceStore, reference: EvidenceRef, *, entry: ChainInventory
) -> AccountInventoryCapture:
    if reference.kind != "account_inventory" or reference.schema_version != 1:
        raise ValueError("unsupported account inventory evidence")
    record = store.get_record(reference)
    if record.request_payload is None:
        raise ValueError("account inventory evidence is missing raw requests")
    header = _bound_header(record.payload, entry)
    capture = AccountInventoryCapture(
        reference.source,
        header,
        entry,
        restore_exchanges(record.request_payload, record.raw_response_payload),
    )
    if capture.payload != record.payload or reference.block != header.ref:
        raise ValueError("inventory interpretation differs from raw reconstruction")
    return capture


__all__ = [
    "AccountInventoryCapture",
    "AccountInventoryQuorum",
    "AssetUnits",
    "FeedAnswer",
    "balance_of_data",
    "collect_account_inventory_quorum",
    "owner_of_data",
    "persist_account_inventory_evidence",
    "replay_account_inventory_evidence",
]
