"""Rebuild pinned prefix and fourth-leg input tiers from retained Robinhood evidence."""

import argparse
import json
from collections import Counter
from pathlib import Path

from eth_abi import decode
from eth_hash.auto import keccak
from replay_robinhood_shadow_window import read, wire

from oasis_agent.arbitrage.cycle_protocols import calldata, data_bytes
from oasis_agent.evidence.robinhood_nitro import FeedMessage
from oasis_agent.evidence.robinhood_quote import (
    MANAGER,
    PATH_ABI,
    QUOTER,
    STATE_VIEW,
    _pool_ids,
    assert_feed_header,
    parse_routes,
)
from oasis_agent.fastpath.multicall_universe import (
    _decode_aggregate3_results,
    _encode_aggregate3_calldata,
)
from oasis_agent.models.common import ZERO_ADDRESS, sha256_hex
from oasis_agent.models.v4 import SwapDirection
from oasis_agent.rpc.budget import BudgetPolicy, RpcBudget
from oasis_agent.uniswap_v4.fees import settled_fee_breakdown
from oasis_agent.uniswap_v4.liquidity_math import amount0_delta, sqrt_price_at_tick
from oasis_agent.uniswap_v4.quotes import decode_single_pool_quote
from scripts import capture_robinhood_nitro_feed as feed
from scripts import measure_robinhood_quote_delivery as quote

DOCS = Path(__file__).resolve().parents[1]
GRID = (
    10**10,
    10**12,
    10**13,
    25 * 10**12,
    5 * 10**13,
    75 * 10**12,
    10**14,
    125 * 10**12,
    2 * 10**14,
    10**15,
)
PREFIX = (1, 2, 3, 4, 24)
PRINCIPAL = 10**14


def probes(route, actual=None, tiers=GRID):
    path = route.path()
    sentinels = [
        (quote.MULTICALL3, False, data_bytes(calldata("getChainId()"))),
        (QUOTER, False, data_bytes(calldata("poolManager()"))),
    ]
    if actual is None:
        sentinels.extend((path[i][0], False, data_bytes(calldata("decimals()"))) for i in (0, 1, 3))
        quotes = [(0, n, PRINCIPAL) for n in PREFIX]
    else:
        quotes = [(3, 4, v) for v in (actual, *tiers)]
    calls = list(sentinels)
    for start, end, amount in quotes:
        origin = ZERO_ADDRESS if start == 0 else path[start - 1][0]
        args = (origin, path[start:end], amount)
        calls.append(
            (
                QUOTER,
                True,
                data_bytes(calldata(f"quoteExactInput({PATH_ABI})", (PATH_ABI,), (args,))),
            )
        )
    return calls


def params(route, block, actual=None):
    encoded = _encode_aggregate3_calldata(probes(route, actual))
    return [
        {"to": quote.MULTICALL3, "data": encoded, "gas": hex(quote.TRANSPORT_GAS)},
        {"blockHash": block, "requireCanonical": True},
    ]


def decode_results(raw, route, actual=None, tiers=GRID):
    results = _decode_aggregate3_results(data_bytes(raw), len(probes(route, actual, tiers)))
    assert results[0][0] and decode(["uint256"], results[0][1]) == (4663,)
    assert results[1][0] and decode(["address"], results[1][1]) == (MANAGER,)
    sizes = {}
    if actual is None:
        for i, result in zip((0, 1, 3), results[2:5], strict=True):
            assert result[0]
            sizes[route.path()[i][0]] = decode(["uint8"], result[1])[0]
    sizes[ZERO_ADDRESS] = 18
    rows = []
    for ok, data in results[5 if actual is None else 2 :]:
        if ok:
            out = decode_single_pool_quote("0x" + data.hex())
            assert out.gas_estimate > 0
            rows.append({"status": "quoted", "amount_out": str(out.quoted_amount)})
        else:
            assert data[:4] == keccak(b"UnexpectedRevertBytes(bytes)")[:4]
            (inner,) = decode(["bytes"], data[4:])
            assert len(inner) == 36 and inner[:4] == keccak(b"NotEnoughLiquidity(bytes32)")[:4]
            pool_id = "0x" + inner[4:].hex()
            assert pool_id == "0x" + list(_pool_ids((route,)))[3].hex()
            rows.append({"status": "NotEnoughLiquidity", "pool_id": pool_id})
    return rows, sizes


FRESH_GRID = (10**3, 10**5, 10**7, 10**8, 5 * 10**8, 10**9, *GRID)


def fresh_params(route, block, actual=None, tick=None):
    pool_id = list(_pool_ids((route,)))[3]
    calls = [
        (
            quote.MULTICALL3,
            False,
            data_bytes(_encode_aggregate3_calldata(probes(route, actual, FRESH_GRID))),
        )
    ]
    calls.extend(
        (STATE_VIEW, False, data_bytes(calldata(method, ("bytes32",), (pool_id,))))
        for method in ("getSlot0(bytes32)", "getLiquidity(bytes32)")
    )
    if tick is not None:
        edge = (tick // route.pools[3].tick_spacing) * route.pools[3].tick_spacing
        calls.append(
            (
                STATE_VIEW,
                False,
                data_bytes(
                    calldata("getTickInfo(bytes32,int24)", ("bytes32", "int24"), (pool_id, edge))
                ),
            )
        )
    return [
        {
            "to": quote.MULTICALL3,
            "data": _encode_aggregate3_calldata(calls),
            "gas": hex(quote.TRANSPORT_GAS),
        },
        {"blockHash": block, "requireCanonical": True},
    ]


def fresh_results(raw, route, actual=None):
    outer = _decode_aggregate3_results(data_bytes(raw), 3 if actual is None else 4)
    assert all(ok for ok, _ in outer)
    rows, sizes = decode_results("0x" + outer[0][1].hex(), route, actual, FRESH_GRID)
    sqrt, tick, protocol, lp = decode(["uint160", "int24", "uint24", "uint24"], outer[1][1])
    (liq,) = decode(["uint128"], outer[2][1])
    assert lp == route.pools[3].fee
    state = {
        "sqrt_price_x96": sqrt,
        "tick": tick,
        "lp_fee": lp,
        "protocol_fee_packed": protocol,
        "liquidity": liq,
    }
    if actual is not None:
        gross, net, _, _ = decode(["uint128", "int128", "uint256", "uint256"], outer[3][1])
        state.update({"lower_tick_liquidity_gross": gross, "lower_tick_liquidity_net": net})
    return rows, sizes, state


def interval(route, state):
    pool = route.pools[3]
    assert pool.currency0 == route.path()[2][0] == ZERO_ADDRESS
    lower = state["tick"] // pool.tick_spacing * pool.tick_spacing
    active = state["liquidity"]
    net_input = amount0_delta(
        sqrt_price_at_tick(lower), state["sqrt_price_x96"], active, round_up=True
    )
    fee = settled_fee_breakdown(
        state["lp_fee"], state["protocol_fee_packed"], SwapDirection.ZERO_FOR_ONE
    ).swap_fee
    divisor = 10**6 - fee
    row = {
        "lower_grid_tick": lower,
        "direction": "zero_for_one",
        "current_liquidity": str(active),
        "swap_fee_pips": fee,
        "net_wei_to_grid_line": str(net_input),
        "gross_wei_to_grid_line": str((net_input * 10**6 + divisor - 1) // divisor),
    }
    if "lower_tick_liquidity_net" in state:
        row["immediate_liquidity_below_grid_line"] = str(active - state["lower_tick_liquidity_net"])
    return row


def head_values(route, prefixes, direct, sizes, tiers):
    assert all(r["status"] == "quoted" for r in prefixes[:3]) and direct[0] == prefixes[3]
    actual = int(prefixes[2]["amount_out"])
    points = [dict(input_wei=str(v), **r) for v, r in zip(tiers, direct[1:], strict=True)]
    passed = [int(p["input_wei"]) for p in points if p["status"] == "quoted"]
    refused = [int(p["input_wei"]) for p in points if p["status"] == "NotEnoughLiquidity"]
    return {
        "decimals": sizes,
        "prefixes": [
            dict(length=k, output_currency=route.path()[k - 1][0], **r)
            for k, r in zip(PREFIX, prefixes, strict=True)
        ],
        "isolated_fourth_at_original_input": dict(input_wei=str(actual), **direct[0]),
        "isolated_fourth_currency_out": route.path()[3][0],
        "isolated_fourth_input_tiers": points,
        "highest_quoted_grid_wei": str(max(passed)) if passed else None,
        "lowest_rejected_grid_wei": str(min(refused)) if refused else None,
    }


def fresh_replay(root, sources, route, pins, stopped_digest):
    scope = read(root / "scope.json")
    assert scope["historical_stop_sha256"] == sha256_hex((root.parent / "STOP.json").read_bytes())
    assert scope["original_checkpoint"] == stopped_digest and scope["seq"] == 53
    assert scope["tiers"] == list(FRESH_GRID) and scope["maximum_by_source"] == {
        "official": 2,
        "publicnode": 4,
    }
    assert scope["total_additional_max"] == 6 and scope["max_usd"] == 0
    assert scope["original_wei"] == str(PRINCIPAL) and scope["frames_max"] == 8
    frames = feed.decode(root / "feed", sources)
    assert read(root / "feed/scope.json")["max_frames"] == 8
    m = next(m for m in frames["messages"] if m["incoming_kind"] == 3 and m["feed_block_hash"])
    n, h = m["sequence"], m["feed_block_hash"]
    received = m["received"]["monotonic_ns"]
    assert scope["created_ns"] < received
    wires = []

    def get(i, method, args, providers):
        rs, refs = wire(root, f"{i:02d}-", i, method, args, suffixes={k: k for k in providers})
        assert all(r["sent_ns"] >= received + 900_000_000 for r in refs.values())
        wires.extend(refs.values())
        val = rs[providers[0]]["result"]
        assert all(r["result"] == val for r in rs.values())
        return val

    header, _ = assert_feed_header(
        get(1, "eth_getBlockByNumber", [hex(n), False], ["publicnode"]), FeedMessage.from_row(m)
    )
    tag = {"blockHash": h, "requireCanonical": True}
    code = (
        b"".join(
            b"\x73" + data_bytes(addr) + b"\x3f\x60" + bytes([32 * i]) + b"\x52"
            for i, addr in enumerate(pins)
        )
        + b"\x60\xa0\x60\x00\xf3"
    )
    assert set(pins) >= {quote.MULTICALL3, QUOTER, MANAGER, STATE_VIEW}
    assert get(
        2, "eth_call", [{"data": "0x" + code.hex(), "gas": "0x30000"}, tag], ["publicnode"]
    ) == "0x" + "".join(pins.values())
    providers = ["official", "publicnode"]
    prefix, sizes, first = fresh_results(
        get(3, "eth_call", fresh_params(route, h), providers), route
    )
    actual = int(prefix[2]["amount_out"])
    tier_params = fresh_params(route, h, actual, first["tick"])
    direct, _, state = fresh_results(get(4, "eth_call", tier_params, providers), route, actual)
    assert all(state[k] == v for k, v in first.items())
    assert prefix[-1] == direct[0] and direct[0]["status"] == "NotEnoughLiquidity"
    final = read(root / "done.json")
    assert final["seq"] == n and final["hash"] == h
    return dict(
        number=n,
        hash=h,
        state_root=header.state_root,
        feed_summary=frames["summary"],
        scope_sha256=sha256_hex((root / "scope.json").read_bytes()),
        runtime_keccaks=pins,
        source_count=2,
        wire=wires,
        slot_and_boundary=state,
        first_band=interval(route, state),
        **head_values(route, prefix, direct, sizes, FRESH_GRID),
    )


def replay(root, sources):
    scope = read(root / "scope.json")
    previous = DOCS / "ROBINHOOD_THREE_CASH_STATES_20261127.json"
    prior = read(previous)
    run = root.parent / "forward"
    assert sha256_hex(previous.read_bytes()) == scope["baseline_sha256"]
    assert (
        sha256_hex((run / "routes.json").read_bytes())
        == prior["original_routes_sha256"]
        == scope["routes_sha256"]
    )
    route = next(r for r in parse_routes(read(run / "routes.json")) if r.name == "v4-24")
    assert route.amount == PRINCIPAL and scope["principal_wei"] == str(PRINCIPAL)
    assert list(GRID) == scope["direct_tiers_wei"] and list(PREFIX) == scope["prefix_lengths"]
    assert (
        scope["max_additional"] == 4
        and scope["source"] == quote.SOURCES["official"]
        and scope["max_usd"] == 0
    )
    old = read(DOCS / "ROBINHOOD_ROUTER_SHADOW_20261127.json")["corrected"]["states"][0]
    now = prior["states"]["discovery"]
    assert scope["ordered_heads"] == [[s["number"], s["hash"]] for s in (now, old)]
    assert old["hash"] == prior["previous_successful_long_route"]["hash"]
    wires = []

    def get(i, h, actual=None):
        env, refs = wire(
            root,
            f"{i:02}-",
            i,
            "eth_call",
            params(route, h, actual),
            suffixes={"official": "official"},
        )
        assert scope["registered_ns"] < refs["official"]["sent_ns"]
        wires.append(refs["official"])
        return env["official"]

    h = now["hash"]
    prefixes, sizes = decode_results(get(1, h)["result"], route)
    actual = int(prefixes[2]["amount_out"])
    direct, _ = decode_results(get(2, h, actual)["result"], route, actual)
    assert prefixes[-1] == direct[0] and direct[0]["pool_id"] == now["failed_long_route"]["pool_id"]
    pool = now["failed_long_route"]["pool_state"]
    afternoon = dict(
        hash=h,
        number=now["number"],
        state_root=now["state_root"],
        source_count=1,
        slot=pool,
        first_band=interval(route, pool),
        **head_values(route, prefixes, direct, sizes, GRID),
    )
    err = get(3, old["hash"])["error"]
    assert (
        err["code"] == -32000
        and "historical state" in err["message"]
        and "not available" in err["message"]
    )
    stop = read(root / "STOP.json")
    assert stop["at"] == 3 and not (root / "04-official.request").exists()
    old_pool = prior["previous_successful_long_route"]["pool_state"]
    earlier = {
        "hash": old["hash"],
        "number": old["number"],
        "historical_quote": prior["previous_successful_long_route"]["quote"],
        "source_count_for_segments": 0,
        "isolated_fourth_at_original_input": None,
        "first_band": interval(route, old_pool),
        "slot": old_pool,
        "error": err,
    }
    with RpcBudget(
        run / "original.sqlite3",
        tuple(BudgetPolicy(s, 30, 250) for s in quote.SOURCES),
        read_only=True,
    ) as b:
        counts = b.counts()
        assert (
            counts == read(root / "fresh/done.json")["counts"] == {"official": 30, "publicnode": 29}
        )
        reservations = list(b.db.execute("SELECT seq,payload,digest FROM rpc_usage ORDER BY seq"))
        counts_at_stop = dict(Counter(json.loads(raw)["source"] for _, raw, _ in reservations[:53]))
        assert counts_at_stop == stop["counts"] == {"official": 28, "publicnode": 25}
        assert (
            reservations[49][2] == scope["original_checkpoint_digest"]
            and scope["original_checkpoint_seq"] == 50
        )
        assert [json.loads(row[1])["source"] for row in reservations[50:53]] == ["official"] * 3
        assert [json.loads(row[1])["source"] for row in reservations[53:]] == [
            "publicnode",
            "publicnode",
            "official",
            "publicnode",
            "official",
            "publicnode",
        ]
    fresh = fresh_replay(
        root / "fresh",
        sources,
        route,
        read(run / "plan.json")["runtime_keccaks"],
        reservations[52][2],
    )
    return {
        "schema": "oasis-robinhood-fourth-leg-capacity-v1",
        "chain_id": 4663,
        "historical_scope_sha256": sha256_hex((root / "scope.json").read_bytes()),
        "baseline_report_sha256": scope["baseline_sha256"],
        "principal_native_wei": str(PRINCIPAL),
        "route": route.name,
        "pool_id": "0x" + list(_pool_ids((route,)))[3].hex(),
        "original_ledger_counts": counts,
        "historical_stop_ledger_counts": counts_at_stop,
        "earlier_morning": earlier,
        "anchored_afternoon": afternoon,
        "historical_wire": wires,
        "subsequent_blind_window": fresh,
    }


if __name__ == "__main__":
    if not __debug__:
        raise RuntimeError("evidence replay requires assertions")
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("root", type=Path)
    cli.add_argument("--sources-dir", type=Path, required=True)
    args = cli.parse_args()
    print(json.dumps(replay(args.root, args.sources_dir), indent=2, sort_keys=True))
