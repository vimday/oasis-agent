"""The decision-block reference price is a ratio of two observed USD feeds.

``build_lp_cycle`` needs a ``reference_price_x192`` at the market window's own
decision block, in the same Q192 unit as ``sqrtPriceX96 ** 2``. These tests pin the
ratio's direction with a worked WETH/USDC example, pin the exact integer deviation
bound, and require both providers' own retained wire bytes before anything is
retained. No case ends with an execution permit.
"""

import json
from dataclasses import asdict, replace
from decimal import Decimal, localcontext
from pathlib import Path

import pytest
from eth_abi.abi import encode
from eth_hash.auto import keccak

from oasis_agent.evidence.exact_capture import wire_envelope
from oasis_agent.evidence.rpc_capture import BoundedExchangeRecorder
from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.execution.lp_reference_price import (
    CURRENCY_FEED_KIND,
    DERIVED_REFERENCE_KIND,
    REFERENCE_NAME,
    REFERENCE_SCHEMA,
    CurrencyDeclaration,
    CurrencyFeed,
    CurrencyFeedCapture,
    FeedPin,
    FeedQuote,
    ReferenceDeclaration,
    ReferenceProvider,
    ReferenceRequest,
    agreed_reference_price,
    collect_currency_feed,
    currency_feed,
    persist_currency_feed_evidence,
    reference_deviation,
    reference_price_x192,
    replay_currency_feed_evidence,
)
from oasis_agent.farming.price_evidence import selector
from oasis_agent.models.chain import BlockSnapshot
from oasis_agent.models.common import ZERO_ADDRESS, BlockRef, canonical_json
from oasis_agent.rpc.transport import JsonRpcTransport, RpcExchange
from oasis_agent.shadow.features import Q192, agreed_value_payload
from tests.unit.test_live_shadow_producer import CHAIN, blocks
from tests.unit.test_lp_cycle_assembly import (
    DECISION_BLOCK,
    assemble,
    evidence_rows,
    market,
    retain_agreed,
)
from tests.unit.test_lp_features import Q96
from tests.unit.test_market_window import POOL

HEADERS = blocks(100, 3)
DECISION = HEADERS[2]
FEED0 = "0x" + "8" * 40
FEED1 = "0x" + "9" * 40
RUNTIME = "0x6000"
RUNTIME_PIN = "0x" + keccak(bytes.fromhex("6000")).hex()
FEED_DECIMALS = 8
MAX_AGE = 3600
#: The same identity at a later timestamp, used to age one observation out.
LATE = BlockSnapshot(
    BlockRef(CHAIN, DECISION.ref.number, "0x" + "c" * 64, 10_000), DECISION.parent_hash
)


def declaration(
    currency: str, feed: str, *, decimals: int, description: str
) -> CurrencyDeclaration:
    return CurrencyDeclaration(
        address=currency,
        decimals=decimals,
        feed=FeedPin(
            address=feed,
            description=description,
            runtime_keccak=RUNTIME_PIN,
            max_age_seconds=MAX_AGE,
        ),
    )


def declarations(
    *, decimals: tuple[int, int] = (18, 18)
) -> tuple[CurrencyDeclaration, CurrencyDeclaration]:
    return (
        declaration(POOL.currency0, FEED0, decimals=decimals[0], description="ETH / USD"),
        declaration(POOL.currency1, FEED1, decimals=decimals[1], description="USDC / USD"),
    )


def currency_feeds(*, decimals: tuple[int, int] = (18, 18)) -> tuple[CurrencyFeed, CurrencyFeed]:
    first, second = declarations(decimals=decimals)
    return currency_feed(CHAIN, first), currency_feed(CHAIN, second)


def capture(
    source: str,
    feed: CurrencyFeed,
    *,
    answer: int = 10**8,
    feed_decimals: int = FEED_DECIMALS,
    wire_decimals: int | None = None,
    code: str = RUNTIME,
    block: BlockSnapshot = DECISION,
    started: int | None = None,
    updated: int | None = None,
) -> CurrencyFeedCapture:
    """One provider's exact eight exchanges for one currency's pinned feed."""
    b = block.ref
    pin = {"blockHash": b.block_hash, "requireCanonical": True}
    policy = feed.policy
    header = {
        "number": hex(b.number),
        "hash": b.block_hash,
        "parentHash": block.parent_hash,
        "timestamp": hex(b.timestamp),
    }
    specs = [
        ("eth_chainId", [], hex(b.chain_id)),
        ("eth_getBlockByNumber", [hex(b.number), False], header),
        ("eth_getCode", [policy.address, pin], code),
        (
            "eth_call",
            [{"to": policy.address, "data": selector("decimals()")}, pin],
            "0x" + encode(["uint8"], [feed_decimals]).hex(),
        ),
        (
            "eth_call",
            [{"to": policy.address, "data": selector("description()")}, pin],
            "0x" + encode(["string"], [policy.description]).hex(),
        ),
        (
            "eth_call",
            [{"to": policy.address, "data": selector("latestRoundData()")}, pin],
            "0x"
            + encode(
                ["uint80", "int256", "uint256", "uint256", "uint80"],
                [
                    2,
                    answer,
                    b.timestamp - 100 if started is None else started,
                    b.timestamp if updated is None else updated,
                    2,
                ],
            ).hex(),
        ),
        (
            "eth_call",
            [{"to": feed.address, "data": selector("decimals()")}, pin],
            "0x"
            + encode(["uint8"], [feed.decimals if wire_decimals is None else wire_decimals]).hex(),
        ),
        ("eth_getBlockByNumber", [hex(b.number), False], header),
    ]
    if feed.address == ZERO_ADDRESS:
        specs.pop(6)
    exchanges = tuple(
        RpcExchange(
            method,
            index,
            canonical_json({"jsonrpc": "2.0", "id": index, "method": method, "params": params}),
            canonical_json({"jsonrpc": "2.0", "id": index, "result": result}),
            1,
        )
        for index, (method, params, result) in enumerate(specs, 1)
    )
    return CurrencyFeedCapture(source, feed.address, feed.decimals, feed.policy, exchanges)


def retained(
    store: EvidenceStore,
    *,
    answers: tuple[int, int] = (10**8, 10**8),
    feed_decimals: int = FEED_DECIMALS,
    block: BlockSnapshot = DECISION,
    sources: tuple[str, str] = ("a", "b"),
) -> tuple[ReferenceProvider, ReferenceProvider]:
    """Both currencies' feeds, as both declared providers observed them."""
    first, second = currency_feeds()
    return tuple(
        ReferenceProvider(
            source=source,
            currency0=persist_currency_feed_evidence(
                store,
                capture(
                    source,
                    first,
                    answer=answers[0],
                    feed_decimals=feed_decimals,
                    block=block,
                ),
            ),
            currency1=persist_currency_feed_evidence(
                store,
                capture(
                    source,
                    second,
                    answer=answers[1],
                    feed_decimals=feed_decimals,
                    block=block,
                ),
            ),
        )
        for source in sources
    )


def derive(
    store: EvidenceStore,
    window: object,
    providers: tuple[ReferenceProvider, ReferenceProvider],
    *,
    bound: str = "0.05",
    pool: object = POOL,
) -> object:
    first, second = currency_feeds()
    return agreed_reference_price(
        store,
        window,  # type: ignore[arg-type]
        pool=pool,  # type: ignore[arg-type]
        currency0=first,
        currency1=second,
        providers=providers,
        max_deviation=reference_deviation(bound),
    )


def test_the_ratio_is_base_units_of_currency0_in_base_units_of_currency1() -> None:
    # WETH at 2000 USD (18 decimals) against USDC at 1 USD (6 decimals): one raw
    # wei is 2e-9 of one raw USDC unit, in the Q192 unit sqrtPriceX96 ** 2 uses.
    weth = FeedQuote(2000 * 10**8, FEED_DECIMALS, 18)
    usdc = FeedQuote(10**8, FEED_DECIMALS, 6)
    assert reference_price_x192(weth, usdc) == 2 * Q192 // 10**9
    # Reversing the pair inverts the price instead of silently reusing it.
    assert reference_price_x192(usdc, weth) == 5 * 10**8 * Q192


def test_equal_whole_token_prices_and_scales_are_the_identity() -> None:
    assert reference_price_x192(FeedQuote(10**8, 8, 18), FeedQuote(10**8, 8, 18)) == Q192
    # The same 1 USD answer with ten feed decimals is the same price.
    assert reference_price_x192(FeedQuote(10**10, 10, 18), FeedQuote(10**8, 8, 18)) == Q192


def test_feed_and_token_decimals_are_not_interchangeable() -> None:
    # Equal answers with a six-decimal currency1 is 10^-12 of the identity, so a
    # mis-declared token scale changes the value rather than cancelling out.
    assert reference_price_x192(FeedQuote(10**8, 8, 18), FeedQuote(10**8, 8, 6)) == Q192 // 10**12


def test_a_ratio_that_floors_to_zero_is_refused() -> None:
    with pytest.raises(ValueError, match="zero reference price"):
        reference_price_x192(FeedQuote(1, 0, 0), FeedQuote(10**60, 0, 0))


def test_untyped_quotes_are_refused() -> None:
    quote = FeedQuote(10**8, 8, 18)
    with pytest.raises(TypeError, match="currency0"):
        reference_price_x192(10**8, quote)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="currency1"):
        reference_price_x192(quote, quote.answer)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "text,expected",
    [("0", Decimal(0)), ("0.05", Decimal("0.05")), ("1.0", Decimal(1))],
)
def test_the_deviation_bound_is_parsed_exactly(text: str, expected: Decimal) -> None:
    assert reference_deviation(text) == expected


@pytest.mark.parametrize(
    "text",
    ["", " ", "1.5", "-0.1", "0.0000000000000000001", "0,5", "NaN", "Infinity", "0x1"],
)
def test_loose_deviation_bounds_are_refused(text: str) -> None:
    with pytest.raises(ValueError):
        reference_deviation(text)


def test_a_deviation_bound_must_be_an_explicit_string() -> None:
    with pytest.raises(ValueError, match="explicit max_reference_deviation"):
        reference_deviation(Decimal("0.05"))  # type: ignore[arg-type]


def test_deviation_precision_is_not_rounded_by_the_decimal_context() -> None:
    with localcontext() as context:
        context.prec = 4
        with pytest.raises(ValueError, match="18 decimal places"):
            reference_deviation("0.1234567890123456789")


@pytest.mark.parametrize("bound", ["1.5", "-0.1", "NaN", "Infinity", "0.0000000000000000001"])
def test_direct_reference_producer_refuses_invalid_bounds_before_writing(
    tmp_path: Path, bound: str
) -> None:
    store, _refs, window = market(tmp_path)
    try:
        providers = retained(store)
        first, second = currency_feeds()
        before = evidence_rows(tmp_path / "evidence.db")
        with pytest.raises(ValueError, match="max_reference_deviation"):
            agreed_reference_price(
                store,
                window,
                pool=POOL,
                currency0=first,
                currency1=second,
                providers=providers,
                max_deviation=Decimal(bound),
            )
        assert evidence_rows(tmp_path / "evidence.db") == before
    finally:
        store.close()


def test_feed_pin_requires_an_explicit_usd_description() -> None:
    with pytest.raises(ValueError, match="USD feed description"):
        FeedPin(
            address=FEED0,
            description="ETH/USD",
            runtime_keccak=RUNTIME_PIN,
            max_age_seconds=MAX_AGE,
        )
    # An empty base symbol passes the pin's own suffix rule but not the policy.
    with pytest.raises(ValueError, match="USD feed description"):
        currency_feed(
            CHAIN,
            CurrencyDeclaration(
                address=POOL.currency0,
                decimals=18,
                feed=FeedPin(
                    address=FEED0,
                    description=" / USD",
                    runtime_keccak=RUNTIME_PIN,
                    max_age_seconds=MAX_AGE,
                ),
            ),
        )


def test_currency_feed_binds_the_declaration_to_its_feed() -> None:
    first, second = currency_feeds()
    assert first.policy.base_token == POOL.currency0
    assert first.policy.address == FEED0
    assert first.policy.chain_id == CHAIN
    assert second.policy.base_token == POOL.currency1
    with pytest.raises(TypeError, match="typed currency declaration"):
        currency_feed(CHAIN, POOL.currency0)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="denomination mismatch"):
        CurrencyFeed(POOL.currency0, 18, second.policy)
    with pytest.raises(ValueError, match="bounded integer"):
        CurrencyFeed(POOL.currency0, True, first.policy)
    with pytest.raises(ValueError, match="bounded integer"):
        CurrencyFeed(POOL.currency0, 37, first.policy)


def test_currency_feed_capture_reconstructs_and_retains_its_own_bytes(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path / "evidence.db")
    try:
        first, _second = currency_feeds()
        observation = capture("a", first, answer=2000 * 10**8)
        header, quote, decimals = observation.reconstruct()
        assert header == DECISION
        assert quote.answer == 2000 * 10**8 and quote.decimals == FEED_DECIMALS
        assert decimals == 18
        reference = persist_currency_feed_evidence(store, observation)
        assert reference.kind == CURRENCY_FEED_KIND
        assert reference.block == DECISION_BLOCK
        assert replay_currency_feed_evidence(store, reference, currency=first) == observation
        record = store.get_record(reference)
        assert record.payload == observation.payload
        body = json.loads(record.payload)
        assert body["schema"] == "oasis.currency-feed-observation.v1"
        assert body["currency_decimals"] == 18
        assert body["observation"]["answer"] == 2000 * 10**8
        interpretation = json.loads(record.interpretation_payload)
        assert interpretation["execution_eligible"] is False
        assert interpretation["validation"] == "raw_rpc_reconstruction"
        # The retained envelopes are the capture's own exchanges, not a summary.
        assert record.request_payload is not None
        restored = json.loads(record.request_payload)
        assert len(restored["exchanges"]) == 8
    finally:
        store.close()


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ("incomplete", "incomplete or oversized"),
        ("runtime", "feed runtime mismatch"),
        ("stale", "stale"),
        ("moved", "block changed"),
        ("wire_decimals", "currency token decimals mismatch"),
    ],
)
def test_a_capture_that_does_not_reconstruct_is_refused(change: str, error: str) -> None:
    first, _second = currency_feeds()
    if change == "incomplete":
        complete = capture("a", first)
        with pytest.raises(ValueError, match=error):
            replace(complete, exchanges=complete.exchanges[:-1])
    elif change == "runtime":
        with pytest.raises(ValueError, match=error):
            capture("a", first, code="0x6001")
    elif change == "stale":
        stale = LATE.ref.timestamp - MAX_AGE
        with pytest.raises(ValueError, match=error):
            capture("a", first, block=LATE, started=stale - 100, updated=stale - 1)
    elif change == "moved":
        complete = capture("a", first)
        other = BlockSnapshot(
            BlockRef(CHAIN, DECISION.ref.number, "0x" + "f" * 64, 9_999), DECISION.parent_hash
        )
        closing = complete.exchanges[-1]
        moved = (
            *complete.exchanges[:-1],
            RpcExchange(
                closing.method,
                closing.request_id,
                closing.request_payload,
                canonical_json(
                    {"jsonrpc": "2.0", "id": closing.request_id, "result": _raw_header(other)}
                ),
                closing.attempt,
            ),
        )
        with pytest.raises(ValueError, match=error):
            replace(complete, exchanges=moved)
    else:
        with pytest.raises(ValueError, match=error):
            capture("a", first, wire_decimals=17)


def _raw_header(snapshot: BlockSnapshot) -> dict[str, object]:
    return {
        "number": hex(snapshot.ref.number),
        "hash": snapshot.ref.block_hash,
        "parentHash": snapshot.parent_hash,
        "timestamp": hex(snapshot.ref.timestamp),
    }


def test_a_forged_retained_payload_is_refused(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path / "evidence.db")
    try:
        first, _second = currency_feeds()
        reference = persist_currency_feed_evidence(store, capture("a", first))
        record = store.get_record(reference)
        payload = json.loads(record.payload)
        payload["observation"]["updated_at"] += 1
        forged = store.put(
            reference.kind,
            canonical_json(payload),
            reference.block,
            reference.source,
            request=json.loads(record.request_payload),
            raw_response=json.loads(record.raw_response_payload),
            interpretation=json.loads(record.interpretation_payload),
        )
        with pytest.raises(ValueError, match="interpretation differs"):
            replay_currency_feed_evidence(store, forged, currency=first)
        assert replay_currency_feed_evidence(store, reference, currency=first) == capture(
            "a", first
        )
    finally:
        store.close()


def test_replay_refuses_a_reference_that_is_not_the_block_it_names(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path / "evidence.db")
    try:
        first, _second = currency_feeds()
        observation = capture("a", first)
        misplaced = store.put(
            CURRENCY_FEED_KIND,
            observation.payload,
            HEADERS[1].ref,
            "a",
            request=wire_envelope(observation.exchanges, response=False),
            raw_response=wire_envelope(observation.exchanges, response=True),
        )
        with pytest.raises(ValueError, match="not the block it claims"):
            replay_currency_feed_evidence(store, misplaced, currency=first)
        with pytest.raises(ValueError, match="scope mismatch"):
            replay_currency_feed_evidence(
                store, replace(misplaced, kind="price_feed_observation"), currency=first
            )
        absent = replace(misplaced, content_hash="a" * 64)
        with pytest.raises(ValueError, match="not retained"):
            replay_currency_feed_evidence(store, absent, currency=first)
    finally:
        store.close()


class FeedPoster:
    """The exact RPC surface one currency feed collection hits, in its own order."""

    def __init__(
        self,
        feed: CurrencyFeed,
        *,
        answer: int = 10**8,
        feed_decimals: int = FEED_DECIMALS,
        closing: BlockSnapshot | None = None,
    ) -> None:
        self.feed = feed
        self.answer = answer
        self.feed_decimals = feed_decimals
        self.headers = (DECISION, closing or DECISION)
        self.reads = 0

    def post(self, raw: bytes) -> bytes:
        request = json.loads(raw)
        method, params = request["method"], request["params"]
        if method == "eth_chainId":
            result: object = hex(CHAIN)
        elif method == "eth_getBlockByNumber":
            index = min(self.reads, 1)
            self.reads += 1
            result = _raw_header(self.headers[index])
        elif method == "eth_getCode":
            result = RUNTIME
        elif method == "eth_call":
            result = self._call(params)
        else:
            raise AssertionError(method)
        return canonical_json({"id": request["id"], "jsonrpc": "2.0", "result": result})

    def _call(self, params: list[object]) -> str:
        call = params[0]
        assert isinstance(call, dict)
        to, data = call["to"], call["data"]
        b = DECISION.ref
        if to == self.feed.policy.address:
            if data == selector("decimals()"):
                return "0x" + encode(["uint8"], [self.feed_decimals]).hex()
            if data == selector("description()"):
                return "0x" + encode(["string"], [self.feed.policy.description]).hex()
            if data == selector("latestRoundData()"):
                return (
                    "0x"
                    + encode(
                        ["uint80", "int256", "uint256", "uint256", "uint80"],
                        [2, self.answer, b.timestamp - 100, b.timestamp, 2],
                    ).hex()
                )
        elif to == self.feed.address and data == selector("decimals()"):
            return "0x" + encode(["uint8"], [self.feed.decimals]).hex()
        raise AssertionError(data)


def session(feed: CurrencyFeed, source: str, **poster: object):
    recorder = BoundedExchangeRecorder()
    transport = JsonRpcTransport(
        "https://fixture.invalid", http=FeedPoster(feed, **poster), recorder=recorder
    )
    return transport, recorder, source


def test_collection_reads_the_currency_scale_beside_the_feed() -> None:
    first, _second = currency_feeds()
    observation = collect_currency_feed(
        session(first, "a", answer=2000 * 10**8),
        currency=first,
        block_number=DECISION.ref.number,
    )
    assert len(observation.exchanges) == 8
    header, quote, decimals = observation.reconstruct()
    assert header == DECISION and decimals == 18
    assert quote.answer == 2000 * 10**8


def test_native_eth_capture_replays_without_a_fabricated_decimals_call(tmp_path: Path) -> None:
    native = currency_feed(
        CHAIN, declaration(ZERO_ADDRESS, FEED0, decimals=18, description="ETH / USD")
    )
    observation = collect_currency_feed(
        session(native, "a", answer=2000 * 10**8),
        currency=native,
        block_number=DECISION.ref.number,
    )
    assert len(observation.exchanges) == 7
    for exchange in observation.exchanges:
        request = json.loads(exchange.request_payload)
        if request["method"] == "eth_call":
            assert request["params"][0]["to"] != ZERO_ADDRESS
    header, quote, decimals = observation.reconstruct()
    assert header == DECISION and decimals == 18
    assert quote.answer == 2000 * 10**8
    store = EvidenceStore(tmp_path / "native.db")
    try:
        reference = persist_currency_feed_evidence(store, observation)
        assert replay_currency_feed_evidence(store, reference, currency=native) == observation
    finally:
        store.close()


@pytest.mark.parametrize(
    ("chain", "decimals", "description"),
    [(CHAIN, 6, "ETH / USD"), (56, 18, "ETH / USD"), (CHAIN, 18, "USDC / USD")],
)
def test_native_eth_refuses_an_unrecognized_protocol_scale_or_feed(
    chain: int, decimals: int, description: str
) -> None:
    with pytest.raises(ValueError, match="native currency requires"):
        currency_feed(
            chain, declaration(ZERO_ADDRESS, FEED0, decimals=decimals, description=description)
        )


def test_collection_refuses_a_block_that_moved_mid_read() -> None:
    first, _second = currency_feeds()
    reorged = BlockSnapshot(
        BlockRef(CHAIN, DECISION.ref.number, "0x" + "e" * 64, 9_999), DECISION.parent_hash
    )
    with pytest.raises(ValueError, match="block moved during collection"):
        collect_currency_feed(
            session(first, "a", closing=reorged),
            currency=first,
            block_number=DECISION.ref.number,
        )


def test_a_reference_declaration_must_describe_the_pools_own_currencies() -> None:
    first, second = declarations()
    with pytest.raises(ValueError, match="pool's own currencies"):
        ReferenceDeclaration(
            pool=POOL,
            currency0=second,
            currency1=first,
            providers=("a", "b"),
            max_reference_deviation="0.05",
        )
    with pytest.raises(ValueError, match="two distinct reference providers"):
        ReferenceDeclaration(
            pool=POOL,
            currency0=first,
            currency1=second,
            providers=("a", "a"),
            max_reference_deviation="0.05",
        )
    with pytest.raises(ValueError):
        ReferenceDeclaration(
            pool=POOL,
            currency0=first,
            currency1=second,
            providers=("a", "b"),
            max_reference_deviation="1.5",
        )


def test_a_reference_request_needs_two_retained_providers(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path / "evidence.db")
    try:
        providers = retained(store)
        first, second = declarations()
        request = ReferenceRequest(
            pool=POOL,
            currency0=first,
            currency1=second,
            providers=providers,
            max_reference_deviation="0.05",
        )
        assert [item.source for item in request.providers] == ["a", "b"]
        assert (
            ReferenceRequest.from_json(canonical_json(request.model_dump(mode="json"))) == request
        )
        with pytest.raises(ValueError, match="two distinct reference providers"):
            ReferenceRequest(
                pool=POOL,
                currency0=first,
                currency1=second,
                providers=(providers[0], providers[1].model_copy(update={"source": "a"})),
                max_reference_deviation="0.05",
            )
        with pytest.raises(ValueError):
            ReferenceRequest(
                pool=POOL,
                currency0=first,
                currency1=second,
                providers=("a", "b"),
                max_reference_deviation="0.05",
            )
    finally:
        store.close()


def test_the_derived_reference_is_two_provider_agreed_evidence(tmp_path: Path) -> None:
    store, refs, window = market(tmp_path)
    try:
        providers = retained(store)
        first, second = currency_feeds()
        reference = derive(store, window, providers)
        assert reference.name == REFERENCE_NAME
        assert reference.value == Q96**2
        assert reference.block == DECISION_BLOCK
        assert frozenset(item.source for item in reference.evidence) == {"a", "b"}
        donors = {item.source: store.get_record(item.currency0) for item in providers}
        parents = sorted(
            tuple(item.currency0 for item in providers)
            + tuple(item.currency1 for item in providers),
            key=lambda item: (item.source, item.content_hash),
        )
        for item in reference.evidence:
            record = store.get_record(item)
            assert item.kind == DERIVED_REFERENCE_KIND
            assert record.payload == agreed_value_payload(REFERENCE_NAME, Q96**2, DECISION_BLOCK)
            body = json.loads(record.interpretation_payload)
            assert body["schema"] == REFERENCE_SCHEMA
            assert body["execution_eligible"] is False
            assert body["bytes_kind"] == (
                "currency_feed_observation_wire_envelope_that_pinned_this_value"
            )
            assert body["pool_id"] == window.pool_id
            assert body["currencies"] == [POOL.currency0, POOL.currency1]
            assert body["currency_decimals"] == [18, 18]
            assert body["feed_pins"] == [asdict(first.policy), asdict(second.policy)]
            assert body["pool_price_x192"] == Q96**2
            assert body["max_reference_deviation"] == "0.05"
            assert [entry["content_hash"] for entry in body["parents"]] == [
                parent.content_hash for parent in parents
            ]
            # Each derived record borrows its own provider's currency0 envelope.
            donor = donors[item.source]
            assert record.request_payload == donor.request_payload
            assert record.raw_response_payload == donor.raw_response_payload
            assert item.retrieved_at == donor.reference.retrieved_at
        # The derived value is what the cycle assembler consumes.
        assembled = assemble(store, refs, window, reference_price_x192=reference)
        assert assembled.cycle.state.block == DECISION_BLOCK
        assert assembled.cycle.input_evidence.kind == "shadow_lp_cycle_input"
    finally:
        store.close()


@pytest.mark.parametrize(
    ("bound", "refused"),
    [("0.75", False), ("0.749999999999999999", True)],
)
def test_the_deviation_bound_is_exact_integer_arithmetic(
    tmp_path: Path, bound: str, refused: bool
) -> None:
    store, _refs, window = market(tmp_path)
    try:
        providers = retained(store)
        # A pool price of (2 * Q96) ** 2 against a derived Q192 is exactly 0.75 away.
        displaced = replace(window, prices=(retain_agreed(store, "sqrt_price_x96", 2 * Q96),))
        before = evidence_rows(tmp_path / "evidence.db")
        if refused:
            with pytest.raises(ValueError, match="not a plausible price"):
                derive(store, displaced, providers, bound=bound)
            assert evidence_rows(tmp_path / "evidence.db") == before
        else:
            assert derive(store, displaced, providers, bound=bound).value == Q192
            assert evidence_rows(tmp_path / "evidence.db") == before + 2
    finally:
        store.close()


def test_the_reference_must_come_from_the_windows_own_providers(tmp_path: Path) -> None:
    store, _refs, window = market(tmp_path)
    try:
        providers = retained(store)
        with pytest.raises(ValueError, match="window's own two providers"):
            derive(store, window, (providers[0].model_copy(update={"source": "c"}), providers[1]))
        with pytest.raises(ValueError, match="not its declared provider"):
            derive(
                store,
                window,
                (
                    providers[0].model_copy(update={"currency0": providers[1].currency0}),
                    providers[1],
                ),
            )
    finally:
        store.close()


def test_a_swapped_or_foreign_feed_pair_is_refused(tmp_path: Path) -> None:
    store, _refs, window = market(tmp_path)
    try:
        providers = retained(store)
        with pytest.raises(ValueError):
            derive(
                store,
                window,
                (
                    providers[0].model_copy(update={"currency0": providers[0].currency1}),
                    providers[1],
                ),
            )
        with pytest.raises(ValueError, match="not the market window's pool"):
            derive(store, window, providers, pool=replace(POOL, fee=3000))
        with pytest.raises(ValueError, match="not on the market window's chain"):
            agreed_reference_price(
                store,
                window,
                pool=POOL,
                currency0=currency_feed(CHAIN + 1, declarations()[0]),
                currency1=currency_feeds()[1],
                providers=providers,
                max_deviation=Decimal("0.05"),
            )
    finally:
        store.close()


def test_a_reference_observed_at_another_block_is_refused(tmp_path: Path) -> None:
    store, _refs, window = market(tmp_path)
    try:
        earlier = retained(store, block=HEADERS[1])
        before = evidence_rows(tmp_path / "evidence.db")
        with pytest.raises(ValueError, match="not the decision block"):
            derive(store, window, earlier)
        assert evidence_rows(tmp_path / "evidence.db") == before
    finally:
        store.close()


def test_providers_that_disagree_on_the_ratio_are_refused(tmp_path: Path) -> None:
    store, _refs, window = market(tmp_path)
    try:
        first, second = currency_feeds()

        def observed(source: str, answer0: int, answer1: int) -> ReferenceProvider:
            return ReferenceProvider(
                source=source,
                currency0=persist_currency_feed_evidence(
                    store, capture(source, first, answer=answer0)
                ),
                currency1=persist_currency_feed_evidence(
                    store, capture(source, second, answer=answer1)
                ),
            )

        providers = (observed("a", 10**8, 10**8), observed("b", 2 * 10**8, 10**8))
        before = evidence_rows(tmp_path / "evidence.db")
        with pytest.raises(ValueError, match="disagree on the value"):
            derive(store, window, providers)
        assert evidence_rows(tmp_path / "evidence.db") == before
    finally:
        store.close()


def test_the_reference_input_types_are_enforced(tmp_path: Path) -> None:
    store, _refs, window = market(tmp_path)
    try:
        providers = retained(store)
        first, second = currency_feeds()
        with pytest.raises(TypeError, match="evidence store"):
            agreed_reference_price(
                "store",  # type: ignore[arg-type]
                window,
                pool=POOL,
                currency0=first,
                currency1=second,
                providers=providers,
                max_deviation=Decimal("0.05"),
            )
        with pytest.raises(TypeError, match="verified market window"):
            derive(store, asdict(window), providers)
        with pytest.raises(TypeError, match="typed pool key"):
            derive(store, window, providers, pool=asdict(POOL))
        with pytest.raises(ValueError, match="two distinct reference providers"):
            derive(store, window, (providers[0],))
        with pytest.raises(TypeError, match="max_reference_deviation"):
            agreed_reference_price(
                store,
                window,
                pool=POOL,
                currency0=first,
                currency1=second,
                providers=providers,
                max_deviation="0.05",  # type: ignore[arg-type]
            )
    finally:
        store.close()
