"""Check a locally modelled cycle against real pool swaps at one pinned block, read-only.

1. Pinned full state and depth read; rebuild the local model; quote the route locally.
2. ``eth_call`` the read-only route probe via state override on the same block hash. Each hop
   runs the pool's real ``swap`` and reverts in the callback, so outputs are exact pool deltas.
Two sources answer the probe; both must agree. Nothing is signed or sent.
"""

import argparse
import json
from pathlib import Path

import httpx
import run_base_log_state_shadow as shadow
from eth_abi.abi import decode, encode

from oasis_agent.fastpath.cycle_bounds import cycle_rate
from oasis_agent.fastpath.pool_graph import quote_pool
from oasis_agent.models.common import canonical_json, sha256_hex
from oasis_agent.rpc.budget import BudgetPolicy, RpcBudget

SOURCES = {"state": "https://base-rpc.publicnode.com", "sim2": "https://mainnet.base.org"}
POLICIES = (BudgetPolicy("state", 12, 250), BudgetPolicy("sim2", 4, 250))
PROBE = "0x00000000000000000000000000000000000a5b3e"
RUNTIME = Path("/tmp/oasis-routeprobe/runtime.hex")
QUOTE_SIG = "quote((address,address)[],uint256)"


class Lane(shadow.Lane):
    def __init__(self, root: Path, key: str, client: httpx.Client) -> None:
        super().__init__(root, key, POLICIES, client)

    def call(self, method: str, params: list[object]) -> object:
        shadow.HTTP[self.key] = SOURCES[self.key]
        return super().call(method, params)


def probe_call(lane: Lane, runtime: str, hops, amount: int, block_hash: str):
    data = (
        shadow._selector(QUOTE_SIG)
        + encode(["(address,address)[]", "uint256"], [hops, amount]).hex()
    )
    raw = lane.call(
        "eth_call",
        [
            {"to": PROBE, "data": data, "gas": hex(6_000_000)},
            {"blockHash": block_hash, "requireCanonical": True},
            {PROBE: {"code": runtime}},
        ],
    )
    assert isinstance(raw, str)
    outputs, gas = decode(["uint256[]", "uint256[]"], bytes.fromhex(raw[2:]))
    return [int(x) for x in outputs], [int(x) for x in gas]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--roster", type=Path, required=True)
    parser.add_argument("--route", required=True, help="comma-separated pool ids, base WETH")
    parser.add_argument(
        "--amounts", default="1000000000000000,10000000000000000,100000000000000000"
    )
    args = parser.parse_args()
    root: Path = args.root
    (root / "rpc").mkdir(parents=True)
    with RpcBudget(root / "original.sqlite3", POLICIES):
        pass
    runtime = RUNTIME.read_text().strip()
    roster = json.loads(args.roster.read_bytes())["pool_specs"]
    route = [p.strip().lower() for p in args.route.split(",")]
    by_id = {row["pool_id"]: row for row in roster}
    specs = tuple(
        shadow.DiscoveredPool(
            **{k: (shadow.PoolKind(v) if k == "kind" else v) for k, v in by_id[p].items()}
        )
        for p in route
    )
    with httpx.Client(timeout=8, trust_env=False, follow_redirects=False) as client:
        state, sim2 = Lane(root, "state", client), Lane(root, "sim2", client)
        head = state.call("eth_getBlockByNumber", ["latest", False])
        assert isinstance(head, dict)
        block_hash, height = head["hash"], int(head["number"], 16)
        nodes, spacings, _ = shadow.pinned_read(state, specs, block_hash, height)
        depths = shadow.depth_read(state, nodes, spacings, block_hash)
        model = {}
        for key, node in nodes.items():
            d = depths.get(key)
            model[key] = (
                node
                if d is None
                else shadow.depth_node(
                    node,
                    sqrt_price_x96=node.sqrt_price_x96,
                    tick=node.tick,
                    liquidity=node.liquidity,
                    depth=d,
                    height=height,
                )
            )
        token = shadow.WETH
        hops = []
        for pool_id in route:
            node = model[pool_id]
            hops.append((pool_id, token))
            token = node.token1 if token == node.token0 else node.token0
        rate = cycle_rate(model, shadow.WETH, route)
        rows = []
        for amount in (int(x) for x in args.amounts.split(",")):
            local: list[object] = []
            x = amount
            try:
                for pool_id, tin in hops:
                    x = quote_pool(model[pool_id], tin, x).amount_out
                    local.append(x)
            except Exception as exc:  # noqa: BLE001 - window exit is a recorded outcome
                local.append(f"{type(exc).__name__}")
            onchain = probe_call(state, runtime, hops, amount, block_hash)
            rows.append(
                {
                    "amount_in": str(amount),
                    "local_outputs": [str(v) for v in local],
                    "onchain_outputs": [str(v) for v in onchain[0]],
                    "onchain_gas": onchain[1],
                    "onchain_gross": str(onchain[0][-1] - amount),
                }
            )
        witness = probe_call(sim2, runtime, hops, int(args.amounts.split(",")[0]), block_hash)
    with RpcBudget(root / "original.sqlite3", POLICIES, read_only=True) as b:
        counts = b.counts()
    summary = {
        "schema": "oasis.base-route-onchain-check.v1",
        "block": {"number": height, "hash": block_hash},
        "route": route,
        "spot_rate_minus_one_bp": float((rate - 1) * 10_000) if rate is not None else None,
        "rows": rows,
        "second_source_first_amount": [str(v) for v in witness[0]],
        "second_source_agrees": [str(v) for v in witness[0]] == rows[0]["onchain_outputs"],
        "probe_runtime_sha256": sha256_hex(runtime.encode()),
        "driver_sha256": sha256_hex(Path(__file__).read_bytes()),
        "ledger": counts,
        "usd": 0,
    }
    (root / "summary.json").write_bytes(canonical_json(summary))
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
