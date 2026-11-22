from hashlib import sha256
from json import dumps

import pytest

from scripts.replay_v2_pool_expansion import WETH, analyze


def _raw(value: dict) -> bytes:
    return dumps(value, sort_keys=True).encode()


def test_baseline_identity_preserves_overlapping_pools_and_pins_routes() -> None:
    asset = "0x" + "01" * 20
    other = "0x" + "02" * 20
    reused, first, second = ("0x" + f"{n:040x}" for n in (11, 12, 13))
    baseline = _raw({"stop_reason": None, "samples": [{"pool_states": [{"address": reused}]}]})
    scope = _raw({"wide_anchor_sha256": sha256(baseline).hexdigest()})
    snapshot = _raw(
        {
            "scope_sha256": sha256(scope).hexdigest(),
            "stop_reason": None,
            "block": {"number": 42, "hash": "0x" + "01" * 32, "base_fee_wei": 10},
            "pools": [
                {
                    "address": pid,
                    "tokens": tokens,
                    "reserves": [10**18, 10**18],
                    "venue": "uni",
                    # One of the new ranges overlapped the retained baseline.
                    "stratum": "newest_stratum",
                }
                for pid, tokens in [
                    (reused, (asset, WETH)),
                    (first, (asset, other)),
                    (second, (other, WETH)),
                ]
            ],
        }
    )
    options = {
        "chain": 1,
        "eth_scope": scope,
        "eth_baseline": baseline,
        "minimum": 1,
        "maximum": 10**18,
        "gas_units": 21_000,
    }
    result = analyze(snapshot, route_data=None, **options)
    assert result["closure_research"]["baseline_pools"] == 1
    assert result["closure_research"]["candidate_pools"] == 2
    assert result["closure_research"]["two_addition_options"] == 1
    assert result["execution_eligible"] is False
    assert len(result["routes"]) == 2
    assert {row["status"] for row in result["routes"]} == {"excluded_by_cost_bound"}
    with pytest.raises(ValueError, match="different snapshot"):
        analyze(snapshot, route_data=_raw({"snapshot_sha256": "wrong"}), **options)
    with pytest.raises(ValueError, match="did not match"):
        analyze(snapshot, route_data=None, **{**options, "eth_baseline": baseline + b" "})
