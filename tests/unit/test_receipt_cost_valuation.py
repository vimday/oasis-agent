import json
from dataclasses import replace

import pytest

from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.farming.cost_allocation import allocate_priced_cost
from oasis_agent.farming.price_evidence import persist_price_observation_evidence
from oasis_agent.farming.receipt_cost_valuation import price_receipt_cost
from oasis_agent.farming.transaction_costs import persist_transaction_cost
from oasis_agent.models.common import ZERO_ADDRESS
from scripts.price_lp_receipt_cost import Request, run
from tests.unit.test_price_evidence import QUOTE, price
from tests.unit.test_transaction_costs import fixture


def inputs(store, chain=8453, status=1):
    cost = fixture(chain=chain, status=status)
    native = price(cost, ZERO_ADDRESS, 2000 * 10**8)
    quote = price(cost, QUOTE, 99 * 10**6)
    providers = ("fixture-a", "fixture-b")
    return {
        "header": cost.header,
        "transaction_hash": cost.transaction_hash,
        "payer": cost.reconstruct().payer,
        "providers": providers,
        "costs": tuple(persist_transaction_cost(store, replace(cost, source=p)) for p in providers),
        "native_prices": tuple(
            persist_price_observation_evidence(store, replace(native, source=p)) for p in providers
        ),
        "quote_prices": tuple(
            persist_price_observation_evidence(store, replace(quote, source=p)) for p in providers
        ),
        "native_policy": native.policy,
        "quote_policy": quote.policy,
        "quote_decimals": 6,
    }


@pytest.mark.parametrize("chain", [1, 8453, 42161])
@pytest.mark.parametrize("status", [0, 1])
@pytest.mark.parametrize("decimals", [0, 6, 18])
def test_whole_cost_replayed_and_rounded_up_including_failed_receipts(
    tmp_path, chain, status, decimals
):
    store = EvidenceStore(tmp_path / "evidence.db")
    try:
        request = inputs(store, chain, status)
        request["quote_decimals"] = decimals
        result = price_receipt_cost(store, **request)
        assert result.status == status
        numerator = result.total_native * 2000 * 10**8 * 10**decimals
        denominator = 10**18 * 99 * 10**6
        assert (
            (result.quote_units - 1) * denominator < numerator <= result.quote_units * denominator
        )
        assert result.position_allocation == "unresolved" and not result.execution_eligible
        assert len(result.evidence) == 6
    finally:
        store.close()


@pytest.mark.parametrize("bad", ["payer", "provider", "missing", "round", "cost", "unretained"])
def test_missing_or_disagreeing_inputs_never_become_zero(tmp_path, bad):
    store = EvidenceStore(tmp_path / "evidence.db")
    try:
        request = inputs(store)
        if bad == "payer":
            request["payer"] = "0x" + "13" * 20
        elif bad == "provider":
            request["providers"] = ("fixture-a", "fixture-a")
        elif bad == "missing":
            request["quote_prices"] = request["quote_prices"][:1]
        elif bad == "round":
            cost = fixture(chain=8453)
            different = replace(
                price(cost, QUOTE, 99 * 10**6, updated=cost.header.ref.timestamp - 1),
                source="fixture-b",
            )
            request["quote_prices"] = (
                request["quote_prices"][0],
                persist_price_observation_evidence(store, different),
            )
        elif bad == "cost":
            different = replace(fixture(chain=8453, status=0), source="fixture-b")
            request["costs"] = (request["costs"][0], persist_transaction_cost(store, different))
        else:
            request["costs"] = (
                replace(request["costs"][0], content_hash="0" * 64),
                request["costs"][1],
            )
        with pytest.raises((ValueError, KeyError)):
            price_receipt_cost(store, **request)
    finally:
        store.close()


def test_offline_report_replays_inputs_and_refusal_writes_nothing(tmp_path):
    evidence = tmp_path / "evidence.db"
    store = EvidenceStore(evidence)
    try:
        values = inputs(store)
    finally:
        store.close()
    request = Request(**{"schema": "oasis.receipt-cost-input.v1", **values})
    source = tmp_path / "input.json"
    source.write_text(request.model_dump_json(by_alias=True))
    output = tmp_path / "report.json"
    run(source, evidence, output)
    payload = json.loads(output.read_bytes())
    assert payload["cost"]["quote_units"] > 0
    assert payload["cost"]["position_allocation"] == "unresolved"
    assert payload["execution_eligible"] is False
    with pytest.raises(ValueError, match="new output"):
        run(source, evidence, output)
    raw = json.loads(source.read_bytes())
    raw["payer"] = "0x" + "23" * 20
    source.write_text(json.dumps(raw))
    denied = tmp_path / "denied.json"
    with pytest.raises(ValueError, match="payer"):
        run(source, evidence, denied)
    assert not denied.exists()


@pytest.mark.parametrize("status", [0, 1])
def test_priced_allocation_conserves_both_units_without_extra_rounding(tmp_path, status):
    store = EvidenceStore(tmp_path / "evidence.db")
    try:
        request = inputs(store, status=status)
        cost = price_receipt_cost(store, **request)
        weights = (("position-b", 2), ("position-a", 1), ("position-c", 3))
        allocation = allocate_priced_cost(cost, weights)
        assert allocation == allocate_priced_cost(cost, tuple(reversed(weights)))
        assert sum(item.native_units for item in allocation) == cost.total_native
        assert sum(item.quote_units for item in allocation) == cost.quote_units
        for item in allocation:
            assert abs(item.quote_units * 6 - cost.quote_units * item.weight) < 6
    finally:
        store.close()


def test_cli_optional_allocation_is_declared_and_invalid_weights_write_nothing(tmp_path):
    evidence = tmp_path / "evidence.db"
    store = EvidenceStore(evidence)
    try:
        values = inputs(store)
    finally:
        store.close()
    request = Request(
        **{
            "schema": "oasis.receipt-cost-input.v1",
            **values,
            "allocation_weights": (("position-a", 1), ("position-b", 1)),
        }
    )
    source = tmp_path / "input.json"
    source.write_text(request.model_dump_json(by_alias=True))
    output = tmp_path / "allocated.json"
    run(source, evidence, output)
    payload = json.loads(output.read_bytes())
    assert payload["allocation"]["scope"] == "declared_bookkeeping_not_observed_position_payment"
    assert (
        sum(item["quote_units"] for item in payload["allocation"]["items"])
        == payload["cost"]["quote_units"]
    )
    assert payload["cost"]["position_allocation"] == "unresolved"
    request = request.model_copy(update={"allocation_weights": (("position-a", 0),)})
    source.write_text(request.model_dump_json(by_alias=True))
    denied = tmp_path / "denied.json"
    with pytest.raises(ValueError, match="positive"):
        run(source, evidence, denied)
    assert not denied.exists()
