import json
from dataclasses import replace

import pytest
from eth_abi.abi import encode
from eth_hash.auto import keccak

from oasis_agent.farming.price_evidence import (
    CostValuation,
    UsdFeedPolicy,
    UsdPriceCapture,
    selector,
    value_cost_in_quote,
    value_native_cost,
)
from oasis_agent.models.common import ZERO_ADDRESS, canonical_json
from oasis_agent.rpc.transport import RpcExchange
from tests.unit.test_transaction_costs import fixture as cost_fixture

QUOTE = "0x" + "99" * 20


def price(cost, token, answer, decimals=8, updated=None):
    b = cost.header.ref
    header = json.loads(cost.exchanges[1].response_payload)["result"]
    policy = UsdFeedPolicy(
        b.chain_id,
        "0x" + "88" * 20,
        token,
        "ETH / USD" if token == ZERO_ADDRESS else "TOKEN / USD",
        "0x" + keccak(bytes.fromhex("6000")).hex(),
        60,
    )
    pin = {"blockHash": b.block_hash, "requireCanonical": True}
    specs = [
        ("eth_chainId", [], hex(b.chain_id)),
        ("eth_getBlockByNumber", [hex(b.number), False], header),
        ("eth_getCode", [policy.address, pin], "0x6000"),
    ]
    values = [
        ("decimals()", ["uint8"], [decimals]),
        ("description()", ["string"], [policy.description]),
        (
            "latestRoundData()",
            ["uint80", "int256", "uint256", "uint256", "uint80"],
            [2, answer, b.timestamp - 100, b.timestamp if updated is None else updated, 2],
        ),
    ]
    specs += [
        (
            "eth_call",
            [{"to": policy.address, "data": selector(sig)}, pin],
            "0x" + encode(types, args).hex(),
        )
        for sig, types, args in values
    ]
    specs += [("eth_getBlockByNumber", [hex(b.number), False], header)]
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
    return UsdPriceCapture(cost.header, policy, "synthetic-price", xs)


def test_cost_rounds_up_and_never_assumes_stablecoin_peg():
    c = cost_fixture()
    native = price(c, ZERO_ADDRESS, 2000 * 10**8)
    for q in (1, 10**6, 10**8, 3 * 10**8):
        quote = price(c, QUOTE, q)
        actual = value_native_cost(c, native, quote, quote_token=QUOTE, quote_decimals=18)
        num = c.reconstruct().total_native * 2000 * 10**8
        assert actual * q >= num
        assert (actual - 1) * q < num


@pytest.mark.parametrize("answer,age", [(0, 0), (-1, 0), (100, 61), (100, -1)])
def test_bad_price_rejected(answer, age):
    c = cost_fixture()
    with pytest.raises(ValueError):
        price(c, QUOTE, answer, updated=c.header.ref.timestamp - age)


def test_feed_identity_and_wrong_snapshot_rejected():
    c = cost_fixture()
    p = price(c, QUOTE, 10**8)
    with pytest.raises(ValueError, match="runtime"):
        replace(p, policy=replace(p.policy, runtime_keccak="0x" + "11" * 32))
    with pytest.raises(ValueError, match="denomination"):
        value_native_cost(c, p, p, quote_token=QUOTE, quote_decimals=18)


def test_price_persistence_reconstructs_wire_and_rejects_forged_answer(tmp_path):
    from oasis_agent.evidence.store import EvidenceStore
    from oasis_agent.farming.price_evidence import (
        persist_price_evidence,
        replay_price_evidence,
    )

    c = cost_fixture()
    p = price(c, QUOTE, 10**8)
    store = EvidenceStore(tmp_path / "price.db")
    ref = persist_price_evidence(store, p)
    assert replay_price_evidence(store, ref, header=p.header, policy=p.policy) == p
    record = store.get_record(ref)
    payload = json.loads(record.payload)
    payload["answer_decimals"][0] = 0
    bad = store.put(
        ref.kind,
        canonical_json(payload),
        ref.block,
        ref.source,
        request=json.loads(record.request_payload),
        raw_response=json.loads(record.raw_response_payload),
        interpretation=json.loads(record.interpretation_payload),
    )
    with pytest.raises(ValueError, match="interpretation differs"):
        replay_price_evidence(store, bad, header=p.header, policy=p.policy)
    store.close()


def test_cost_valuation_is_quote_units_and_usd_uses_feed_not_peg():
    c = cost_fixture()
    native = price(c, ZERO_ADDRESS, 2000 * 10**8)
    quote = price(c, QUOTE, 10**8)
    v = value_cost_in_quote(c, native, quote, quote_token=QUOTE, quote_decimals=18)
    assert isinstance(v, CostValuation)
    assert v.total_native == c.reconstruct().total_native
    assert v.quote_token == QUOTE and v.quote_decimals == 18
    assert v.value_quote_units == value_native_cost(
        c, native, quote, quote_token=QUOTE, quote_decimals=18
    )
    # usd_nominal = value * quote_answer / 10^feed_decimals, rounded up.
    num = v.value_quote_units * v.quote_answer
    assert v.usd_nominal * 10**v.quote_feed_decimals >= num
    assert (v.usd_nominal - 1) * 10**v.quote_feed_decimals < num


def test_valuation_persistence_records_quote_units(tmp_path):
    from oasis_agent.evidence.store import EvidenceStore
    from oasis_agent.farming.price_evidence import persist_cost_valuation

    c = cost_fixture()
    native = price(c, ZERO_ADDRESS, 2000 * 10**8)
    quote = price(c, QUOTE, 10**8)
    v = value_cost_in_quote(c, native, quote, quote_token=QUOTE, quote_decimals=18)
    store = EvidenceStore(tmp_path / "valuation.db")
    ref = persist_cost_valuation(store, v, block=c.header.ref, source=c.source)
    record = store.get_record(ref)
    assert json.loads(record.payload)["valuation"]["value_quote_units"] == v.value_quote_units
    assert json.loads(record.payload)["peg_assumption"] is False
    store.close()
