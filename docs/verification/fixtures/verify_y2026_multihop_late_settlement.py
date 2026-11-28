"""Bind specialized Robinhood v4 and Gnosis vault legs to retained primary receipts."""

import argparse
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from replay_y2026_bsc_rh_landed import RH_WETH, USDG
from replay_y2026_multihop_late import NETWORKS, Lane, digest, plan, read, run
from replay_y2026_op_arb import moves
from replay_y2026_open_tail import event_amounts

from oasis_agent.arbitrage.cycle_protocols import abi, calldata, data_bytes, topic
from oasis_agent.evidence.public_receipts import quantity as q
from oasis_agent.models.common import canonical_json
from scripts.capture_public_rpc import durable_new

ROOT = Path(__file__).resolve().parents[3] / "reports/research"
PRIMARY = ROOT / "y2026-multihop-late-20261128"
ZERO = "0x" + "00" * 20
RH_TX = "0x777132856550f9f2c8d46aed268ac608efbdad79482bcaab8ea83283cbad4fb0"
GN_TX = "0x764425ad3f683a80a3f130764316b5e98d13c6f097e769bc0a913468d2393645"
BALANCER_V2 = "0xba12222222228d8ba445958a75a0704d566bf2c8"
BALANCER_V3 = "0xba1333333333a1ba1108e8412f11850a5c319ba9"
SDAI = "0xaf204776c7245bf4147c2612bf6e5972ee483701"


def selected(prior, chain, tx):
    [case] = [r for r in prior["chains"][chain]["verified"] if r["tx"] == tx]
    path = PRIMARY / chain / "audit" / f"r{case['height']}.response"
    r = read(path)["result"][case["index"]]
    assert (r["blockHash"], r["transactionHash"]) == (case["hash"], tx)
    return case, r, {q(log["logIndex"]): log for log in r["logs"]}, moves(r)


def robinhood(prior):
    case, r, events, transfers = selected(prior, "robinhood", RH_TX)
    holder, manager = r["to"], events[60]["address"]
    a = transfers
    v4 = events[60]
    assert v4["topics"][0] == topic(
        "Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)"
    )
    assert v4["topics"][2] == "0x" + holder[2:].zfill(64)
    amount0, amount1, *_ = abi(
        ("int128", "int128", "uint160", "uint128", "int24", "uint24"), data_bytes(v4["data"])
    )
    principal, proceeds = a[51][3], a[62][3]
    assert a[51] == (USDG, manager, holder, principal)
    assert a[54] == (USDG, holder, events[55]["address"], principal)
    assert event_amounts(events[55], "v3") == (a[54][3], a[52][3])
    assert event_amounts(events[59], "pancake_v3_shape") == (a[57][3], a[56][3])
    assert a[52][0] == a[57][0] and a[52][3] == a[57][3]
    assert (a[52][1:3], a[57][1:3]) == (
        (events[55]["address"], holder),
        (holder, events[59]["address"]),
    )
    assert a[56] == (a[61][0], events[59]["address"], holder, -amount0)
    assert a[61] == (a[56][0], holder, manager, -amount0)
    assert a[62] == (USDG, manager, holder, amount1)
    assert a[63] == (USDG, holder, manager, principal)
    residual = proceeds - principal
    assert a[65] == (USDG, holder, events[66]["address"], residual)
    weth = a[64][3]
    assert event_amounts(events[66], "v3") == (residual, weth)
    assert a[64] == (RH_WETH, events[66]["address"], holder, weth)
    assert a[67] == (RH_WETH, holder, ZERO, weth)
    assert not case["role_token_amounts"]
    return {
        "tx": RH_TX,
        "cycle_event_order": [55, 59, 60],
        "conversion_event": 66,
        "manager_pool_id": v4["topics"][1],
        "usd_g_principal_raw": principal,
        "usd_g_repaid_raw": a[63][3],
        "usd_g_residue_raw": residual,
        "weth_received_and_burned_wei": weth,
        "whole_network_fee_eth_wei": case["network_fee"]["amount_native_wei"],
        "conditional_less_network_wei": weth - case["network_fee"]["amount_native_wei"],
        "final_native_recipient": None,
    }


def gnosis(root, scope, capture, prior):
    case, r, events, a = selected(prior, "gnosis", GN_TX)
    holder = r["to"]
    w = NETWORKS["gnosis"]["wrapped_native"]
    assert a[108] == (w, BALANCER_V3, holder, a[370][3])
    assert a[370] == (w, holder, BALANCER_V3, a[108][3])
    assert a[110] == (w, holder, SDAI, a[108][3])
    assert a[368] == (w, SDAI, holder, a[368][3])
    wx = a[368][3] - a[370][3]
    [wx_delta] = [r for r in case["role_token_amounts"] if r["token"] == w]
    assert wx_delta["transfer_delta_raw"] == wx
    assert a[111] == (SDAI, ZERO, holder, a[111][3])
    assert a[117] == (SDAI, holder, events[119]["address"], a[117][3])
    signed_in, signed_out = event_amounts(events[119], "v3")
    assert signed_in == a[117][3]
    assert event_amounts(events[122], "v2") == (signed_out, a[120][3])
    assert a[120][1:3] == (events[122]["address"], holder)
    twins = [
        i
        for i in (115, 116)
        if a[i][1:] == (*[events[j]["address"] for j in (119, 122)], signed_out)
    ]
    assert len(twins) == 2 and a[115][0] != a[116][0]
    asset = a[120][0]
    split = [(i, t[3]) for i, t in a.items() if t[:3] == (asset, holder, ZERO)]
    [receipt_residual] = [r for r in case["role_token_amounts"] if r["token"] == asset]
    assert sum(n for _, n in split) + receipt_residual["transfer_delta_raw"] == a[120][3]
    swaps = []
    for i, log in events.items():
        if log["address"] != BALANCER_V2 or log["topics"][0] != topic(
            "Swap(bytes32,address,address,uint256,uint256)"
        ):
            continue
        _, pool_id, input_word, output_word = log["topics"]
        x, y = ("0x" + word[-40:] for word in (input_word, output_word))
        spent, received = abi(("uint256", "uint256"), data_bytes(log["data"]))
        assert a[i + 2] == (x, holder, BALANCER_V2, spent)
        assert a[i + 3] == (y, BALANCER_V2, holder, received)
        swaps.append(
            {
                "log": i,
                "pool_id": pool_id,
                "in_token": x,
                "in_raw": spent,
                "out_token": y,
                "out_raw": received,
            }
        )
    assert len(swaps) == 13 and {s["out_token"] for s in swaps} == {SDAI}
    minted = Counter()
    spent = Counter()
    sale_tokens = {s["in_token"] for s in swaps}
    for t, src, dst, n in a.values():
        if t in sale_tokens and (src, dst) == (ZERO, holder):
            minted[t] += n
    for s in swaps:
        spent[s["in_token"]] += s["in_raw"]
    remainder = Counter(
        {
            row["token"]: row["transfer_delta_raw"]
            for row in case["role_token_amounts"]
            if row["token"] in sale_tokens
        }
    )
    assert minted == spent + remainder

    pairs = []

    def identify(ask):
        pin = {"blockHash": case["hash"], "requireCanonical": True}
        for i in (119, 122):
            address = events[i]["address"]
            assets = []
            for j in (0, 1):
                opts = [{"to": address, "data": calldata(f"token{j}()", (), ())}, pin]
                raw = ask(f"p{i}-token{j}", "eth_call", opts)
                [symbol] = abi(("address",), data_bytes(raw))
                assets.append(symbol)
            assert len(set(assets)) == 2
            pairs.append({"event": i, "address": address, "tokens": assets})

    source = NETWORKS["gnosis"]["scan"]
    proofs = Lane(root / "gnosis", "quote", source, capture, scope).run(identify)
    exact = []
    if len(pairs) == 2:
        [mid] = set(pairs[0]["tokens"]) & set(pairs[1]["tokens"])
        assert {SDAI, mid} == set(pairs[0]["tokens"])
        assert {asset, mid} == set(pairs[1]["tokens"])
        exact = [i for i in twins if a[i][0] == mid]
        assert len(exact) == 1
    return {
        "tx": GN_TX,
        "wx_dai_principal_returned_raw": a[370][3],
        "wx_dai_cash_residue_raw": wx,
        "additional_sdai_input_raw": a[117][3] - a[111][3],
        "unique_v3_v2_asset_link_logs": exact,
        "duplicate_amount_link_logs": twins,
        "verified_pool_assets": pairs,
        "balancer_v2_sell_count": len(swaps),
        "balance_split_burns": split,
        "vault_swaps": swaps,
        "wx_dai_less_network_fee_raw": wx - case["network_fee"]["amount_native_wei"],
        "owner_all_cost_net": None,
        "proof": proofs,
    }


def scope_for(frozen):
    return {
        "schema": "oasis.2026-multihop-late-settlement.v1",
        "frozen_utc": frozen,
        "driver_sha256": digest(Path(__file__)),
        "initial_scope_sha256": digest(PRIMARY / "scope.json"),
        "initial_output_sha256": digest(PRIMARY / "stdout.json"),
        "transactions": [RH_TX, GN_TX],
        "budgets": {"quote": 4},
        "minimum_interval_ms": 600,
        "usd": 0,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "capture", "replay"))
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    path = args.root / "scope.json"
    if args.action == "prepare":
        durable_new(path, canonical_json(scope_for(datetime.now(UTC).isoformat())))
        return
    scope = read(path)
    assert scope == scope_for(scope["frozen_utc"])
    assert digest(args.root / "capture.py") == scope["driver_sha256"]
    initial = read(PRIMARY / "scope.json")
    assert initial == plan(initial["frozen_utc"])
    prior = run(PRIMARY, initial, False)
    assert json.loads(json.dumps(prior)) == read(PRIMARY / "stdout.json")
    closure = robinhood(prior)
    trace = gnosis(args.root, scope, args.action == "capture", prior)
    result = prior | {
        "settlement_scope_sha256": digest(path),
        "supplemental_settlement": {"robinhood": closure, "gnosis": trace},
        "rpc_reservations": prior["rpc_reservations"] + trace["proof"]["counts"]["quote"],
    }
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
