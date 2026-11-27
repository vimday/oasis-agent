import pytest
from eth_abi import encode

from oasis_agent.fastpath.eth_v2_batch_probe import WETH
from scripts.run_eth_v2_batch_shadow import (
    CHILD_CLOCK_BASIS,
    GAS_LIMIT,
    Head,
    ShadowStop,
    Wire,
    envelope,
    legacy_control_replay,
    rank_batches,
    reduce_outcomes,
    simulation,
)

USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"


def _active():
    return [
        {
            "address": "0x" + "11" * 20,
            "assets": ("USDC", "WETH"),
            "venue": "uni",
            "reserves": (2000 * 10**15, 1000 * 10**15),
        },
        {
            "address": "0x" + "22" * 20,
            "assets": ("USDC", "WETH"),
            "venue": "sushi",
            "reserves": (1000 * 10**15, 2000 * 10**15),
        },
    ]


def test_natural_shortlist_builds_the_same_pinned_batch_for_both_providers():
    active = _active()
    ranking, orders = rank_batches(active, 1)
    assert orders and ranking["eligible_shortlist"][0]["order_ids"] == tuple(
        order.order_id for order in orders
    )
    head = Head(42, "0x" + "01" * 32, "0x" + "02" * 32, 1, 1, 2)
    payload = envelope(orders, active, head, b"\x00", 0)
    capital = int(payload["call"]["value"], 16)
    ladders = [(order.amount_in, 77, order.amount_in + 5) for order in orders]
    data = (
        "0x"
        + encode(
            ("address[]", "uint256[]", "uint256[]", "uint256[][]", "uint256"),
            ((WETH, USDC), (capital, 0), (capital + 5 * len(orders), 0), ladders, 140_000),
        ).hex()
    )

    class Reads:
        def __init__(self, disagree=False):
            self.disagree = disagree

        def pair(self, left, right):
            assert (
                left
                == right
                == ("eth_call", [payload["call"], head.pin, payload["override"]], True)
            )
            return (
                Wire("publicnode", "a" * 64, "b" * 64, 3, 4, 1, None, {"result": data}),
                Wire(
                    "drpc",
                    "a" * 64,
                    "c" * 64,
                    3,
                    5,
                    1,
                    "free",
                    {"result": "0x00" if self.disagree else data},
                ),
            )

    result = {}
    simulation(Reads(), head, payload, orders, result)
    assert result["status"] == "succeeded"
    assert result["gross_weth_wei"] == 5 * len(orders)
    assert result["execution_fee_upper_wei"] == GAS_LIMIT * result["effective_gas_price_wei"]
    assert result["modeled_full_net_wei_lower"] < 0
    with pytest.raises(ShadowStop, match="dual_source_whole_output_mismatch"):
        simulation(Reads(disagree=True), head, payload, orders, {})


def test_ethereum_post_quorum_child_poll_does_not_rate_natural_timing():
    discovery = Head(42, "0x" + "01" * 32, "0x" + "02" * 32, 1, 90, 100)
    latest = Head(42, discovery.block_hash, discovery.parent_hash, 1, 125, 135)
    child = Head(43, "0x" + "03" * 32, discovery.block_hash, 1, 145, 155)

    def simulation_row(started, proof_marker):
        return {
            "status": "succeeded",
            "gross_weth_wei": 10**15,
            "execution_fee_upper_wei": 100,
            "failure_reserve_wei": 100,
            "primary": {
                "source": "publicnode",
                "sent_ns": started,
                "received_ns": started + 3,
                "request_sha256": proof_marker * 64,
                "response_sha256": "e" * 64,
            },
            "secondary": {
                "source": "drpc",
                "sent_ns": started + 1,
                "received_ns": started + 4,
                "request_sha256": proof_marker * 64,
                "response_sha256": "f" * 64,
            },
        }

    report = {
        "scope_sha256": "a" * 64,
        "envelope_sha256": "b" * 64,
        "candidate_kind": "natural_forward",
        "phases": {
            "H": simulation_row(110, "c"),
            "K": {"reused_phase": "H"},
            "C": simulation_row(160, "d"),
        },
    }
    reduce_outcomes(report, discovery, latest, child)
    assert report["child_clock_basis"] == CHILD_CLOCK_BASIS
    assert report["publicnode_post_quorum_latest_was_discovery"] is True
    outcome = report["normalized_outcome"]
    assert outcome["full_cost_positive_on_direct_child"] is True
    assert outcome["quorum_return_delay_ns"] == 14
    assert outcome["quorum_before_direct_child"] is None
    assert outcome["timely_h_k_c_survivor"] is None
    assert report["summary"]["natural_timely_h_k_c_rated"] == 0
    with pytest.raises(ShadowStop, match="legacy_replay_requires_completed_negative_control"):
        legacy_control_replay(report)
