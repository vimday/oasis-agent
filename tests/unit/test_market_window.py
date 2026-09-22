"""Raw two-provider activity and state become causal, pool-side markouts."""

import json
from dataclasses import asdict, replace
from decimal import Decimal

import pytest
from eth_abi.abi import encode
from eth_hash.auto import keccak

from oasis_agent.evidence.rpc_capture import BoundedExchangeRecorder, persist_block_range_evidence
from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.models.common import canonical_json
from oasis_agent.models.v4 import PoolKey, SwapDirection
from oasis_agent.rpc.collector import BlockRangeDataset
from oasis_agent.rpc.transport import JsonRpcTransport
from oasis_agent.shadow.features import produce_pool_state
from oasis_agent.shadow.market_window import build_market_window, validate_market_successor
from oasis_agent.shadow.pool_state_capture import collect_slot0_window, persist_slot0_capture
from oasis_agent.uniswap_v4.events import SWAP_TOPIC
from oasis_agent.uniswap_v4.pool_id import pool_id_from_key
from scripts.build_lp_market_window import build as build_command
from tests.rpc_evidence_helpers import range_exchanges

from .test_live_shadow_producer import CHAIN, MANAGER, blocks, swap_log
from .test_lp_features import agreed

POOL = PoolKey("0x" + "1" * 40, "0x" + "2" * 40, 500, 60, "0x" + "0" * 40)
POOL_ID = pool_id_from_key(POOL)
VIEW = "0x" + "3" * 40
RUNTIME = "0x6000"
PIN = "0x" + keccak(bytes.fromhex(RUNTIME[2:])).hex()
Q96 = 2**96


class StatePoster:
    def __init__(self, headers, prices):
        self.headers, self.prices = headers, prices

    def post(self, raw):
        request = json.loads(raw)
        method, params = request["method"], request["params"]
        if method == "eth_chainId":
            result = hex(CHAIN)
        elif method == "eth_getBlockByNumber":
            h = self.headers[int(params[0], 16) - 100]
            result = {
                "number": hex(h.ref.number),
                "hash": h.ref.block_hash,
                "parentHash": h.parent_hash,
                "timestamp": hex(h.ref.timestamp),
            }
        elif method == "eth_getCode":
            result = RUNTIME
        elif method == "eth_call":
            if params[0]["data"] == "0x" + keccak(b"poolManager()")[:4].hex():
                result = "0x" + encode(["address"], [MANAGER]).hex()
            else:
                index = next(
                    i
                    for i, h in enumerate(self.headers)
                    if h.ref.block_hash == params[1]["blockHash"]
                )
                result = (
                    "0x"
                    + encode(
                        ["uint160", "int24", "uint24", "uint24"], [self.prices[index], 0, 0, 500]
                    ).hex()
                )
        else:
            raise AssertionError(method)
        return canonical_json({"id": request["id"], "jsonrpc": "2.0", "result": result})


def inputs(store, *, empty=False, disagree=False, final_swap=True):
    headers = blocks(100, 3)
    logs = []
    events = ((100, -100, 90), (101, 90, -100), *((102, -100, 90),) * final_swap)
    for index, (number, a0, a1) in enumerate(events if not empty else ()):
        log = swap_log(
            number,
            zero_for_one=a0 < 0,
            amount0=a0,
            amount1=a1,
            sqrt_price=Q96,
            liquidity=100,
            tick=0,
            fee=500,
            log_index=index,
        )
        logs.append(replace(log, transaction_index=0, topics=(SWAP_TOPIC, POOL_ID, log.topics[2])))
    swaps, prices = [], []
    for source in ("a", "b"):
        dataset = BlockRangeDataset(source, headers, tuple(logs), MANAGER, (SWAP_TOPIC, POOL_ID))
        swaps.append(persist_block_range_evidence(store, dataset, range_exchanges(dataset)))
        recorder = BoundedExchangeRecorder()
        values = (Q96, Q96 // 2, Q96 + (1 if disagree and source == "b" else 0))
        rpc = JsonRpcTransport(
            "https://fixture.invalid", http=StatePoster(headers, values), recorder=recorder
        )
        capture = collect_slot0_window(
            (rpc, recorder, source),
            chain_id=CHAIN,
            state_view=VIEW,
            pool_manager=MANAGER,
            pool_id=POOL_ID,
            block_numbers=(100, 101, 102),
            state_view_runtime_keccak=PIN,
            pool_manager_runtime_keccak=PIN,
        )
        prices.append(
            persist_slot0_capture(store, capture, retrieved_at="2026-09-13T00:00:00+00:00")
        )
    return tuple(swaps), tuple(prices)


def build(store, refs, horizon=1):
    return build_market_window(
        store,
        swap_references=refs[0],
        price_references=refs[1],
        pool=POOL,
        pool_manager=MANAGER,
        horizon_seconds=horizon,
        max_lateness_seconds=1,
    )


def test_pool_side_deltas_maturity_and_pending_are_explicit(tmp_path):
    store = EvidenceStore(tmp_path / "evidence.db")
    try:
        refs = inputs(store)
        result = build(store, refs)
        assert result.swap_count == 3 and result.pending_count == 1
        assert not result.markouts_complete
        first, second = result.matured
        assert first.value.direction is SwapDirection.ZERO_FOR_ONE
        assert (first.value.amount0, first.value.amount1) == (100, -90)
        assert first.value.future_price_x192.value == Q96**2 // 4
        assert second.value.direction is SwapDirection.ONE_FOR_ZERO
        assert (second.value.amount0, second.value.amount1) == (-90, 100)
        assert second.value.future_price_x192.value == Q96**2
        assert build(store, refs).payload == result.payload
        assert json.loads(result.payload)["execution_eligible"] is False
        for value in (*result.prices, *(m.value.future_price_x192 for m in result.matured)):
            for reference in value.evidence:
                assert json.loads(store.get(reference))["value"] == value.value
        later = build(store, refs, horizon=2)
        assert later.pending_count == 2
        assert len(later.matured) == 1
        assert later.matured[0].value.future_price_x192.block.number == 102
    finally:
        store.close()


def test_quorum_disagreement_wrong_pool_and_missing_provider_rejected(tmp_path):
    store = EvidenceStore(tmp_path / "evidence.db")
    try:
        with pytest.raises(ValueError, match="disagree"):
            build(store, inputs(store, disagree=True))
        refs = inputs(store)
        with pytest.raises(ValueError):
            build_market_window(
                store,
                swap_references=refs[0],
                price_references=refs[1],
                pool=replace(POOL, fee=3000),
                pool_manager=MANAGER,
                horizon_seconds=1,
                max_lateness_seconds=1,
            )
        with pytest.raises(ValueError):
            build(store, (refs[0][:1], refs[1]))
    finally:
        store.close()


def test_verified_empty_window_is_distinct_from_pending_markouts(tmp_path):
    store = EvidenceStore(tmp_path / "evidence.db")
    try:
        empty = build(store, inputs(store, empty=True))
        assert empty.swap_count == 0 and empty.pending_count == 0 and empty.markouts_complete
        pending = build(store, inputs(store), horizon=10)
        assert pending.pending_count == 3 and not pending.markouts_complete
        assert empty.matured == pending.matured == ()
    finally:
        store.close()


def test_offline_command_replays_store_and_preserves_previous_report(tmp_path):
    evidence = tmp_path / "evidence.db"
    store = EvidenceStore(evidence)
    try:
        swaps, prices = inputs(store)
    finally:
        store.close()
    request = tmp_path / "request.json"
    request.write_bytes(
        canonical_json(
            {
                "swap_references": [asdict(ref) for ref in swaps],
                "price_references": [asdict(ref) for ref in prices],
                "pool": asdict(POOL),
                "pool_manager": MANAGER,
                "horizon_seconds": 1,
                "max_lateness_seconds": 1,
            }
        )
    )
    output = tmp_path / "market.json"
    build_command(request, evidence, output)
    original = output.read_bytes()
    assert json.loads(original)["pending_count"] == 1
    with pytest.raises(ValueError):
        build_command(request, evidence, output)
    assert output.read_bytes() == original


def test_complete_market_window_feeds_existing_lp_features_and_pending_blocks(tmp_path):
    store = EvidenceStore(tmp_path / "features.db")
    try:
        account_and_price = {
            "inventory0": agreed("inventory0", 3000, 102),
            "inventory1": agreed("inventory1", 1000, 102),
            "reference_price_x192": agreed("reference_price_x192", Q96**2, 102),
        }
        complete = build(store, inputs(store, final_swap=False))
        state = produce_pool_state(complete.feature_window(**account_and_price))
        assert state.inventory_imbalance == Decimal("0.5")
        assert state.adverse_selection > 0  # toxic core flows must not become zero-risk input
        assert state.block == complete.decision_block
        pending = build(store, inputs(store))
        with pytest.raises(ValueError, match="markouts"):
            pending.feature_window(**account_and_price)
    finally:
        store.close()


def test_fixed_maturity_tail_does_not_select_on_returns(tmp_path):
    store = EvidenceStore(tmp_path / "rolling.db")
    try:
        refs = inputs(store)
        result = build_market_window(
            store,
            swap_references=refs[0],
            price_references=refs[1],
            pool=POOL,
            pool_manager=MANAGER,
            horizon_seconds=1,
            max_lateness_seconds=1,
            mature_prefix=True,
        )
        assert result.event_cutoff_timestamp == result.decision_block.timestamp - 2
        assert result.pending_count == 0 and result.deferred_count == 2
        assert result.swap_count == len(result.matured) + result.deferred_count
        assert result.matured[0].value.amount0 == 100
        assert result.markouts_complete
        with pytest.raises(ValueError, match="maturity tail"):
            build_market_window(
                store,
                swap_references=refs[0],
                price_references=refs[1],
                pool=POOL,
                pool_manager=MANAGER,
                horizon_seconds=2,
                max_lateness_seconds=1,
                mature_prefix=True,
            )
    finally:
        store.close()


def test_market_sequence_rejects_gaps_reorgs_and_policy_drift(tmp_path):
    store = EvidenceStore(tmp_path / "sequence.db")
    try:
        previous = build(store, inputs(store))
        new_price = agreed("sqrt_price_x96", Q96, 103)
        current = replace(
            previous, decision_block=new_price.block, prices=(*previous.prices[1:], new_price)
        )
        validate_market_successor(previous, current)
        with pytest.raises(ValueError, match="deferred prefix"):
            validate_market_successor(
                replace(previous, event_cutoff_timestamp=1100),
                replace(current, event_cutoff_timestamp=1101),
            )
        with pytest.raises(ValueError, match="overlap"):
            validate_market_successor(previous, replace(current, prices=(new_price,)))
        with pytest.raises(ValueError, match="changed"):
            validate_market_successor(
                previous,
                replace(current, prices=(agreed("sqrt_price_x96", Q96 + 1, 102), new_price)),
            )
        with pytest.raises(ValueError, match="policy"):
            validate_market_successor(previous, replace(current, horizon_seconds=10))
        with pytest.raises(ValueError, match="advance"):
            validate_market_successor(previous, previous)
    finally:
        store.close()
