"""Read-only Base shadow: seed 58 pools once, then advance them from streamed logs only.

Measures, per head, local receive -> provisional state -> Rust scan done, against the direct
child head. Reconciles the log state against an independent pinned full read every
``AUDIT_EVERY`` confirmed blocks. Counts swaps on untracked pools as a venue census.
Every request is reserved on the original ledger first; the first refusal stops the run.
Nothing is signed or broadcast; no candidate is eligible for execution.
"""

import argparse
import json
import ssl
import threading
import time
from collections import Counter
from dataclasses import replace
from pathlib import Path
from queue import Empty, Queue

import certifi
import httpx
from eth_abi.abi import decode, encode
from websockets.sync.client import connect

from oasis_agent.fastpath.base_log_state import (
    CONFIRM_LAG,
    V3_FEE_SPACING,
    BaseLogState,
    ClDepth,
    LogStateError,
    StreamHead,
    cl_interval_node,
    decode_stream_head,
    decode_stream_log,
    depth_node,
    depth_window,
    stream_topic_filter,
    topic_name,
)
from oasis_agent.fastpath.base_mixed_snapshot import address_roster
from oasis_agent.fastpath.cycle_bounds import bound_residual
from oasis_agent.fastpath.live_shadow_runner import (
    decode_aero_factory_fee_pips,
    decode_aero_get_reserves,
    decode_l1_fee_wei,
    decode_liquidity_word,
    decode_slipstream_fee_pips,
    decode_slot0_words,
)
from oasis_agent.fastpath.multicall_universe import (
    MULTICALL3,
    DiscoveredPool,
    _decode_aggregate3_results,
    _encode_aggregate3_calldata,
    _selector,
)
from oasis_agent.fastpath.pool_graph import PoolKind, PoolNode
from oasis_agent.fastpath.resident_graph import AnchorScan, GraphBlock, ResidentPoolGraph
from oasis_agent.models.common import canonical_json, sha256_hex
from oasis_agent.rpc.budget import BudgetPolicy, RpcBudget

WS = "wss://base-rpc.publicnode.com"
HTTP = {
    "seed": "https://base-rpc.publicnode.com",
    "fee": "https://base-rpc.publicnode.com",
    "audit": "https://mainnet.base.org",
}
ROSTER = Path("reports/research/base-resident-forward-20261123/run/multihop_replay/manifest.json")
WETH = "0x4200000000000000000000000000000000000006"
USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
ORACLE = "0x420000000000000000000000000000000000000f"
AUDIT_EVERY = 16
FEE_EVERY = 4  # CL dynamic fees change without logs; re-read them off the critical path
L2_GAS = 550_000  # same fixed envelope as the prior Base 58-pool scans
TIP_WEI = 1_000_000
SLOT0 = _selector("slot0()")
LIQ = _selector("liquidity()")
FEE = _selector("fee()")
SPACING = _selector("tickSpacing()")
RESERVES = _selector("getReserves()")
BITMAP = _selector("tickBitmap(int16)")
TICKS = _selector("ticks(int24)")
HALF_WORDS = 1  # bitmap words read on each side of the active word
TICKS_PER_SIDE = 16  # nearest initialized ticks kept on each side of the active tick
MAX_DEPTH_TICKS = 1_600
# Uniswap V3 Tick.Info: gross, net, feeGrowth0/1, tickCumulative, secondsPerLiquidity,
# secondsOutside, initialized. Slipstream adds stakedLiquidityNet and rewardGrowthOutside.
V3_TICK_TYPES = (
    "uint128",
    "int128",
    "uint256",
    "uint256",
    "int56",
    "uint160",
    "uint32",
    "bool",
)
SLIPSTREAM_TICK_TYPES = (
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
GET_FEE = _selector("getFee(address,bool)")
L1_FEE = _selector("getL1FeeUpperBound(uint256)")


def policies(heads: int) -> tuple[BudgetPolicy, ...]:
    audits = heads // AUDIT_EVERY + 2
    return (
        BudgetPolicy("ws", 4, 0),
        BudgetPolicy("seed", 3, 250),
        BudgetPolicy("audit", 3 * audits, 250),
        BudgetPolicy("fee", heads // FEE_EVERY + 4, 250),
    )


class Lane:
    """Pinned JSON-RPC with ledger reservation, raw retention and one-shot refusal."""

    def __init__(self, root: Path, key: str, pol: tuple[BudgetPolicy, ...], client: httpx.Client):
        self.root, self.key, self.pol, self.client, self.count = root, key, pol, client, 0

    def call(self, method: str, params: list[object]) -> object:
        self.count += 1
        prefix = self.root / "rpc" / f"{self.key}-{self.count:03d}-{method}"
        with RpcBudget(self.root / "original.sqlite3", self.pol, require_existing=True) as budget:
            while wait := budget.reserve(self.key, now_ms=time.time_ns() // 1_000_000):
                time.sleep(wait / 1000 + 0.001)
        body = canonical_json(
            {"jsonrpc": "2.0", "id": self.count, "method": method, "params": params}
        )
        prefix.with_suffix(".request").write_bytes(body)
        sent = time.monotonic_ns()
        reply = self.client.post(
            HTTP[self.key], content=body, headers={"Content-Type": "application/json"}
        )
        raw = reply.content
        prefix.with_suffix(".response").write_bytes(raw)
        meta = {
            "status": reply.status_code,
            "sent_ns": sent,
            "received_ns": time.monotonic_ns(),
            "request_sha256": sha256_hex(body),
            "response_sha256": sha256_hex(raw),
        }
        prefix.with_suffix(".meta").write_bytes(canonical_json(meta))
        if reply.status_code != 200:
            raise RuntimeError(f"{self.key} HTTP refusal {reply.status_code}")
        env = json.loads(raw)
        if env.get("id") != self.count or "error" in env or "result" not in env:
            raise RuntimeError(f"{self.key} RPC refusal {env.get('error')}")
        return env["result"]


def pinned_read(
    lane: Lane, specs: tuple[DiscoveredPool, ...], block_hash: str, height: int
) -> tuple[dict[str, PoolNode], dict[str, int | None], int]:
    """One Multicall pinned by hash: CL slot0/liquidity/fee/spacing, V2 reserves/fee, L1 fee."""
    calls: list[tuple[str, bool, bytes]] = [
        (ORACLE, False, bytes.fromhex(L1_FEE[2:]) + encode(["uint256"], [600])),
        (MULTICALL3, False, bytes.fromhex(_selector("getChainId()")[2:])),
    ]
    slices = []
    for spec in specs:
        start = len(calls)
        if spec.kind in (PoolKind.V3, PoolKind.SLIPSTREAM):
            calls.extend(
                (spec.pool_id, False, bytes.fromhex(sel[2:])) for sel in (SLOT0, LIQ, FEE, SPACING)
            )
        else:
            calls.append((spec.pool_id, False, bytes.fromhex(RESERVES[2:])))
            fee_cd = bytes.fromhex(GET_FEE[2:]) + encode(["address", "bool"], [spec.pool_id, False])
            calls.append((spec.factory, False, fee_cd))
        slices.append((spec, start))
    pin = {"blockHash": block_hash, "requireCanonical": True}
    raw = lane.call(
        "eth_call", [{"to": MULTICALL3, "data": _encode_aggregate3_calldata(calls)}, pin]
    )
    assert isinstance(raw, str)
    results = _decode_aggregate3_results(bytes.fromhex(raw[2:]), len(calls))
    if not all(ok for ok, _ in results):
        raise RuntimeError("pinned read had a failed subcall")
    if int.from_bytes(results[1][1], "big") != 8453:
        raise RuntimeError("pinned read chain mismatch")
    l1_fee = decode_l1_fee_wei(results[0][1])
    nodes: dict[str, PoolNode] = {}
    spacings: dict[str, int | None] = {}
    for spec, start in slices:
        base = PoolNode(
            8453,
            spec.pool_id,
            spec.kind,
            spec.token0,
            spec.token1,
            spec.decimals0,
            spec.decimals1,
            1,
            height,
        )
        if spec.kind in (PoolKind.V3, PoolKind.SLIPSTREAM):
            sqrt_p, tick = decode_slot0_words(results[start][1])
            liq = decode_liquidity_word(results[start + 1][1])
            fee = decode_slipstream_fee_pips(results[start + 2][1])
            (spacing,) = decode(["int24"], results[start + 3][1])
            if spec.kind == PoolKind.V3 and V3_FEE_SPACING.get(fee) != spacing:
                raise RuntimeError(f"{spec.label} fee/spacing mismatch")
            if spec.tick_spacing is not None and spec.tick_spacing != spacing:
                raise RuntimeError(f"{spec.label} spacing changed")
            node = cl_interval_node(
                PoolNode(**{**_fields(base), "fee_pips": fee}),
                sqrt_price_x96=sqrt_p,
                tick=tick,
                liquidity=liq,
                spacing=spacing,
                height=height,
            )
            spacings[spec.pool_id] = spacing
        else:
            r0, r1, _ = decode_aero_get_reserves(results[start][1])
            fee = decode_aero_factory_fee_pips(results[start + 1][1])
            node = PoolNode(**{**_fields(base), "fee_pips": fee, "reserve0": r0, "reserve1": r1})
            spacings[spec.pool_id] = None
        nodes[spec.pool_id] = node
    return nodes, spacings, l1_fee


def _multicall(lane: Lane, calls: list[tuple[str, bool, bytes]], block_hash: str) -> list[bytes]:
    pin = {"blockHash": block_hash, "requireCanonical": True}
    raw = lane.call(
        "eth_call", [{"to": MULTICALL3, "data": _encode_aggregate3_calldata(calls)}, pin]
    )
    assert isinstance(raw, str)
    results = _decode_aggregate3_results(bytes.fromhex(raw[2:]), len(calls))
    if not all(ok for ok, _ in results):
        raise RuntimeError("pinned Multicall had a failed subcall")
    return [data for _, data in results]


def depth_read(
    lane: Lane, nodes: dict[str, PoolNode], spacings: dict[str, int | None], block_hash: str
) -> dict[str, ClDepth]:
    """Two pinned Multicalls: bitmap words around each active tick, then every set tick."""
    windows = {}
    calls: list[tuple[str, bool, bytes]] = []
    for key, node in nodes.items():
        spacing = spacings[key]
        if spacing is None or node.tick is None:
            continue
        lower, upper, words = depth_window(node.tick, spacing, HALF_WORDS)
        windows[key] = (spacing, lower, upper, words, len(calls))
        calls.extend(
            (key, False, bytes.fromhex(BITMAP[2:]) + encode(["int16"], [w])) for w in words
        )
    bitmaps = _multicall(lane, calls, block_hash)
    tick_calls: list[tuple[str, bool, bytes]] = []
    owners: list[tuple[str, int]] = []
    bounded = {}
    for key, (spacing, lower, upper, words, start) in windows.items():
        initialized = []
        for offset, word in enumerate(words):
            bits = int.from_bytes(bitmaps[start + offset], "big")
            while bits:
                bit = (bits & -bits).bit_length() - 1
                bits &= bits - 1
                initialized.append((word * 256 + bit) * spacing)
        tick = nodes[key].tick
        assert tick is not None
        below = [t for t in initialized if t <= tick][-(TICKS_PER_SIDE + 1) :]
        above = [t for t in initialized if t > tick][: TICKS_PER_SIDE + 1]
        # The (N+1)th initialized tick, when present, becomes a draining edge; nets strictly
        # between the edges are complete because the bitmap words covering them were read.
        low = below[0] if len(below) > TICKS_PER_SIDE else lower
        high = above[-1] if len(above) > TICKS_PER_SIDE else upper
        bounded[key] = (spacing, low, high)
        for t in initialized:
            if low < t < high:
                owners.append((key, t))
                tick_calls.append((key, False, bytes.fromhex(TICKS[2:]) + encode(["int24"], [t])))
    if len(tick_calls) > MAX_DEPTH_TICKS:
        raise RuntimeError(f"depth read would need {len(tick_calls)} tick calls")
    values = _multicall(lane, tick_calls, block_hash) if tick_calls else []
    nets: dict[str, dict[int, int]] = {key: {} for key in windows}
    for (key, tick), raw in zip(owners, values, strict=True):
        types = V3_TICK_TYPES if nodes[key].kind == PoolKind.V3 else SLIPSTREAM_TICK_TYPES
        if len(raw) != 32 * len(types):
            raise RuntimeError(f"ticks() returned {len(raw)} bytes for {nodes[key].kind}")
        gross, net, *_, initialized = decode(list(types), raw)
        if not initialized or gross <= 0 or abs(net) > gross:
            raise RuntimeError("bitmap marks an uninitialized or inconsistent tick")
        nets[key][tick] = net
    return {
        key: ClDepth(spacing, low, high, nets[key]) for key, (spacing, low, high) in bounded.items()
    }


def _fields(node: PoolNode) -> dict[str, object]:
    return {name: getattr(node, name) for name in node.__dataclass_fields__}


def fee_read(lane: Lane, specs: tuple[DiscoveredPool, ...], block_hash: str) -> dict[str, int]:
    """Pinned ``fee()`` for every CL pool in one Multicall."""
    cl = [spec for spec in specs if spec.kind in (PoolKind.V3, PoolKind.SLIPSTREAM)]
    calls = [(spec.pool_id, False, bytes.fromhex(FEE[2:])) for spec in cl]
    pin = {"blockHash": block_hash, "requireCanonical": True}
    raw = lane.call(
        "eth_call", [{"to": MULTICALL3, "data": _encode_aggregate3_calldata(calls)}, pin]
    )
    assert isinstance(raw, str)
    results = _decode_aggregate3_results(bytes.fromhex(raw[2:]), len(calls))
    if not all(ok for ok, _ in results):
        raise RuntimeError("fee read had a failed subcall")
    return {
        spec.pool_id: decode_slipstream_fee_pips(data)
        for spec, (_, data) in zip(cl, results, strict=True)
    }


def model_key(node: PoolNode) -> tuple[object, ...]:
    return (
        node.fee_pips,
        node.reserve0,
        node.reserve1,
        node.sqrt_price_x96,
        node.tick,
        node.liquidity,
        node.ticks,
    )


def price_key(node: PoolNode) -> tuple[object, ...]:
    """Fields a pinned read and the log state must agree on, independent of tick modelling."""
    return (node.reserve0, node.reserve1, node.sqrt_price_x96, node.tick, node.liquidity)


def eth_price_micro_usdc(nodes: dict[str, PoolNode]) -> int:
    prices = []
    for n in nodes.values():
        if {n.token0, n.token1} != {WETH, USDC}:
            continue
        if n.sqrt_price_x96:
            sq = n.sqrt_price_x96 * n.sqrt_price_x96
            prices.append(sq * 10**18 >> 192 if n.token0 == WETH else (10**18 << 192) // sq)
        elif n.reserve0 and n.reserve1:
            prices.append(
                n.reserve1 * 10**18 // n.reserve0
                if n.token0 == WETH
                else n.reserve0 * 10**18 // n.reserve1
            )
    prices.sort()
    return prices[len(prices) // 2]


def anchors(nodes: dict[str, PoolNode], head: StreamHead, l1_fee: int) -> tuple[AnchorScan, ...]:
    gas = L2_GAS * (head.base_fee_per_gas + TIP_WEI)
    price = eth_price_micro_usdc(nodes)

    def usdc(x: int) -> int:
        return (x * price + 10**18 - 1) // 10**18

    return (
        AnchorScan(WETH, 10**15, 10**18, gas_cost_in_base=gas, l1_fee_in_base=l1_fee),
        AnchorScan(
            USDC, 5_000_000, 2_000_000_000, gas_cost_in_base=usdc(gas), l1_fee_in_base=usdc(l1_fee)
        ),
    )


def stream(root: Path, pol, out: Queue, stop: threading.Event) -> None:
    frames = (root / "frames.jsonl").open("xb")
    try:
        with RpcBudget(root / "original.sqlite3", pol, require_existing=True) as budget:
            for _ in range(3):
                if budget.reserve("ws", now_ms=time.time_ns() // 1_000_000):
                    raise RuntimeError("ws budget pacing")
        with connect(
            WS,
            proxy=None,
            ssl=ssl.create_default_context(cafile=certifi.where()),
            open_timeout=6,
            close_timeout=1,
            max_size=4 << 20,
            compression=None,
            ping_interval=None,
        ) as ws:
            ws.send(
                canonical_json(
                    {"jsonrpc": "2.0", "id": 1, "method": "eth_subscribe", "params": ["newHeads"]}
                )
            )
            ws.send(
                canonical_json(
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "eth_subscribe",
                        "params": ["logs", stream_topic_filter()],
                    }
                )
            )
            roles: dict[str, str] = {}
            while not stop.is_set():
                try:
                    raw = ws.recv(timeout=4)
                except TimeoutError:
                    raise RuntimeError("ws receive deadline") from None
                now = time.monotonic_ns()
                data = raw if isinstance(raw, bytes) else raw.encode()
                frames.write(canonical_json({"mono_ns": now, "frame": data.decode()}) + b"\n")
                frames.flush()
                msg = json.loads(data)
                if "id" in msg:
                    if "error" in msg:
                        raise RuntimeError(f"subscription refused {msg['error']}")
                    roles[msg["result"]] = "head" if msg["id"] == 1 else "log"
                    continue
                params = msg["params"]
                out.put((roles[params["subscription"]], params["result"], now))
    except Exception as exc:  # noqa: BLE001 - surfaced to the consumer, which stops the run
        out.put(("error", f"{type(exc).__name__}: {exc}", time.monotonic_ns()))
    finally:
        frames.close()


SETTLE_NS = 80_000_000  # wait at most 80 ms after a head for Bloom-flagged tracked pools


def candidate_rows(reports, height: int, label: str) -> list[dict[str, object]]:
    return [
        {
            "height": height,
            "scan": label,
            "anchor": anchor,
            "net": str(c.net_profit),
            "amount_in": str(c.optimal_amount_in),
            "amount_out": str(c.amount_out),
            "gas": str(c.gas_cost_in_base),
            "l1": str(c.l1_fee_in_base),
            "pools": [h.pool_id for h in c.hops],
        }
        for anchor, report in zip(("WETH", "USDC"), reports, strict=True)
        for c in report.candidates
    ]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--heads", type=int, default=300)
    parser.add_argument("--depth", action="store_true", help="model CL tick windows from logs")
    args = parser.parse_args()
    root: Path = args.root
    (root / "rpc").mkdir(parents=True)
    pol = policies(args.heads)
    with RpcBudget(root / "original.sqlite3", pol):
        pass
    specs = address_roster(json.loads(ROSTER.read_bytes())["pool_specs"])
    plan = {
        "schema": "oasis.base-log-state-shadow.v1",
        "chain_id": 8453,
        "usd": 0,
        "execution_eligible": False,
        "ws": WS,
        "http": HTTP,
        "heads": args.heads,
        "audit_every": AUDIT_EVERY,
        "settle_ms": SETTLE_NS // 10**6,
        "roster_sha256": sha256_hex(ROSTER.read_bytes()),
        "driver_sha256": sha256_hex(Path(__file__).read_bytes()),
        "budgets": {p.source: p.max_requests for p in pol},
        "model": (
            f"CL ticks in +/-{HALF_WORDS} bitmap words from logs, interval fallback"
            if args.depth
            else "CL exact within current tick-spacing interval; V2 exact reserves"
        ),
        "cost": f"{L2_GAS} L2 gas x (baseFee + {TIP_WEI}) + pinned L1 upper bound (600 B)",
    }
    (root / "plan.json").write_bytes(canonical_json(plan))
    events: Queue = Queue()
    stop = threading.Event()
    thread = threading.Thread(target=stream, args=(root, pol, events, stop), daemon=True)
    thread.start()
    rows: list[dict[str, object]] = []
    audits: list[dict[str, object]] = []
    positives: list[dict[str, object]] = []
    census: Counter[tuple[str, str]] = Counter()
    tracked_census: Counter[str] = Counter()
    fault = None
    with httpx.Client(timeout=4, trust_env=False, follow_redirects=False) as client:
        seed_lane, audit_lane = Lane(root, "seed", pol, client), Lane(root, "audit", pol, client)
        audit_slot: list[threading.Thread] = []
        try:
            kind, first, received = events.get(timeout=10)
            while kind != "head":
                if kind == "error":
                    raise RuntimeError(first)
                kind, first, received = events.get(timeout=10)
            seed_head = decode_stream_head(first, received)
            nodes, spacings, l1_fee = pinned_read(
                seed_lane, specs, seed_head.block_hash, seed_head.height
            )
            depths: dict[str, ClDepth] = {}
            if args.depth:
                depths = depth_read(seed_lane, nodes, spacings, seed_head.block_hash)
                for key, depth in depths.items():
                    node = nodes[key]
                    assert node.sqrt_price_x96 is not None and node.tick is not None
                    assert node.liquidity is not None
                    nodes[key] = depth_node(
                        node,
                        sqrt_price_x96=node.sqrt_price_x96,
                        tick=node.tick,
                        liquidity=node.liquidity,
                        depth=depth,
                        height=seed_head.height,
                    )
            state = BaseLogState(
                seed_head.height,
                seed_head.block_hash,
                tuple(nodes.values()),
                spacings,
                history=64,
                depths=depths,
            )
            seed_depth = {
                "pools": len(depths),
                "initialized_ticks": sum(len(d.nets) for d in depths.values()),
            }
            graph_block = GraphBlock(
                8453, seed_head.height, seed_head.block_hash, seed_head.parent_hash
            )
            graph = ResidentPoolGraph(graph_block, tuple(nodes.values()))
            graph_nodes = dict(nodes)
            state_depths_at: dict[int, dict[str, ClDepth]] = {}
            seed_ready_ns = time.monotonic_ns()
            fees = {k: n.fee_pips for k, n in nodes.items()}
            fee_height = [seed_head.height]
            fee_events: list[dict[str, object]] = []
            fee_slot: list[threading.Thread] = []
            fee_lane = Lane(root, "fee", pol, client)

            def poll_fees(height: int, block_hash: str) -> None:
                try:
                    fresh = fee_read(fee_lane, specs, block_hash)
                except Exception as exc:  # noqa: BLE001 - one-shot: record and stop polling
                    fee_events.append({"height": height, "error": f"{type(exc).__name__}: {exc}"})
                    return
                moved = {k: (fees[k], v) for k, v in fresh.items() if fees[k] != v}
                fees.update(fresh)
                fee_height[0] = height
                fee_events.append({"height": height, "moved": len(moved), "changes": moved})

            open_head: StreamHead | None = None
            scans_by_height: dict[int, list[dict[str, object]]] = {}
            child_ns: dict[int, int] = {}
            scans: list[dict[str, object]] = []
            scanned_logs = -1
            confirmed_blocks = 0

            def scan(label: str) -> None:
                nonlocal graph_block, graph_nodes, scanned_logs
                assert open_head is not None
                start = time.monotonic_ns()
                raw_view, _ = state.view(open_head.height)
                # The native graph binds every observation to the scanned block. A pool's own
                # last-log height stays in the log state for reconciliation.
                view = {
                    k: replace(
                        n,
                        block_number=open_head.height,
                        timestamp_ms=open_head.timestamp_ms,
                        fee_pips=fees[k],
                    )
                    for k, n in raw_view.items()
                }
                block = GraphBlock(
                    8453, open_head.height, open_head.block_hash, open_head.parent_hash
                )
                changed = [n for k, n in view.items() if model_key(graph_nodes[k]) != model_key(n)]
                if graph_block.block_number == block.block_number - 1:
                    graph.advance(block, changed)
                else:
                    graph.replace(block, tuple(view.values()))
                graph_block, graph_nodes = block, view
                applied = time.monotonic_ns()
                scan_anchors = anchors(view, open_head, l1_fee)
                reports = graph.scan(block, scan_anchors)
                done = time.monotonic_ns()
                bounded = [0, 0, 0]  # falsified, open, no exact bound
                best_open: list[object] | None = None
                for anchor, report in zip(scan_anchors, reports, strict=True):
                    hop = anchor.per_hop_gas_cost_in_base
                    for residual in report.sizing_residuals or ():
                        cost = anchor.gas_cost_in_base + anchor.l1_fee_in_base
                        cost += hop * len(residual.pool_ids)
                        result = bound_residual(view, residual, cost, anchor.min_net_profit)
                        if result.rate is None:
                            bounded[2] += 1
                        elif result.falsified:
                            bounded[0] += 1
                        else:
                            bounded[1] += 1
                            assert result.profit_upper is not None
                            if best_open is None or result.profit_upper > int(str(best_open[1])):
                                best_open = [
                                    anchor.base_token,
                                    str(result.profit_upper),
                                    list(residual.pool_ids),
                                    str(residual.min_amount_in),
                                    str(residual.max_amount_in),
                                ]
                bound_ns = time.monotonic_ns() - done
                logs_now = state.buffered_logs(open_head.height)
                scanned_logs = sum(
                    state.buffered_logs(h) for h in range(state.height + 1, open_head.height + 1)
                )
                found = candidate_rows(reports, open_head.height, label)
                positives.extend(found)
                scans_by_height.setdefault(open_head.height, []).append(
                    {
                        "label": label,
                        "tracked_logs": logs_now,
                        "changed": len(changed),
                        "start_after_head_ns": start - open_head.received_ns,
                        "apply_ns": applied - start,
                        "scan_ns": done - applied,
                        "done_after_head_ns": done - open_head.received_ns,
                        "fee_age_blocks": open_head.height - fee_height[0],
                        "cycles": sum(r.cycles_evaluated for r in reports),
                        "residuals": sum(len(r.sizing_residuals or ()) for r in reports),
                        "residuals_falsified_open_unbounded": bounded,
                        "best_open_residual": best_open,
                        "bound_ns": bound_ns,
                        "residual_upper_max": [
                            max(
                                (x.net_profit_upper for x in r.sizing_residuals or ()), default=None
                            )
                            for r in reports
                        ],
                        "residual_upper_positive": sum(
                            x.net_profit_upper > 0
                            for r in reports
                            for x in r.sizing_residuals or ()
                        ),
                        "positives": len(found),
                    }
                )

            def audit(height: int, expected: dict[str, PoolNode], block_hash: str) -> None:
                began = time.monotonic_ns()
                try:
                    fresh, fresh_spacing, _ = pinned_read(audit_lane, specs, block_hash, height)
                    tracked_depth = {k: state_depths_at[height][k] for k in state_depths_at[height]}
                    fresh_depth = (
                        depth_read(audit_lane, fresh, fresh_spacing, block_hash)
                        if tracked_depth
                        else {}
                    )
                except Exception as exc:  # noqa: BLE001 - one-shot: record and stop audits
                    audits.append({"height": height, "error": f"{type(exc).__name__}: {exc}"})
                    return
                diffs = []
                for key, node in fresh.items():
                    if price_key(node) != price_key(expected[key]):
                        fields = [
                            f
                            for f in (
                                "reserve0",
                                "reserve1",
                                "sqrt_price_x96",
                                "tick",
                                "liquidity",
                            )
                            if getattr(node, f) != getattr(expected[key], f)
                        ]
                        diffs.append(
                            {
                                "pool": key,
                                "fields": fields,
                                "last_log_height": expected[key].block_number,
                            }
                        )
                depth_diffs = []
                for key, depth in tracked_depth.items():
                    other = fresh_depth.get(key)
                    if other is None:
                        depth_diffs.append({"pool": key, "reason": "missing_in_fresh_read"})
                        continue
                    inside = {
                        t: n
                        for t, n in other.nets.items()
                        if max(depth.lower, other.lower) < t < min(depth.upper, other.upper)
                    }
                    mine = {
                        t: n
                        for t, n in depth.nets.items()
                        if max(depth.lower, other.lower) < t < min(depth.upper, other.upper)
                    }
                    if inside != mine:
                        wrong = sorted(set(inside.items()) ^ set(mine.items()))[:6]
                        depth_diffs.append(
                            {
                                "pool": key,
                                "ticks": [list(x) for x in wrong],
                                "windows": [[depth.lower, depth.upper], [other.lower, other.upper]],
                            }
                        )
                audits.append(
                    {
                        "height": height,
                        "block_hash": block_hash,
                        "depth_pools": len(tracked_depth),
                        "depth_mismatched": depth_diffs,
                        "pools": len(fresh),
                        "mismatched": diffs,
                        "ms": (time.monotonic_ns() - began) / 1e6,
                    }
                )

            heads_seen = 1
            while heads_seen <= args.heads:
                timeout = 6.0
                if open_head is not None and not scans:
                    timeout = max(
                        0.0, (open_head.received_ns + SETTLE_NS - time.monotonic_ns()) / 1e9
                    )
                try:
                    kind, result, received = events.get(timeout=timeout)
                except Empty:
                    if open_head is not None and not scans:
                        scan("settle_deadline")
                        continue
                    raise RuntimeError("event stream stalled") from None
                if kind == "error":
                    raise RuntimeError(result)
                if kind == "log":
                    log = decode_stream_log(result, received)
                    if state.on_log(log):
                        tracked_census[topic_name(log.topics[0])] += 1
                        if open_head is not None and log.height <= open_head.height:
                            buffered = sum(
                                state.buffered_logs(h)
                                for h in range(state.height + 1, open_head.height + 1)
                            )
                            if scans and buffered > scanned_logs:
                                scan(
                                    "late_log_rescan"
                                    if log.height == open_head.height
                                    else "parent_late_log_rescan"
                                )
                            elif not scans and not state.pending(open_head.height):
                                scan("bloom_complete")
                    elif log.height > state.height:
                        census[(log.address, topic_name(log.topics[0]))] += 1
                    continue
                head = decode_stream_head(result, received)
                heads_seen += 1
                if open_head is not None and not scans:
                    scan("child_arrived")
                report = state.on_head(head)
                if report is not None and report.height in scans_by_height:
                    block_scans = scans_by_height.pop(report.height)
                    final_logs = report.tracked_logs
                    rows.append(
                        {
                            "height": report.height,
                            "tracked_logs": final_logs,
                            "logs_before_head": report.logs_before_head,
                            "changed": len(report.changed_pools),
                            "bloom_flagged": len(report.bloom_flagged),
                            "bloom_only": len(report.bloom_only),
                            "last_log_after_head_ns": None
                            if report.last_log_received_ns is None
                            else report.last_log_received_ns - report.head_received_ns,
                            "child_after_head_ns": child_ns.pop(report.height)
                            - report.head_received_ns,
                            "first_scan_included_all_logs": block_scans[0]["tracked_logs"]
                            == final_logs,
                            "logs_out_of_order": report.logs_out_of_order,
                            "head_before_seed_ready": report.head_received_ns < seed_ready_ns,
                            "scans": block_scans,
                        }
                    )
                    confirmed_blocks += 1
                    if confirmed_blocks % AUDIT_EVERY == 0 and not any(
                        t.is_alive() for t in audit_slot
                    ):
                        block_hash, expected = state.confirmed(report.height)
                        state_depths_at[report.height] = state.depth_snapshot()
                        worker = threading.Thread(
                            target=audit, args=(report.height, expected, block_hash)
                        )
                        worker.start()
                        audit_slot.append(worker)
                fee_stopped = bool(fee_events) and "error" in fee_events[-1]
                if (
                    not fee_stopped
                    and report is not None
                    and report.height % FEE_EVERY == 0
                    and not any(t.is_alive() for t in fee_slot)
                ):
                    worker = threading.Thread(
                        target=poll_fees, args=(report.height, report.block_hash)
                    )
                    worker.start()
                    fee_slot.append(worker)
                if open_head is not None:
                    child_ns[open_head.height] = head.received_ns
                open_head, scanned_logs = head, -1
                scans = scans_by_height.setdefault(head.height, [])
                if head.height <= state.height + CONFIRM_LAG and not state.pending(head.height):
                    scan("bloom_complete")
        except (
            LogStateError,
            RuntimeError,
            OSError,
            ValueError,
            KeyError,
            TimeoutError,
            Empty,
        ) as exc:
            fault = f"{type(exc).__name__}: {exc}"
        finally:
            stop.set()
            thread.join(timeout=6)
            for worker in audit_slot:
                worker.join(timeout=10)
            for worker in locals().get("fee_slot", []):
                worker.join(timeout=10)
    with RpcBudget(root / "original.sqlite3", pol, read_only=True) as budget:
        counts = budget.counts()
    summary = {
        "schema": "oasis.base-log-state-shadow-result.v1",
        "plan_sha256": sha256_hex((root / "plan.json").read_bytes()),
        "frames_sha256": sha256_hex((root / "frames.jsonl").read_bytes()),
        "fault": fault,
        "ledger": counts,
        "fee_polls": locals().get("fee_events", []),
        "seed_depth": locals().get("seed_depth"),
        "depth_downgrades": [list(x) for x in getattr(locals().get("state"), "downgrades", [])],
        "blocks": rows,
        "audits": audits,
        "positives": positives,
        "tracked_logs_by_topic": dict(tracked_census),
        "untracked_top": [
            {"pool": k[0], "topic": k[1], "logs": v} for k, v in census.most_common(80)
        ],
        "untracked_emitters": len({k[0] for k in census}),
    }
    (root / "summary.json").write_bytes(canonical_json(summary))
    done = rows
    timely = sum(
        1
        for r in done
        if r["scans"] and r["scans"][0]["done_after_head_ns"] < r["child_after_head_ns"]
    )
    print(
        json.dumps(
            {
                "fault": fault,
                "blocks": len(done),
                "timely_first_scan": timely,
                "first_scan_complete": sum(r["first_scan_included_all_logs"] for r in done),
                "audits": [
                    (a.get("height"), len(a.get("mismatched", [])), a.get("error")) for a in audits
                ],
                "positives": len(positives),
                "ledger": counts,
            },
            indent=1,
        )
    )


if __name__ == "__main__":
    main()
