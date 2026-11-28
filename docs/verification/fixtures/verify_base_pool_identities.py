"""Verify untracked Base log emitters as canonical pools before they may enter the roster.

A pool qualifies only when an approved factory's ``getPool`` returns the emitter itself for
the token pair and fee or spacing the emitter reports. Read-only, pinned to one block hash,
one source, budgeted on the original ledger. The first refusal stops the run.
"""

import argparse
import json
import time
from pathlib import Path

import httpx
from eth_abi.abi import decode, encode

from oasis_agent.fastpath.multicall_universe import (
    AERO_CL2_FACTORY,
    MULTICALL3,
    SLIPSTREAM_FACTORY,
    UNIV3_FACTORY,
    _encode_aggregate3_calldata,
    _selector,
)
from oasis_agent.models.common import canonical_json, sha256_hex
from oasis_agent.rpc.budget import BudgetPolicy, RpcBudget
from oasis_agent.sniping.venues import AERO_FACTORY, SLIPSTREAM

SOURCE = "https://base-rpc.publicnode.com"
POLICIES = (BudgetPolicy("ident", 12, 250),)
CHUNK = 120
WETH = "0x4200000000000000000000000000000000000006"
USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
# Factory -> (kind, venue). Slipstream generations share one ABI.
FACTORIES = {
    UNIV3_FACTORY: ("V3", "uniswap_v3"),
    SLIPSTREAM_FACTORY: ("Slipstream", "aerodrome_slipstream"),
    AERO_CL2_FACTORY: ("Slipstream", "aero_cl2"),
    AERO_FACTORY: ("V2", "aerodrome_v2"),
    **{f: ("Slipstream", "slipstream_gen") for f in SLIPSTREAM if f != SLIPSTREAM_FACTORY},
}
PROBES = ("factory()", "token0()", "token1()", "fee()", "tickSpacing()", "stable()")


def sel(signature: str) -> bytes:
    return bytes.fromhex(_selector(signature)[2:])


def word_address(raw: bytes) -> str | None:
    if len(raw) != 32 or int.from_bytes(raw[:12], "big"):
        return None
    return "0x" + raw[12:].hex()


def word_int(raw: bytes, signed: bool = False) -> int | None:
    if len(raw) != 32:
        return None
    return decode(["int256" if signed else "uint256"], raw)[0]


class Lane:
    def __init__(self, root: Path, client: httpx.Client) -> None:
        self.root, self.client, self.count = root, client, 0

    def multicall(self, calls: list[tuple[str, bytes]], pin: dict[str, object]) -> list:
        self.count += 1
        prefix = self.root / f"ident-{self.count:03d}"
        with RpcBudget(self.root / "original.sqlite3", POLICIES, require_existing=True) as b:
            while wait := b.reserve("ident", now_ms=time.time_ns() // 1_000_000):
                time.sleep(wait / 1000 + 0.001)
        data = _encode_aggregate3_calldata([(t, True, d) for t, d in calls])
        body = canonical_json(
            {
                "jsonrpc": "2.0",
                "id": self.count,
                "method": "eth_call",
                "params": [{"to": MULTICALL3, "data": data}, pin],
            }
        )
        prefix.with_suffix(".request").write_bytes(body)
        reply = self.client.post(SOURCE, content=body, headers={"Content-Type": "application/json"})
        prefix.with_suffix(".response").write_bytes(reply.content)
        if reply.status_code != 200:
            raise RuntimeError(f"HTTP refusal {reply.status_code}")
        env = json.loads(reply.content)
        if env.get("id") != self.count or "result" not in env:
            raise RuntimeError(f"RPC refusal {env.get('error')}")
        (rows,) = decode(["(bool,bytes)[]"], bytes.fromhex(env["result"][2:]))
        if len(rows) != len(calls):
            raise RuntimeError("Multicall result count mismatch")
        return rows

    def chunked(self, calls: list[tuple[str, bytes]], pin: dict[str, object]) -> list:
        out: list = []
        for i in range(0, len(calls), CHUNK * 8):
            out.extend(self.multicall(calls[i : i + CHUNK * 8], pin))
        return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("emitters", type=Path)
    args = parser.parse_args()
    root: Path = args.root
    root.mkdir(parents=True)
    with RpcBudget(root / "original.sqlite3", POLICIES):
        pass
    emitters = json.loads(args.emitters.read_bytes())["emitters"]
    (root / "emitters.json").write_bytes(canonical_json(emitters))
    with httpx.Client(timeout=8, trust_env=False, follow_redirects=False) as client:
        lane = Lane(root, client)
        head_body = canonical_json(
            {
                "jsonrpc": "2.0",
                "id": 0,
                "method": "eth_getBlockByNumber",
                "params": ["latest", False],
            }
        )
        with RpcBudget(root / "original.sqlite3", POLICIES, require_existing=True) as b:
            if b.reserve("ident", now_ms=time.time_ns() // 1_000_000):
                raise RuntimeError("budget pacing")
        head_raw = client.post(
            SOURCE, content=head_body, headers={"Content-Type": "application/json"}
        )
        (root / "head.response").write_bytes(head_raw.content)
        head = json.loads(head_raw.content)["result"]
        pin = {"blockHash": head["hash"], "requireCanonical": True}

        # Stage 1: self-reported identity.
        calls = [(e, sel(p)) for e in emitters for p in PROBES]
        rows = lane.chunked(calls, pin)
        claims = {}
        for i, e in enumerate(emitters):
            got = {p: rows[i * len(PROBES) + j] for j, p in enumerate(PROBES)}
            factory = word_address(got["factory()"][1]) if got["factory()"][0] else None
            t0 = word_address(got["token0()"][1]) if got["token0()"][0] else None
            t1 = word_address(got["token1()"][1]) if got["token1()"][0] else None
            claims[e] = {
                "factory": factory,
                "token0": t0,
                "token1": t1,
                "fee": word_int(got["fee()"][1]) if got["fee()"][0] else None,
                "spacing": word_int(got["tickSpacing()"][1], True)
                if got["tickSpacing()"][0]
                else None,
                "stable": word_int(got["stable()"][1]) if got["stable()"][0] else None,
            }

        # Stage 2: approved factory must map the claim back to the emitter; read token facts.
        checks: list[tuple[str, str, bytes]] = []
        for e, c in claims.items():
            f = c["factory"]
            if f not in FACTORIES or not c["token0"] or not c["token1"]:
                continue
            kind = FACTORIES[f][0]
            t0, t1 = c["token0"], c["token1"]
            if kind == "V3" and c["fee"] is not None:
                arg = encode(["address", "address", "uint24"], [t0, t1, c["fee"]])
                checks.append((e, f, sel("getPool(address,address,uint24)") + arg))
            elif kind == "Slipstream" and c["spacing"] is not None:
                arg = encode(["address", "address", "int24"], [t0, t1, c["spacing"]])
                checks.append((e, f, sel("getPool(address,address,int24)") + arg))
            elif kind == "V2" and c["stable"] in (0, 1):
                arg = encode(["address", "address", "bool"], [t0, t1, bool(c["stable"])])
                checks.append((e, f, sel("getPool(address,address,bool)") + arg))
            else:
                continue
            for token in (t0, t1):
                checks.append((e, token, sel("decimals()")))
                checks.append((e, token, sel("balanceOf(address)") + encode(["address"], [e])))
        rows2 = lane.chunked([(t, d) for _, t, d in checks], pin)
    verdicts = {}
    i = 0
    while i < len(checks):
        e = checks[i][0]
        group = rows2[i : i + 5]
        c = claims[e]
        mapped = word_address(group[0][1]) if group[0][0] else None
        dec = [
            word_int(group[1][1]) if group[1][0] else None,
            word_int(group[3][1]) if group[3][0] else None,
        ]
        bal = [
            word_int(group[2][1]) if group[2][0] else None,
            word_int(group[4][1]) if group[4][0] else None,
        ]
        kind, venue = FACTORIES[c["factory"]]
        ok = mapped == e and all(d is not None and d <= 36 for d in dec) and all(b for b in bal)
        verdicts[e] = {
            **c,
            "kind": kind,
            "venue": venue,
            "factory_maps_back": mapped == e,
            "decimals": dec,
            "balances": [str(b) if b is not None else None for b in bal],
            "anchor_pair": WETH in (c["token0"], c["token1"]) or USDC in (c["token0"], c["token1"]),
            "verified": ok,
        }
        i += 5
    unknown = {e: c for e, c in claims.items() if e not in verdicts}
    with RpcBudget(root / "original.sqlite3", POLICIES, read_only=True) as b:
        counts = b.counts()
    summary = {
        "schema": "oasis.base-pool-identity.v1",
        "source": SOURCE,
        "block": {"number": int(head["number"], 16), "hash": head["hash"]},
        "emitters": len(emitters),
        "verified": sorted(e for e, v in verdicts.items() if v["verified"]),
        "verdicts": verdicts,
        "rejected_unknown_factory_or_shape": {e: c["factory"] for e, c in unknown.items()},
        "ledger": counts,
        "driver_sha256": sha256_hex(Path(__file__).read_bytes()),
    }
    (root / "summary.json").write_bytes(canonical_json(summary))
    print(json.dumps({k: summary[k] for k in ("block", "emitters", "ledger")}))
    print("verified", len(summary["verified"]), "unknown/other", len(unknown))


if __name__ == "__main__":
    main()
