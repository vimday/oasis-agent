import copy
import json
import sys

import pytest

from oasis_agent.models.common import canonical_json
from scripts import replay_cycle_case
from tests.fixtures import cycle_research as fixture


def _row(entries, method, params):
    return next(
        copy.deepcopy(row)
        for row in entries
        if (row["request"]["method"], row["request"]["params"]) == (method, params)
    )


def _write(directory, rows, *, projected=False):
    directory.mkdir()
    for i, row in enumerate(rows, 1):
        for part in ("request", "response"):
            row[part]["id"] = i
            (directory / f"{i:02}.{part}").write_bytes(canonical_json(row[part]))
    if projected:
        fixture.write_bundle(directory / "parsed.json", rows)


def _capture(tmp_path):
    entries = fixture.records()
    chain = _row(entries, "eth_chainId", [])
    headers = [_row(entries, "eth_getBlockByNumber", [hex(n), False]) for n in (100, 101, 102)]
    complete = [_row(entries, "eth_getBlockByNumber", [hex(n), True]) for n in (101, 102)]
    receipts = [
        _row(entries, "eth_getTransactionReceipt", [h]) for h in (fixture.TRIGGER, fixture.WINNER)
    ]
    tx = complete[1]["response"]["result"]["transactions"][1]
    tx.update(maxFeePerGas="0x30", maxPriorityFeePerGas="0x20", gas="0x12345", accessList=[])
    target, amount = fixture.EXECUTOR, fixture.WINNER_OUTPUT - fixture.WINNER_PRINCIPAL
    pin = {"blockHash": fixture.block_hash(101), "requireCanonical": True}

    def response(method, params, result):
        row = copy.deepcopy(chain)
        row["request"].update(method=method, params=params)
        row["response"]["result"] = result
        return row

    fields = ("from", "gas", "value", "maxFeePerGas", "maxPriorityFeePerGas", "accessList")
    call = {"to": target, "data": "0x12345678", **{f: tx[f] for f in fields}}
    tx["input"] = call["data"]
    primary, secondary, simulation = (tmp_path / name for name in ("a", "b", "sim"))
    _write(primary, copy.deepcopy([chain, *headers, *complete, *receipts]), projected=True)
    _write(secondary, copy.deepcopy([chain, *headers, *receipts]))
    _write(
        simulation,
        [
            chain,
            copy.deepcopy(headers[1]),
            response("eth_getCode", [target, pin], fixture.RUNTIME),
            response("eth_getBalance", [target, pin], hex(fixture.WINNER_PRINCIPAL + 1)),
            response("eth_call", [call, pin], fixture.data(["uint256"], [amount])),
            headers[1],
        ],
    )
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps(fixture.plan()))
    return plan, primary, secondary, simulation


@pytest.mark.parametrize("fault", [None, "wrong_pin", "wrong_value", "wrong_return", "forked_peer"])
def test_raw_case_and_atomic_boundaries_through_cli(tmp_path, monkeypatch, fault):
    plan, primary, secondary, simulation = _capture(tmp_path)
    if fault in ("wrong_pin", "wrong_value", "wrong_return"):
        path = simulation / ("05.response" if fault == "wrong_return" else "05.request")
        payload = json.loads(path.read_bytes())
        if fault == "wrong_pin":
            payload["params"][1]["blockHash"] = fixture.block_hash(102)
        elif fault == "wrong_value":
            payload["params"][0]["value"] = "0x1"
        else:
            payload["result"] = fixture.data(["uint256"], [0])
        path.write_bytes(canonical_json(payload))
    if fault == "forked_peer":
        path = secondary / "04.response"
        payload = json.loads(path.read_bytes())
        payload["result"]["hash"] = fixture.block_hash(999)
        path.write_bytes(canonical_json(payload))
    output = tmp_path / "report.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "replay_cycle_case",
            "--plan",
            str(plan),
            "--primary",
            str(primary),
            "--secondary",
            str(secondary),
            "--block-end",
            str(simulation),
            "--output",
            str(output),
        ],
    )
    assert replay_cycle_case.main() == (2 if fault else 0)
    if fault:
        assert not output.exists()
        return
    report = json.loads(output.read_bytes())
    assert report["scenario"] == "synthetic"
    assert report["block_end_atomic_call"]["context"]["ref"]["number"] == 101
    assert report["block_end_atomic_call"]["executor_return_word_raw"] == (
        fixture.WINNER_OUTPUT - fixture.WINNER_PRINCIPAL
    )
    assert report["block_end_atomic_call"]["native_balance_delta"] is None
    assert report["reserves"]["net_reserve_transition_matches_receipts"]
    assert report["reserves"]["before_trigger_swap_inferred_raw"] == list(fixture.BEFORE)
    prestate = report["exact_winner_prestate"]
    assert prestate["target_block"]["ref"]["number"] == 102
    assert prestate["status"] == "unresolved"
    assert [(item["index"], item["type"]) for item in prestate["prefix"]] == [(0, 106)]
    assert report["execution_eligible"] is False
