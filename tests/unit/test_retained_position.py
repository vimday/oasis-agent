"""Declared-position valuation: two agreeing providers, pinned identity, refusal on drift.

The valuation may enter an account frame, so these tests attack the bindings
rather than the arithmetic: a proof from an undeclared provider, a second
provider that saw a different position, a pool state window that misses the
position's block, a pool or token from another declaration, and a stale feed
round must each refuse instead of silently revaluing the account.
"""

import json
from dataclasses import replace
from pathlib import Path

import pytest
from eth_abi.abi import encode
from eth_hash.auto import keccak

from oasis_agent.evidence.rpc_capture import BoundedExchangeRecorder
from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.execution.retained_position import RetainedPosition, value_retained_position
from oasis_agent.farming.account_inventory import FeedAnswer
from oasis_agent.farming.position_capture import (
    PositionSnapshotCapture,
    collect_position_snapshot,
    persist_position_snapshot_evidence,
    replay_position_snapshot_evidence,
)
from oasis_agent.farming.position_quorum import PositionQuorum
from oasis_agent.farming.position_valuation import CurrencyPrice, value_position
from oasis_agent.farming.positions import PositionSnapshot, position_id
from oasis_agent.models.common import BlockRef, canonical_json
from oasis_agent.models.v4 import DYNAMIC_FEE_FLAG, PoolKey
from oasis_agent.rpc.client import RpcClient
from oasis_agent.rpc.transport import JsonRpcTransport
from oasis_agent.shadow.pool_state_capture import (
    collect_slot0_window_quorum,
    persist_slot0_window_evidence,
)
from oasis_agent.uniswap_v4.liquidity_math import sqrt_price_at_tick
from oasis_agent.uniswap_v4.pool_id import pool_id_from_key
from oasis_agent.uniswap_v4.state_rpc import Slot0State

CHAIN_ID = 4663
BLOCK_NUMBER = 10
BLOCK_HASH = "0x" + "aa" * 32
PARENT_HASH = "0x" + "99" * 32
TIMESTAMP = 1_000
NEXT_BLOCK = 11
NEXT_HASH = "0x" + "bb" * 32
POSITION_MANAGER = "0x" + "22" * 20
STATE_VIEW = "0x" + "99" * 20
POOL_MANAGER = "0x" + "11" * 20
WETH = "0x" + "55" * 20
USDC = "0x" + "66" * 20
HOOKS = "0x" + "77" * 20
OWNER = "0x" + "3a" * 20
TOKEN_ID = 7
LIQUIDITY = 10**24
TICK_LOWER = -120
TICK_UPPER = 120
LP_FEE = 3_000
STATE_CODE = "6000"
MANAGER_CODE = "6001"
STATE_HASH = "0x" + keccak(bytes.fromhex(STATE_CODE)).hex()
MANAGER_HASH = "0x" + keccak(bytes.fromhex(MANAGER_CODE)).hex()
OTHER_HASH = "0x" + "5a" * 32
FEED_DECIMALS = 8
MAX_AGE = 300
WETH_ANSWER = 200_000_000_000
USDC_ANSWER = 100_000_000
PROVIDERS = frozenset({"rpc-a", "rpc-b"})


def reply(request: dict[str, object], result: object) -> bytes:
    return canonical_json({"jsonrpc": "2.0", "id": request["id"], "result": result})


def pool_key() -> PoolKey:
    return PoolKey(WETH, USDC, DYNAMIC_FEE_FLAG, 60, HOOKS)


def position_snapshot(*, liquidity: int = LIQUIDITY) -> PositionSnapshot:
    key = pool_key()
    return PositionSnapshot(
        BlockRef(CHAIN_ID, BLOCK_NUMBER, BLOCK_HASH, TIMESTAMP),
        POSITION_MANAGER,
        TOKEN_ID,
        OWNER,
        POSITION_MANAGER,
        key,
        pool_id_from_key(key),
        position_id(POSITION_MANAGER, TICK_LOWER, TICK_UPPER, TOKEN_ID),
        TICK_LOWER,
        TICK_UPPER,
        False,
        liquidity,
        0,
        0,
        0,
        0,
        0,
        0,
    )


def position_capture(source: str, snapshot: PositionSnapshot) -> PositionSnapshotCapture:
    """One hash-pinned position session whose raw calls reconstruct `snapshot`."""
    key = snapshot.pool_key
    packed = (
        (int(snapshot.pool_id[:52], 16) << 56)
        | ((snapshot.tick_lower & 0xFFFFFF) << 8)
        | ((snapshot.tick_upper & 0xFFFFFF) << 32)
    )
    values = {
        "ownerOf(uint256)": (["address"], [snapshot.nft_owner]),
        "getPoolAndPositionInfo(uint256)": (
            ["(address,address,uint24,int24,address)", "uint256"],
            [(key.currency0, key.currency1, key.fee, key.tick_spacing, key.hooks), packed],
        ),
        "getPositionLiquidity(uint256)": (["uint128"], [snapshot.liquidity]),
        "getPositionInfo(bytes32,bytes32)": (
            ["uint128", "uint256", "uint256"],
            [snapshot.liquidity, 0, 0],
        ),
        "getFeeGrowthInside(bytes32,int24,int24)": (["uint256", "uint256"], [0, 0]),
    }
    results = {
        "0x" + keccak(signature.encode())[:4].hex(): "0x" + encode(*value).hex()
        for signature, value in values.items()
    }

    class Poster:
        def post(self, payload: bytes) -> bytes:
            request = json.loads(payload)
            method = request["method"]
            if method == "eth_chainId":
                return reply(request, hex(snapshot.block.chain_id))
            if method == "eth_getBlockByNumber":
                return reply(
                    request,
                    {
                        "number": hex(snapshot.block.number),
                        "hash": snapshot.block.block_hash,
                        "timestamp": hex(snapshot.block.timestamp),
                        "parentHash": PARENT_HASH,
                    },
                )
            assert method == "eth_call"
            return reply(request, results[request["params"][0]["data"][:10]])

    recorder = BoundedExchangeRecorder()
    rpc = RpcClient(JsonRpcTransport("https://fixture.invalid", http=Poster(), recorder=recorder))
    header, collected = collect_position_snapshot(
        rpc,
        position_manager=POSITION_MANAGER,
        state_view=STATE_VIEW,
        token_id=TOKEN_ID,
        block_number=BLOCK_NUMBER,
    )
    assert collected == snapshot
    return PositionSnapshotCapture(source, header, STATE_VIEW, collected, recorder.exchanges)


class Slot0Poster:
    """One pinned slot0 session; it answers the exact call set the capture expects."""

    def __init__(self, *, number: int, block_hash: str, timestamp: int, manager_bad: bool) -> None:
        self.number = number
        self.block_hash = block_hash
        self.timestamp = timestamp
        self.manager_bad = manager_bad

    def post(self, payload: bytes) -> bytes:
        request = json.loads(payload)
        method, params = request["method"], request["params"]
        if method == "eth_chainId":
            return reply(request, hex(CHAIN_ID))
        if method == "eth_getBlockByNumber":
            assert params == [hex(self.number), False]
            return reply(
                request,
                {
                    "number": hex(self.number),
                    "hash": self.block_hash,
                    "parentHash": PARENT_HASH,
                    "timestamp": hex(self.timestamp),
                },
            )
        pin = {"blockHash": self.block_hash, "requireCanonical": True}
        if method == "eth_getCode":
            assert params[1] == pin
            return reply(request, "0x" + (STATE_CODE if params[0] == STATE_VIEW else MANAGER_CODE))
        assert method == "eth_call"
        assert params[1] == pin
        call = params[0]
        if call["data"] == "0x" + keccak(b"poolManager()")[:4].hex():
            address = STATE_VIEW if self.manager_bad else POOL_MANAGER
            return reply(request, "0x" + "00" * 12 + address[2:])
        assert call["to"] == STATE_VIEW
        assert call["data"].startswith("0x" + keccak(b"getSlot0(bytes32)")[:4].hex())
        return reply(
            request,
            "0x"
            + encode(
                ["uint160", "int24", "uint24", "uint24"],
                [sqrt_price_at_tick(0), 0, 0, LP_FEE],
            ).hex(),
        )


def slot0_session(
    source: str, **overrides: object
) -> tuple[JsonRpcTransport, BoundedExchangeRecorder, str]:
    options: dict[str, object] = {
        "number": BLOCK_NUMBER,
        "block_hash": BLOCK_HASH,
        "timestamp": TIMESTAMP,
        "manager_bad": False,
    }
    options.update(overrides)
    recorder = BoundedExchangeRecorder()
    return (
        JsonRpcTransport("https://fixture.invalid", http=Slot0Poster(**options), recorder=recorder),
        recorder,
        source,
    )


def slot0_state() -> Slot0State:
    return Slot0State(sqrt_price_at_tick(0), 0, 0, LP_FEE)


def feed(asset_id: str, answer: int, *, updated_at: int = TIMESTAMP) -> FeedAnswer:
    return FeedAnswer(asset_id, answer, FEED_DECIMALS, 1, updated_at)


def price(
    address: str, asset_id: str, answer: int, *, decimals: int, **overrides: object
) -> CurrencyPrice:
    options: dict[str, object] = {"updated_at": TIMESTAMP, "max_age": MAX_AGE}
    options.update(overrides)
    return CurrencyPrice(
        address,
        asset_id,
        decimals,
        feed(asset_id, answer, updated_at=options["updated_at"]),
        options["max_age"],
    )


def weth_price(**overrides: object) -> CurrencyPrice:
    return price(WETH, "weth", WETH_ANSWER, decimals=18, **overrides)


def usdc_price(**overrides: object) -> CurrencyPrice:
    return price(USDC, "usdc", USDC_ANSWER, decimals=6, **overrides)


def retained(**overrides: object) -> RetainedPosition:
    options: dict[str, object] = {
        "chain_id": CHAIN_ID,
        "position_manager": POSITION_MANAGER,
        "state_view": STATE_VIEW,
        "pool_id": pool_id_from_key(pool_key()),
        "token_id": TOKEN_ID,
        "state_view_runtime_keccak": STATE_HASH,
        "pool_manager_runtime_keccak": MANAGER_HASH,
    }
    options.update(overrides)
    return RetainedPosition(**options)


def retain_snapshots(store: EvidenceStore, snapshot: PositionSnapshot | None = None):
    left = position_capture("rpc-a", snapshot if snapshot is not None else position_snapshot())
    right = position_capture("rpc-b", snapshot if snapshot is not None else position_snapshot())
    return PositionQuorum(left, right).retain(store)


def retain_pool_state(store: EvidenceStore, **overrides: object):
    quorum = collect_slot0_window_quorum(
        slot0_session("rpc-a", **overrides),
        slot0_session("rpc-b", **overrides),
        chain_id=CHAIN_ID,
        state_view=STATE_VIEW,
        pool_manager=POOL_MANAGER,
        pool_id=pool_id_from_key(pool_key()),
        block_numbers=(overrides.get("number", BLOCK_NUMBER),),
        state_view_runtime_keccak=STATE_HASH,
        pool_manager_runtime_keccak=MANAGER_HASH,
    )
    return persist_slot0_window_evidence(store, quorum)


def value(store: EvidenceStore, snapshots, pool_state, **overrides: object):
    options: dict[str, object] = {
        "retained": retained(),
        "currency0": weth_price(),
        "currency1": usdc_price(),
        "providers": PROVIDERS,
    }
    options.update(overrides)
    return value_retained_position(store, snapshots=snapshots, pool_state=pool_state, **options)  # type: ignore[arg-type]


def test_a_declared_position_is_valued_from_two_agreeing_providers(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path / "position.db")
    try:
        snapshots = retain_snapshots(store)
        pool_state = retain_pool_state(store)
        valuation = value(store, snapshots, pool_state)
        replayed = replay_position_snapshot_evidence(store, snapshots[0]).snapshot
        assert valuation == value_position(
            replayed, slot0_state(), currency0=weth_price(), currency1=usdc_price()
        )
        assert valuation.usd_micros > 0
        assert valuation.pool_id == pool_id_from_key(pool_key())
        assert valuation.token_id == TOKEN_ID
        assert valuation.block == replayed.block
    finally:
        store.close()


def test_a_proof_from_an_undeclared_provider_is_refused(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path / "provider.db")
    try:
        snapshots = retain_snapshots(store)
        pool_state = retain_pool_state(store)
        with pytest.raises(ValueError, match="exactly the declared providers"):
            value(store, snapshots, pool_state, providers=frozenset({"rpc-a", "rpc-c"}))
        with pytest.raises(ValueError, match="exactly two declared providers"):
            value(store, snapshots, pool_state, providers=frozenset({"rpc-a"}))
    finally:
        store.close()


def test_a_second_provider_that_saw_another_position_is_refused(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path / "disagreement.db")
    try:
        left = position_capture("rpc-a", position_snapshot())
        right = position_capture("rpc-b", position_snapshot(liquidity=2 * LIQUIDITY))
        snapshots = (
            persist_position_snapshot_evidence(
                store, left, retrieved_at="2026-01-01T00:00:00+00:00"
            ),
            persist_position_snapshot_evidence(
                store, right, retrieved_at="2026-01-01T00:00:00+00:00"
            ),
        )
        pool_state = retain_pool_state(store)
        with pytest.raises(ValueError, match="two position providers disagree"):
            value(store, snapshots, pool_state)
    finally:
        store.close()


@pytest.mark.parametrize(
    "overrides,match",
    [
        ({"token_id": TOKEN_ID + 1}, "token id differs"),
        ({"chain_id": 8453}, "not on the declared chain"),
        ({"position_manager": HOOKS}, "position manager differs"),
        ({"state_view": HOOKS}, "state view differs"),
        ({"pool_id": OTHER_HASH}, "pool differs"),
    ],
)
def test_a_position_that_does_not_match_the_declaration_is_refused(
    tmp_path: Path, overrides: dict[str, object], match: str
) -> None:
    store = EvidenceStore(tmp_path / "identity.db")
    try:
        snapshots = retain_snapshots(store)
        pool_state = retain_pool_state(store)
        with pytest.raises(ValueError, match=match):
            value(store, snapshots, pool_state, retained=retained(**overrides))
    finally:
        store.close()


@pytest.mark.parametrize(
    "overrides,match",
    [
        ({"state_view_runtime_keccak": OTHER_HASH}, "state view runtime hash"),
        ({"pool_manager_runtime_keccak": OTHER_HASH}, "pool manager runtime hash"),
    ],
)
def test_a_runtime_hash_that_differs_from_the_declaration_is_refused(
    tmp_path: Path, overrides: dict[str, object], match: str
) -> None:
    store = EvidenceStore(tmp_path / "runtime.db")
    try:
        snapshots = retain_snapshots(store)
        pool_state = retain_pool_state(store)
        with pytest.raises(ValueError, match=match):
            value(store, snapshots, pool_state, retained=retained(**overrides))
    finally:
        store.close()


def test_a_pool_state_window_without_the_position_block_is_refused(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path / "window.db")
    try:
        snapshots = retain_snapshots(store)
        pool_state = retain_pool_state(store, number=NEXT_BLOCK, block_hash=NEXT_HASH)
        with pytest.raises(ValueError, match="does not cover the position block"):
            value(store, snapshots, pool_state)
    finally:
        store.close()


def test_two_copies_of_one_proof_cannot_form_a_pair(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path / "duplicate.db")
    try:
        snapshots = retain_snapshots(store)
        pool_state = retain_pool_state(store)
        with pytest.raises(ValueError, match="two distinct retained proofs"):
            value(store, (snapshots[0], snapshots[0]), pool_state)
        with pytest.raises(ValueError, match="two distinct retained proofs"):
            value(store, snapshots, (pool_state[1], pool_state[1]))
    finally:
        store.close()


def test_a_proof_of_another_kind_is_refused(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path / "kind.db")
    try:
        snapshots = retain_snapshots(store)
        pool_state = retain_pool_state(store)
        with pytest.raises(ValueError, match="retained proof kind must be position_snapshot"):
            value(store, pool_state, pool_state)
        with pytest.raises(ValueError, match="retained proof kind must be slot0_window"):
            value(store, snapshots, snapshots)
    finally:
        store.close()


def test_a_feed_round_stale_at_the_position_block_is_refused(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path / "stale.db")
    try:
        snapshots = retain_snapshots(store)
        pool_state = retain_pool_state(store)
        with pytest.raises(ValueError, match="stale or future currency valuation"):
            value(
                store,
                snapshots,
                pool_state,
                currency1=usdc_price(updated_at=TIMESTAMP - MAX_AGE - 1),
            )
    finally:
        store.close()


def test_a_position_valuation_requires_its_own_identity_object(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path / "type.db")
    try:
        snapshots = retain_snapshots(store)
        pool_state = retain_pool_state(store)
        with pytest.raises(TypeError, match="retained position identity required"):
            value(store, snapshots, pool_state, retained="position")
        with pytest.raises(ValueError, match="explicit bounded chain"):
            replace(retained(), chain_id=0)
        with pytest.raises(ValueError, match="bounded token id"):
            replace(retained(), token_id=0)
    finally:
        store.close()


def test_an_expected_owner_binds_ownership_to_the_same_replay(tmp_path: Path) -> None:
    """Ownership may be asserted from the snapshot itself; case is not identity."""
    store = EvidenceStore(tmp_path / "owner.db")
    try:
        snapshots = retain_snapshots(store)
        pool_state = retain_pool_state(store)
        assert value(store, snapshots, pool_state, owner=OWNER) == value(
            store, snapshots, pool_state
        )
        assert value(store, snapshots, pool_state, owner="0x" + OWNER[2:].upper()).usd_micros > 0
        with pytest.raises(ValueError, match="not owned by the expected account"):
            value(store, snapshots, pool_state, owner=HOOKS)
    finally:
        store.close()
