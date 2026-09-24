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
    persist_account_inventory_evidence,
)
from oasis_agent.farming.price_evidence import selector
from oasis_agent.models.common import ZERO_ADDRESS, canonical_json
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
    PortfolioManifest,
)
from oasis_agent.rpc.transport import JsonRpcTransport
from tests.unit.test_account_flows import FlowFixture, transfer_log
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
    )


def risk_policy(*chains: ChainInventory) -> GlobalRiskPolicy:
    return GlobalRiskPolicy(
        inventory={chain.key: tuple(asset.asset_id for asset in chain.assets) for chain in chains},
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
    )
    quorum = collect_token_flow_quorum(
        flow_session(sources[0], FlowFixture(incoming=list(incoming), outgoing=list(outgoing))),
        flow_session(sources[1], FlowFixture(incoming=list(incoming), outgoing=list(outgoing))),
        scope=scope,
    )
    return persist_token_flow_evidence(store, quorum)


def observation(
    inventory: tuple,
    *,
    flows: tuple = (),
    native_gas_units: int = 0,
    non_trading_native_units: int = 0,
) -> AccountObservation:
    return AccountObservation(
        inventory=inventory,
        flows=flows,
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


def usdc_in(units: int) -> FlowFixture:
    log = usdc_transfer(from_address=THIRD_PARTY, to_address=ACCOUNT, units=units)
    return FlowFixture(incoming=[log])


def usdc_out(units: int) -> FlowFixture:
    log = usdc_transfer(from_address=ACCOUNT, to_address=THIRD_PARTY, units=units)
    return FlowFixture(outgoing=[log])


def ceil_micros(units: int, *, answer: int, decimals: int) -> int:
    """Independent integer re-derivation of the reader's conservative pricing."""
    return -(-(units * answer * 1_000_000) // (10**8 * 10**decimals))


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
        scope = FlowScope(CHAIN_ID, ACCOUNT, "usdc", USDC_SPEC.address, 6, BLOCK_NUMBER, BLOCK_TWO)
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
