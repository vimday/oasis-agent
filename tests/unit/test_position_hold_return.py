"""No-action LP/hold composition uses synthetic raw proof only, never live PnL."""

import hashlib
import json
from dataclasses import asdict, replace
from fractions import Fraction

import pytest
from eth_abi.abi import encode
from eth_hash.auto import keccak

from oasis_agent.evidence.ingestion import persist_and_verify_block_range
from oasis_agent.farming.position_capture import (
    persist_position_snapshot_evidence,
    replay_position_snapshot_evidence,
)
from oasis_agent.farming.position_hold_return import (
    HoldEndpointProof,
    PositionHoldRequest,
    replay_position_hold_return,
)
from oasis_agent.farming.price_evidence import persist_price_observation_evidence
from oasis_agent.farming.token_identity import (
    TokenIdentityCapture,
    TokenIdentityPin,
    TokenIdentityQuorum,
    persist_token_identity_evidence,
    replay_token_identity_evidence,
)
from oasis_agent.models.common import canonical_json
from oasis_agent.rpc.transport import RpcExchange
from oasis_agent.shadow.pool_state_capture import (
    Slot0WindowQuorum,
    persist_slot0_window_evidence,
    replay_slot0_window_evidence,
)
from oasis_agent.uniswap_v4.liquidity_math import sqrt_price_at_tick
from oasis_agent.uniswap_v4.state_rpc import Slot0State
from scripts.replay_position_hold_return import Request, run
from tests.rpc_evidence_helpers import range_exchanges
from tests.unit.test_position_dataset import OWNER, scenario
from tests.unit.test_position_return_replay import PEERS, STATE_HASH, feed_capture, pool_state
from tests.unit.test_position_return_replay import inputs as action_inputs


def snapshot_units(store, refs, *, liquidity=10**24, growth=0):
    capture = replay_position_snapshot_evidence(store, refs[0])
    getters = {
        "getPositionLiquidity(uint256)": (["uint128"], [liquidity]),
        "getPositionInfo(bytes32,bytes32)": (["uint128", "uint256", "uint256"], [liquidity, 0, 0]),
        "getFeeGrowthInside(bytes32,int24,int24)": (["uint256", "uint256"], [growth, growth]),
    }
    values = {
        "0x" + keccak(s.encode())[:4].hex(): "0x" + encode(*v).hex() for s, v in getters.items()
    }
    xs = []
    for exchange in capture.exchanges:
        req = json.loads(exchange.request_payload)
        raw = json.loads(exchange.response_payload)
        if exchange.method == "eth_call" and req["params"][0]["data"][:10] in values:
            raw["result"] = values[req["params"][0]["data"][:10]]
        xs.append(replace(exchange, response_payload=canonical_json(raw)))
    fees = liquidity * growth // (1 << 128)
    snapshot = replace(
        capture.snapshot,
        liquidity=liquidity,
        fee_growth_inside0_x128=growth,
        fee_growth_inside1_x128=growth,
        uncollected_fee0=fees,
        uncollected_fee1=fees,
    )
    capture = replace(capture, snapshot=snapshot, exchanges=tuple(xs))
    return tuple(
        persist_position_snapshot_evidence(store, replace(capture, source=s)) for s in PEERS
    )


def identities(store, header, token):
    pin = {"blockHash": header.ref.block_hash, "requireCanonical": True}
    hdr = {
        "number": hex(header.ref.number),
        "hash": header.ref.block_hash,
        "parentHash": header.parent_hash,
        "timestamp": hex(header.ref.timestamp),
    }
    specs = [
        ("eth_chainId", [], hex(header.ref.chain_id)),
        ("eth_getBlockByNumber", [hex(header.ref.number), False], hdr),
        ("eth_getCode", [token, pin], "0x6000"),
        (
            "eth_call",
            [{"to": token, "data": "0x313ce567"}, pin],
            "0x" + encode(["uint8"], [18]).hex(),
        ),
        ("eth_getBlockByNumber", [hex(header.ref.number), False], hdr),
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
        TokenIdentityCapture(s, header, TokenIdentityPin(token, 18, STATE_HASH), xs) for s in PEERS
    )
    return persist_token_identity_evidence(store, TokenIdentityQuorum(*captures))


@pytest.fixture
def inputs(tmp_path):
    generator = action_inputs.__wrapped__(tmp_path)
    store, active, _ = next(generator)
    try:
        _, _, core, _ = scenario()
        core = replace(core, logs=())
        core_refs = tuple(
            persist_and_verify_block_range(
                store,
                replace(core, source=s),
                range_exchanges(replace(core, source=s)),
                research_single_provider=True,
            ).evidence
            for s in PEERS
        )
        opening = snapshot_units(store, active.cash_window.opening)
        closing = snapshot_units(store, active.cash_window.closing)
        cash = replace(
            active.cash_window, opening=opening, closing=closing, core=core_refs, payments=()
        )
        opening_capture = replay_position_snapshot_evidence(store, opening[0])
        closing_capture = replay_position_snapshot_evidence(store, closing[0])
        open_state = pool_state(store, opening_capture)
        old_state = pool_state(store, closing_capture)
        state = replay_slot0_window_evidence(store, old_state[0])
        sqrt = sqrt_price_at_tick(60)
        xs = list(state.exchanges)
        raw = json.loads(xs[5].response_payload)
        raw["result"] = (
            "0x" + encode(["uint160", "int24", "uint24", "uint24"], [sqrt, 60, 0, 3000]).hex()
        )
        xs[5] = replace(xs[5], response_payload=canonical_json(raw))
        peers = tuple(
            replace(state, source=s, slot0=(Slot0State(sqrt, 60, 0, 3000),), exchanges=tuple(xs))
            for s in PEERS
        )
        close_state = persist_slot0_window_evidence(store, Slot0WindowQuorum(*peers))
        endpoints = []
        for capture in (opening_capture, closing_capture):
            key = capture.snapshot.pool_key
            answer0 = 99000000 if capture == opening_capture else sqrt**2 * 99000000 // (1 << 192)
            feeds = (
                feed_capture(capture, key.currency0, answer0, address="0x" + "81" * 20),
                feed_capture(capture, key.currency1, 99000000, address="0x" + "82" * 20),
            )
            prices = tuple(
                tuple(
                    persist_price_observation_evidence(store, replace(f, source=s)) for s in PEERS
                )
                for f in feeds
            )
            endpoints.append(
                HoldEndpointProof(
                    identities(store, capture.header, key.currency0),
                    identities(store, capture.header, key.currency1),
                    *prices,
                )
            )
        request = PositionHoldRequest(
            cash,
            active.retained,
            open_state,
            close_state,
            *endpoints,
            active.currency0_policy,
            active.currency1_policy,
        )
        yield store, request, active
    finally:
        generator.close()


def test_empty_complete_lp_range_produces_marked_hold_baseline_without_fake_payment(inputs):
    store, request, _ = inputs
    result = replay_position_hold_return(store, request)
    assert result.owner == OWNER and result.observed_lp_action_count == 0
    assert result.observed_modification_cash_units == (0, 0)
    assert result.opening.total0 > 0 and result.opening.total1 > 0
    assert result.principal_vs_hold_usd_micros < 0
    assert result.uncollected_fee_change_at_closing_usd_micros == 0
    assert (
        result.marked_excess_vs_hold_usd_micros
        == result.marked_lp_pnl_usd_micros - result.marked_hold_pnl_usd_micros
    )
    assert 0 <= result.decomposition_rounding_usd_micros <= 4
    answer0 = sqrt_price_at_tick(60) ** 2 * 99000000 // (1 << 192)
    exact = Fraction(
        (result.closing.total0 - result.opening.total0) * answer0
        + (result.closing.total1 - result.opening.total1) * 99000000,
        10**20,
    )
    assert result.marked_excess_vs_hold_usd_micros <= exact
    assert exact - result.marked_excess_vs_hold_usd_micros < 4
    assert result.net_pnl is None and "gas" in result.missing_full_cost
    assert not result.execution_eligible and not result.out_of_sample_accepted
    assert all(store.get_record(ref) for ref in result.evidence)


def test_positive_uncollected_claim_excess_still_cannot_pass_full_net_or_release(inputs):
    store, request, _ = inputs
    closing = snapshot_units(store, request.cash_window.closing, growth=(1 << 128) // 10**5)
    result = replay_position_hold_return(
        store, replace(request, cash_window=replace(request.cash_window, closing=closing))
    )
    assert result.uncollected_fee_change_at_closing_usd_micros > 0
    assert result.marked_excess_vs_hold_usd_micros > 0
    assert result.net_pnl is None and not result.canary_accepted


def test_modification_window_cannot_be_reclassified_as_hold(inputs):
    store, request, active = inputs
    with pytest.raises(ValueError, match="no LP modification"):
        replay_position_hold_return(store, replace(request, cash_window=active.cash_window))


@pytest.mark.parametrize("case", ["opening_block", "same_source", "unretained", "missing"])
def test_missing_or_foreign_token_unit_proof_refuses_complete_observation(inputs, case):
    store, request, _ = inputs
    old = request.closing_marks.currency0_identity
    if case == "opening_block":
        refs = request.opening_marks.currency0_identity
    elif case == "same_source":
        refs = (old[0], old[0])
    elif case == "unretained":
        refs = (old[0], replace(old[1], content_hash="0" * 64))
    else:
        refs = ()
    marks = replace(request.closing_marks, currency0_identity=refs)
    with pytest.raises((ValueError, KeyError)):
        replay_position_hold_return(store, replace(request, closing_marks=marks))


@pytest.mark.parametrize("case", ["decimals", "runtime"])
def test_consistent_each_endpoint_but_changed_token_identity_refuses(inputs, case):
    store, request, _ = inputs
    capture = replay_token_identity_evidence(store, request.closing_marks.currency1_identity[0])
    xs = list(capture.exchanges)
    index = 3 if case == "decimals" else 2
    raw = json.loads(xs[index].response_payload)
    raw["result"] = "0x" + encode(["uint8"], [6]).hex() if case == "decimals" else "0x6002"
    xs[index] = replace(xs[index], response_payload=canonical_json(raw))
    pin = (
        replace(capture.pin, decimals=6)
        if case == "decimals"
        else replace(capture.pin, runtime_keccak="0x" + keccak(bytes.fromhex("6002")).hex())
    )
    peers = tuple(replace(capture, source=s, pin=pin, exchanges=tuple(xs)) for s in PEERS)
    refs = persist_token_identity_evidence(store, TokenIdentityQuorum(*peers))
    with pytest.raises(ValueError, match="identity changed"):
        replay_position_hold_return(
            store,
            replace(request, closing_marks=replace(request.closing_marks, currency1_identity=refs)),
        )


def test_transfer_away_and_back_with_equal_endpoint_owner_refuses(inputs):
    store, request, _ = inputs
    _, _, _, nft = scenario()
    first = nft.logs[0]
    second = replace(
        first,
        topics=(first.topics[0], first.topics[2], first.topics[1], first.topics[3]),
        log_index=2,
    )
    peers = tuple(replace(nft, source=s, logs=(first, second)) for s in PEERS)
    refs = tuple(
        persist_and_verify_block_range(
            store, d, range_exchanges(d), research_single_provider=True
        ).evidence
        for d in peers
    )
    with pytest.raises(ValueError, match="unchanged owner"):
        replay_position_hold_return(
            store, replace(request, cash_window=replace(request.cash_window, ownership=refs))
        )


def test_cli_deterministic_offline_marks_and_refusal_without_output(inputs, tmp_path):
    _, request, _ = inputs
    source = tmp_path / "input.json"
    source.write_bytes(
        canonical_json({"schema": "oasis.position-hold-input.v1", "request": asdict(request)})
    )
    evidence = tmp_path / "cash.db"
    before = hashlib.sha256(evidence.read_bytes()).hexdigest()
    first, second = tmp_path / "first.json", tmp_path / "second.json"
    run(source, evidence, first)
    run(source, evidence, second)
    assert first.read_bytes() == second.read_bytes()
    assert hashlib.sha256(evidence.read_bytes()).hexdigest() == before
    payload = json.loads(first.read_bytes())
    assert payload["net_pnl"] is None and not payload["execution_eligible"]
    with pytest.raises(ValueError, match="new report"):
        run(source, evidence, first)
    raw = json.loads(source.read_bytes())
    raw["request"]["closing_marks"]["currency0_price"] = raw["request"]["opening_marks"][
        "currency0_price"
    ]
    source.write_bytes(canonical_json(raw))
    refused = tmp_path / "refused.json"
    with pytest.raises(ValueError):
        run(source, evidence, refused)
    assert not refused.exists()


@pytest.mark.parametrize(
    "override", ["decimals", "gas_cost", "cost_waivers", "net_pnl", "execution_eligible"]
)
def test_operator_numbers_or_waivers_cannot_fill_idle_window(inputs, override):
    _, request, _ = inputs
    raw = asdict(request)
    raw[override] = 0
    with pytest.raises(ValueError):
        Request(**{"schema": "oasis.position-hold-input.v1", "request": raw})
