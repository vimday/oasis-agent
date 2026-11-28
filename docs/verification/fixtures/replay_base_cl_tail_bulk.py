"""Reprice the original Base TS1 gaps at canonical historical hashes."""

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from threading import Event

import httpx
import replay_base_mixed_whole as mixed
from measure_base_batched_quote import DISCOVERY_GAS, ENDPOINTS, PAIRS, POOLS, Lane, envelope, pin
from run_base_cl_coverage import PROBE, SOURCE, WIRE, leg, query, retained

from oasis_agent.arbitrage.cycle_protocols import abi, calldata, data_bytes
from oasis_agent.models.common import sha256_hex
from oasis_agent.rpc.budget import BudgetPolicy, RpcBudget

DERIVED = "BaseCLTailExactBatch"
REGULAR_GAS = 440_000
THIN_GAS = 8_000_000
OUTER_GAS = 32_000_000
PILOT = "sim2"
BULK = "state"
INPUTS = ("uint256", "uint256", "uint256")
HISTORIC = mixed.DIR.parent / "base-cl-coverage-bootstrap-20261128"
WETH, USDC = mixed.WETH, mixed.USDC


def source():
    text = SOURCE.read_text()
    start = text.index("    function quote(")
    stop = text.index("    function leg(", start)
    replacement = f"""    function quoteTail(uint256 amount, uint256 unsignedSize, uint256 mask)
        external
        returns (Batch memory result)
    {{
        if (
            block.chainid != 8453 || address(this) != PROBE || amount == 0 || amount > 1 ether
                || unsignedSize > 512
                || mask == 0 || mask >= 1 << 30
        ) revert InvalidObservation();
        (result.other, result.pools) = market(0);
        result.number = block.number;
        result.timestampMs = CLOCK.timestampMs();
        result.l1Wei = FEES.getL1FeeUpperBound(unsignedSize);
        result.operatorWei = FEES.getOperatorFee(550_000);
        result.codeHashes = new bytes32[](6);
        result.routes = new Route[](30);
        uint256 next;
        for (uint256 i = 0; i < 6; ++i) {{
            address left = result.pools[i];
            result.codeHashes[i] = left.codehash;
            Leg memory first;
            if ((mask >> next) & 31 != 0) first = leg(left, true, amount, 0, quoteGas(i));
            for (uint256 j = 0; j < 6; ++j) {{
                if (i == j) continue;
                if ((mask >> next) & 1 != 0) {{
                    Leg memory second;
                    if (first.status == 1) {{
                        second = leg(result.pools[j], false, first.output, 0, quoteGas(j));
                    }}
                    result.routes[next] = Route(first, second);
                }}
                ++next;
            }}
        }}
    }}

    function quoteGas(uint256 index) private pure returns (uint256) {{
        return index == 3 || index == 4 ? {THIN_GAS} : {REGULAR_GAS};
    }}

"""
    return (text[:start] + replacement + text[stop:]).replace(
        "contract BaseConcentratedReadOnlyProbe", f"contract {DERIVED}", 1
    )


def pilot_heights(history):
    return (63345200, 63345203, next(h for h, (_, lot, _) in history.items() if lot == 10**18))


def runtime(root):
    return "0x" + (root / f"{DERIVED}.bin-runtime").read_text().strip()


def mask_for(old):
    missing = [i for i, amounts in enumerate(old[6]) if not all(amounts)]
    control = next(i for i, amounts in enumerate(old[6]) if all(amounts))
    return sum(1 << i for i in [*missing, control]), control


def plan(root, history, provider, stamp):
    pilot = pilot_heights(history)
    heights = pilot if provider == PILOT else tuple(h for h in history if h not in pilot)
    original = root / f"{DERIVED}.sol"
    assert original.read_text() == source()
    return {
        "schema": "oasis.base-cl-tail-full-amount.v1",
        "utc": stamp,
        "endpoint": ENDPOINTS[provider],
        "provider": provider,
        "price_limit": "protocol",
        "request_cap": len(heights) + 2,
        "min_interval_ms": 250,
        "retry": False,
        "usd": 0,
        "outer_gas": OUTER_GAS,
        "regular_gas": REGULAR_GAS,
        "thin_gas": THIN_GAS,
        "input_report_sha256": mixed.digest(HISTORIC / "stdout.json"),
        "driver_sha256": mixed.digest(Path(__file__)),
        "parent_solidity_sha256": mixed.digest(SOURCE),
        "solidity_sha256": mixed.digest(original),
        "runtime_sha256": sha256_hex(data_bytes(runtime(root))),
        "compiler": (root / "solc-version.txt").read_text().strip(),
        "compiler_args": [
            "--via-ir",
            "--optimize",
            "--optimize-runs",
            "200",
            "--evm-version",
            "cancun",
        ],
        "pins": [
            {
                "height": h,
                "hash": history[h][0].block_hash,
                "principal_wei": history[h][1],
                "mask": mask_for(history[h][2])[0],
            }
            for h in heights
        ],
    }


def observe(lane, header, principal, old, code):
    mask, control = mask_for(old)
    sized = mixed.unsigned_size(envelope(principal))
    payload = calldata("quoteTail(uint256,uint256,uint256)", INPUTS, (principal, sized, mask))
    raw, proof = query(lane, "full-range", header, code, payload, OUTER_GAS)
    (result,) = abi((WIRE,), raw)
    number, timestamp, l1, operator, token, pools, hashes, quote_rows = result
    if (number, timestamp, token, pools, hashes, l1, operator) != (
        header.number,
        header.timestamp_ms,
        USDC,
        POOLS,
        old[5],
        old[3][0],
        old[2],
    ):
        raise ValueError("hash-pinned fee, time, or catalog witness differs")
    gas_price = min(mixed.CAP, header.base_fee + mixed.TIP)
    selected = []
    for index, ((i, j), legs) in enumerate(zip(PAIRS, quote_rows, strict=True)):
        left, right = map(leg, legs)
        if not mask & (1 << index):
            if left["status"] or right["status"] or left["spent_gas"] or right["spent_gas"]:
                raise ValueError("unselected route returned a witness")
            continue
        if left["status"] == 0 or (left["status"] == 1) != (right["status"] != 0):
            raise ValueError("incoherent dependent route")
        for witness, required in ((left, principal), (right, left["output"])):
            full = witness["consumed"] == required and witness["output"] > 0
            if witness["consumed"] > required or full != (witness["status"] == 1):
                raise ValueError("incoherent input consumption")
        exact = left["status"] == right["status"] == 1
        if index == control and (not exact or (left["output"], right["output"]) != old[6][index]):
            raise ValueError("exact control conflicts with original Quoter")
        gross = right["output"] - principal if exact else None
        selected.append(
            {
                "pool_indices": [i, j],
                "left": left,
                "right": right,
                "old_control": index == control,
                "gross_wei": gross,
                "net_model_wei": gross - gas_price * DISCOVERY_GAS - l1 - operator
                if exact
                else None,
            }
        )
    return {
        "height": number,
        "hash": header.block_hash,
        "principal_wei": principal,
        "l1_wei": l1,
        "operator_wei": operator,
        "gas_price_wei": gas_price,
        "proof": proof,
        "routes": selected,
    }


def execute(root, scope, history, capture):
    provider = scope["provider"]
    policy = (BudgetPolicy(provider, scope["request_cap"], scope["min_interval_ms"]),)
    if capture:
        with RpcBudget(root / "original.sqlite3", policy):
            pass
    rows = []
    fault = None
    try:
        with httpx.Client(timeout=12, trust_env=False, follow_redirects=False) as client:
            lane = Lane(root, provider, policy, Event(), client if capture else None)
            if lane.rpc("chain", "eth_chainId", [])[0] != "0x2105":
                raise ValueError("wrong chain")
            first_header = history[scope["pins"][0]["height"]][0]
            override, proof = lane.rpc("override-empty", "eth_getCode", [PROBE, pin(first_header)])
            if data_bytes(override):
                raise ValueError("quote-only override address contains deployed code")
            for p in scope["pins"]:
                header, principal, original = history[p["height"]]
                rows.append(observe(lane, header, principal, original, runtime(root)))
    except (AssertionError, ValueError, TypeError, KeyError, OSError, httpx.HTTPError) as exc:
        fault = str(exc) or "invalid call witness"
    stop = {"fault": fault}
    if capture:
        mixed.write(root / "STOP.json", stop)
    else:
        assert mixed.read(root / "STOP.json") == stop
    with RpcBudget(root / "original.sqlite3", policy, read_only=True) as budget:
        count = budget.counts()
    missing = [q for row in rows for q in row["routes"] if not q["old_control"]]
    completed = [row for row in missing if row["gross_wei"] is not None]
    return {
        "rows": rows,
        "requests": count,
        "coverage": {
            "touched": len(missing),
            "exact": len(completed),
            "controls": len(rows),
            "gross_positive": sum(row["gross_wei"] > 0 for row in completed),
            "net_model_positive": sum(row["net_model_wei"] > 0 for row in completed),
            "best_gross_wei": max((row["gross_wei"] for row in completed), default=None),
            "best_net_model_wei": max((row["net_model_wei"] for row in completed), default=None),
        },
        "scope_sha256": mixed.digest(root / "scope.json"),
        "override_proof": proof if rows else None,
        **stop,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("source", "prepare", "capture", "replay"))
    parser.add_argument("root", type=Path)
    parser.add_argument("--provider", choices=(PILOT, BULK))
    args = parser.parse_args()
    if args.mode == "source":
        (args.root / f"{DERIVED}.sol").write_text(source())
        return
    history = retained()
    path = args.root / "scope.json"
    if args.mode == "prepare":
        assert args.provider
        mixed.write(path, plan(args.root, history, args.provider, datetime.now(UTC).isoformat()))
        return
    scope = mixed.read(path)
    assert scope == plan(args.root, history, scope["provider"], scope["utc"])
    assert scope["driver_sha256"] == mixed.digest(args.root / "capture.py")
    print(
        json.dumps(
            execute(args.root, scope, history, args.mode == "capture"), sort_keys=True, indent=2
        )
    )


if __name__ == "__main__":
    main()
