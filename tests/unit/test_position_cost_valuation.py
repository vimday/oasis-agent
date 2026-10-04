"""Raw owner cash and action receipts, not declared gas allocations, determine cost."""

import json
from dataclasses import replace

import pytest

from oasis_agent.farming.cash_window import replay_cash_window
from oasis_agent.farming.position_cost_valuation import (
    ActionPriceProof,
    quote_position_action_costs,
)
from oasis_agent.farming.price_evidence import (
    persist_price_observation_evidence,
    replay_price_observation_evidence,
)
from oasis_agent.models.common import ZERO_ADDRESS, canonical_json
from scripts.price_position_action_costs import Request, run
from tests.unit.test_position_action_costs import costs
from tests.unit.test_position_action_costs import inputs as action_inputs
from tests.unit.test_price_evidence import price


@pytest.fixture
def inputs(tmp_path):
    generator = action_inputs.__wrapped__(tmp_path)
    try:
        yield next(generator)
    finally:
        generator.close()


def price_inputs(store, request, core):
    token = replay_cash_window(store, request).ledger.opening.pool_key.currency1
    native = price(core, ZERO_ADDRESS, 2000 * 10**8)
    quote = price(core, token, 99 * 10**6)
    # These are distinct declared feeds, each with two retained synthetic wires.
    address = "0x" + "89" * 20
    xs = []
    for exchange in quote.exchanges:
        data = json.loads(exchange.request_payload)
        if exchange.method == "eth_getCode":
            data["params"][0] = address
        elif exchange.method == "eth_call":
            data["params"][0]["to"] = address
        xs.append(replace(exchange, request_payload=canonical_json(data)))
    quote = replace(quote, policy=replace(quote.policy, address=address), exchanges=tuple(xs))
    peers = ("rpc-a", "rpc-b")
    return {
        "prices": (
            ActionPriceProof(
                core.entry.transaction_hash,
                tuple(
                    persist_price_observation_evidence(store, replace(native, source=p))
                    for p in peers
                ),
                tuple(
                    persist_price_observation_evidence(store, replace(quote, source=p))
                    for p in peers
                ),
            ),
        ),
        "native_policy": native.policy,
        "quote_policy": quote.policy,
    }


def test_quote_action_gas_uses_raw_decimal_scale_and_action_time_owner(inputs):
    store, request, core = inputs
    proof = costs(store, core)
    values = price_inputs(store, request, core)
    result = quote_position_action_costs(store, request, (proof,), **values)
    cash = replay_cash_window(store, request)
    assert result.quote_decimals == cash.payments[0].amounts.token1.decimals
    assert result.quote_token == cash.payments[0].amounts.token1.token
    action = result.actions[0]
    assert action.owner == cash.payments[0].owner != cash.ledger.closing.nft_owner
    assert action.native_units == 200
    # Independent exact rational check: one quote token costs USD .99, not USD 1.
    num = 200 * 2000 * 10**8 * 10**result.quote_decimals
    den = 10**18 * 99 * 10**6
    assert (action.quote_units - 1) * den < num <= action.quote_units * den
    assert result.total_quote_units == action.quote_units
    assert result.quote_cost_by_owner == {action.owner: action.quote_units}
    assert len(action.evidence) == 8
    assert result.net_pnl is None and not result.execution_eligible


@pytest.mark.parametrize("case", ["missing", "duplicate", "foreign", "provider", "unretained"])
def test_incomplete_or_foreign_action_prices_do_not_make_gas_free(inputs, case):
    store, request, core = inputs
    values = price_inputs(store, request, core)
    p = values["prices"][0]
    if case == "missing":
        values["prices"] = ()
    elif case == "duplicate":
        values["prices"] = (p, p)
    elif case == "foreign":
        values["prices"] = (replace(p, transaction_hash="0x" + "fe" * 32),)
    elif case == "provider":
        values["prices"] = (replace(p, native_prices=(p.native_prices[0], p.native_prices[0])),)
    else:
        values["prices"] = (
            replace(
                p,
                quote_prices=(
                    p.quote_prices[0],
                    replace(p.quote_prices[1], content_hash="0" * 64),
                ),
            ),
        )
    with pytest.raises((ValueError, KeyError)):
        quote_position_action_costs(store, request, (costs(store, core),), **values)


@pytest.mark.parametrize("field", ["base_token", "address", "max_age_seconds"])
def test_unobserved_quote_feed_policy_cannot_replace_the_retained_one(inputs, field):
    store, request, core = inputs
    values = price_inputs(store, request, core)
    change = {field: 61 if field == "max_age_seconds" else "0x" + "fe" * 20}
    values["quote_policy"] = replace(values["quote_policy"], **change)
    with pytest.raises(ValueError):
        quote_position_action_costs(store, request, (costs(store, core),), **values)


def test_changed_or_sponsored_receipt_never_becomes_owner_gas(inputs):
    store, request, core = inputs
    values = price_inputs(store, request, core)
    proof = costs(store, core, change={"from": "0x" + "fe" * 20})
    with pytest.raises(ValueError, match="receipt differs"):
        quote_position_action_costs(store, request, (proof,), **values)


@pytest.mark.parametrize("leg", ["native_prices", "quote_prices"])
def test_price_from_another_block_cannot_value_the_action(inputs, leg):
    store, request, core = inputs
    values = price_inputs(store, request, core)
    p = values["prices"][0]
    policy = values["native_policy" if leg == "native_prices" else "quote_policy"]
    capture = replay_price_observation_evidence(
        store, getattr(p, leg)[0], header=core.header, policy=policy
    )
    header = replace(
        core.header,
        ref=replace(
            core.header.ref,
            number=core.header.ref.number + 1,
            block_hash="0x" + "cc" * 32,
            timestamp=core.header.ref.timestamp + 1,
        ),
        parent_hash=core.header.ref.block_hash,
    )
    xs = []
    for exchange in capture.exchanges:
        request_wire = json.loads(exchange.request_payload)
        response_wire = json.loads(exchange.response_payload)
        if exchange.method == "eth_getBlockByNumber":
            request_wire["params"] = [hex(header.ref.number), False]
            response_wire["result"] = {
                "number": hex(header.ref.number),
                "hash": header.ref.block_hash,
                "parentHash": header.parent_hash,
                "timestamp": hex(header.ref.timestamp),
            }
        elif exchange.method in ("eth_call", "eth_getCode"):
            request_wire["params"][-1]["blockHash"] = header.ref.block_hash
        xs.append(
            replace(
                exchange,
                request_payload=canonical_json(request_wire),
                response_payload=canonical_json(response_wire),
            )
        )
    other = replace(capture, header=header, exchanges=tuple(xs))
    refs = tuple(
        persist_price_observation_evidence(store, replace(other, source=s))
        for s in ("rpc-a", "rpc-b")
    )
    values["prices"] = (replace(p, **{leg: refs}),)
    with pytest.raises(ValueError):
        quote_position_action_costs(store, request, (costs(store, core),), **values)


@pytest.mark.parametrize("leg", ["native_prices", "quote_prices"])
def test_two_sources_with_different_feed_answers_refuse(inputs, leg):
    store, request, core = inputs
    values = price_inputs(store, request, core)
    p = values["prices"][0]
    old = getattr(p, leg)[1]
    policy = values["native_policy" if leg == "native_prices" else "quote_policy"]
    capture = replay_price_observation_evidence(store, old, header=core.header, policy=policy)
    xs = list(capture.exchanges)
    raw = json.loads(xs[5].response_payload)
    words = bytearray.fromhex(raw["result"][2:])
    answer = int.from_bytes(words[32:64], "big")
    words[32:64] = (answer + 1).to_bytes(32)
    raw["result"] = "0x" + words.hex()
    xs[5] = replace(xs[5], response_payload=canonical_json(raw))
    different = persist_price_observation_evidence(store, replace(capture, exchanges=tuple(xs)))
    values["prices"] = (replace(p, **{leg: (getattr(p, leg)[0], different)}),)
    with pytest.raises(ValueError, match="providers disagree"):
        quote_position_action_costs(store, request, (costs(store, core),), **values)


def test_offline_cli_replay_is_deterministic_and_refusal_writes_nothing(inputs, tmp_path):
    store, cash, core = inputs
    request = Request(
        **{
            "schema": "oasis.position-action-price-input.v1",
            "cash_window": cash,
            "costs": (costs(store, core),),
            **price_inputs(store, cash, core),
        }
    )
    source = tmp_path / "input.json"
    source.write_text(request.model_dump_json(by_alias=True))
    first, second = tmp_path / "first.json", tmp_path / "second.json"
    for output in (first, second):
        run(source, tmp_path / "cash.db", output)
    assert first.read_bytes() == second.read_bytes()
    result = json.loads(first.read_bytes())
    assert result["total_quote_units"] > 0
    assert result["net_pnl"] is None
    assert not result["out_of_sample_accepted"] and not result["canary_accepted"]
    assert not result["execution_eligible"]
    with pytest.raises(ValueError, match="new output"):
        run(source, tmp_path / "cash.db", first)
    request = request.model_copy(update={"prices": ()})
    source.write_text(request.model_dump_json(by_alias=True))
    refused = tmp_path / "refused.json"
    with pytest.raises(ValueError):
        run(source, tmp_path / "cash.db", refused)
    assert not refused.exists()


@pytest.mark.parametrize("extra", ["payer", "quote_decimals", "gas_units", "allocation_weights"])
def test_cli_rejects_operator_cost_or_scale_overrides(inputs, extra):
    store, cash, core = inputs
    raw = {
        "schema": "oasis.position-action-price-input.v1",
        "cash_window": cash,
        "costs": (costs(store, core),),
        **price_inputs(store, cash, core),
        extra: 6,
    }
    with pytest.raises(ValueError):
        Request(**raw)
