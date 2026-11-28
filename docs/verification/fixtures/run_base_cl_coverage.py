"""Diagnose original Base quote gaps and reprice the same states with amount-consumption checks."""

import argparse
import json
from datetime import UTC, datetime
from itertools import permutations
from pathlib import Path
from threading import Event

import httpx
import replay_base_mixed_whole as mixed
from measure_base_batched_quote import DISCOVERY_GAS, POOLS, Lane, envelope, pin
from run_base_fixed_hkc import market

from oasis_agent.arbitrage.cycle_protocols import abi, calldata, data_bytes, topic
from oasis_agent.evidence.base_newheads import replay_head_observation
from oasis_agent.models.common import sha256_hex
from oasis_agent.rpc.budget import BudgetPolicy, RpcBudget

PROBE = "0x" + "00" * 18 + "a259"
ARTIFACT = "BaseConcentratedReadOnlyProbe.bin-runtime"
SOURCE = Path(__file__).with_name("BaseConcentratedReadOnlyProbe.sol")
PAST = mixed.DIR.parent / "base-combined-lot-natural-20261128"
PAST_REPORT = Path("docs/verification/BASE_BATCHED_NATURAL_20261128.json")
LEG = "(uint8,uint256,uint256,uint256,bytes4,uint256)"
WIRE = f"(uint256,uint256,uint256,uint256,address,address[],bytes32[],({LEG},{LEG})[])"
BAND = 500
MARKETS = (
    (mixed.USDC, POOLS),
    (
        "0xcbb7c0000ab88b473b1f5afd9ef808440eed33bf",
        (
            "0x7aea2e8a3843516afa07293a10ac8e49906dabd1",
            "0x8c7080564b5a792a33ef2fd473fba6364d5495e5",
            "0x22aee3699b6a0fed71490c103bd4e5f3309891d5",
            "0x70acdf2ad0bf2402c957154f944c19ef4e1cbae1",
        ),
    ),
    (
        "0x940181a94a35a4569e4529a3cdfb74e38fd98631",
        (
            "0x3d5d143381916280ff91407febeb52f2b60f33cf",
            "0x0d5959a52e7004b601f0be70618d01ac3cdce976",
            "0x82321f3beb69f503380d6b233857d5c43562e2d0",
        ),
    ),
)
DIAGNOSTICS = (
    (63345200, 4, 0, 400_000),
    (63345200, 4, 0, 2_000_000),
    (63345200, 4, BAND, 440_000),
    (63345203, 3, 0, 400_000),
    (63345203, 3, 0, 2_000_000),
    (63345203, 3, BAND, 440_000),
)
POLICIES = (BudgetPolicy("state", 76, 250), BudgetPolicy("sim2", 3, 250))


def artifact(root):
    return "0x" + (root / ARTIFACT).read_text().strip()


def query(lane, label, header, runtime, payload, gas, earliest=1):
    raw, proof = lane.rpc(
        f"{header.number}-{label}",
        "eth_call",
        [{"to": PROBE, "data": payload, "gas": hex(gas)}, pin(header), {PROBE: {"code": runtime}}],
        earliest,
    )
    return data_bytes(raw), proof


def leg(row):
    status, consumed, output, spent_gas, selector, size = row
    if status not in (0, 1, 2, 3, 4):
        raise ValueError("unknown leg status")
    if status in (1, 2) and (
        size != 68 or "0x" + selector.hex() != topic("Outcome(int256,int256)")[:10]
    ):
        raise ValueError("invalid callback witness")
    if status in (0, 3, 4) and (consumed or output):
        raise ValueError("failed leg has a settlement")
    return {
        "status": status,
        "consumed": consumed,
        "output": output,
        "spent_gas": spent_gas,
        "error_selector": "0x" + selector.hex(),
        "reason_size": size,
    }


def observe(lane, header, runtime, cohort, amount, earliest=1):
    payload = calldata(
        "quote(uint8,uint256,uint256,uint16)",
        ("uint8", "uint256", "uint256", "uint16"),
        (cohort, amount, mixed.unsigned_size(envelope(amount)), BAND),
    )
    raw, proof = query(lane, "bounded", header, runtime, payload, 18_000_000, earliest)
    (snapshot,) = abi((WIRE,), raw)
    number, timestamp, l1, operator, token, pools, hashes, raw_routes = snapshot
    if ((number, timestamp), (token, pools)) != (
        (header.number, header.timestamp_ms),
        MARKETS[cohort],
    ):
        raise ValueError("bounded quote state or pool identity differs")
    routes = []
    for pair, (a, b) in zip(permutations(pools, 2), raw_routes, strict=True):
        left, right = leg(a), leg(b)
        if left["consumed"] > amount or right["consumed"] > left["output"]:
            raise ValueError("quote over-consumed principal")
        if (left["status"] == 1) != (right["status"] != 0):
            raise ValueError("incoherent dependent quote")
        if (left["status"] == 1 and left["consumed"] != amount) or (
            right["status"] == 1 and right["consumed"] != left["output"]
        ):
            raise ValueError("full quote omitted input")
        routes.append({"pools": list(pair), "left": left, "right": right})
    return {
        "token": token,
        "pool_code": ["0x" + data.hex() for data in hashes],
        "l1_wei": l1,
        "operator_wei": operator,
        "proof": proof,
        "routes": routes,
    }


def diagnose(lane, header, runtime, pool, amount, band, gas):
    payload = calldata(
        "diagnose(uint8,bool,uint256,uint16,uint256)",
        ("uint8", "bool", "uint256", "uint16", "uint256"),
        (pool, True, amount, band, gas),
    )
    raw, proof = query(lane, f"diag-{pool}-{band}-{gas}", header, runtime, payload, 3_500_000)
    (payload,) = abi((LEG,), raw)
    return leg(payload) | {"band": band, "gas_limit": gas, "proof": proof}


def retained():
    report = mixed.read(PAST_REPORT)
    actual = mixed.read(PAST / "stdout.json")
    assert report == actual and mixed.digest(PAST / "scope.json") == report["scope_sha256"]
    result = {}
    bodies = {int(p.name.split("-")[2]): p for p in PAST.glob("state-*-batched.response")}
    for i, captured in enumerate(report["panels"], 1):
        panel = PAST / f"window-{i}"
        heads = {n["head"]["height"]: n for n in mixed.read(panel / "heads.json")}
        for row in captured["rows"]:
            h = row["h"]
            obs = replay_head_observation(panel / "heads", heads[h])
            header = market(panel, obs.head, obs)
            raw = bodies[h]
            assert row["h_hash"] == header.block_hash
            assert row["quote_proof"]["response_sha256"] == mixed.digest(raw)
            assert row["quote_proof"]["request_sha256"] == mixed.digest(raw.with_suffix(".request"))
            (decoded,) = abi(
                ("(uint256,uint256,uint256,uint256[],address[6],bytes32[6],(uint256,uint256)[])",),
                data_bytes(mixed.read(raw)["result"]),
            )
            assert decoded[0] == h and tuple(decoded[4]) == POOLS
            assert sum(bool(a and b) for a, b in decoded[6]) == row["completed_quotes"]
            result[h] = (header, row["principal_wei"], decoded)
    assert (
        len(result) == 68
        and sum(not (a and b) for _, _, q in result.values() for a, b in q[6]) == 936
    )
    return result


def plan(root, stamp, limited):
    return {
        "schema": "oasis.base-cl-coverage.v1",
        "frozen_utc": stamp,
        "runtime_sha256": sha256_hex(data_bytes(artifact(root))),
        "source_sha256": mixed.digest(SOURCE),
        "driver_sha256": mixed.digest(Path(__file__)),
        "prior_sha256": mixed.digest(PAST_REPORT),
        "band": BAND,
        "control": "require exact prior outputs whenever the bounded callback consumes all input",
        "recomputed_heads": 0 if limited else 68,
        "diagnostics": [list(row) for row in DIAGNOSTICS],
        "providers": ["state", "sim2"],
        "usd": 0,
    }


def classify(snapshot, original, header, amount):
    assert snapshot["pool_code"] == ["0x" + digest.hex() for digest in original[5]]
    assert snapshot["l1_wei"] == original[3][0] and snapshot["operator_wei"] == original[2]
    best_net = None
    control_pairs, observed = 0, []
    for route, old in zip(snapshot["routes"], original[6], strict=True):
        left, right = route["left"], route["right"]
        full = left["status"] == right["status"] == 1
        if old[0] and old[1] and full:
            if (left["output"], right["output"]) != old:
                raise ValueError("full bounded quote conflicts with archived Quoter outputs")
            control_pairs += 1
        if full:
            gas_price = min(mixed.CAP, header.base_fee + mixed.TIP)
            net = right["output"] - amount - gas_price * DISCOVERY_GAS
            net -= snapshot["l1_wei"] + snapshot["operator_wei"]
            best_net = net if best_net is None else max(net, best_net)
        if not (old[0] and old[1]):
            observed.append(route | {"exact_recovered": full})
    return {
        "height": header.number,
        "hash": header.block_hash,
        "old_confirmed": control_pairs,
        "principal_wei": amount,
        "best_net_wei": best_net,
        "missing": observed,
        "proof": snapshot["proof"],
    }


def collect(root, scope, capture):
    history = retained()
    runtime = artifact(root)
    if capture:
        with RpcBudget(root / "original.sqlite3", POLICIES):
            pass
    report = {"diagnostics": [], "rows": []}
    fault = None
    try:
        with httpx.Client(timeout=6, trust_env=False, follow_redirects=False) as client:
            stop = Event()
            state = Lane(root, "state", POLICIES, stop, client if capture else None)
            if state.rpc("chain", "eth_chainId", [])[0] != "0x2105":
                raise ValueError("wrong chain")
            first = next(iter(history.values()))[0]
            code, proof = state.rpc("override-empty", "eth_getCode", [PROBE, pin(first)])
            assert not data_bytes(code)
            report["override_proof"] = proof
            for h, pool, band, gas in DIAGNOSTICS:
                header, amount, _old = history[h]
                row = diagnose(state, header, runtime, pool, amount, band, gas)
                report["diagnostics"].append(
                    row | {"height": h, "pool": POOLS[pool], "amount": amount}
                )
            if scope["recomputed_heads"]:
                witness = Lane(root, "sim2", POLICIES, stop, client if capture else None)
                if witness.rpc("chain", "eth_chainId", [])[0] != "0x2105":
                    raise ValueError("wrong witness chain")
                for h, (header, amount, old) in history.items():
                    current = observe(state, header, runtime, 0, amount)
                    row = classify(current, old, header, amount)
                    if h in (63345200, 63345203):
                        other = observe(witness, header, runtime, 0, amount)
                        decoded = [entry | {"proof": None} for entry in (current, other)]
                        if decoded[0] != decoded[1]:
                            raise ValueError("independent bounded reads disagree")
                        row["second_source"] = other["proof"]
                    report["rows"].append(row)
    except (AssertionError, ValueError, TypeError, KeyError, OSError, httpx.HTTPError) as exc:
        fault = str(exc) or "assertion failed"
    stop = {"fault": fault}
    if capture:
        mixed.write(root / "STOP.json", stop)
    else:
        assert mixed.read(root / "STOP.json") == stop
    with RpcBudget(root / "original.sqlite3", POLICIES, read_only=True) as ledger:
        report["requests"] = ledger.counts()
    return report | {"scope_sha256": mixed.digest(root / "scope.json"), **stop}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("prepare", "capture", "replay"))
    parser.add_argument("root", type=Path)
    parser.add_argument("--diagnostic-only", action="store_true")
    args = parser.parse_args()
    if args.mode == "prepare":
        mixed.write(
            args.root / "scope.json",
            plan(args.root, datetime.now(UTC).isoformat(), args.diagnostic_only),
        )
        return
    active = mixed.read(args.root / "scope.json")
    assert active == plan(args.root, active["frozen_utc"], not active["recomputed_heads"])
    result = collect(args.root, active, args.mode == "capture")
    print(json.dumps(result, indent=2, sort_keys=True))
    raise SystemExit(2 if result["fault"] else 0)


if __name__ == "__main__":
    main()
