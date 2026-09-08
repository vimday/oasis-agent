"""Unit tests for the live shadow observation producer (WP6)."""

import json
from decimal import Decimal

import pytest
from eth_abi import encode

from oasis_agent.evidence.rpc_capture import BoundedExchangeRecorder
from oasis_agent.farming.strategy import (
    Band,
    BandTableFeeStrategy,
    OnchainAmountBandFeePolicy,
)
from oasis_agent.models.chain import BlockSnapshot, LogRecord
from oasis_agent.models.common import BlockRef, EvidenceRef
from oasis_agent.shadow.producer import (
    ObservationReport,
    ObservedPoolKey,
    SwapQuoteComparison,
    build_observation,
    open_observation_rpc,
    swap_state,
)
from oasis_agent.uniswap_v4.events import SWAP_TOPIC

CHAIN = 46630
TOKEN0 = "0x" + "10" * 20
TOKEN1 = "0x" + "20" * 20
HOOK = "0x" + "30" * 20
MANAGER = "0x" + "40" * 20
POOL_ID = "0x" + "5b" * 32


def evidence_ref(block: BlockRef) -> EvidenceRef:
    return EvidenceRef(
        kind="canonical_block_range",
        content_hash="aa" * 32,
        block=block,
        source="fixture",
        retrieved_at="2026-09-07T00:00:00+00:00",
        request_hash="01" * 32,
        raw_response_hash="02" * 32,
        interpretation_hash="03" * 32,
        schema_version=2,
    )


def strategy() -> BandTableFeeStrategy:
    return BandTableFeeStrategy(
        min_fee=100,
        max_fee=10_000,
        base_fee=500,
        volatility_bands=(Band(100, 100), Band(1_000, 400)),
        impact_bands0=(Band(1_000_000, 200), Band(10_000_000, 800)),
        impact_bands1=(Band(1_000, 200), Band(10_000, 800)),
        toxicity_bands=(Band(100, 100), Band(500, 300)),
        inventory_bands=(Band(100, 100), Band(500, 500)),
    )


def pool_key() -> ObservedPoolKey:
    return ObservedPoolKey(CHAIN, TOKEN0, TOKEN1, HOOK, POOL_ID)


def deployed_policy() -> OnchainAmountBandFeePolicy:
    return OnchainAmountBandFeePolicy(
        500,
        2_000,
        5_000,
        10_000_000,
        100_000_000,
        10_000_000,
        100_000_000,
        1,
    )


def swap_log(
    block_number: int,
    *,
    zero_for_one: bool,
    amount0: int,
    amount1: int,
    sqrt_price: int,
    liquidity: int,
    tick: int,
    fee: int,
    tx_hash: str = "0x" + "aa" * 32,
    log_index: int = 0,
) -> LogRecord:
    data = encode(
        ["int128", "int128", "uint160", "uint128", "int24", "uint24"],
        [amount0, amount1, sqrt_price, liquidity, tick, fee],
    )
    return LogRecord(
        address=MANAGER,
        topics=(SWAP_TOPIC, POOL_ID, "0x" + "00" * 12 + "99" * 20),
        data="0x" + data.hex(),
        block_number=block_number,
        transaction_hash=tx_hash,
        log_index=log_index,
        block_hash="0x" + f"{block_number:064x}",
    )


def blocks(first: int, count: int) -> tuple[BlockSnapshot, ...]:
    previous = "0x" + "00" * 32
    result = []
    for offset in range(count):
        number = first + offset
        snapshot = BlockSnapshot(
            BlockRef(CHAIN, number, "0x" + f"{number:064x}", 1_000 + number), previous
        )
        previous = snapshot.ref.block_hash
        result.append(snapshot)
    return tuple(result)


def make_dataset(
    start: int, logs: tuple[LogRecord, ...]
) -> tuple[tuple[BlockSnapshot, ...], tuple[LogRecord, ...]]:
    snapshots = blocks(start, 10)
    return snapshots, logs


def test_swap_state_volatility_and_skew() -> None:
    decision = BlockRef(CHAIN, 10, "0x" + "11" * 32, 1_010)
    swaps = [
        type("S", (), {"sqrt_price_x96": 2**96})(),
        type("S", (), {"sqrt_price_x96": 2 * 2**96})(),
    ]
    state = swap_state(
        decision_block=decision,
        price_history=tuple(swaps),  # type: ignore[arg-type]
    )
    assert state.volatility == Decimal(3)
    assert state.inventory_imbalance == 0
    assert state.observed_at_timestamp == 1_010
    assert state.block == decision


def test_swap_state_validates_explicit_reference_and_falls_back_only_when_absent() -> None:
    decision = BlockRef(CHAIN, 10, "0x" + "11" * 32, 1_010)
    swaps = (
        type("S", (), {"sqrt_price_x96": 2**96})(),
        type("S", (), {"sqrt_price_x96": 2 * 2**96})(),
    )
    # An explicit reference is compared, not silently replaced.
    explicit = swap_state(
        decision_block=decision,
        price_history=swaps,  # type: ignore[arg-type]
        reference_sqrt_price_x96=2**96,
    )
    assert explicit.oracle_deviation == Decimal(3)  # (4-1)/1 in squared prices
    for invalid in (0, -1, 2**160):
        with pytest.raises(ValueError, match="reference sqrt price"):
            swap_state(
                decision_block=decision,
                price_history=swaps,  # type: ignore[arg-type]
                reference_sqrt_price_x96=invalid,
            )
    # No reference: the last pool price is the explicit research fallback.
    fallback = swap_state(decision_block=decision, price_history=swaps)  # type: ignore[arg-type]
    assert fallback.oracle_deviation == 0


def test_build_observation_quotes_both_modes() -> None:
    snapshots = blocks(100, 20)
    s1 = swap_log(
        101,
        zero_for_one=True,
        amount0=1_000_000,
        amount1=-999_000,
        sqrt_price=2**96,
        liquidity=10**18,
        tick=-5,
        fee=500,
    )
    s2 = swap_log(
        102,
        zero_for_one=False,
        amount0=-990_000,
        amount1=1_000_000,
        sqrt_price=int(1.01 * 2**96),
        liquidity=10**18,
        tick=1,
        fee=500,
        log_index=1,
    )
    dataset = type(
        "Dataset",
        (),
        {
            "blocks": snapshots,
            "logs": (s1, s2),
            "start_block": 100,
            "end_block": 119,
            "content_hash": "0x" + "00" * 32,
            "log_filter": {},
            "chain_id": CHAIN,
            "source": "fixture",
        },
    )
    report = build_observation(
        dataset=dataset,  # type: ignore[arg-type]
        window_evidence=evidence_ref(snapshots[-1].ref),
        pool=pool_key(),
        strategy=strategy(),
        min_benefit=Decimal(0),
        source="fixture",
        deployed_hook_policy=deployed_policy(),
    )
    assert isinstance(report, ObservationReport)
    assert report.swap_count == 2
    assert report.execution_eligible is False
    assert report.trust == "single_provider_research"
    first, second = report.comparisons
    assert isinstance(first, SwapQuoteComparison)
    assert first.zero_for_one is True
    assert first.observed_fee_pips == 500
    assert first.quote_exact_input_fee_pips is not None
    assert first.deployed_hook_exact_input_fee_pips == 500
    assert first.deployed_hook_exact_output_fee_pips == 500
    assert first.exact_fee_match is True
    assert first.transaction_hash.startswith("0x")
    assert second.direction == "one_for_zero"
    digest = report.digest
    assert len(digest) == 64
    payload = json.loads(report.payload)
    assert payload["execution_eligible"] is False
    assert payload["schema"] == "oasis.lp-live-shadow-observation.v2"


def test_exact_output_quote_uses_output_delta_and_ambiguous_mode_stays_unknown() -> None:
    snapshots = blocks(300, 2)
    swap = swap_log(
        300,
        zero_for_one=True,
        amount0=10_000,
        amount1=-100,
        sqrt_price=2**96,
        liquidity=10**18,
        tick=0,
        fee=2_000,
    )
    dataset = type(
        "Dataset",
        (),
        {
            "blocks": snapshots,
            "logs": (swap,),
            "start_block": 300,
            "end_block": 301,
        },
    )
    policy = OnchainAmountBandFeePolicy(500, 2_000, 5_000, 1_000, 10_000, 10, 1_000, 1)
    report = build_observation(
        dataset=dataset,  # type: ignore[arg-type]
        window_evidence=evidence_ref(snapshots[-1].ref),
        pool=pool_key(),
        strategy=strategy(),
        min_benefit=Decimal(0),
        source="fixture",
        deployed_hook_policy=policy,
    )
    comparison = report.comparisons[0]
    assert comparison.deployed_hook_exact_input_fee_pips == 5_000
    assert comparison.deployed_hook_exact_output_fee_pips == 2_000
    assert comparison.exact_fee_match is None


def test_build_observation_rejects_foreign_pool_swap() -> None:
    snapshots = blocks(200, 10)
    foreign = swap_log(
        201,
        zero_for_one=True,
        amount0=1,
        amount1=-1,
        sqrt_price=2**96,
        liquidity=1,
        tick=0,
        fee=500,
    )
    foreign = LogRecord(
        address=MANAGER,
        topics=(SWAP_TOPIC, "0x" + "ff" * 32, "0x" + "00" * 12 + "99" * 20),
        data=foreign.data,
        block_number=201,
        transaction_hash="0x" + "bb" * 32,
        log_index=0,
        block_hash="0x" + "c9" * 32,
    )
    dataset = type(
        "Dataset",
        (),
        {
            "blocks": snapshots,
            "logs": (foreign,),
            "start_block": 200,
            "end_block": 209,
            "content_hash": "0x" + "00" * 32,
            "log_filter": {},
            "chain_id": CHAIN,
            "source": "fixture",
        },
    )
    with pytest.raises(ValueError, match="pinned pool"):
        build_observation(
            dataset=dataset,  # type: ignore[arg-type]
            window_evidence=type("R", (), {}),  # type: ignore[arg-type]
            pool=pool_key(),
            strategy=strategy(),
            min_benefit=Decimal(0),
            source="fixture",
        )


def test_report_rejects_execution_eligible() -> None:
    # execution_eligible is an init=False frozen field: the constructor must
    # reject any attempt to pass it, so no report can ever carry that flag.
    with pytest.raises(TypeError, match="execution_eligible"):
        ObservationReport(
            chain_id=CHAIN,
            source="fixture",
            start_block=1,
            end_block=2,
            pool_id=POOL_ID,
            window_evidence=type("R", (), {}),  # type: ignore[arg-type]
            block_count=2,
            swap_count=0,
            comparisons=(),
            execution_eligible=True,  # type: ignore[arg-type]
        )
    report = ObservationReport(
        chain_id=CHAIN,
        source="fixture",
        start_block=1,
        end_block=2,
        pool_id=POOL_ID,
        window_evidence=evidence_ref(BlockRef(CHAIN, 2, "0x" + "22" * 32, 1_002)),
        block_count=2,
        swap_count=0,
        comparisons=(),
    )
    assert report.execution_eligible is False
    assert report.trust == "single_provider_research"
    payload = json.loads(report.payload)
    assert payload["execution_eligible"] is False
    # Tampering with the frozen field must never leak into the serialized
    # report: the payload property pins execution_eligible to false.
    object.__setattr__(report, "execution_eligible", True)  # type: ignore[attr-defined]
    assert report.execution_eligible is True  # type: ignore[comparison-overlap]
    payload = json.loads(report.payload)
    assert payload["execution_eligible"] is False


def test_recorder_bounds_fail_closed() -> None:
    recorder = BoundedExchangeRecorder(max_exchanges=1, max_bytes=1_000_000)
    exchange = type(
        "X",
        (),
        {
            "request_payload": b"a",
            "response_payload": b"b",
            "request_id": 1,
            "method": "eth_chainId",
            "attempt": 0,
        },
    )()
    recorder.record(exchange)
    with pytest.raises(ValueError, match="capacity exceeded"):
        recorder.record(exchange)


def test_swap_state_rejects_negative_inventory() -> None:
    with pytest.raises(ValueError, match="nonnegative"):
        swap_state(
            decision_block=BlockRef(CHAIN, 1, "0x" + "11" * 32, 1),
            price_history=(),
            inventory0=-1,
        )


def test_pool_key_validates_ordering_and_identity() -> None:
    with pytest.raises(ValueError, match="token0 < token1"):
        ObservedPoolKey(CHAIN, TOKEN1, TOKEN0, HOOK, POOL_ID)
    with pytest.raises(ValueError, match="pool id"):
        ObservedPoolKey(CHAIN, TOKEN0, TOKEN1, HOOK, "0x1234")


def test_open_observation_rpc_wires_poster_and_chain_guard() -> None:
    chain_id_hex = hex(CHAIN)

    class FakePoster:
        def post(self, payload: bytes) -> bytes:
            request = json.loads(payload)
            if request["method"] == "eth_chainId":
                result = chain_id_hex
            else:
                raise AssertionError(f"unexpected method {request['method']}")
            return json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}).encode()

    recorder = BoundedExchangeRecorder(max_exchanges=10, max_bytes=1_000_000)
    rpc = open_observation_rpc(
        url="https://example.invalid",
        chain_id=CHAIN,
        recorder=recorder,
        poster=FakePoster(),  # type: ignore[arg-type]
    )
    assert rpc.chain_id() == CHAIN
    assert len(recorder.exchanges) == 1
    assert recorder.exchanges[0].method == "eth_chainId"
    with pytest.raises(ValueError, match="unexpected chain id"):
        open_observation_rpc(
            url="https://example.invalid",
            chain_id=CHAIN + 1,
            recorder=BoundedExchangeRecorder(max_exchanges=10, max_bytes=1_000_000),
            poster=FakePoster(),  # type: ignore[arg-type]
        )
