"""Verify and summarize the independent eight-head Base discovery holdout."""

import json
from pathlib import Path

from replay_y2026_inventory import digest, read

from oasis_agent.arbitrage.base_survival import WETH
from oasis_agent.arbitrage.cycle_protocols import abi, data_bytes
from oasis_agent.fastpath.multicall_universe import (
    SLIPSTREAM_QUOTE_SINGLE_SELECTOR,
    UNIV3_QUOTE_SINGLE_SELECTOR,
    UNIV3_QUOTER_V2,
)
from oasis_agent.fastpath.multihop_replay import _call_pair
from oasis_agent.models.common import canonical_json, sha256_hex
from oasis_agent.sniping.venues import SLIPSTREAM
from scripts.replay_base_shadow_survival import replay

ROOT = Path(__file__).resolve().parents[3]
CAPTURE = ROOT / "reports/research/base-policy-natural-20261128"
FROZEN = "119add2877bebde6c06e0a88706efcbe08d94127c6b65657aa02ee219b506c4c"


def verify_quotes(directory, trace, snapshot, block, quote):
    attempts = trace["attempts"]
    attempt = quote["quote_replay_attempt"]
    amount = int(quote["input_raw"])
    outputs = []
    specs = {entry["pool_id"]: entry for entry in trace["pool_specs"]}
    for raw, (pool, token_in, token_out), source in zip(
        _call_pair(directory, attempts, attempt),
        quote["route"],
        quote["hop_quote_sources"],
        strict=True,
    ):
        request, response = json.loads(raw.request), json.loads(raw.response)
        call, state = request["params"]
        assert request["method"] == "eth_call" and request["id"] == response["id"]
        assert state == {"blockHash": block, "requireCanonical": True}
        if source == "v3_quoter":
            assert call["to"] == UNIV3_QUOTER_V2
            assert call["data"][:10] == UNIV3_QUOTE_SINGLE_SELECTOR
            parameter = specs[pool]["fixed_fee_pips"]
            param_type = "(address,address,uint256,uint24,uint160)"
        else:
            assert (
                source == "slipstream_quoter"
                and call["data"][:10] == SLIPSTREAM_QUOTE_SINGLE_SELECTOR
            )
            assert call["to"] == SLIPSTREAM[specs[pool]["factory"]][0]
            parameter = specs[pool]["tick_spacing"]
            param_type = "(address,address,uint256,int24,uint160)"
        (args,) = abi((param_type,), data_bytes(call["data"])[4:])
        assert args == (token_in, token_out, amount, parameter, 0)
        amount, *_ = abi(
            ("uint256", "uint160", "uint32", "uint256"), data_bytes(response["result"])
        )
        outputs.append(amount)
    l1, l2 = int(quote["l1_cost_raw"]), int(quote["l2_cost_raw"])
    gross = amount - int(quote["input_raw"])
    assert amount == int(quote["quoted_output_raw"]) and gross - l1 - l2 == int(
        quote["quoted_net_raw"]
    )
    quote["retained_hop_outputs_raw"] = outputs
    if quote["base_token"] == WETH:
        price = int(snapshot["base_fee_per_gas_wei"]) + int(snapshot["priority_fee_wei"])
        assert l2 == snapshot["gas_limit_units"] * price
        quote["break_even_gas_for_positive_at_model_fees"] = (gross - l1 - 1) // price


def main():
    scope = read(CAPTURE / "preregistered.json")
    assert digest(CAPTURE / "preregistered.json") == FROZEN
    assert digest(CAPTURE / "run_capture.py") == scope["driver_sha256"]
    assert all(digest(ROOT / name) == expected for name, expected in scope["code_identity"].items())
    campaign = CAPTURE / "run"
    result = replay(campaign, arrival_deadlines=True)
    summary = read(campaign / "shadow_summary.json")
    assert result["campaign_stopped_early"] is False and len(result["heads"]) == scope["ticks"]
    assert summary["execution_eligible"] is False and summary["chain_id"] == 8453
    counts = (
        summary["mixed_base_state"]["rpc_attempts_by_source"]
        | summary["base_newheads"]["rpc_attempts_by_source"]
    )
    assert sum(counts.values()) == summary["rpc_calls_used"] <= scope["all_base_primitives_cap"]
    deadlines = {r["block"]: r for r in result["observed_raw_child_deadlines"]}
    quotes = {}
    head_clocks = {
        row["observation"]["head"]["height"]: row["observation"]["received_monotonic_ns"]
        for row in result["head_schedule"]["frames"]
        if row.get("observation") and row["status"] != "duplicate_same_head"
    }
    directory = campaign / "multihop_replay"
    trace = read(directory / "manifest.json")
    assert (
        sha256_hex(canonical_json({k: v for k, v in trace.items() if k != "manifest_sha256"}))
        == trace["manifest_sha256"]
    )
    assert trace["campaign_summary_sha256"] == summary["summary_sha256"]
    for index, state in enumerate(result["heads"], 1):
        n = state["block"]
        child = deadlines[n]["observed_child"]
        path = directory / f"snapshot-{index:08d}.json"
        assert digest(path) == trace["snapshots"][index - 1]["sha256"]
        snapshot = read(path)
        assert (snapshot["block_number"], snapshot["block_hash"]) == (n, state["block_hash"])
        for quote in state["normal_ranked_candidates"]:
            verify_quotes(directory, trace, snapshot, state["block_hash"], quote)
            quote["child_receive_minus_quote_finish_ns"] = (
                None if child is None else head_clocks[child] - quote["quote_finished_monotonic_ns"]
            )
        quotes[n] = deadlines[n]
    return {
        "scope_sha256": FROZEN,
        "scope": scope,
        "base_http_ws_reservations": counts,
        "attribution_sha256": result["attribution_sha256"],
        "summary_sha256": result["summary_sha256"],
        "schedule_sha256": summary["base_newheads"]["schedule_sha256"],
        "heads": result["heads"],
        "direct_child_witnesses": quotes,
        "normal_discovery_candidates": result["normal_discovery_candidates"],
        "whole_transaction_simulations": result["whole_transaction_simulations"],
        "positive_survival_rate": result["positive_survival_rate"],
    }


if __name__ == "__main__":
    print(json.dumps(main(), indent=2, sort_keys=True))
