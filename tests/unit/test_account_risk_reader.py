"""Evidence-replay account reader: complete frames, worst-case commitment, fail-closed.

The reader is the only bridge between retained raw proofs and the global risk
ledger. These tests drive it with two-provider fixtures, so a missing provider,
a disagreeing transfer set, an unexplained movement, a non-midnight day
boundary, a stale frame, a skewed cross-chain snapshot, an undeclared pool
currency or malformed calldata each refuse admission instead of becoming a
permissive default. The last tests bind the reader to the real signer guard.
"""

import json
from dataclasses import dataclass, field, replace

import pytest
from eth_abi.abi import encode
from eth_hash.auto import keccak

from oasis_agent.evidence.rpc_capture import BoundedExchangeRecorder
from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.execution.account_risk_guard import AccountRiskGuard
from oasis_agent.execution.account_risk_reader import (
    AccountObservation,
    AssetFlowEvidence,
    EvidenceReplayAccountRiskReader,
    PositionEvidence,
)
from oasis_agent.farming.account_flows import (
    FlowScope,
    collect_token_flow_quorum,
    persist_token_flow_evidence,
)
from oasis_agent.farming.account_inventory import (
    DECIMALS_SELECTOR,
    balance_of_data,
    collect_account_inventory_quorum,
    owner_of_data,
    persist_account_inventory_evidence,
)
from oasis_agent.farming.inventory import PositionInventory, position_inventory
from oasis_agent.farming.position_quorum import collect_position_quorum
from oasis_agent.farming.positions import PositionSnapshot, position_id
from oasis_agent.farming.price_evidence import selector, value_usd_micros
from oasis_agent.models.common import ZERO_ADDRESS, BlockRef, canonical_json
from oasis_agent.models.v4 import PoolKey
from oasis_agent.risk.account_risk import (
    GlobalRiskJournal,
    GlobalRiskPolicy,
    PortfolioFrame,
    RiskState,
    reconcile,
)
from oasis_agent.risk.portfolio_manifest import (
    ChainInventory,
    ManifestAsset,
    ManifestFeed,
    ManifestPosition,
    PortfolioManifest,
)
from oasis_agent.rpc.transport import JsonRpcTransport
from oasis_agent.shadow.pool_state_capture import (
    collect_slot0_window_quorum,
    persist_slot0_window_evidence,
)
from oasis_agent.uniswap_v4.liquidity_math import sqrt_price_at_tick
from oasis_agent.uniswap_v4.pool_id import pool_id_from_key
from tests.unit.test_account_flows import FlowFixture, erc721_transfer_log, transfer_log
from tests.unit.test_lp_signer_sidecar import ACCOUNT, make_policy, make_request

CHAIN_ID = 4663
BLOCK_NUMBER = 101
BLOCK_HASH = "0x" + "cd" * 32
BLOCK_TWO = 102
HASH_TWO = "0x" + "ce" * 32
TIMESTAMP = 1_010
PARENT_HASH = "0x" + "ee" * 32
FEED_MAX_AGE = 300
MAX_SKEW = 60
NATIVE_ANSWER = 240_000_000_000
USDC_ANSWER = 100_000_000
WETH_ANSWER = 240_000_000_000
NATIVE_UNITS = 3 * 10**18
USDC_UNITS = 1_000_000_000
WETH_UNITS = 2 * 10**18
THIRD_PARTY = "0x" + "e9" * 20
SECOND_ACCOUNT = "0x" + "ea" * 20
ASSET_IDS = ("native", "usdc", "weth")
CID_MIDNIGHT = 1_800_028_800
DAY_START = 1_800_000_000

POSITION_MANAGER = "0x" + "21" * 20
POSITION_MANAGER_CODE = "0x6004"
STATE_VIEW = "0x" + "31" * 20
STATE_VIEW_CODE = "0x6005"
POOL_MANAGER = "0x" + "41" * 20
POOL_MANAGER_CODE = "0x6006"
POSITION_ASSET = "lp-position"
POSITION_ID = 7
POSITION_LIQUIDITY = 10**12
TICK_LOWER = -120
TICK_UPPER = 120
POOL_FEE = 3_000
POOL_MANAGER_SELECTOR = "0x" + keccak(b"poolManager()")[:4].hex()
GET_SLOT0_SELECTOR = "0x" + keccak(b"getSlot0(bytes32)")[:4].hex()


def reply(request: dict[str, object], result: object) -> bytes:
    return canonical_json({"jsonrpc": "2.0", "id": request["id"], "result": result})


def runtime_hash(code: str) -> str:
    return "0x" + keccak(bytes.fromhex(code[2:])).hex()


@dataclass(frozen=True)
class FeedSpec:
    address: str
    description: str
    answer: int
    code: str = "0x6001"


@dataclass(frozen=True)
class TokenSpec:
    asset_id: str
    address: str
    code: str
    decimals: int
    units: int
    feed: FeedSpec


NATIVE_SPEC = FeedSpec("0x" + "f1" * 20, "ETH / USD", NATIVE_ANSWER, "0x6009")
USDC_SPEC = TokenSpec(
    "usdc",
    "0x" + "11" * 20,
    "0x6002",
    6,
    USDC_UNITS,
    FeedSpec("0x" + "f2" * 20, "USDC / USD", USDC_ANSWER),
)
WETH_SPEC = TokenSpec(
    "weth",
    "0x" + "22" * 20,
    "0x6003",
    18,
    WETH_UNITS,
    FeedSpec("0x" + "f3" * 20, "WETH / USD", WETH_ANSWER),
)


@dataclass(frozen=True)
class PositionSpec:
    """One declared position and the `ownerOf` answer this provider gives for it."""

    asset_id: str = POSITION_ASSET
    token_id: int = POSITION_ID
    owner: str = ACCOUNT
    code: str = POSITION_MANAGER_CODE
    native_pool: bool = False


@dataclass
class InventoryFixture:
    """One deterministic provider for one manifest account at one pinned block."""

    chain_id: int = CHAIN_ID
    account: str = ACCOUNT
    block_number: int = BLOCK_NUMBER
    block_hash: str = BLOCK_HASH
    parent_hash: str = PARENT_HASH
    timestamp: int = TIMESTAMP
    native_units: int = NATIVE_UNITS
    native_feed: FeedSpec = NATIVE_SPEC
    tokens: tuple[TokenSpec, ...] = (USDC_SPEC, WETH_SPEC)
    positions: tuple[PositionSpec, ...] = ()
    feed_age_seconds: int = 10
    flip_closing_header: bool = False
    methods: list[str] = field(default_factory=list)
    block_calls: int = 0

    def post(self, payload: bytes) -> bytes:
        request = json.loads(payload)
        method, params = request["method"], request["params"]
        self.methods.append(method)
        result = self._result(method, params)
        return canonical_json({"jsonrpc": "2.0", "id": request["id"], "result": result})

    def _result(self, method: str, params: list[object]) -> object:
        if method == "eth_chainId":
            return hex(self.chain_id)
        if method == "eth_getBlockByNumber":
            self.block_calls += 1
            flipped = self.flip_closing_header and self.block_calls > 1
            return {
                "number": hex(self.block_number),
                "hash": "0x" + "ff" * 32 if flipped else self.block_hash,
                "parentHash": "0x" + "dd" * 32 if flipped else self.parent_hash,
                "timestamp": hex(self.timestamp),
            }
        if method == "eth_getBalance":
            return hex(self.native_units)
        if method == "eth_getCode":
            return self.codes[params[0]]  # type: ignore[index]
        if method == "eth_call":
            return self._call(params)
        raise AssertionError(method)

    @property
    def codes(self) -> dict[str, str]:
        codes = {self.native_feed.address: self.native_feed.code}
        for token in self.tokens:
            codes[token.address] = token.code
            codes[token.feed.address] = token.feed.code
        for position in self.positions:
            codes[POSITION_MANAGER] = position.code
        return codes

    @property
    def feeds(self) -> tuple[FeedSpec, ...]:
        return (self.native_feed, *(token.feed for token in self.tokens))

    def _call(self, params: list[object]) -> str:
        call = params[0]
        assert isinstance(call, dict)
        address, data = call["to"], call["data"]
        assert isinstance(address, str) and isinstance(data, str)
        for token in self.tokens:
            if address == token.address:
                return self._token(token, data)
        position = next((item for item in self.positions if address == POSITION_MANAGER), None)
        if position is not None:
            if data != owner_of_data(position.token_id):
                raise AssertionError(data)
            return "0x" + encode(["address"], [position.owner]).hex()
        return self._feed(next(feed for feed in self.feeds if feed.address == address), data)

    def _token(self, token: TokenSpec, data: str) -> str:
        if data == DECIMALS_SELECTOR:
            return "0x" + encode(["uint8"], [token.decimals]).hex()
        if data == balance_of_data(self.account):
            return "0x" + encode(["uint256"], [token.units]).hex()
        raise AssertionError(data)

    def _feed(self, spec: FeedSpec, data: str) -> str:
        if data == selector("decimals()"):
            return "0x" + encode(["uint8"], [8]).hex()
        if data == selector("description()"):
            return "0x" + encode(["string"], [spec.description]).hex()
        if data == selector("latestRoundData()"):
            updated_at = self.timestamp - self.feed_age_seconds
            values = [7, spec.answer, updated_at - 10, updated_at, 7]
            return "0x" + encode(["uint80", "int256", "uint256", "uint256", "uint80"], values).hex()
        raise AssertionError(data)


def inventory_session(source: str, fixture: InventoryFixture | None = None) -> tuple:
    recorder = BoundedExchangeRecorder()
    return (
        JsonRpcTransport(
            "https://fixture.invalid", http=fixture or InventoryFixture(), recorder=recorder
        ),
        recorder,
        source,
    )


def flow_session(source: str, fixture: FlowFixture | None = None) -> tuple:
    recorder = BoundedExchangeRecorder()
    return (
        JsonRpcTransport(
            "https://fixture.invalid", http=fixture or FlowFixture(), recorder=recorder
        ),
        recorder,
        source,
    )


def manifest_feed(spec: FeedSpec) -> ManifestFeed:
    return ManifestFeed(
        address=spec.address,
        description=spec.description,
        runtime_keccak=runtime_hash(spec.code),
        max_age_seconds=FEED_MAX_AGE,
    )


def position_pool_key(*, native: bool = False) -> PoolKey:
    if native:
        return PoolKey(ZERO_ADDRESS, USDC_SPEC.address, POOL_FEE, 60, ZERO_ADDRESS)
    return PoolKey(USDC_SPEC.address, WETH_SPEC.address, POOL_FEE, 60, ZERO_ADDRESS)


def position_snapshot(
    *,
    block_number: int = BLOCK_NUMBER,
    block_hash: str = BLOCK_HASH,
    timestamp: int = TIMESTAMP,
    owner: str = ACCOUNT,
    token_id: int = POSITION_ID,
    liquidity: int = POSITION_LIQUIDITY,
    native_pool: bool = False,
) -> PositionSnapshot:
    """One synthetic live position whose pool trades the two declared currencies."""
    key = position_pool_key(native=native_pool)
    return PositionSnapshot(
        BlockRef(CHAIN_ID, block_number, block_hash, timestamp),
        POSITION_MANAGER,
        token_id,
        owner,
        POSITION_MANAGER,
        key,
        pool_id_from_key(key),
        position_id(POSITION_MANAGER, TICK_LOWER, TICK_UPPER, token_id),
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


def slot0_sqrt_price() -> int:
    return sqrt_price_at_tick(0)


def position_session(source: str, snapshot: PositionSnapshot) -> tuple:
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
            if method == "eth_call":
                return reply(request, results[request["params"][0]["data"][:10]])
            raise AssertionError(method)

    recorder = BoundedExchangeRecorder()
    return (
        JsonRpcTransport("https://fixture.invalid", http=Poster(), recorder=recorder),
        recorder,
        source,
    )


def slot0_session(
    source: str,
    *,
    number: int = BLOCK_NUMBER,
    block_hash: str = BLOCK_HASH,
    timestamp: int = TIMESTAMP,
) -> tuple:
    """One pinned slot0 window session; it answers the exact call set the capture expects."""

    class Poster:
        def post(self, payload: bytes) -> bytes:
            request = json.loads(payload)
            method, params = request["method"], request["params"]
            if method == "eth_chainId":
                return reply(request, hex(CHAIN_ID))
            if method == "eth_getBlockByNumber":
                return reply(
                    request,
                    {
                        "number": hex(number),
                        "hash": block_hash,
                        "parentHash": PARENT_HASH,
                        "timestamp": hex(timestamp),
                    },
                )
            pin = {"blockHash": block_hash, "requireCanonical": True}
            assert params[1] == pin
            if method == "eth_getCode":
                code = STATE_VIEW_CODE if params[0] == STATE_VIEW else POOL_MANAGER_CODE
                return reply(request, code)
            assert method == "eth_call"
            call = params[0]
            if call["data"] == POOL_MANAGER_SELECTOR:
                return reply(request, "0x" + "00" * 12 + POOL_MANAGER[2:])
            assert call["to"] == STATE_VIEW and call["data"].startswith(GET_SLOT0_SELECTOR)
            return reply(
                request,
                "0x"
                + encode(
                    ["uint160", "int24", "uint24", "uint24"],
                    [slot0_sqrt_price(), 0, 0, POOL_FEE],
                ).hex(),
            )

    recorder = BoundedExchangeRecorder()
    return (
        JsonRpcTransport("https://fixture.invalid", http=Poster(), recorder=recorder),
        recorder,
        source,
    )


def chain_inventory(fixture: InventoryFixture) -> ChainInventory:
    return ChainInventory(
        chain_id=fixture.chain_id,
        account=fixture.account,
        primary_source="rpc_a",
        secondary_source="rpc_b",
        assets=(
            ManifestAsset(
                asset_id="native",
                address=ZERO_ADDRESS,
                decimals=18,
                runtime_keccak=None,
                role="cash",
                feed=manifest_feed(fixture.native_feed),
            ),
            *(
                ManifestAsset(
                    asset_id=token.asset_id,
                    address=token.address,
                    decimals=token.decimals,
                    runtime_keccak=runtime_hash(token.code),
                    role="cash",
                    feed=manifest_feed(token.feed),
                )
                for token in fixture.tokens
            ),
        ),
        positions=tuple(
            ManifestPosition(
                asset_id=position.asset_id,
                position_manager=POSITION_MANAGER,
                position_manager_runtime_keccak=runtime_hash(position.code),
                token_id=position.token_id,
                pool_id=pool_id_from_key(position_pool_key(native=position.native_pool)),
                state_view=STATE_VIEW,
                state_view_runtime_keccak=runtime_hash(STATE_VIEW_CODE),
                pool_manager=POOL_MANAGER,
                pool_manager_runtime_keccak=runtime_hash(POOL_MANAGER_CODE),
                currency0_id="native" if position.native_pool else "usdc",
                currency1_id="usdc" if position.native_pool else "weth",
            )
            for position in fixture.positions
        ),
    )


def risk_policy(*chains: ChainInventory) -> GlobalRiskPolicy:
    return GlobalRiskPolicy(
        inventory={chain.key: chain.asset_ids for chain in chains},
        total_capital_usd_micros=500_000_000,
        daily_loss_usd_micros=200_000_000,
        cumulative_loss_usd_micros=500_000_000,
        max_age_seconds=FEED_MAX_AGE,
        max_skew_seconds=MAX_SKEW,
    )


def manifest(*fixtures: InventoryFixture) -> PortfolioManifest:
    chains = tuple(chain_inventory(fixture) for fixture in fixtures)
    return PortfolioManifest(policy=risk_policy(*chains), chains=chains)


def observe_inventory(
    store: EvidenceStore,
    fixture: InventoryFixture,
    *,
    sources: tuple[str, str] = ("rpc_a", "rpc_b"),
) -> tuple:
    quorum = collect_account_inventory_quorum(
        inventory_session(sources[0], fixture),
        inventory_session(sources[1], replace(fixture, methods=[], block_calls=0)),
        entry=chain_inventory(fixture),
        block_number=fixture.block_number,
    )
    return persist_account_inventory_evidence(store, quorum)


def observe_flow(
    store: EvidenceStore,
    fixture: InventoryFixture,
    *,
    asset_id: str,
    start_block: int,
    incoming: tuple = (),
    outgoing: tuple = (),
    sources: tuple[str, str] = ("rpc_a", "rpc_b"),
) -> tuple:
    token = next(asset for asset in fixture.tokens if asset.asset_id == asset_id)
    scope = FlowScope(
        fixture.chain_id,
        fixture.account,
        asset_id,
        token.address,
        token.decimals,
        start_block,
        fixture.block_number,
        start_block_hash=BLOCK_HASH,
        end_block_hash=fixture.block_hash,
    )
    quorum = collect_token_flow_quorum(
        flow_session(sources[0], bound_flow_fixture(fixture, incoming=incoming, outgoing=outgoing)),
        flow_session(sources[1], bound_flow_fixture(fixture, incoming=incoming, outgoing=outgoing)),
        scope=scope,
    )
    return persist_token_flow_evidence(store, quorum)


def observe_position(
    store: EvidenceStore,
    fixture: InventoryFixture,
    *,
    snapshot: PositionSnapshot | None = None,
    sources: tuple[str, str] = ("rpc_a", "rpc_b"),
) -> tuple:
    """Retain the ownership, pool-state and slot0 proof a declared position needs."""
    position = chain_inventory(fixture).positions[0]
    view = (
        position_snapshot(native_pool=fixture.positions[0].native_pool)
        if snapshot is None
        else snapshot
    )
    block = view.block
    if block.timestamp is None:
        raise ValueError("retained position proof requires a timestamped block")
    snapshots = collect_position_quorum(
        position_session(sources[0], view),
        position_session(sources[1], view),
        chain_id=fixture.chain_id,
        position_manager=POSITION_MANAGER,
        state_view=STATE_VIEW,
        token_id=position.token_id,
        block_number=block.number,
    ).retain(store)
    window = collect_slot0_window_quorum(
        slot0_session(
            sources[0], number=block.number, block_hash=block.block_hash, timestamp=block.timestamp
        ),
        slot0_session(
            sources[1], number=block.number, block_hash=block.block_hash, timestamp=block.timestamp
        ),
        chain_id=fixture.chain_id,
        state_view=STATE_VIEW,
        pool_manager=POOL_MANAGER,
        pool_id=position.pool_id,
        block_numbers=(block.number,),
        state_view_runtime_keccak=position.state_view_runtime_keccak,
        pool_manager_runtime_keccak=position.pool_manager_runtime_keccak,
    )
    return (
        PositionEvidence(
            position.asset_id, snapshots, persist_slot0_window_evidence(store, window)
        ),
    )


def observe_position_flow(
    store: EvidenceStore,
    fixture: InventoryFixture,
    *,
    start_block: int,
    incoming: tuple = (),
    outgoing: tuple = (),
    sources: tuple[str, str] = ("rpc_a", "rpc_b"),
) -> tuple:
    """Retain the four-topic ERC-721 movement window of the declared position."""
    position = chain_inventory(fixture).positions[0]
    scope = FlowScope(
        fixture.chain_id,
        fixture.account,
        position.asset_id,
        position.position_manager,
        0,
        start_block,
        fixture.block_number,
        position.token_id,
        start_block_hash=BLOCK_HASH,
        end_block_hash=fixture.block_hash,
    )
    quorum = collect_token_flow_quorum(
        flow_session(sources[0], bound_flow_fixture(fixture, incoming=incoming, outgoing=outgoing)),
        flow_session(sources[1], bound_flow_fixture(fixture, incoming=incoming, outgoing=outgoing)),
        scope=scope,
    )
    return persist_token_flow_evidence(store, quorum)


def observation(
    inventory: tuple,
    *,
    flows: tuple = (),
    positions: tuple = (),
    native_gas_units: int = 0,
    non_trading_native_units: int = 0,
) -> AccountObservation:
    return AccountObservation(
        inventory=inventory,
        flows=flows,
        positions=positions,
        native_gas_units=native_gas_units,
        non_trading_native_units=non_trading_native_units,
    )


def reader_for(
    store: EvidenceStore,
    *,
    fixtures: tuple[InventoryFixture, ...],
    observations: dict[str, AccountObservation],
    previous: PortfolioFrame | None = None,
) -> EvidenceReplayAccountRiskReader:
    return EvidenceReplayAccountRiskReader(
        store, manifest(*fixtures), observations, previous=previous
    )


def baseline(store: EvidenceStore, fixture: InventoryFixture) -> PortfolioFrame:
    return reader_for(
        store,
        fixtures=(fixture,),
        observations={chain_inventory(fixture).key: observation(observe_inventory(store, fixture))},
    ).reconcile()


def retoken(
    fixture: InventoryFixture,
    *,
    asset_id: str,
    units: int | None = None,
    answer: int | None = None,
) -> InventoryFixture:
    tokens = tuple(
        replace(
            token,
            units=token.units if units is None else units,
            feed=token.feed if answer is None else replace(token.feed, answer=answer),
        )
        if token.asset_id == asset_id
        else token
        for token in fixture.tokens
    )
    return replace(fixture, tokens=tokens)


def later(
    fixture: InventoryFixture,
    *,
    timestamp: int = TIMESTAMP + 30,
    block_number: int = BLOCK_TWO,
) -> InventoryFixture:
    return replace(fixture, block_number=block_number, block_hash=HASH_TWO, timestamp=timestamp)


def usdc_transfer(*, from_address: str, to_address: str, units: int) -> dict[str, object]:
    return transfer_log(
        from_address=from_address,
        to_address=to_address,
        units=units,
        block_number=BLOCK_TWO,
        token=USDC_SPEC.address,
    )


def bound_flow_fixture(fixture: InventoryFixture, *, incoming=(), outgoing=()) -> FlowFixture:
    return FlowFixture(
        chain_id=fixture.chain_id,
        incoming=list(incoming),
        outgoing=list(outgoing),
        bind_hashes=True,
        headers={
            BLOCK_NUMBER: {
                "number": hex(BLOCK_NUMBER),
                "hash": BLOCK_HASH,
                "parentHash": PARENT_HASH,
                "timestamp": hex(TIMESTAMP),
            },
            fixture.block_number: {
                "number": hex(fixture.block_number),
                "hash": fixture.block_hash,
                "parentHash": BLOCK_HASH,
                "timestamp": hex(fixture.timestamp),
            },
        },
    )


def usdc_in(units: int) -> FlowFixture:
    log = usdc_transfer(from_address=THIRD_PARTY, to_address=ACCOUNT, units=units)
    return bound_flow_fixture(later(InventoryFixture()), incoming=(log,))


def usdc_out(units: int) -> FlowFixture:
    log = usdc_transfer(from_address=ACCOUNT, to_address=THIRD_PARTY, units=units)
    return bound_flow_fixture(later(InventoryFixture()), outgoing=(log,))


def ceil_micros(units: int, *, answer: int, decimals: int) -> int:
    """Independent integer re-derivation of the reader's conservative pricing."""
    return -(-(units * answer * 1_000_000) // (10**8 * 10**decimals))


def expected_position_micros(snapshot: PositionSnapshot) -> int:
    """Independent re-derivation of the floored position value the reader must report."""
    inventory: PositionInventory = position_inventory(snapshot, slot0_sqrt_price())
    return value_usd_micros(
        inventory.total0, numerator=USDC_ANSWER * 1_000_000, denominator=10**8, decimals=6
    ) + value_usd_micros(
        inventory.total1, numerator=WETH_ANSWER * 1_000_000, denominator=10**8, decimals=18
    )


def cst_midnight(timestamp: int) -> int:
    """CST midnight is 16:00 UTC; the ledger requires that exact boundary."""
    return timestamp - (timestamp + 8 * 3600) % 86_400


def test_reader_rejects_incomplete_observation_sets(tmp_path) -> None:
    store = EvidenceStore(tmp_path / "coverage.db")
    fixture = InventoryFixture()
    try:
        references = observe_inventory(store, fixture)
        key = chain_inventory(fixture).key
        with pytest.raises(ValueError, match="must exactly cover the manifest accounts"):
            reader_for(store, fixtures=(fixture,), observations={})
        with pytest.raises(ValueError, match="must exactly cover the manifest accounts"):
            reader_for(
                store,
                fixtures=(fixture,),
                observations={"4663:0x" + "00" * 20: observation(references)},
            )
        with pytest.raises(ValueError, match="previous frame must cover"):
            reader_for(
                store,
                fixtures=(fixture,),
                observations={key: observation(references)},
                previous=PortfolioFrame(accounts=()),
            )
    finally:
        store.close()


def test_inception_frame_is_a_complete_zero_flow_baseline(tmp_path) -> None:
    store = EvidenceStore(tmp_path / "inception.db")
    fixture = InventoryFixture()
    try:
        reader = reader_for(
            store,
            fixtures=(fixture,),
            observations={
                chain_inventory(fixture).key: observation(observe_inventory(store, fixture))
            },
        )
        frame = reader.reconcile()
        assert frame == reader.reconcile()  # same-block replay is byte-identical
        account = frame.accounts[0]
        assert (account.key, account.block_number, account.block_hash) == (
            chain_inventory(fixture).key,
            BLOCK_NUMBER,
            BLOCK_HASH,
        )
        assert account.movements == dict.fromkeys(ASSET_IDS, 0)
        assert (account.non_trading_flow_usd_micros, account.unresolved_operations) == (0, 0)
        assert account.nav == 13_000_000_000
        policy = risk_policy(chain_inventory(fixture))
        state = reconcile(policy, RiskState(), frame, now=TIMESTAMP)
        assert (state.halted, state.cumulative_loss, state.frame) == (False, 0, frame)
        repeated = reconcile(policy, state, frame, now=TIMESTAMP)
        assert repeated.halted is False
    finally:
        store.close()


def test_inception_must_not_carry_flow_attribution(tmp_path) -> None:
    store = EvidenceStore(tmp_path / "inception-flow.db")
    fixture = InventoryFixture()
    try:
        reader = reader_for(
            store,
            fixtures=(fixture,),
            observations={
                chain_inventory(fixture).key: observation(
                    observe_inventory(store, fixture), native_gas_units=1
                )
            },
        )
        with pytest.raises(ValueError, match="inception baseline must not carry flow"):
            reader.reconcile()
    finally:
        store.close()


def test_a_deposit_is_explained_and_credited_without_a_loss(tmp_path) -> None:
    store = EvidenceStore(tmp_path / "deposit.db")
    fixture = InventoryFixture()
    try:
        first = baseline(store, fixture)
        second = later(retoken(fixture, asset_id="usdc", units=USDC_UNITS + 100_000_000))
        flow = observe_flow(
            store,
            second,
            asset_id="usdc",
            start_block=BLOCK_NUMBER,
            incoming=(
                usdc_transfer(from_address=THIRD_PARTY, to_address=ACCOUNT, units=100_000_000),
            ),
        )
        frame = reader_for(
            store,
            fixtures=(second,),
            observations={
                chain_inventory(second).key: observation(
                    observe_inventory(store, second), flows=(AssetFlowEvidence("usdc", flow),)
                )
            },
            previous=first,
        ).reconcile()
        account = frame.accounts[0]
        assert account.movements == {"native": 0, "usdc": 100_000_000, "weth": 0}
        assert (account.non_trading_flow_usd_micros, account.unresolved_operations) == (
            100_000_000,
            0,
        )
        state = reconcile(
            risk_policy(chain_inventory(second)), RiskState(frame=first), frame, now=TIMESTAMP + 30
        )
        assert (state.halted, state.cumulative_loss) == (False, 0)
    finally:
        store.close()


def test_production_reader_refuses_legacy_height_only_flow_proofs(tmp_path) -> None:
    store = EvidenceStore(tmp_path / "legacy-flow.db")
    fixture = InventoryFixture()
    try:
        first = baseline(store, fixture)
        second = later(fixture)
        legacy_scope = FlowScope(
            CHAIN_ID, ACCOUNT, "usdc", USDC_SPEC.address, 6, BLOCK_NUMBER, BLOCK_TWO
        )
        quorum = collect_token_flow_quorum(
            flow_session("rpc_a", FlowFixture()),
            flow_session("rpc_b", FlowFixture()),
            scope=legacy_scope,
        )
        refs = persist_token_flow_evidence(store, quorum)
        assert all(ref.schema_version == 1 and ref.block is None for ref in refs)
        reader = reader_for(
            store,
            fixtures=(second,),
            observations={
                chain_inventory(second).key: observation(
                    observe_inventory(store, second), flows=(AssetFlowEvidence("usdc", refs),)
                )
            },
            previous=first,
        )
        with pytest.raises(ValueError, match="unsupported"):
            reader.reconcile()
    finally:
        store.close()


def test_a_withdrawal_is_explained_and_never_becomes_a_loss(tmp_path) -> None:
    store = EvidenceStore(tmp_path / "withdrawal.db")
    fixture = InventoryFixture()
    try:
        first = baseline(store, fixture)
        second = later(retoken(fixture, asset_id="usdc", units=USDC_UNITS - 100_000_000))
        flow = observe_flow(
            store,
            second,
            asset_id="usdc",
            start_block=BLOCK_NUMBER,
            outgoing=(
                usdc_transfer(from_address=ACCOUNT, to_address=THIRD_PARTY, units=100_000_000),
            ),
        )
        frame = reader_for(
            store,
            fixtures=(second,),
            observations={
                chain_inventory(second).key: observation(
                    observe_inventory(store, second), flows=(AssetFlowEvidence("usdc", flow),)
                )
            },
            previous=first,
        ).reconcile()
        assert frame.accounts[0].non_trading_flow_usd_micros == -100_000_000
        state = reconcile(
            risk_policy(chain_inventory(second)), RiskState(frame=first), frame, now=TIMESTAMP + 30
        )
        assert (state.halted, state.cumulative_loss) == (False, 0)
    finally:
        store.close()


def test_an_internal_transfer_between_monitored_accounts_is_not_a_loss(tmp_path) -> None:
    """A 300 USD move between two monitored accounts exceeds the daily limit if misread."""
    store = EvidenceStore(tmp_path / "internal.db")
    fixture = InventoryFixture()
    other = replace(fixture, account=SECOND_ACCOUNT)
    moved = 300_000_000
    try:
        prior = PortfolioFrame(
            accounts=(
                baseline(store, fixture).accounts[0],
                baseline(store, other).accounts[0],
            )
        )
        sent = later(retoken(fixture, asset_id="usdc", units=USDC_UNITS - moved))
        received = later(
            replace(
                retoken(fixture, asset_id="usdc", units=USDC_UNITS + moved),
                account=SECOND_ACCOUNT,
            )
        )
        leg = usdc_transfer(from_address=ACCOUNT, to_address=SECOND_ACCOUNT, units=moved)
        frame = reader_for(
            store,
            fixtures=(sent, received),
            observations={
                chain_inventory(sent).key: observation(
                    observe_inventory(store, sent),
                    flows=(
                        AssetFlowEvidence(
                            "usdc",
                            observe_flow(
                                store,
                                sent,
                                asset_id="usdc",
                                start_block=BLOCK_NUMBER,
                                outgoing=(leg,),
                            ),
                        ),
                    ),
                ),
                chain_inventory(received).key: observation(
                    observe_inventory(store, received),
                    flows=(
                        AssetFlowEvidence(
                            "usdc",
                            observe_flow(
                                store,
                                received,
                                asset_id="usdc",
                                start_block=BLOCK_NUMBER,
                                incoming=(leg,),
                            ),
                        ),
                    ),
                ),
            },
            previous=prior,
        ).reconcile()
        assert [account.non_trading_flow_usd_micros for account in frame.accounts] == [
            -moved,
            moved,
        ]
        state = reconcile(
            risk_policy(chain_inventory(sent), chain_inventory(received)),
            RiskState(frame=prior),
            frame,
            now=TIMESTAMP + 30,
        )
        assert (state.cumulative_loss, state.halted) == (0, False)
    finally:
        store.close()


def test_evidence_from_an_undeclared_provider_is_refused(tmp_path) -> None:
    store = EvidenceStore(tmp_path / "declared-sources.db")
    fixture = InventoryFixture()
    try:
        rogue = observe_inventory(store, fixture, sources=("rpc_a", "rpc_c"))
        with pytest.raises(ValueError, match="declared manifest providers"):
            reader_for(
                store,
                fixtures=(fixture,),
                observations={chain_inventory(fixture).key: observation(rogue)},
            ).reconcile()

        first = baseline(store, fixture)
        second = later(retoken(fixture, asset_id="usdc", units=USDC_UNITS - 100_000_000))
        flow = observe_flow(
            store,
            second,
            asset_id="usdc",
            start_block=BLOCK_NUMBER,
            outgoing=(
                usdc_transfer(from_address=ACCOUNT, to_address=THIRD_PARTY, units=100_000_000),
            ),
            sources=("rpc_a", "rpc_c"),
        )
        with pytest.raises(ValueError, match="declared manifest providers"):
            reader_for(
                store,
                fixtures=(second,),
                observations={
                    chain_inventory(second).key: observation(
                        observe_inventory(store, second),
                        flows=(AssetFlowEvidence("usdc", flow),),
                    )
                },
                previous=first,
            ).reconcile()
    finally:
        store.close()


def test_an_unexplained_movement_blocks_the_frame(tmp_path) -> None:
    store = EvidenceStore(tmp_path / "unexplained.db")
    fixture = InventoryFixture()
    try:
        first = baseline(store, fixture)
        second = later(retoken(fixture, asset_id="usdc", units=USDC_UNITS - 100_000_000))
        frame = reader_for(
            store,
            fixtures=(second,),
            observations={
                chain_inventory(second).key: observation(observe_inventory(store, second))
            },
            previous=first,
        ).reconcile()
        assert frame.accounts[0].unresolved_operations == 1
        with pytest.raises(ValueError, match="unresolved or stale account state"):
            reconcile(
                risk_policy(chain_inventory(second)),
                RiskState(frame=first),
                frame,
                now=TIMESTAMP + 30,
            )
    finally:
        store.close()


def test_an_unexplained_native_spend_blocks_the_frame(tmp_path) -> None:
    store = EvidenceStore(tmp_path / "native.db")
    fixture = InventoryFixture()
    try:
        first = baseline(store, fixture)
        second = later(replace(fixture, native_units=NATIVE_UNITS - 1))
        frame = reader_for(
            store,
            fixtures=(second,),
            observations={
                chain_inventory(second).key: observation(observe_inventory(store, second))
            },
            previous=first,
        ).reconcile()
        assert frame.accounts[0].movements["native"] == -1
        assert frame.accounts[0].unresolved_operations == 1
    finally:
        store.close()


def test_retained_gas_attribution_explains_a_native_spend(tmp_path) -> None:
    store = EvidenceStore(tmp_path / "gas.db")
    fixture = InventoryFixture()
    gas = 21_000 * 10**9
    try:
        first = baseline(store, fixture)
        second = later(replace(fixture, native_units=NATIVE_UNITS - gas))
        frame = reader_for(
            store,
            fixtures=(second,),
            observations={
                chain_inventory(second).key: observation(
                    observe_inventory(store, second), native_gas_units=gas
                )
            },
            previous=first,
        ).reconcile()
        account = frame.accounts[0]
        assert (account.movements["native"], account.unresolved_operations) == (-gas, 0)
        assert account.non_trading_flow_usd_micros == 0
    finally:
        store.close()


def test_a_flow_provider_disagreement_blocks_the_frame(tmp_path) -> None:
    store = EvidenceStore(tmp_path / "disagree.db")
    fixture = InventoryFixture()
    try:
        first = baseline(store, fixture)
        second = later(retoken(fixture, asset_id="usdc", units=USDC_UNITS - 100_000_000))
        scope = FlowScope(
            CHAIN_ID,
            ACCOUNT,
            "usdc",
            USDC_SPEC.address,
            6,
            BLOCK_NUMBER,
            BLOCK_TWO,
            start_block_hash=BLOCK_HASH,
            end_block_hash=HASH_TWO,
        )
        left = collect_token_flow_quorum(
            flow_session("rpc_a", usdc_in(1)),
            flow_session("rpc_b", usdc_in(1)),
            scope=scope,
        )
        right = collect_token_flow_quorum(
            flow_session("rpc_a", usdc_in(2)),
            flow_session("rpc_b", usdc_in(2)),
            scope=scope,
        )
        references = (
            persist_token_flow_evidence(store, left)[0],
            persist_token_flow_evidence(store, right)[1],
        )
        reader = reader_for(
            store,
            fixtures=(second,),
            observations={
                chain_inventory(second).key: observation(
                    observe_inventory(store, second),
                    flows=(AssetFlowEvidence("usdc", references),),
                )
            },
            previous=first,
        )
        with pytest.raises(ValueError, match="flow providers disagree"):
            reader.reconcile()
    finally:
        store.close()


def test_a_price_drop_is_a_real_loss_and_latches_at_the_daily_limit(tmp_path) -> None:
    store = EvidenceStore(tmp_path / "loss.db")
    fixture = InventoryFixture()
    try:
        first = baseline(store, fixture)
        second = later(retoken(fixture, asset_id="usdc", answer=USDC_ANSWER // 2))
        frame = reader_for(
            store,
            fixtures=(second,),
            observations={
                chain_inventory(second).key: observation(observe_inventory(store, second))
            },
            previous=first,
        ).reconcile()
        assert frame.accounts[0].movements == dict.fromkeys(ASSET_IDS, 0)
        state = reconcile(
            risk_policy(chain_inventory(second)), RiskState(frame=first), frame, now=TIMESTAMP + 30
        )
        assert state.cumulative_loss == 500_000_000
        assert state.halted is True
    finally:
        store.close()


def test_a_day_boundary_requires_an_exact_cst_midnight_reconciliation(tmp_path) -> None:
    store = EvidenceStore(tmp_path / "day.db")
    fixture = replace(InventoryFixture(), timestamp=DAY_START)
    midnight = cst_midnight(DAY_START) + 86_400
    assert midnight == CID_MIDNIGHT
    try:
        first = baseline(store, fixture)
        policy = risk_policy(chain_inventory(fixture))
        for timestamp, expected in ((midnight, None), (midnight + 1, "missing day-boundary")):
            crossing = later(fixture, timestamp=timestamp)
            frame = reader_for(
                store,
                fixtures=(crossing,),
                observations={
                    chain_inventory(crossing).key: observation(observe_inventory(store, crossing))
                },
                previous=first,
            ).reconcile()
            if expected is None:
                assert (
                    reconcile(policy, RiskState(frame=first), frame, now=timestamp).halted is False
                )
            else:
                with pytest.raises(ValueError, match=expected):
                    reconcile(policy, RiskState(frame=first), frame, now=timestamp)
    finally:
        store.close()


def test_a_stale_or_future_frame_is_rejected(tmp_path) -> None:
    store = EvidenceStore(tmp_path / "stale.db")
    fixture = InventoryFixture()
    try:
        frame = baseline(store, fixture)
        policy = risk_policy(chain_inventory(fixture))
        for now in (TIMESTAMP + FEED_MAX_AGE + 1, TIMESTAMP - 1):
            with pytest.raises(ValueError, match="unresolved or stale account state"):
                reconcile(policy, RiskState(), frame, now=now)
    finally:
        store.close()


def test_cross_chain_skew_beyond_the_policy_is_rejected(tmp_path) -> None:
    store = EvidenceStore(tmp_path / "skew.db")
    base = InventoryFixture()
    other = replace(InventoryFixture(), chain_id=8453, timestamp=TIMESTAMP + MAX_SKEW + 1)
    try:
        frame = reader_for(
            store,
            fixtures=(base, other),
            observations={
                chain_inventory(base).key: observation(observe_inventory(store, base)),
                chain_inventory(other).key: observation(observe_inventory(store, other)),
            },
        ).reconcile()
        assert len(frame.accounts) == 2
        with pytest.raises(ValueError, match="cross-chain snapshot skew"):
            reconcile(
                risk_policy(chain_inventory(base), chain_inventory(other)),
                RiskState(),
                frame,
                now=TIMESTAMP,
            )
    finally:
        store.close()


def test_signing_binding_prices_the_worst_case_with_ceiling_rounding(tmp_path) -> None:
    store = EvidenceStore(tmp_path / "worst-case.db")
    fixture = InventoryFixture()
    try:
        reader = reader_for(
            store,
            fixtures=(fixture,),
            observations={
                chain_inventory(fixture).key: observation(observe_inventory(store, fixture))
            },
        )
        request = make_request(ACCOUNT)
        intent = replace(request.signing.intent, max_swap_input=10**18)
        request = replace(request, signing=replace(request.signing, intent=intent))
        frame, amount = reader.reconcile_and_price(request)
        assert frame.accounts[0].block_hash == BLOCK_HASH
        # An ambiguous swap cap is priced at the more expensive base unit, never cheaper.
        swap_term = max(
            ceil_micros(intent.max_swap_input, answer=USDC_ANSWER, decimals=6),
            ceil_micros(intent.max_swap_input, answer=WETH_ANSWER, decimals=18),
        )
        assert swap_term == 10**18  # one USDC base unit buys more micros than one wei
        assert amount == (
            ceil_micros(intent.amount0_max, answer=USDC_ANSWER, decimals=6)
            + ceil_micros(intent.amount1_max, answer=WETH_ANSWER, decimals=18)
            + swap_term
            + ceil_micros(
                request.signing.gas_limit * request.max_fee_per_gas + request.signing.value,
                answer=NATIVE_ANSWER,
                decimals=18,
            )
        )
        assert amount == 1_000_000 + 1 + 10**18 + 2_400_000
    finally:
        store.close()


def test_a_signing_request_on_another_block_or_pool_is_rejected(tmp_path) -> None:
    store = EvidenceStore(tmp_path / "binding.db")
    fixture = InventoryFixture()
    later_fixture = later(fixture)
    try:
        other_block = reader_for(
            store,
            fixtures=(later_fixture,),
            observations={
                chain_inventory(later_fixture).key: observation(
                    observe_inventory(store, later_fixture)
                )
            },
        )
        with pytest.raises(ValueError, match="account block differs from the signing request"):
            other_block.reconcile_and_price(make_request(ACCOUNT))

        undeclared = replace(fixture, tokens=(USDC_SPEC,))
        narrow = reader_for(
            store,
            fixtures=(undeclared,),
            observations={
                chain_inventory(undeclared).key: observation(observe_inventory(store, undeclared))
            },
        )
        with pytest.raises(ValueError, match="pool currency is not a declared inventory asset"):
            narrow.reconcile_and_price(make_request(ACCOUNT))
    finally:
        store.close()


def test_a_signing_request_for_an_unreconciled_account_is_refused(tmp_path) -> None:
    """An account outside the manifest must be named, never raise StopIteration.

    The signer's admission boundary catches a bounded refusal and reports it as
    one; an escaping StopIteration would instead surface as an unhandled signal
    from the reader, so the unreconciled account is rejected explicitly.
    """
    store = EvidenceStore(tmp_path / "unreconciled.db")
    fixture = InventoryFixture()
    try:
        reader = reader_for(
            store,
            fixtures=(fixture,),
            observations={
                chain_inventory(fixture).key: observation(observe_inventory(store, fixture))
            },
        )
        with pytest.raises(ValueError, match="not a reconciled manifest account"):
            reader.reconcile_and_price(make_request(SECOND_ACCOUNT))
    finally:
        store.close()


def test_malformed_or_unselected_calldata_is_rejected(tmp_path) -> None:
    store = EvidenceStore(tmp_path / "calldata.db")
    fixture = InventoryFixture()
    try:
        reader = reader_for(
            store,
            fixtures=(fixture,),
            observations={
                chain_inventory(fixture).key: observation(observe_inventory(store, fixture))
            },
        )
        request = make_request(ACCOUNT)
        truncated = replace(
            request, signing=replace(request.signing, calldata=request.signing.calldata[:8])
        )
        with pytest.raises(ValueError, match="malformed or noncanonical"):
            reader.reconcile_and_price(truncated)
        unselected = replace(
            request,
            signing=replace(
                request.signing, calldata=b"\x00\x00\x00\x00" + request.signing.calldata[4:]
            ),
        )
        with pytest.raises(ValueError, match="not a bounded executor call"):
            reader.reconcile_and_price(unselected)
    finally:
        store.close()


def test_flow_evidence_for_an_undeclared_asset_is_rejected(tmp_path) -> None:
    store = EvidenceStore(tmp_path / "undeclared-flow.db")
    fixture = InventoryFixture()
    try:
        first = baseline(store, fixture)
        second = later(retoken(fixture, asset_id="usdc", units=USDC_UNITS - 100_000_000))
        references = observe_flow(
            store,
            second,
            asset_id="usdc",
            start_block=BLOCK_NUMBER,
            outgoing=(
                usdc_transfer(from_address=ACCOUNT, to_address=THIRD_PARTY, units=100_000_000),
            ),
        )
        reader = reader_for(
            store,
            fixtures=(second,),
            observations={
                chain_inventory(second).key: observation(
                    observe_inventory(store, second),
                    flows=(AssetFlowEvidence("ghost", references),),
                )
            },
            previous=first,
        )
        with pytest.raises(ValueError, match="flow evidence for an undeclared asset"):
            reader.reconcile()
    finally:
        store.close()


def test_block_change_during_inventory_collection_is_rejected(tmp_path) -> None:
    store = EvidenceStore(tmp_path / "moved.db")
    fixture = InventoryFixture(flip_closing_header=True)
    try:
        with pytest.raises(ValueError, match="block changed during collection"):
            observe_inventory(store, fixture)
    finally:
        store.close()


def test_the_real_reader_reserves_and_releases_ledger_capacity(tmp_path) -> None:
    store = EvidenceStore(tmp_path / "guard-evidence.db")
    fixture = InventoryFixture()
    policy = risk_policy(chain_inventory(fixture))
    try:
        reader = reader_for(
            store,
            fixtures=(fixture,),
            observations={
                chain_inventory(fixture).key: observation(observe_inventory(store, fixture))
            },
        )
        guard = AccountRiskGuard(
            GlobalRiskJournal(tmp_path / "guard-ledger.db", policy), reader, clock=lambda: TIMESTAMP
        )
        request = make_request(ACCOUNT)
        assert len(guard.reserve(request)) == 64
        state = guard.journal.current_state()
        assert [r.usd_micros for r in state.reservations] == [3_400_001]
        assert state.frame is not None
        assert state.frame.accounts[0].key == chain_inventory(fixture).key
        guard.release(request, reason="signing attempt produced no transaction")
        assert guard.journal.current_state().reservations == ()
        guard.reserve(request)
        assert len(guard.journal.current_state().reservations) == 1
    finally:
        store.close()


def test_the_real_reader_stops_an_over_ceiling_attempt(tmp_path) -> None:
    store = EvidenceStore(tmp_path / "ceiling-evidence.db")
    fixture = InventoryFixture()
    policy = risk_policy(chain_inventory(fixture))
    try:
        reader = reader_for(
            store,
            fixtures=(fixture,),
            observations={
                chain_inventory(fixture).key: observation(observe_inventory(store, fixture))
            },
        )
        guard = AccountRiskGuard(
            GlobalRiskJournal(tmp_path / "ceiling-ledger.db", policy),
            reader,
            clock=lambda: TIMESTAMP,
        )
        request = make_request(ACCOUNT)
        intent = replace(request.signing.intent, max_swap_input=10**18)
        request = replace(request, signing=replace(request.signing, intent=intent))
        with pytest.raises(ValueError, match="capital ceiling blocked admission"):
            guard.reserve(request)
        assert guard.journal.current_state().reservations == ()
    finally:
        store.close()


def test_the_reader_binds_the_real_signer_policy_account() -> None:
    entry = chain_inventory(InventoryFixture())
    policy = make_policy(ACCOUNT)
    assert entry.account == ACCOUNT == policy.account
    assert entry.chain_id == policy.chain_id == 4663
    assert entry.key == f"{CHAIN_ID}:{ACCOUNT}"
    assert json.loads(canonical_json({"chain_id": entry.chain_id}))["chain_id"] == CHAIN_ID


def test_position_evidence_requires_two_distinct_provider_pairs(tmp_path) -> None:
    store = EvidenceStore(tmp_path / "position-evidence.db")
    fixture = InventoryFixture(positions=(PositionSpec(),))
    try:
        evidence = observe_position(store, fixture)[0]
        with pytest.raises(ValueError, match="two distinct snapshots providers"):
            replace(evidence, snapshots=(evidence.snapshots[0], evidence.snapshots[0]))
        with pytest.raises(ValueError, match="two distinct pool_state providers"):
            replace(evidence, pool_state=(evidence.pool_state[1], evidence.pool_state[1]))
        with pytest.raises(ValueError, match="must retain slot0_window proofs"):
            replace(evidence, pool_state=evidence.snapshots)
        with pytest.raises(ValueError, match="position evidence requires an asset identifier"):
            replace(evidence, asset_id="")
        inventory = observe_inventory(store, fixture)
        with pytest.raises(ValueError, match="one position proof per position required"):
            observation(inventory, positions=(evidence, evidence))
    finally:
        store.close()


def test_a_declared_position_is_carried_into_the_frame_at_its_retained_value(tmp_path) -> None:
    """The position enters NAV as an investment; its value never redefines the inventory."""
    store = EvidenceStore(tmp_path / "position.db")
    fixture = InventoryFixture(positions=(PositionSpec(),))
    try:
        declared = chain_inventory(fixture)
        positions = observe_position(store, fixture)
        frame = reader_for(
            store,
            fixtures=(fixture,),
            observations={
                declared.key: observation(observe_inventory(store, fixture), positions=positions)
            },
        ).reconcile()
        account = frame.accounts[0]
        expected = expected_position_micros(position_snapshot())
        assert expected > 0
        assert account.movements == dict.fromkeys((*ASSET_IDS, POSITION_ASSET), 0)
        assert (account.non_trading_flow_usd_micros, account.unresolved_operations) == (0, 0)
        assert account.nav == 13_000_000_000 + expected
        balance = next(asset for asset in account.assets if asset.asset_id == POSITION_ASSET)
        assert (balance.units, balance.decimals, balance.role) == (1, 0, "investment")
        assert (balance.value, balance.price_timestamp) == (expected, TIMESTAMP)
        assert balance.evidence_hash == positions[0].snapshots[0].content_hash
        state = reconcile(risk_policy(declared), RiskState(), frame, now=TIMESTAMP)
        assert (state.halted, state.cumulative_loss) == (False, 0)
    finally:
        store.close()


def test_native_eth_position_uses_its_feed_without_counting_wallet_eth_twice(tmp_path) -> None:
    store = EvidenceStore(tmp_path / "native-position.db")
    fixture = InventoryFixture(positions=(PositionSpec(native_pool=True),))
    try:
        declared = chain_inventory(fixture)
        proofs = observe_position(store, fixture)
        frame = reader_for(
            store,
            fixtures=(fixture,),
            observations={
                declared.key: observation(observe_inventory(store, fixture), positions=proofs)
            },
        ).reconcile()
        amounts = position_inventory(
            position_snapshot(native_pool=True), sqrt_price_x96=slot0_sqrt_price()
        )
        expected = amounts.total0 * NATIVE_ANSWER * 10**6 // (
            10**8 * 10**18
        ) + amounts.total1 * USDC_ANSWER * 10**6 // (10**8 * 10**6)
        account = frame.accounts[0]
        balances = {asset.asset_id: asset for asset in account.assets}
        assert balances["native"].units == NATIVE_UNITS
        assert balances[POSITION_ASSET].value == expected > 0
        assert account.nav == 13_000_000_000 + expected
        assert account.unresolved_operations == 0
    finally:
        store.close()


def test_position_evidence_must_cover_exactly_the_declared_positions(tmp_path) -> None:
    store = EvidenceStore(tmp_path / "position-coverage.db")
    declared = InventoryFixture(positions=(PositionSpec(),))
    bare = InventoryFixture()
    try:
        with pytest.raises(ValueError, match="position evidence must cover exactly"):
            reader_for(
                store,
                fixtures=(declared,),
                observations={
                    chain_inventory(declared).key: observation(observe_inventory(store, declared))
                },
            ).reconcile()
        stray = observe_position(store, declared)
        with pytest.raises(ValueError, match="position evidence must cover exactly"):
            reader_for(
                store,
                fixtures=(bare,),
                observations={
                    chain_inventory(bare).key: observation(
                        observe_inventory(store, bare), positions=stray
                    )
                },
            ).reconcile()
    finally:
        store.close()


def test_a_position_no_longer_owned_at_the_reconciled_block_is_refused(tmp_path) -> None:
    """A declared position must be owned; an exit requires a manifest change, never a guess."""
    store = EvidenceStore(tmp_path / "position-not-owned.db")
    fixture = InventoryFixture(positions=(PositionSpec(owner=THIRD_PARTY),))
    try:
        with pytest.raises(ValueError, match="must be owned at the reconciled block"):
            reader_for(
                store,
                fixtures=(fixture,),
                observations={
                    chain_inventory(fixture).key: observation(
                        observe_inventory(store, fixture),
                        positions=observe_position(store, fixture),
                    )
                },
            ).reconcile()
    finally:
        store.close()


def test_a_position_snapshot_owned_by_another_account_is_refused(tmp_path) -> None:
    """Ownership and value must come from one replay, not from a side agreement."""
    store = EvidenceStore(tmp_path / "position-owner.db")
    fixture = InventoryFixture(positions=(PositionSpec(),))
    try:
        with pytest.raises(ValueError, match="not owned by the expected account"):
            reader_for(
                store,
                fixtures=(fixture,),
                observations={
                    chain_inventory(fixture).key: observation(
                        observe_inventory(store, fixture),
                        positions=observe_position(
                            store, fixture, snapshot=position_snapshot(owner=THIRD_PARTY)
                        ),
                    )
                },
            ).reconcile()
    finally:
        store.close()


def test_a_position_valuation_on_another_block_is_refused(tmp_path) -> None:
    store = EvidenceStore(tmp_path / "position-block.db")
    fixture = InventoryFixture(positions=(PositionSpec(),))
    try:
        later_view = position_snapshot(
            block_number=BLOCK_TWO, block_hash=HASH_TWO, timestamp=TIMESTAMP + 30
        )
        with pytest.raises(ValueError, match="position valuation is not at the reconciled block"):
            reader_for(
                store,
                fixtures=(fixture,),
                observations={
                    chain_inventory(fixture).key: observation(
                        observe_inventory(store, fixture),
                        positions=observe_position(store, fixture, snapshot=later_view),
                    )
                },
            ).reconcile()
    finally:
        store.close()


def test_a_position_with_no_retained_value_is_refused(tmp_path) -> None:
    """Dropping a zero-valued asset would redefine the inventory; refuse instead."""
    store = EvidenceStore(tmp_path / "position-zero.db")
    fixture = InventoryFixture(positions=(PositionSpec(),))
    try:
        thin = position_snapshot(liquidity=1)
        assert expected_position_micros(thin) == 0
        with pytest.raises(ValueError, match="positive retained value"):
            reader_for(
                store,
                fixtures=(fixture,),
                observations={
                    chain_inventory(fixture).key: observation(
                        observe_inventory(store, fixture),
                        positions=observe_position(store, fixture, snapshot=thin),
                    )
                },
            ).reconcile()
    finally:
        store.close()


def test_position_evidence_from_an_undeclared_provider_is_refused(tmp_path) -> None:
    store = EvidenceStore(tmp_path / "position-provider.db")
    fixture = InventoryFixture(positions=(PositionSpec(),))
    try:
        rogue = observe_position(store, fixture, sources=("rpc_a", "rpc_c"))
        with pytest.raises(ValueError, match="declared manifest providers"):
            reader_for(
                store,
                fixtures=(fixture,),
                observations={
                    chain_inventory(fixture).key: observation(
                        observe_inventory(store, fixture), positions=rogue
                    )
                },
            ).reconcile()
    finally:
        store.close()


def position_frame(store: EvidenceStore, fixture: InventoryFixture) -> PortfolioFrame:
    fixture = InventoryFixture(positions=fixture.positions, account=fixture.account)
    return reader_for(
        store,
        fixtures=(fixture,),
        observations={
            chain_inventory(fixture).key: observation(
                observe_inventory(store, fixture), positions=observe_position(store, fixture)
            )
        },
    ).reconcile()


def test_a_position_flow_window_is_rebuilt_from_its_own_token_id(tmp_path) -> None:
    """A four-topic ERC-721 window that shows no movement leaves the position unchanged."""
    store = EvidenceStore(tmp_path / "position-flow.db")
    fixture = InventoryFixture(positions=(PositionSpec(),))
    try:
        first = position_frame(store, fixture)
        second = later(fixture)
        window = observe_position_flow(store, second, start_block=BLOCK_NUMBER)
        frame = reader_for(
            store,
            fixtures=(second,),
            observations={
                chain_inventory(second).key: observation(
                    observe_inventory(store, second),
                    flows=(AssetFlowEvidence(POSITION_ASSET, window),),
                    positions=observe_position(
                        store,
                        second,
                        snapshot=position_snapshot(
                            block_number=BLOCK_TWO, block_hash=HASH_TWO, timestamp=TIMESTAMP + 30
                        ),
                    ),
                )
            },
            previous=first,
        ).reconcile()
        account = frame.accounts[0]
        assert account.movements[POSITION_ASSET] == 0
        assert (account.non_trading_flow_usd_micros, account.unresolved_operations) == (0, 0)
    finally:
        store.close()


def test_a_claimed_position_transfer_that_contradicts_ownership_blocks_the_frame(tmp_path) -> None:
    """The ERC-721 net units must equal the delta the two inventory providers proved."""
    store = EvidenceStore(tmp_path / "position-flow-mismatch.db")
    fixture = InventoryFixture(positions=(PositionSpec(),))
    try:
        first = position_frame(store, fixture)
        second = later(fixture)
        log = erc721_transfer_log(
            from_address=ACCOUNT,
            to_address=THIRD_PARTY,
            token=POSITION_MANAGER,
            token_id=POSITION_ID,
            block_number=BLOCK_TWO,
        )
        window = observe_position_flow(store, second, start_block=BLOCK_NUMBER, outgoing=(log,))
        view = position_snapshot(
            block_number=BLOCK_TWO, block_hash=HASH_TWO, timestamp=TIMESTAMP + 30
        )
        frame = reader_for(
            store,
            fixtures=(second,),
            observations={
                chain_inventory(second).key: observation(
                    observe_inventory(store, second),
                    flows=(AssetFlowEvidence(POSITION_ASSET, window),),
                    positions=observe_position(store, second, snapshot=view),
                )
            },
            previous=first,
        ).reconcile()
        account = frame.accounts[0]
        assert account.movements[POSITION_ASSET] == 0
        assert account.unresolved_operations == 1
        with pytest.raises(ValueError, match="unresolved or stale account state"):
            reconcile(
                risk_policy(chain_inventory(second)),
                RiskState(frame=first),
                frame,
                now=TIMESTAMP + 30,
            )
    finally:
        store.close()
