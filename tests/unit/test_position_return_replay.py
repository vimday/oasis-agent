"""The entire accepted path uses synthetic raw wire proofs, not live LP profits."""

import hashlib
import json
from dataclasses import asdict, replace
from fractions import Fraction
from types import SimpleNamespace

import pytest
from eth_abi.abi import encode
from eth_hash.auto import keccak

from oasis_agent.evidence.ingestion import persist_and_verify_block_range
from oasis_agent.farming.position_capture import (
    persist_position_snapshot_evidence,
    replay_position_snapshot_evidence,
)
from oasis_agent.farming.position_return_replay import (
    PositionReturnRequest,
    ReturnPriceProof,
    replay_position_return,
)
from oasis_agent.farming.price_evidence import persist_price_observation_evidence
from oasis_agent.models.common import ZERO_ADDRESS, canonical_json
from oasis_agent.rpc.transport import RpcExchange
from oasis_agent.shadow.pool_state_capture import (
    Slot0WindowCapture,
    Slot0WindowQuorum,
    persist_slot0_window_evidence,
)
from oasis_agent.uniswap_v4.state_rpc import Slot0State
from scripts.replay_position_return import Request, run
from tests.rpc_evidence_helpers import range_exchanges
from tests.unit.test_position_action_costs import costs
from tests.unit.test_position_action_costs import inputs as action_inputs
from tests.unit.test_position_dataset import OWNER, scenario
from tests.unit.test_price_evidence import price
from tests.unit.test_retained_position import RetainedPosition

PEERS = ("rpc-a", "rpc-b")
STATE_CODE, MANAGER_CODE = "6000", "6001"
STATE_HASH = "0x" + keccak(bytes.fromhex(STATE_CODE)).hex()
MANAGER_HASH = "0x" + keccak(bytes.fromhex(MANAGER_CODE)).hex()


def unchanged_owner(store, request):
    capture = replay_position_snapshot_evidence(store, request.closing[0])
    xs = []
    selector = "0x" + keccak(b"ownerOf(uint256)")[:4].hex()
    for exchange in capture.exchanges:
        raw = json.loads(exchange.response_payload)
        req = json.loads(exchange.request_payload)
        if exchange.method == "eth_call" and req["params"][0]["data"].startswith(selector):
            raw["result"] = "0x" + encode(["address"], [OWNER]).hex()
        xs.append(replace(exchange, response_payload=canonical_json(raw)))
    capture = replace(
        capture, snapshot=replace(capture.snapshot, nft_owner=OWNER), exchanges=tuple(xs)
    )
    closing = tuple(
        persist_position_snapshot_evidence(store, replace(capture, source=s)) for s in PEERS
    )
    _, _, _, nft = scenario()
    nft = replace(nft, logs=())
    ownership = tuple(
        persist_and_verify_block_range(
            store,
            replace(nft, source=s),
            range_exchanges(replace(nft, source=s)),
            research_single_provider=True,
        ).evidence
        for s in PEERS
    )
    return replace(request, closing=closing, ownership=ownership)


def pool_state(store, snapshot):
    block = snapshot.header.ref
    pin = {"blockHash": block.block_hash, "requireCanonical": True}
    header = {
        "number": hex(block.number),
        "hash": block.block_hash,
        "timestamp": hex(block.timestamp),
        "parentHash": snapshot.header.parent_hash,
    }
    state = Slot0State(1 << 96, 0, 0, 3000)
    specs = [
        ("eth_chainId", [], hex(block.chain_id)),
        ("eth_getBlockByNumber", [hex(block.number), False], header),
        ("eth_getCode", [snapshot.state_view, pin], "0x" + STATE_CODE),
        ("eth_getCode", [scenario()[2].address, pin], "0x" + MANAGER_CODE),
        (
            "eth_call",
            [{"to": snapshot.state_view, "data": "0x" + keccak(b"poolManager()")[:4].hex()}, pin],
            "0x" + encode(["address"], [scenario()[2].address]).hex(),
        ),
        (
            "eth_call",
            [
                {
                    "to": snapshot.state_view,
                    "data": "0x"
                    + keccak(b"getSlot0(bytes32)")[:4].hex()
                    + encode(["bytes32"], [bytes.fromhex(snapshot.snapshot.pool_id[2:])]).hex(),
                },
                pin,
            ],
            "0x" + encode(["uint160", "int24", "uint24", "uint24"], [1 << 96, 0, 0, 3000]).hex(),
        ),
        ("eth_getBlockByNumber", [hex(block.number), False], header),
    ]
    xs = tuple(
        RpcExchange(
            m,
            i,
            canonical_json({"jsonrpc": "2.0", "id": i, "method": m, "params": p}),
            canonical_json({"jsonrpc": "2.0", "id": i, "result": r}),
            1,
        )
        for i, (m, p, r) in enumerate(specs, 1)
    )
    captures = tuple(
        Slot0WindowCapture(
            s,
            (snapshot.header,),
            snapshot.state_view,
            scenario()[2].address,
            snapshot.snapshot.pool_id,
            STATE_HASH,
            MANAGER_HASH,
            (state,),
            xs,
        )
        for s in PEERS
    )
    return persist_slot0_window_evidence(store, Slot0WindowQuorum(*captures))


def feed_capture(snapshot, token, answer, *, address):
    context = SimpleNamespace(header=snapshot.header, exchanges=snapshot.exchanges)
    capture = price(context, token, answer)
    xs = []
    for exchange in capture.exchanges:
        raw = json.loads(exchange.request_payload)
        if exchange.method == "eth_getCode":
            raw["params"][0] = address
        elif exchange.method == "eth_call":
            raw["params"][0]["to"] = address
        xs.append(replace(exchange, request_payload=canonical_json(raw)))
    return replace(capture, policy=replace(capture.policy, address=address), exchanges=tuple(xs))


@pytest.fixture
def inputs(tmp_path):
    generator = action_inputs.__wrapped__(tmp_path)
    store, cash, core = next(generator)
    try:
        original = cash
        cash = unchanged_owner(store, cash)
        snapshots = [
            replay_position_snapshot_evidence(store, pair[0])
            for pair in (cash.opening, cash.closing)
        ]
        points = []
        policies = []
        for snapshot in snapshots:
            key = snapshot.snapshot.pool_key
            captures = [
                feed_capture(snapshot, token, answer, address="0x" + address * 20)
                for token, answer, address in (
                    (key.currency0, 333333333, "81"),
                    (key.currency1, 99000000, "82"),
                    (ZERO_ADDRESS, 200000000000, "83"),
                )
            ]
            pairs = [
                tuple(
                    persist_price_observation_evidence(store, replace(c, source=s)) for s in PEERS
                )
                for c in captures
            ]
            native = pairs[2] if snapshot.header.ref == core.header.ref else None
            points.append(ReturnPriceProof(snapshot.header.ref, pairs[0], pairs[1], native))
            policies = [c.policy for c in captures]
        snapshot = snapshots[0].snapshot
        identity = RetainedPosition(
            snapshot.block.chain_id,
            snapshot.position_manager,
            snapshots[0].state_view,
            snapshot.pool_id,
            snapshot.token_id,
            STATE_HASH,
            MANAGER_HASH,
        )
        request = PositionReturnRequest(
            cash,
            identity,
            pool_state(store, snapshots[0]),
            pool_state(store, snapshots[1]),
            tuple(points),
            (costs(store, core),),
            *policies,
        )
        yield store, request, original
    finally:
        generator.close()


def test_entire_raw_chain_produces_conservative_marked_cash_and_gas_return(inputs):
    store, request, _ = inputs
    result = replay_position_return(store, request)
    assert result.owner == OWNER
    assert result.cash_flows[0].amount0 == -100
    assert result.cash_flows[0].amount1 == -200
    # Sub-micro deposits remain debits; truncation toward zero would hide both.
    assert result.cumulative_cash_usd_micros == -2
    assert result.action_gas_usd_micros == 1
    assert result.marked_pnl_after_action_gas_usd_micros == -3
    assert result.net_pnl is None
    assert "failed_execution" in result.missing_full_cost
    assert (
        not result.execution_eligible
        and not result.out_of_sample_accepted
        and not result.canary_accepted
    )

    # Independent rational lower bound over the integer claim inventory.
    def usd(a, b):
        return Fraction(a * 333333333 + b * 99000000, 10**20)

    exact = (
        usd(result.closing.total0, result.closing.total1)
        - usd(result.opening.total0, result.opening.total1)
        + usd(-100, -200)
        - Fraction(200 * 2000 * 10**6, 10**18)
    )
    assert result.marked_pnl_after_action_gas_usd_micros <= exact
    assert exact - result.marked_pnl_after_action_gas_usd_micros < 7
    assert all(store.get_record(ref) for ref in result.evidence)


def test_owner_change_cannot_become_one_owner_profit(inputs):
    store, request, original = inputs
    with pytest.raises(ValueError, match="unchanged owner"):
        replay_position_return(store, replace(request, cash_window=original))


@pytest.mark.parametrize(
    "case",
    [
        "missing",
        "duplicate",
        "foreign",
        "missing_native",
        "unused_native",
        "provider",
        "unretained",
    ],
)
def test_any_price_or_gas_gap_refuses_entire_return(inputs, case):
    store, request, _ = inputs
    opening, closing = request.prices
    if case == "missing":
        points = (closing,)
    elif case == "duplicate":
        points = (closing, closing)
    elif case == "foreign":
        points = (
            replace(opening, block=replace(opening.block, block_hash="0x" + "ff" * 32)),
            closing,
        )
    elif case == "missing_native":
        points = (opening, replace(closing, native_prices=None))
    elif case == "unused_native":
        points = (replace(opening, native_prices=closing.native_prices), closing)
    elif case == "provider":
        points = (opening, replace(closing, currency0=(closing.currency0[0], closing.currency0[0])))
    else:
        points = (
            opening,
            replace(
                closing,
                currency1=(
                    closing.currency1[0],
                    replace(closing.currency1[1], content_hash="0" * 64),
                ),
            ),
        )
    with pytest.raises((ValueError, KeyError)):
        replay_position_return(store, replace(request, prices=points))


@pytest.mark.parametrize(
    "field", ["state_view_runtime_keccak", "pool_manager_runtime_keccak", "pool_id", "token_id"]
)
def test_declared_position_must_match_cash_and_raw_inventory(inputs, field):
    store, request, _ = inputs
    value = 8 if field == "token_id" else "0x" + "fe" * 32
    with pytest.raises(ValueError):
        replay_position_return(
            store, replace(request, retained=replace(request.retained, **{field: value}))
        )


def test_closing_pool_price_cannot_substitute_for_opening_pool_price(inputs):
    store, request, _ = inputs
    with pytest.raises(ValueError, match="does not cover"):
        replay_position_return(
            store, replace(request, opening_pool_state=request.closing_pool_state)
        )


@pytest.mark.parametrize("field", ["currency0_policy", "currency1_policy", "native_policy"])
def test_no_feed_denomination_or_policy_override(inputs, field):
    store, request, _ = inputs
    with pytest.raises(ValueError, match="denominations"):
        replay_position_return(
            store,
            replace(
                request, **{field: replace(getattr(request, field), base_token="0x" + "fe" * 20)}
            ),
        )


def test_cli_two_replays_identical_and_refusal_produces_no_report(inputs, tmp_path):
    _, request, _ = inputs
    source = tmp_path / "request.json"
    source.write_bytes(
        canonical_json({"schema": "oasis.position-return-input.v1", "request": asdict(request)})
    )
    evidence = tmp_path / "cash.db"
    before = hashlib.sha256(evidence.read_bytes()).hexdigest()
    first, second = tmp_path / "first.json", tmp_path / "second.json"
    for output in (first, second):
        run(source, evidence, output)
    assert first.read_bytes() == second.read_bytes()
    assert hashlib.sha256(evidence.read_bytes()).hexdigest() == before
    payload = json.loads(first.read_bytes())
    assert payload["net_pnl"] is None and payload["marked_pnl_after_action_gas_usd_micros"] == -3
    with pytest.raises(ValueError, match="new report"):
        run(source, evidence, first)
    raw = json.loads(source.read_bytes())
    raw["request"]["costs"] = []
    source.write_bytes(canonical_json(raw))
    refused = tmp_path / "refused.json"
    with pytest.raises(ValueError):
        run(source, evidence, refused)
    assert not refused.exists()


@pytest.mark.parametrize(
    "field", ["net_pnl", "cash_amounts", "quote_decimals", "cost_disclosures", "execution_eligible"]
)
def test_cli_numerical_or_release_override_refused(inputs, field):
    _, request, _ = inputs
    raw = asdict(request)
    raw[field] = 0
    with pytest.raises(ValueError):
        Request(**{"schema": "oasis.position-return-input.v1", "request": raw})


def test_uncollected_fees_are_marked_claims_not_received_cash_or_complete_profit(inputs):
    store, request, _ = inputs
    capture = replay_position_snapshot_evidence(store, request.cash_window.closing[0])
    selector = "0x" + keccak(b"getFeeGrowthInside(bytes32,int24,int24)")[:4].hex()
    growth = (1 << 128) * 10**18
    xs = []
    for exchange in capture.exchanges:
        req = json.loads(exchange.request_payload)
        raw = json.loads(exchange.response_payload)
        if exchange.method == "eth_call" and req["params"][0]["data"].startswith(selector):
            raw["result"] = "0x" + encode(["uint256", "uint256"], [growth, growth]).hex()
        xs.append(replace(exchange, response_payload=canonical_json(raw)))
    snapshot = replace(
        capture.snapshot,
        fee_growth_inside0_x128=growth,
        fee_growth_inside1_x128=growth,
        uncollected_fee0=120 * 10**18,
        uncollected_fee1=120 * 10**18,
    )
    updated = replace(capture, snapshot=snapshot, exchanges=tuple(xs))
    refs = tuple(
        persist_position_snapshot_evidence(store, replace(updated, source=s)) for s in PEERS
    )
    request = replace(request, cash_window=replace(request.cash_window, closing=refs))
    result = replay_position_return(store, request)
    assert result.closing.uncollected_fee0 == 120 * 10**18
    assert result.closing.uncollected_fee1 == 120 * 10**18
    assert result.closing.usd_micros == 518799999
    assert result.cumulative_cash_usd_micros == -2
    assert result.marked_pnl_after_action_gas_usd_micros > 0
    assert result.net_pnl is None
    assert not result.execution_eligible and not result.out_of_sample_accepted


@pytest.mark.parametrize("case", ["stale", "future", "disagreement", "forged_interpretation"])
def test_complete_report_refuses_raw_price_rejections_without_dropping_sample(inputs, case):
    store, request, _ = inputs
    opening, closing = request.prices
    ref = closing.currency1[1]
    record = store.get_record(ref)
    payload = json.loads(record.payload)
    wire = json.loads(record.raw_response_payload)
    if case == "forged_interpretation":
        payload["observation"]["answer"] += 1
    else:
        exchange = wire["exchanges"][5]
        response = json.loads(bytes.fromhex(exchange["payload"]))
        words = bytearray.fromhex(response["result"][2:])
        if case == "disagreement":
            slot, value = 1, int.from_bytes(words[32:64], "big") + 1
        else:
            slot = 3
            value = closing.block.timestamp - 61 if case == "stale" else closing.block.timestamp + 1
        words[slot * 32 : (slot + 1) * 32] = value.to_bytes(32)
        response["result"] = "0x" + words.hex()
        exchange["payload"] = canonical_json(response).hex()
    forged = store.put(
        ref.kind,
        canonical_json(payload),
        ref.block,
        ref.source,
        request=record.request_payload,
        raw_response=canonical_json(wire),
        interpretation=record.interpretation_payload,
        schema_version=ref.schema_version,
    )
    request = replace(
        request, prices=(opening, replace(closing, currency1=(closing.currency1[0], forged)))
    )
    with pytest.raises(ValueError):
        replay_position_return(store, request)
    # The rejected raw sample is retained; it was never converted into a zero price.
    assert store.get_record(forged).raw_response_payload == canonical_json(wire)


def test_nonzero_endpoint_inventory_uses_upper_opening_and_lower_closing_marks(inputs):
    store, request, _ = inputs
    liquidity_selector = "0x" + keccak(b"getPositionLiquidity(uint256)")[:4].hex()
    core_selector = "0x" + keccak(b"getPositionInfo(bytes32,bytes32)")[:4].hex()
    endpoints = []
    for old_refs, liquidity in (
        (request.cash_window.opening, 10**24),
        (request.cash_window.closing, 10**24 + 20),
    ):
        capture = replay_position_snapshot_evidence(store, old_refs[0])
        xs = []
        for exchange in capture.exchanges:
            req = json.loads(exchange.request_payload)
            raw = json.loads(exchange.response_payload)
            if exchange.method == "eth_call":
                selector = req["params"][0]["data"][:10]
                if selector == liquidity_selector:
                    raw["result"] = "0x" + encode(["uint128"], [liquidity]).hex()
                elif selector == core_selector:
                    raw["result"] = (
                        "0x" + encode(["uint128", "uint256", "uint256"], [liquidity, 0, 0]).hex()
                    )
            xs.append(replace(exchange, response_payload=canonical_json(raw)))
        capture = replace(
            capture, snapshot=replace(capture.snapshot, liquidity=liquidity), exchanges=tuple(xs)
        )
        endpoints.append(
            tuple(
                persist_position_snapshot_evidence(store, replace(capture, source=s)) for s in PEERS
            )
        )
    request = replace(
        request,
        cash_window=replace(request.cash_window, opening=endpoints[0], closing=endpoints[1]),
    )
    result = replay_position_return(store, request)
    assert result.opening.total0 > 0 and result.opening.total1 > 0

    def mark(a, b):
        return Fraction(a * 333333333 + b * 99000000, 10**20)

    exact_open = mark(result.opening.total0, result.opening.total1)
    exact_close = mark(result.closing.total0, result.closing.total1)
    assert result.opening_debit_usd_micros >= exact_open >= result.opening.usd_micros
    assert result.closing.usd_micros <= exact_close
    exact = exact_close - exact_open + mark(-100, -200) - Fraction(200 * 2000 * 10**6, 10**18)
    assert result.marked_pnl_after_action_gas_usd_micros <= exact
    assert exact - result.marked_pnl_after_action_gas_usd_micros < 7
