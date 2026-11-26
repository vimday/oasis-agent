"""Capture and replay one frozen Base Slipstream depth window without transaction submission."""

import argparse
import json
import time
from collections import Counter
from dataclasses import asdict, dataclass
from itertools import pairwise
from pathlib import Path
from typing import Any

import httpx

from oasis_agent.arbitrage.cycle_protocols import abi, calldata, data_bytes
from oasis_agent.fastpath.base_mixed_snapshot import MixedState, replay_mixed_snapshot
from oasis_agent.fastpath.bridge import FastpathCallError
from oasis_agent.fastpath.multicall_universe import (
    MULTICALL3,
    SLIPSTREAM_QUOTE_SINGLE_SELECTOR,
    DiscoveredPool,
    _encode_aggregate3_calldata,
)
from oasis_agent.fastpath.pool_graph import FastpathPoolGraph, PoolNode
from oasis_agent.fastpath.slipstream_depth import (
    DepthCoverageError,
    SlipstreamDepth,
    initialized_ticks,
)
from oasis_agent.models.common import canonical_json, sha256_hex
from oasis_agent.rpc.budget import BudgetPolicy, RpcBudget
from oasis_agent.sniping.venues import SLIPSTREAM
from scripts.capture_public_rpc import durable_new
from scripts.measure_robinhood_quote_delivery import _body, _fetch, _read

POOL = "0x4e392fbfe4d0557c82d2f97f02ec39daa31516dd"
HEIGHTS = tuple(range(61_476_050, 61_476_054))
SOURCES = {"official": "https://mainnet.base.org", "publicnode": "https://base-rpc.publicnode.com"}
POLICIES = tuple(BudgetPolicy(source, 8, 250) for source in SOURCES)
CALLS_FOR_TICK = (
    "uint128",
    "int128",
    "int128",
    "uint256",
    "uint256",
    "uint256",
    "int56",
    "uint160",
    "uint32",
    "bool",
)
SLOT0 = ("uint160", "int24", "uint16", "uint16", "uint16", "bool")
MAX_TICKS = 128
HALF_WORDS = 8
type Json = dict[str, Any]


@dataclass(frozen=True, slots=True)
class BlockInput:
    state: MixedState
    pool: PoolNode
    identity: DiscoveredPool
    first_word: int
    manifest_digest: str
    anchors: list[Json]


def _inputs(archive: Path) -> tuple[tuple[BlockInput, ...], str]:
    raw = (archive / "shadow_attribution.jsonl").read_bytes()
    blocks = []
    for line in raw.splitlines():
        row = json.loads(line)
        mixed = row["shadow_cycle_lifecycle"]["mixed_base_state"]
        manifest = archive / mixed["manifest"]
        digest = sha256_hex(manifest.read_bytes())
        if digest != mixed["manifest_sha256"]:
            raise ValueError("mixed state manifest disagrees with attribution")
        state = replay_mixed_snapshot(manifest)
        pool = next(node for node in state.nodes if node.pool_id == POOL)
        spec = next(item for item in state.address_state.pools if item.pool_id == POOL)
        if pool.tick is None or spec.tick_spacing != 1 or spec.factory not in SLIPSTREAM:
            raise ValueError("unexpected archived Slipstream pool tick or identity")
        blocks.append(
            BlockInput(state, pool, spec, (pool.tick >> 8) - HALF_WORDS, digest, mixed["anchors"])
        )
    if tuple(block.state.block.number for block in blocks) != HEIGHTS or any(
        child.state.block.parent_hash != parent.state.block.block_hash
        for parent, child in pairwise(blocks)
    ):
        raise ValueError("expected four retained consecutive Base blocks")
    return tuple(blocks), sha256_hex(raw)


def _call(
    target: str, signature: str, types: tuple[str, ...] = (), args: tuple[object, ...] = ()
) -> tuple[str, bool, bytes]:
    return target, False, data_bytes(calldata(signature, types, args))


def _aggregate(block: BlockInput, calls: list[tuple[str, bool, bytes]]) -> list[object]:
    return [
        {"to": MULTICALL3, "data": _encode_aggregate3_calldata(calls)},
        {"blockHash": block.state.block.block_hash, "requireCanonical": True},
    ]


def _bitmap_request(block: BlockInput) -> list[object]:
    spec = block.identity
    return _aggregate(
        block,
        [
            _call(MULTICALL3, "getChainId()"),
            _call(POOL, "slot0()"),
            _call(POOL, "liquidity()"),
            _call(POOL, "fee()"),
            _call(POOL, "tickSpacing()"),
            _call(POOL, "factory()"),
            _call(POOL, "token0()"),
            _call(POOL, "token1()"),
            _call(spec.factory, "getSwapFee(address)", ("address",), (POOL,)),
            _call(
                spec.factory,
                "getPool(address,address,int24)",
                ("address", "address", "int24"),
                (spec.token0, spec.token1, 1),
            ),
            _call(SLIPSTREAM[spec.factory][0], "factory()"),
            *(
                _call(POOL, "tickBitmap(int16)", ("int16",), (word,))
                for word in range(block.first_word, block.first_word + 2 * HALF_WORDS + 1)
            ),
        ],
    )


def _subcalls(payload: object, count: int) -> list[bytes]:
    (entries,) = abi(("(bool,bytes)[]",), data_bytes(payload))
    if len(entries) != count or not all(ok for ok, _ in entries):
        raise ValueError("depth Multicall3 subcall failed")
    return [result for _, result in entries]


def _decode_bitmap(
    block: BlockInput, payload: object
) -> tuple[int, int, int, int, int, dict[int, int]]:
    words_count = 2 * HALF_WORDS + 1
    values = _subcalls(payload, 11 + words_count)
    (chain,) = abi(("uint256",), values[0])
    price, tick, _, _, _, unlocked = abi(SLOT0, values[1])
    (liquidity,) = abi(("uint128",), values[2])
    (fee,) = abi(("uint24",), values[3])
    (spacing,) = abi(("int24",), values[4])
    (factory,), (token0,), (token1,) = [abi(("address",), val) for val in values[5:8]]
    (factory_fee,) = abi(("uint24",), values[8])
    (pool,), (quoter_factory,) = [abi(("address",), val) for val in values[9:11]]
    if (
        chain != 8453
        or not unlocked
        or not liquidity
        or spacing != block.identity.tick_spacing
        or factory != block.identity.factory
        or factory != quoter_factory
        or pool != POOL
        or (token0, token1) != (block.identity.token0, block.identity.token1)
        or (price, tick, fee) != (block.pool.sqrt_price_x96, block.pool.tick, block.pool.fee_pips)
        or fee != factory_fee
    ):
        raise ValueError("depth state disagrees with archived price, fee or pool identity")
    words = {
        block.first_word + idx: abi(("uint256",), val)[0] for idx, val in enumerate(values[11:])
    }
    return price, tick, liquidity, fee, spacing, words


def _decode_nets(ticks: list[int], payload: object) -> dict[int, int]:
    nets = {}
    for tick, value in zip(ticks, _subcalls(payload, len(ticks)), strict=True):
        gross, net, *_, initialized = abi(CALLS_FOR_TICK, value)
        if not initialized or not gross > 0 or abs(net) > gross:
            raise ValueError("inconsistent initialized tick data")
        nets[tick] = net
    return nets


def _plan(
    blocks: tuple[BlockInput, ...], attribution: str, previous_control: bytes
) -> dict[str, object]:
    return {
        "schema": "oasis-base-slipstream-depth-v1",
        "chain": 8453,
        "pool": POOL,
        "anchors_sha256": attribution,
        "sources": SOURCES,
        "usd_cap": 0,
        "first_refusal_stops": True,
        "retry": False,
        "per_source_max_calls": {policy.source: policy.max_requests for policy in POLICIES},
        "minimum_source_interval_ms": 250,
        "max_ticks_per_block": MAX_TICKS,
        "signed_or_submitted_transactions": 0,
        "prior_quote_probe_sha256": sha256_hex(previous_control),
        "blocks": [
            {
                "header": asdict(block.state.block),
                "mixed_manifest_sha256": block.manifest_digest,
                "bitmap_eth_call_params": _bitmap_request(block),
                "dynamic_tick_stage": (
                    "two identical pinned Multicall3 ticks(int24), sorted bitmap set bits only"
                ),
            }
            for block in blocks
        ],
    }


def _measure(
    blocks: tuple[BlockInput, ...], folder: Path, *, replay: bool
) -> tuple[Json, dict[int, SlipstreamDepth]]:
    if not replay:
        folder.mkdir()
    result: Json = {"status": "incomplete", "steps": [], "depths": []}
    depths: dict[int, SlipstreamDepth] = {}
    earliest = 1
    ledger = folder / "original.sqlite3"
    with (
        RpcBudget(ledger, POLICIES, read_only=replay) as budget,
        httpx.Client(
            timeout=4,
            trust_env=False,
            follow_redirects=False,
        ) as client,
    ):

        def read(source: str, params: list[object]) -> object:
            nonlocal earliest
            number = len(result["steps"]) + 1
            prefix = folder / f"{number:02}-{source}"
            body = _body(number, "eth_call", params)
            entry = {"method": "eth_call", "source": source, "request_sha256": sha256_hex(body)}
            result["steps"].append(entry)
            stopped = prefix.with_suffix(".transport-stop")
            if not replay:
                wait = budget.reserve(source, now_ms=time.time_ns() // 1_000_000)
                if wait:
                    time.sleep((wait + 1) / 1000)
                    if budget.reserve(source, now_ms=time.time_ns() // 1_000_000):
                        raise ValueError("source pacing reservation declined")
                try:
                    _fetch(client, SOURCES[source], prefix, body)
                except (OSError, ValueError, httpx.HTTPError) as exc:
                    durable_new(
                        stopped,
                        canonical_json({"reason": str(exc), "request_sha256": sha256_hex(body)}),
                    )
            if stopped.exists():
                failure = json.loads(stopped.read_bytes())
                if (
                    failure["request_sha256"] != sha256_hex(body)
                    or prefix.with_suffix(".request").read_bytes() != body
                ):
                    raise ValueError("transport stop request disagrees with frozen plan")
                raise ValueError(failure["reason"])
            payload, earliest = _read(prefix, body, number, earliest)
            entry["response_sha256"] = sha256_hex(prefix.with_suffix(".response").read_bytes())
            entry["received_monotonic_ns"] = str(earliest)
            return payload

        def paired(params: list[object]) -> object:
            official = read("official", params)
            if read("publicnode", params) != official:
                raise ValueError("dual-source depth state disagreement")
            return official

        try:
            for block in blocks:
                state = _decode_bitmap(block, paired(_bitmap_request(block)))
                ticks = sorted(initialized_ticks(state[-1], state[-2]))
                if len(ticks) > MAX_TICKS:
                    raise ValueError("initialized tick count exceeds precommitted bound")
                params = _aggregate(
                    block, [_call(POOL, "ticks(int24)", ("int24",), (tick,)) for tick in ticks]
                )
                liquidity_nets = _decode_nets(ticks, paired(params)) if ticks else {}
                depth = SlipstreamDepth(*state, liquidity_nets)
                number = block.state.block.number
                depths[number] = depth
                result["depths"].append(
                    {
                        "block_number": number,
                        "initialized_ticks": ticks,
                        "bitmap_word_range": [min(depth.words), max(depth.words)],
                        "tick": depth.tick,
                        "sqrt_price_x96": str(depth.sqrt_price_x96),
                        "liquidity": depth.liquidity,
                        "fee_pips": depth.fee_pips,
                        "tick_spacing": depth.spacing,
                    }
                )
            result["status"] = "complete"
        except (ValueError, OSError) as exc:
            result["reason"] = str(exc)
        result["rpc_counts"] = budget.counts()
    return result, depths


def _quote_control(folder: Path, block: BlockInput, depth: SlipstreamDepth) -> Json:
    plan_raw = (folder / "plan.json").read_bytes()
    plan = json.loads(plan_raw)
    report = json.loads((folder / "run/report.json").read_bytes())
    spec = block.identity
    block_hash = block.state.block.block_hash
    sample = plan["sample"]
    token_in, token_out = sample["token_in"], sample["token_out"]
    amount_in = sample["leg_amount_in"]
    if (
        report["status"] != "complete"
        or report["scope_sha256"] != sha256_hex(plan_raw)
        or report["rpc_counts"] != {"official": 3, "publicnode": 3}
        or sample["block"]["block_hash"] != block_hash
        or sample["identified_pool"]["pool_id"] != POOL
        or {token_in, token_out} != {spec.token0, spec.token1}
    ):
        raise ValueError("prior dual-source Quoter control disagrees with depth scope")
    target, _, call = _call(
        SLIPSTREAM[spec.factory][0],
        "quoteExactInputSingle((address,address,uint256,int24,uint160))",
        ("(address,address,uint256,int24,uint160)",),
        ((token_in, token_out, amount_in, depth.spacing, 0),),
    )
    data = "0x" + call.hex()
    if not data.startswith(SLIPSTREAM_QUOTE_SINGLE_SELECTOR):
        raise ValueError("unexpected Slipstream Quoter selector")
    params = [{"to": target, "data": data}, {"blockHash": block_hash, "requireCanonical": True}]
    quote = depth.quote(amount_in, zero_for_one=token_in == spec.token0)
    expected = (quote.amount_out, quote.sqrt_price_x96, quote.initialized_ticks_crossed)
    for number, source in enumerate(SOURCES, 5):
        method = "eth_call"
        body = _body(number, method, params)
        prefix = folder / "run" / f"{number:02}-{source}"
        step = report["steps"][number - 1]
        if (
            plan["steps"][number - 1] != [source, method, params]
            or step["source"] != source
            or step["response_sha256"] != sha256_hex(prefix.with_suffix(".response").read_bytes())
        ):
            raise ValueError("prior Quoter wire proof disagrees with the report")
        raw, _ = _read(prefix, body, number, 1)
        if abi(("uint256", "uint160", "uint32", "uint256"), data_bytes(raw))[:3] != expected:
            raise ValueError("Slipstream integer replay disagrees with dual-source Quoter")
    return {
        "block_number": block.state.block.number,
        "block_hash": block_hash,
        "token_in": token_in,
        "token_out": token_out,
        "amount_in": amount_in,
        "amount_out": quote.amount_out,
        "sqrt_price_x96_after": str(quote.sqrt_price_x96),
        "initialized_ticks_crossed": quote.initialized_ticks_crossed,
        "dual_source_exact_match": True,
    }


def _route_quote(
    graph: FastpathPoolGraph,
    block: BlockInput,
    pools: list[str],
    asset: str,
    initial: int,
    depth: SlipstreamDepth | None,
) -> tuple[int | None, str | None]:
    token, amount = asset, initial
    for pool in pools:
        try:
            if pool == POOL and depth is not None:
                if token not in (block.pool.token0, block.pool.token1):
                    raise ValueError("invalid depth leg input token")
                direction = token == block.pool.token0
                amount = depth.quote(amount, zero_for_one=direction).amount_out
                balances = block.state.address_state.balances_by_pool[POOL]
                if amount > balances[int(direction)]:
                    raise ValueError("Slipstream output exceeds pinned pool balance")
                token = block.pool.token1 if direction else block.pool.token0
            else:
                leg = graph.quote_pool(8453, pool, token, amount)
                if leg.amount_in != amount or leg.token_in != token:
                    raise ValueError("unconsumed modeled quote")
                token, amount = leg.token_out, leg.amount_out
            if amount <= 0:
                raise ValueError("zero quote")
        except DepthCoverageError:
            return None, "slipstream_bitmap_coverage"
        except FastpathCallError, ValueError:
            return None, pool
    if token != asset:
        raise ValueError("sizing residual route did not close")
    return amount, None


def _grid_anchor(
    block: BlockInput, anchor: Json, graph: FastpathPoolGraph, depth: SlipstreamDepth
) -> Json:
    asset = anchor["base_token"]
    cost = int(anchor["gas_cost_in_base"]) + int(anchor["l1_fee_in_base"])
    counts: Counter[str] = Counter()
    missing_before: Counter[str] = Counter()
    missing_after: Counter[str] = Counter()
    best = None
    for route in anchor["sizing_residuals"]:
        lo, hi, pools = route["min_amount_in"], route["max_amount_in"], route["pool_ids"]
        if len(set(pools)) != len(pools):
            raise ValueError("grid cannot quote a route that reuses pool state")
        for amount in sorted({lo + (hi - lo) * i // 32 for i in range(33)}):
            counts["points"] += 1
            base, miss0 = _route_quote(graph, block, pools, asset, amount, None)
            changed, miss1 = (
                _route_quote(graph, block, pools, asset, amount, depth)
                if POOL in pools
                else (base, miss0)
            )
            if miss0 is not None:
                missing_before[miss0] += 1
            if miss1 is not None:
                missing_after[miss1] += 1
            counts["baseline_closed_quotes"] += base is not None
            counts["depth_closed_quotes"] += changed is not None
            counts["restored_quotes"] += base is None and changed is not None
            counts["lost_quotes"] += base is not None and changed is None
            counts["both_quoted_different"] += (
                base is not None and changed is not None and base != changed
            )
            counts["baseline_positive"] += base is not None and base - amount - cost > 0
            if changed is not None:
                net = changed - amount - cost
                counts["depth_positive"] += net > 0
                if best is None or net > best["modeled_net_raw"]:
                    best = {
                        "input_raw": amount,
                        "output_raw": changed,
                        "modeled_net_raw": net,
                        "pools": pools,
                    }
    return {
        "block_number": block.state.block.number,
        "anchor": asset,
        "fixed_cost_raw": cost,
        "counts": dict(counts),
        "first_unpriced_before": dict(missing_before),
        "first_unpriced_after": dict(missing_after),
        "best_with_depth": best,
    }


def _grid(blocks: tuple[BlockInput, ...], depths: dict[int, SlipstreamDepth]) -> Json:
    subsets = []
    totals: Counter[str] = Counter()
    for block in blocks:
        graph = FastpathPoolGraph()
        for node in block.state.nodes:
            graph.upsert_pool(node)
        depth = depths[block.state.block.number]
        for anchor in block.anchors:
            row = _grid_anchor(block, anchor, graph, depth)
            totals.update(row["counts"])
            subsets.append(row)
    return {
        "qualification": "one exact leg; other legs and costs retain the original model",
        "counts": dict(totals),
        "by_block_and_anchor": subsets,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("prepare", "capture", "replay", "compare-grid"))
    parser.add_argument("--scope", required=True, type=Path)
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--quote-control", required=True, type=Path)
    args = parser.parse_args()
    blocks, evidence = _inputs(args.archive)
    previous = (args.quote_control / "run/report.json").read_bytes()
    plan = canonical_json(_plan(blocks, evidence, previous))
    plan_path = args.scope / "plan.json"
    if args.operation == "prepare":
        args.scope.mkdir()
        durable_new(plan_path, plan)
        print(sha256_hex(plan))
        return 0
    if plan_path.read_bytes() != plan:
        raise ValueError("frozen scope or retained input changed")
    run = args.scope / "run"
    replay = args.operation != "capture"
    report, depths = _measure(blocks, run, replay=replay)
    report["plan_sha256"] = sha256_hex(plan)
    raw = canonical_json(report)
    if replay:
        if raw != (run / "capture.json").read_bytes():
            raise ValueError("replay envelope differs from retained capture")
    else:
        durable_new(run / "capture.json", raw)
        durable_new(run / "STOP.json", canonical_json({"status": report["status"]}))
    if args.operation == "compare-grid" and report["status"] == "complete":
        control = _quote_control(args.quote_control, blocks[0], depths[HEIGHTS[0]])
        raw = canonical_json(
            {
                "capture_sha256": sha256_hex(raw),
                "quote_control": control,
                "grid": _grid(blocks, depths),
            }
        )
    print(raw.decode())
    return 0 if report["status"] == "complete" else 2


if __name__ == "__main__":
    raise SystemExit(main())
