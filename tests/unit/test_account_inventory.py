"""Two-provider pinned account inventory: exact capture, agreement and replay.

Every figure must be re-derived from retained raw bytes at one canonical block,
so a disagreeing provider, a stale oracle round, a moved block or a forged
payload has to fail closed instead of becoming a permissive default.
"""

import json
from dataclasses import dataclass, field, replace

import pytest
from eth_abi.abi import encode
from eth_hash.auto import keccak

from oasis_agent.errors import RpcError
from oasis_agent.evidence.rpc_capture import BoundedExchangeRecorder
from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.farming.account_inventory import (
    BALANCE_OF_SELECTOR,
    DECIMALS_SELECTOR,
    OWNER_OF_SELECTOR,
    balance_of_data,
    collect_account_inventory_quorum,
    owner_of_data,
    persist_account_inventory_evidence,
    replay_account_inventory_evidence,
)
from oasis_agent.farming.price_evidence import selector
from oasis_agent.models.common import ZERO_ADDRESS, canonical_json
from oasis_agent.risk.portfolio_manifest import (
    ChainInventory,
    ManifestAsset,
    ManifestFeed,
    ManifestPosition,
)
from oasis_agent.rpc.transport import JsonRpcTransport

CHAIN_ID = 4663
ACCOUNT = "0x" + "e6" * 20
OTHER_ACCOUNT = "0x" + "e7" * 20
BLOCK_NUMBER = 42
BLOCK_HASH = "0x" + "aa" * 32
PARENT_HASH = "0x" + "bb" * 32
TIMESTAMP = 1_800_000_000
NATIVE_UNITS = 5_000_000_000_000_000_000
TOKEN = "0x" + "11" * 20
TOKEN_CODE = "0x6001"
TOKEN_RUNTIME = "0x" + keccak(bytes.fromhex("6001")).hex()
TOKEN_DECIMALS = 6
TOKEN_UNITS = 1_000_000_000
NATIVE_FEED = "0x" + "f1" * 20
NATIVE_FEED_CODE = "0x6002"
NATIVE_FEED_RUNTIME = "0x" + keccak(bytes.fromhex("6002")).hex()
TOKEN_FEED = "0x" + "f2" * 20
TOKEN_FEED_CODE = "0x6003"
TOKEN_FEED_RUNTIME = "0x" + keccak(bytes.fromhex("6003")).hex()
NATIVE_ANSWER = 240_000_000_000
TOKEN_ANSWER = 100_000_000
ROUND_ID = 7
FEED_MAX_AGE = 300
WETH = "0x" + "12" * 20
WETH_CODE = "0x6004"
WETH_RUNTIME = "0x" + keccak(bytes.fromhex("6004")).hex()
WETH_DECIMALS = 18
WETH_UNITS = 2 * 10**18
WETH_FEED = "0x" + "f3" * 20
WETH_FEED_CODE = "0x6005"
WETH_FEED_RUNTIME = "0x" + keccak(bytes.fromhex("6005")).hex()
WETH_ANSWER = 240_000_000_000
POSITION_MANAGER = "0x" + "21" * 20
POSITION_MANAGER_CODE = "0x6006"
POSITION_MANAGER_RUNTIME = "0x" + keccak(bytes.fromhex("6006")).hex()
POSITION_ID = 7
STATE_VIEW = "0x" + "31" * 20
STATE_VIEW_RUNTIME = "0x" + "df" * 32
POOL_MANAGER_RUNTIME = "0x" + "e1" * 32
POOL_ID = "0x" + "3c" * 32

CODES = {
    TOKEN: TOKEN_CODE,
    NATIVE_FEED: NATIVE_FEED_CODE,
    TOKEN_FEED: TOKEN_FEED_CODE,
    WETH: WETH_CODE,
    WETH_FEED: WETH_FEED_CODE,
    POSITION_MANAGER: POSITION_MANAGER_CODE,
}


@dataclass
class Fixture:
    """One deterministic provider for one manifest account at one block."""

    chain_id: int = CHAIN_ID
    block_number: int = BLOCK_NUMBER
    block_hash: str = BLOCK_HASH
    parent_hash: str = PARENT_HASH
    timestamp: int = TIMESTAMP
    native_units: int = NATIVE_UNITS
    token_units: int = TOKEN_UNITS
    token_decimals: int = TOKEN_DECIMALS
    token_code: str = TOKEN_CODE
    feed_updated_at: int = TIMESTAMP - 10
    token_feed_description: str = "USDC / USD"
    weth_units: int = WETH_UNITS
    weth_decimals: int = WETH_DECIMALS
    position_owner: str = ACCOUNT
    position_manager_code: str = POSITION_MANAGER_CODE
    burns_position: bool = False
    flip_closing_header: bool = False
    methods: list[str] = field(default_factory=list)
    block_calls: int = 0

    def post(self, payload: bytes) -> bytes:
        request = json.loads(payload)
        method, params = request["method"], request["params"]
        self.methods.append(method)
        if self.burns_position and method == "eth_call":
            call = params[0]
            if isinstance(call, dict) and call.get("data") == owner_of_data(POSITION_ID):
                # A burned position reverts instead of reporting a zero owner, so
                # the capture must refuse rather than turn absence into zero units.
                return canonical_json(
                    {
                        "jsonrpc": "2.0",
                        "id": request["id"],
                        "error": {"code": 3, "message": "execution reverted"},
                    }
                )
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
                "hash": "0x" + "cc" * 32 if flipped else self.block_hash,
                "parentHash": "0x" + "dd" * 32 if flipped else self.parent_hash,
                "timestamp": hex(self.timestamp),
            }
        if method == "eth_getBalance":
            return hex(self.native_units)
        if method == "eth_getCode":
            address = params[0]
            assert isinstance(address, str)
            if address == TOKEN:
                return self.token_code
            if address == POSITION_MANAGER:
                return self.position_manager_code
            return CODES[address]
        if method == "eth_call":
            return self._call(params)
        raise AssertionError(method)

    def _call(self, params: list[object]) -> object:
        call = params[0]
        assert isinstance(call, dict)
        address, data = call["to"], call["data"]
        assert isinstance(address, str) and isinstance(data, str)
        if address == TOKEN:
            if data == DECIMALS_SELECTOR:
                return "0x" + encode(["uint8"], [self.token_decimals]).hex()
            if data == balance_of_data(ACCOUNT):
                return "0x" + encode(["uint256"], [self.token_units]).hex()
        elif address == WETH:
            if data == DECIMALS_SELECTOR:
                return "0x" + encode(["uint8"], [self.weth_decimals]).hex()
            if data == balance_of_data(ACCOUNT):
                return "0x" + encode(["uint256"], [self.weth_units]).hex()
        elif address == POSITION_MANAGER:
            assert data == owner_of_data(POSITION_ID)
            return "0x" + encode(["address"], [self.position_owner]).hex()
        else:
            return self._feed(address, data)
        raise AssertionError(data)

    def _feed(self, address: str, data: str) -> str:
        if data == selector("decimals()"):
            return "0x" + encode(["uint8"], [8]).hex()
        if data == selector("description()"):
            descriptions = {
                NATIVE_FEED: "ETH / USD",
                TOKEN_FEED: self.token_feed_description,
                WETH_FEED: "WETH / USD",
            }
            return "0x" + encode(["string"], [descriptions[address]]).hex()
        if data == selector("latestRoundData()"):
            answers = {NATIVE_FEED: NATIVE_ANSWER, TOKEN_FEED: TOKEN_ANSWER, WETH_FEED: WETH_ANSWER}
            values = [
                ROUND_ID,
                answers[address],
                self.feed_updated_at - 10,
                self.feed_updated_at,
                ROUND_ID,
            ]
            return "0x" + encode(["uint80", "int256", "uint256", "uint256", "uint80"], values).hex()
        raise AssertionError(data)


def session(source: str, fixture: Fixture | None = None) -> tuple:
    recorder = BoundedExchangeRecorder()
    return (
        JsonRpcTransport("https://fixture.invalid", http=fixture or Fixture(), recorder=recorder),
        recorder,
        source,
    )


def entry(*, account: str = ACCOUNT, chain_id: int = CHAIN_ID, positions=()) -> ChainInventory:
    assets = (
        ManifestAsset(
            asset_id="native",
            address=ZERO_ADDRESS,
            decimals=18,
            runtime_keccak=None,
            role="cash",
            feed=ManifestFeed(
                address=NATIVE_FEED,
                description="ETH / USD",
                runtime_keccak=NATIVE_FEED_RUNTIME,
                max_age_seconds=FEED_MAX_AGE,
            ),
        ),
        ManifestAsset(
            asset_id="usdc",
            address=TOKEN,
            decimals=TOKEN_DECIMALS,
            runtime_keccak=TOKEN_RUNTIME,
            role="cash",
            feed=ManifestFeed(
                address=TOKEN_FEED,
                description="USDC / USD",
                runtime_keccak=TOKEN_FEED_RUNTIME,
                max_age_seconds=FEED_MAX_AGE,
            ),
        ),
    )
    if positions:
        assets = (
            *assets,
            ManifestAsset(
                asset_id="weth",
                address=WETH,
                decimals=WETH_DECIMALS,
                runtime_keccak=WETH_RUNTIME,
                role="cash",
                feed=ManifestFeed(
                    address=WETH_FEED,
                    description="WETH / USD",
                    runtime_keccak=WETH_FEED_RUNTIME,
                    max_age_seconds=FEED_MAX_AGE,
                ),
            ),
        )
    return ChainInventory(
        chain_id=chain_id,
        account=account,
        primary_source="rpc_a",
        secondary_source="rpc_b",
        assets=assets,
        positions=positions,
    )


def position(**overrides: object) -> ManifestPosition:
    options: dict[str, object] = {
        "asset_id": "lp-position",
        "position_manager": POSITION_MANAGER,
        "position_manager_runtime_keccak": POSITION_MANAGER_RUNTIME,
        "token_id": POSITION_ID,
        "pool_id": POOL_ID,
        "state_view": STATE_VIEW,
        "state_view_runtime_keccak": STATE_VIEW_RUNTIME,
        "pool_manager_runtime_keccak": POOL_MANAGER_RUNTIME,
        "currency0_id": "usdc",
        "currency1_id": "weth",
    }
    options.update(overrides)
    return ManifestPosition(**options)  # type: ignore[arg-type]


def quorum(left: Fixture | None = None, right: Fixture | None = None, **kwargs: object):
    return collect_account_inventory_quorum(
        session("rpc_a", left),
        session("rpc_b", right),
        entry=entry(**kwargs),  # type: ignore[arg-type]
        block_number=BLOCK_NUMBER,
    )


def test_two_provider_inventory_persists_and_replays_exactly(tmp_path) -> None:
    collected = quorum()
    assert collected.header.ref.number == BLOCK_NUMBER
    assert [unit.asset_id for unit in collected.units] == ["native", "usdc"]
    assert [unit.units for unit in collected.units] == [NATIVE_UNITS, TOKEN_UNITS]
    assert [feed.answer for feed in collected.feeds] == [NATIVE_ANSWER, TOKEN_ANSWER]
    store = EvidenceStore(tmp_path / "inventory.db")
    try:
        references = persist_account_inventory_evidence(store, collected)
        for reference, capture in zip(references, (collected.left, collected.right), strict=True):
            assert replay_account_inventory_evidence(store, reference, entry=entry()) == capture
            assert store.get_record(reference).payload == capture.payload
        interpretation = store.get_record(references[0]).interpretation_payload
        assert json.loads(interpretation)["execution_eligible"] is False
    finally:
        store.close()


def test_feed_answers_scale_to_usd_micros_and_a_bound_denominator() -> None:
    collected = quorum()
    native_feed = collected.feeds[0]
    assert native_feed.usd_micros_numerator == NATIVE_ANSWER * 1_000_000
    assert native_feed.price_denominator == 10**8


def test_providers_must_agree_on_balances_and_valuations() -> None:
    with pytest.raises(ValueError, match="disagree on balances or valuations"):
        quorum(None, Fixture(token_units=TOKEN_UNITS + 1))
    with pytest.raises(ValueError, match="disagree on balances or valuations"):
        quorum(None, Fixture(feed_updated_at=TIMESTAMP - 20))


def test_providers_must_agree_on_the_block() -> None:
    with pytest.raises(ValueError, match="disagree on the block"):
        quorum(None, Fixture(timestamp=TIMESTAMP + 1))


def test_a_single_session_cannot_form_a_quorum() -> None:
    shared = session("rpc_a")
    with pytest.raises(ValueError, match="distinct fresh provider sessions required"):
        collect_account_inventory_quorum(shared, shared, entry=entry(), block_number=BLOCK_NUMBER)


def test_token_decimals_must_match_the_manifest() -> None:
    with pytest.raises(ValueError, match="token decimals mismatch"):
        quorum(None, Fixture(token_decimals=7))


def test_runtime_hash_mismatch_is_rejected() -> None:
    with pytest.raises(ValueError, match="runtime hash mismatch"):
        quorum(None, Fixture(token_code="0x6009"))


def test_feed_description_mismatch_is_rejected() -> None:
    with pytest.raises(ValueError, match="feed description mismatch"):
        quorum(None, Fixture(token_feed_description="USDT / USD"))


def test_stale_or_future_feed_round_is_rejected() -> None:
    with pytest.raises(ValueError, match="stale or future inventory valuation"):
        quorum(None, Fixture(feed_updated_at=TIMESTAMP - FEED_MAX_AGE - 1))
    with pytest.raises(ValueError, match="stale or future inventory valuation"):
        quorum(None, Fixture(feed_updated_at=TIMESTAMP + 1))


def test_block_change_during_collection_is_rejected() -> None:
    with pytest.raises(ValueError, match="block changed during collection"):
        quorum(None, Fixture(flip_closing_header=True))


def test_a_provider_on_another_chain_is_rejected() -> None:
    with pytest.raises(ValueError, match="unexpected chain id"):
        quorum(None, Fixture(chain_id=8453))


def test_replay_is_bound_to_the_manifest_account_and_chain(tmp_path) -> None:
    store = EvidenceStore(tmp_path / "bound.db")
    try:
        reference = persist_account_inventory_evidence(store, quorum())[0]
        with pytest.raises(ValueError, match="does not match the manifest"):
            replay_account_inventory_evidence(store, reference, entry=entry(account=OTHER_ACCOUNT))
        with pytest.raises(ValueError, match="does not match the manifest"):
            replay_account_inventory_evidence(store, reference, entry=entry(chain_id=8453))
    finally:
        store.close()


def test_replay_rejects_a_forged_balance_even_with_a_recomputed_hash(tmp_path) -> None:
    store = EvidenceStore(tmp_path / "forged.db")
    try:
        reference = persist_account_inventory_evidence(store, quorum())[0]
        record = store.get_record(reference)
        data = json.loads(record.payload)
        data["units"][1]["units"] = data["units"][1]["units"] + 1
        forged = store.put(
            reference.kind,
            canonical_json(data),
            reference.block,
            reference.source,
            request=record.request_payload,
            raw_response=record.raw_response_payload,
            retrieved_at=reference.retrieved_at,
        )
        with pytest.raises(ValueError, match="differs from raw reconstruction"):
            replay_account_inventory_evidence(store, forged, entry=entry())
    finally:
        store.close()


def test_balance_of_calldata_is_canonical() -> None:
    assert balance_of_data(ACCOUNT) == BALANCE_OF_SELECTOR + ACCOUNT[2:].rjust(64, "0")
    assert len(balance_of_data(ACCOUNT)) == 74


def test_replace_keeps_a_fresh_reference_provider() -> None:
    # Guards the fixture itself: a replaced provider must not share call state.
    first = Fixture()
    second = replace(first, methods=[], block_calls=0)
    assert first.methods is not second.methods


def test_a_declared_position_is_owned_exactly_when_owner_of_names_the_account(tmp_path) -> None:
    """Ownership is captured, not assumed: another owner means zero units."""
    declared = position()
    held = quorum(None, None, positions=(declared,))
    assert [(unit.asset_id, unit.units) for unit in held.units] == [
        ("native", NATIVE_UNITS),
        ("usdc", TOKEN_UNITS),
        ("weth", WETH_UNITS),
        ("lp-position", 1),
    ]
    not_held = quorum(
        Fixture(position_owner=OTHER_ACCOUNT),
        Fixture(position_owner=OTHER_ACCOUNT),
        positions=(declared,),
    )
    assert not_held.units[-1] == not_held.left.units[-1]
    assert not_held.units[-1].asset_id == "lp-position"
    assert not_held.units[-1].units == 0
    store = EvidenceStore(tmp_path / "position.db")
    try:
        references = persist_account_inventory_evidence(store, held)
        declared_entry = entry(positions=(declared,))
        for reference, capture in zip(references, (held.left, held.right), strict=True):
            replayed = replay_account_inventory_evidence(store, reference, entry=declared_entry)
            assert replayed == capture
    finally:
        store.close()


def test_providers_must_agree_on_position_ownership() -> None:
    declared = position()
    with pytest.raises(ValueError, match="disagree on balances or valuations"):
        quorum(
            Fixture(),
            Fixture(position_owner=OTHER_ACCOUNT),
            positions=(declared,),
        )


def test_a_position_manager_runtime_hash_mismatch_is_rejected() -> None:
    with pytest.raises(ValueError, match="runtime hash mismatch"):
        quorum(None, Fixture(position_manager_code="0x6009"), positions=(position(),))


def test_a_burned_position_is_refused_rather_than_counted_as_zero() -> None:
    """A reverting `ownerOf` is an error result, so the whole capture fails closed."""
    with pytest.raises(RpcError, match="execution reverted"):
        quorum(Fixture(burns_position=True), None, positions=(position(),))


def test_owner_of_calldata_is_canonical() -> None:
    assert owner_of_data(POSITION_ID) == OWNER_OF_SELECTOR + f"{POSITION_ID:064x}"
    assert len(owner_of_data(POSITION_ID)) == 74
    for invalid in (0, -1, 2**256, True, 1.0):
        with pytest.raises(ValueError, match="bounded unsigned integer"):
            owner_of_data(invalid)  # type: ignore[arg-type]
