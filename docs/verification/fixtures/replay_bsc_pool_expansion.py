"""Recompute the frozen BSC baseline/additions, exact limited quotes, and receipt overlap."""

import argparse
import builtins
import json
import sys
from collections import Counter, defaultdict
from contextlib import redirect_stdout
from dataclasses import dataclass
from functools import lru_cache
from hashlib import sha256
from importlib.util import module_from_spec, spec_from_file_location
from io import StringIO
from math import prod
from pathlib import Path
from types import SimpleNamespace

from oasis_agent.arbitrage.cycle_protocols import abi, calldata, data_bytes, topic
from oasis_agent.evidence.nitro_block import indexed_trie_root
from oasis_agent.evidence.public_receipts import quantity
from oasis_agent.fastpath.slipstream_depth import _partial_price
from oasis_agent.rpc.budget import RpcBudget
from oasis_agent.uniswap_v4.liquidity_math import (
    MAX_TICK,
    MIN_TICK,
    Q96,
    amount0_delta,
    amount1_delta,
    sqrt_price_at_tick,
)
from scripts.replay_weth_principal import _receipt_leaf

WBNB = "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c"
STATE_VIEW = "0xd13dd3d6e93f276fafc9db9e6bb47c1180aee0c4"
ZERO = "0x" + "00" * 20
TAX_TOKENS = {
    "0xad6d2d8fcb2c521ae92c4b2adda00ffafea47777",
    "0xfb5b838b6cfeedc2873ab27866079ac55363d37e",
}
GRID = (10**14, 10**15, 10**16, 5 * 10**16, 10**17, 25 * 10**16)
RESERVES = ("uint112", "uint112", "uint32")
SLOT_V3 = ("uint160", "int24", "uint16", "uint16", "uint16", "uint32", "bool")
SLOT_MANAGER = ("uint160", "int24", "uint24", "uint24")
LIMIT = 1_000_000
SwapPath = tuple[tuple[int, bool], ...]

# The original capture and prior forensic decoders needed for this one replay.
# Validate the whole pinned tree before evaluating any script.
EVIDENCE_SCRIPTS = {
    "bsc-pool-expansion-20261127/identity/run.py": (
        "418772f081c2f0b912d5ce3dee81bc11f03b1e66a379d0b44343b1a76a9b6c5a"
    ),
    "bsc-pool-expansion-20261127/forward/run.py": (
        "572693f39bfaf3482dfc7b370683a18df6762b87996b5fdd817dc4f437d0f06f"
    ),
    "bsc-pool-expansion-20261127/landed/replay.py": (
        "8be51be4020db38e6851f9619bc132b1a01e4df1e50cfd4d54ed72ba78187495"
    ),
    "bsc-pool-expansion-20261127/whole/run.py": (
        "60ebea88618bf8e4b93ef10c282fb9ba4e5f6fa053649e549b3090f671f5bede"
    ),
    "bsc-wide-forward-20261123/forward.py": (
        "4a74613fdf1d3e41dbcaffc9740f6a1f0fb456b275313a55eec020f2e9345b18"
    ),
    "bsc-recent-arbitrage-20261123/collect.py": (
        "76e6c2629d83568e7bedb61dc1df6db6eee593fa2f8c8f78fd57ed201d3bd17b"
    ),
    "bsc-eight-address-holdout-20261124/capture.py": (
        "2f9a9130f1dd40a0bea70a049c49560a6001331c5ed714a1c70e48037eb0c570"
    ),
    "bsc-eight-address-holdout-20261124/replay.py": (
        "a49b25c9a462a11ef67f19028834f00e1947ce486bb129eef66451cae776464a"
    ),
    "eth-cohort-cost-outlier-20261123/verify.py": (
        "b9e563da64fb5b570f35cac81313cef56051de6a3321009b4ad6f831bd9ee4e8"
    ),
}
_SCRIPT_LIMIT = 1_048_576


class _PinnedModuleLoader:
    def __init__(self, registry, path):
        self.registry = registry
        self.path = path

    def create_module(self, _spec):
        return None

    def exec_module(self, module):
        self.registry._evaluate(self.path, module.__dict__)


class PinnedEvidenceScripts:
    """Evaluate only verified source bytes, including transitive retained-script loads."""

    def __init__(self, root, required):
        self.sources = {}
        root = root.resolve()
        for relative, expected in required.items():
            path = root / relative
            if not path.is_relative_to(root) or path.resolve() != path:
                raise ValueError(f"evidence script escapes its registered path: {relative}")
            try:
                with path.open("rb") as stream:
                    source = stream.read(_SCRIPT_LIMIT + 1)
            except OSError as exc:
                raise ValueError(f"required evidence script unavailable: {relative}") from exc
            if len(source) > _SCRIPT_LIMIT or sha256(source).hexdigest() != expected:
                raise ValueError(f"evidence script SHA-256 differs: {relative}")
            self.sources[path] = source
        self.runpy = SimpleNamespace(run_path=self.run_path)
        self.util = SimpleNamespace(
            module_from_spec=module_from_spec, spec_from_file_location=self._spec
        )
        self.importlib = SimpleNamespace(util=self.util)

    def _import(self, name, globals_=None, locals_=None, fromlist=(), level=0):
        if level == 0 and name == "runpy":
            return self.runpy
        if level == 0 and name == "importlib.util":
            return self.util if fromlist else self.importlib
        return builtins.__import__(name, globals_, locals_, fromlist, level)

    def _registered(self, path):
        absolute = Path(path).resolve()
        if absolute not in self.sources:
            raise ValueError(f"unregistered evidence script: {path}")
        return absolute

    def _spec(self, name, path):
        path = self._registered(path)
        return spec_from_file_location(name, path, loader=_PinnedModuleLoader(self, path))

    def _evaluate(self, path, namespace):
        namespace["__builtins__"] = {**vars(builtins), "__import__": self._import}
        exec(compile(self.sources[path], str(path), "exec", dont_inherit=True), namespace)

    def run_path(self, path):
        path = self._registered(path)
        namespace = {"__name__": "<run_path>", "__file__": str(path)}
        self._evaluate(path, namespace)
        return namespace


def read(path):
    return json.loads(path.read_bytes())


def digest(path):
    return sha256(path.read_bytes()).hexdigest()


def base_pools(old):
    return [
        {
            **p,
            "pool_id": p["pool"],
            "contract": p["pool"],
            "kind": "v2",
            "tokens": (p["token0"], p["token1"]),
        }
        for p in old
    ]


def request_specs(pools):
    queries = []
    for i, p in enumerate(pools):
        address = p["contract"]
        kind = p["kind"]
        if kind == "v2":
            queries.append((address, calldata("getReserves()"), i, "reserves", RESERVES))
        elif kind == "pancake_v3":
            queries.append((address, calldata("slot0()"), i, "slot", SLOT_V3))
            queries.append((address, calldata("liquidity()"), i, "liquidity", ("uint128",)))
        else:
            if p["hooks"] != ZERO:
                raise ValueError("hooked pool is outside pricing scope")
            address = STATE_VIEW if kind == "v4" else address
            for s, f, t in (
                ("getSlot0", "slot", SLOT_MANAGER),
                ("getLiquidity", "liquidity", ("uint128",)),
            ):
                query = calldata(f"{s}(bytes32)", ("bytes32",), (data_bytes(p["pool_id"]),))
                queries.append((address, query, i, f, t))
    return queries


def decoded_state(specs, values):
    state = defaultdict(dict)
    for (_, _, index, label, types), raw in zip(specs, values, strict=True):
        state[index][label] = abi(types, raw)
    return state


def enumerate_paths(pools, max_hops=4):
    edges = defaultdict(list)
    for i, pool in enumerate(pools):
        a, b = pool["tokens"]
        edges[a].append((i, True, b))
        edges[b].append((i, False, a))

    def visit(assets, legs):
        for i, direction, other in edges[assets[-1]]:
            if any(j == i for j, _ in legs):
                continue
            steps = (*legs, (i, direction))
            if other == WBNB and len(steps) > 1:
                yield steps
            elif other not in assets and len(steps) < max_hops:
                yield from visit((*assets, other), steps)

    return tuple(visit((WBNB,), ()))


@lru_cache(maxsize=4096)
def tick_price(tick):
    return sqrt_price_at_tick(tick)


@dataclass(frozen=True)
class Leg:
    rate: tuple[int, int]
    reserves: tuple[int, int] | None = None
    cl: tuple[int, int, int, int] | None = None

    def quote(self, amount, direction):
        if self.reserves is not None:
            r_in, _ = self.reserves
            a, b = self.rate
            # The multiplier is encoded with reserves to keep the same ordered floors.
            return a * amount // (b + amount * self.reserves[1]) if r_in else None
        if self.cl is None:
            return None
        price, liquidity, target, fee = self.cl
        if not liquidity:
            return None
        usable = amount * (LIMIT - fee) // LIMIT
        delta_in, delta_out = (
            (amount0_delta, amount1_delta) if direction else (amount1_delta, amount0_delta)
        )
        # An initialized step boundary may change liquidity and integer rounding.
        if usable >= delta_in(price, target, liquidity, round_up=True):
            return None
        next_price = _partial_price(price, liquidity, usable, direction)
        if next_price == target:
            return None
        return delta_out(price, next_price, liquidity, round_up=False)


def prepare(pools, states):
    legs = {}
    ids = {pool["pool_id"]: i for i, pool in enumerate(pools)}
    if len(ids) != len(pools):
        raise ValueError("duplicate pool identity")
    for i, pool in enumerate(pools):
        p = states[i]
        if pool["kind"] == "v2":
            a, b, _ = p["reserves"]
            m, d = pool["fee_multiplier"], pool["fee_denominator"]
            legs[i, True] = Leg((m * b, d * a), reserves=(a, m))
            legs[i, False] = Leg((m * a, d * b), reserves=(b, m))
            continue
        price, tick, *_ = p["slot"]
        (liq,) = p["liquidity"]
        if pool["kind"] == "pancake_v3":
            spacing, lp, directional = pool["tickSpacing"], pool["fee"], 0
        else:
            if pool["kind"] == "infinity_cl":
                parameters = int(pool["key"][5], 16)
                if parameters & (2**16 - 1):
                    raise ValueError("unexpected Infinity hook bitmap")
                spacing = (parameters >> 16) & (2**24 - 1)
            else:
                spacing = pool["key"][3]
            directional, lp = p["slot"][2:]
        if not (0 < spacing <= 32767 and MIN_TICK <= tick < MAX_TICK):
            raise ValueError("invalid concentrated tick state")
        if not (tick_price(tick) <= price <= tick_price(tick + 1)):
            raise ValueError("concentrated price inconsistent with tick")
        low = (tick // spacing) * spacing
        for forward, target_tick in (
            (True, max(MIN_TICK, low)),
            (False, min(MAX_TICK, low + spacing)),
        ):
            protocol = directional & 4095 if forward else directional >> 12
            max_protocol = 4000 if pool["kind"] == "infinity_cl" else 1000
            fee = protocol + lp - protocol * lp // LIMIT
            if not (0 <= protocol <= max_protocol and 0 <= fee < LIMIT):
                raise ValueError("unsupported concentrated fee")
            squared = price * price
            a, b = (squared, Q96**2) if forward else (Q96**2, squared)
            legs[i, forward] = Leg(
                (a * (LIMIT - fee), b * LIMIT), cl=(price, liq, tick_price(target_tick), fee)
            )
    return legs


def path_quote(path, prepared, amount):
    amounts = [amount]
    for index, direction in path:
        output = prepared[index, direction].quote(amounts[-1], direction)
        if output is None or output <= 0:
            return {"amounts": amounts, "unknown_at_pool_index": index}
        amounts.append(output)
    return {"amounts": amounts, "gross": amounts[-1] - amounts[0]}


def score(pools, paths, legs, gas_price, *, new_from):
    counts = Counter()
    best = None
    safest_new = None
    for path in paths:
        seen_new = any(i >= new_from for i, _ in path)
        counts["topological_paths"] += 1
        counts["paths_using_additions"] += seen_new
        pool_legs = tuple(legs[key] for key in path)
        if any((leg.cl is not None and not leg.cl[1]) or 0 in leg.rate for leg in pool_legs):
            counts["unavailable_paths"] += 1
            continue
        rates = tuple(leg.rate for leg in pool_legs)
        if prod(x for x, _ in rates) <= prod(y for _, y in rates):
            continue
        counts["marginal_positive_paths"] += 1
        addresses = [pools[i]["pool_id"] for i, _ in path]
        currencies = [WBNB, *(pools[i]["tokens"][forward] for i, forward in path)]
        has_tax = bool(TAX_TOKENS.intersection(currencies))
        for amount in GRID:
            quoted = path_quote(path, legs, amount)
            if "gross" not in quoted:
                counts["grid_residual_or_dust"] += 1
                continue
            counts["modelled_grid"] += 1
            gross = quoted["gross"]
            counts["gross_positive"] += gross > 0
            counts["above_21k_at_observed_gas_price"] += gross > 21_000 * gas_price
            counts["above_60k_at_observed_gas_price"] += gross > 60_000 * gas_price
            counts["gross_positive_known_tax_path"] += gross > 0 and has_tax
            point = {**quoted, "pools": addresses, "assets": currencies, "uses_additions": seen_new}
            if best is None or gross > best["gross"]:
                best = point
            if (
                seen_new
                and len(path) == 2
                and not has_tax
                and all(pools[i]["kind"] in {"v2", "pancake_v3"} for i, _ in path)
                and len(set(currencies)) == 2
                and (safest_new is None or gross > safest_new["gross"])
            ):
                safest_new = point
    return {"counts": counts, "best_grid": best, "two_leg_mixed_control_candidate": safest_new}


def original_paths(cases, paid, pools):
    by_id = {p["pool_id"]: i for i, p in enumerate(pools)}
    out = []
    for case, prior in zip(cases, paid, strict=True):
        if case["tx"] != prior["tx"]:
            raise ValueError("original route samples reordered")
        by_log = {leg["i"]: leg for leg in case["legs"]}
        for family in prior["routes"]:
            indices = family["swap_logs"]
            trace = [by_log[i] for i in indices]
            route = []
            for p in trace:
                i = by_id[p["pool_id"]]
                token0, token1 = pools[i]["tokens"]
                direction = p["in"][0] == token0
                if (p["in"][0], p["out"][0]) != (
                    (token0, token1) if direction else (token1, token0)
                ):
                    raise ValueError("historical path contradicts pool asset identity")
                route.append((i, direction))
            out.append(
                {
                    "tx": case["tx"],
                    "swap_logs": indices,
                    "pools": [p["pool_id"] for p in trace],
                    "input": int(family["in_wbnb_wei"]),
                    "historical_output": int(family["out_wbnb_wei"]),
                    "known_tax": any(p["in"][0] in TAX_TOKENS for p in trace),
                    "route": route,
                }
            )
    return out


def receipts_summary(header, receipts, base, extension, topics):
    if not isinstance(receipts, list) or len(receipts) != len(header["transactions"]):
        raise ValueError("holdout receipts count mismatch")
    actual = "0x" + indexed_trie_root([_receipt_leaf(r) for r in receipts]).hex()
    if header["receiptsRoot"] != actual:
        raise ValueError("holdout receipts root mismatch")
    histogram = Counter()
    gas = next_log = 0
    used = []
    for idx, (tx, rc) in enumerate(zip(header["transactions"], receipts, strict=True)):
        expected = (tx, header["hash"], header["number"], idx)
        if (
            rc["transactionHash"],
            rc["blockHash"],
            rc["blockNumber"],
            quantity(rc["transactionIndex"]),
        ) != expected:
            raise ValueError("holdout receipt outside pinned block or receipt order")
        gas += quantity(rc["gasUsed"])
        if gas != quantity(rc["cumulativeGasUsed"]):
            raise ValueError("holdout receipt gas accounting mismatch")
        recognized = shared = added = 0
        for log in rc["logs"]:
            if (
                log["transactionHash"],
                log["blockHash"],
                log["blockNumber"],
                quantity(log["transactionIndex"]),
            ) != expected:
                raise ValueError("holdout log outside pinned receipt")
            if quantity(log["logIndex"]) != next_log or log["removed"] is not False:
                raise ValueError("holdout log sequence or removal mismatch")
            next_log += 1
            if log["topics"] and log["topics"][0] in topics:
                logical = (
                    log["topics"][1] if log["address"] in extension["managers"] else log["address"]
                )
                recognized += 1
                shared += logical in base
                added += logical in extension["pools"]
        histogram[recognized] += 1
        if shared or added:
            used.append(
                {
                    "i": idx,
                    "tx": tx,
                    "known_baseline_events": shared,
                    "added_events": added,
                    "event_shapes": recognized,
                    "actual_gas_price_wei": quantity(rc["effectiveGasPrice"]),
                }
            )
    if gas != quantity(header["gasUsed"]):
        raise ValueError("holdout block gas accounting mismatch")
    return {
        "transaction_counts_by_matching_events": histogram,
        "overlap_transactions": used,
        "receipts_root": actual,
        "all_receipts": len(receipts),
    }


def registered_landed_tax_closure(audit, saved, found, identity):
    """Reconcile the one committed V3-to-V2 transfer deduction in this window."""
    tx = found["target"]["tx"]
    if tx != "0x040cd60bf6604c7248b6276a5be83a11bdc420cda16b77bb077abdd5da6d094f":
        raise ValueError("unregistered tax observation")
    row = found["verified"]
    token = "0x19f46fddbac1ab00d203cc80515a44e320260419"
    source = "0x104d02118fa70c809b68345e3d9aba9fd428593d"
    target = "0x16f06339447daa1b742934e7f44c2cc018f07513"
    holder = row["executor"]
    paths = row["matched_transfer_funding_paths"]
    (before,) = (x for x in paths if x["swaps"] == [794, 783])
    (after,) = (x for x in paths if x["swaps"] == [788, 791])
    sample = next(x for x in saved["scored_heads"] if x["height"] == row["height"])
    (pin,) = (x for x in sample["snapshots"] if x["type"] == "receipts")
    receipt = read(audit / "forward/wire" / f"{pin['sequence']:02}.response")["result"][
        row["index"]
    ]
    trace = read(audit / "landed/040cd60bf660-trace.response.json")["result"]
    logs = {quantity(log["logIndex"]): log for log in receipt["logs"]}
    branch = trace["calls"][36]["calls"][1]["calls"]

    def transfer(i, currency, sender, recipient):
        log = logs[i]
        expected = [
            topic("Transfer(address,address,uint256)"),
            *("0x" + "0" * 24 + x[2:] for x in (sender, recipient)),
        ]
        if log["address"] != currency or log["topics"] != expected:
            raise ValueError("registered transfer roles differ")
        return abi(("uint256",), data_bytes(log["data"]))[0]

    gross = transfer(781, token, source, holder)
    retained = transfer(784, token, holder, token)
    credited = transfer(785, token, holder, target)
    balances = [branch[i] for i in (1, 4, 10)]
    balance_call = calldata("balanceOf(address)", ("address",), (holder,))
    source_call = branch[2]
    source_in = "0x7a848a5a8169aa6a2f603d056a749f924f504444"
    proved_source = next(leg for leg in row["legs"] if leg["i"] == 783)
    proved_target = next(leg for leg in row["legs"] if leg["i"] == 788)
    signed = abi(("int256", "int256"), data_bytes(source_call["output"]))
    recipient_arg, zero_for_one, amount, _, _ = abi(
        ("address", "bool", "int256", "uint160", "bytes"),
        data_bytes(source_call["input"])[4:],
    )
    actual_movement = branch[6]
    post = branch[9]["calls"][1]
    old_reserve, _, _ = abi(RESERVES, data_bytes(branch[7]["output"]))
    sync = logs[787]
    synced_balance, _ = abi(("uint112", "uint112"), data_bytes(sync["data"]))
    if (
        (before["after"], after["before"]) != ([token, gross], [token, credited])
        or before["before"][0] != WBNB
        or after["after"][0] != WBNB
        or gross != retained + credited
        or any((x["from"], x["to"], x["input"]) != (holder, token, balance_call) for x in balances)
        or [abi(("uint256",), data_bytes(x["output"]))[0] for x in balances] != [0, gross, 0]
        or any(x.get("error") is not None for x in (*balances, source_call, actual_movement, post))
        or (source_call["from"], source_call["to"], source_call["input"][:10])
        != (
            holder,
            source,
            calldata(
                "swap(address,bool,int256,uint160,bytes)",
                ("address", "bool", "int256", "uint160", "bytes"),
                (holder, False, 1, 1, b""),
            )[:10],
        )
        or (recipient_arg, zero_for_one, amount) != (holder, False, proved_source["in"][1])
        or proved_source["in"][0] != source_in
        or proved_source["out"] != [token, gross]
        or signed != (-gross, amount)
        or (identity["source_pool"], identity["source_tokens"]) != (source, [token, source_in])
        or (identity["target_pool"], identity["target_tokens"])
        != (target, [token, proved_target["out"][0]])
        or (actual_movement["from"], actual_movement["to"], actual_movement["input"])
        != (
            holder,
            token,
            calldata("transfer(address,uint256)", ("address", "uint256"), (target, gross)),
        )
        or branch[7]["to"] != target
        or branch[7]["input"] != calldata("getReserves()")
        or (post["from"], post["to"], post["input"])
        != (target, token, calldata("balanceOf(address)", ("address",), (target,)))
        or sync["address"] != target
        or sync["topics"] != [topic("Sync(uint112,uint112)")]
        or int(post["output"], 16) != synced_balance
        or synced_balance - old_reserve != credited
    ):
        raise ValueError("registered deduction and target pool reserve evidence do not reconcile")
    (external_native,) = row["direct_third_party_native_payment_candidates"]
    (external_wbnb,) = (
        log
        for log in [logs[796]]
        if log["address"] == WBNB and log["topics"][0] == topic("Transfer(address,address,uint256)")
    )
    recipient = "0x" + external_wbnb["topics"][2][-40:]
    payout = transfer(796, WBNB, holder, recipient)
    total = (
        sum(path["after"][1] - path["before"][1] for path in found["wbnb_closed"])
        + after["after"][1]
        - before["before"][1]
    )
    if row["visible_anchor_pnl_after_exec_costs"] or total != payout + external_native[2]:
        raise ValueError("registered payout reconciliation does not close")
    return {
        "tx": tx,
        "swaps": before["swaps"] + after["swaps"],
        "input_wbnb_wei": before["before"][1],
        "output_wbnb_wei": after["after"][1],
        "token": token,
        "gross": gross,
        "retained": retained,
        "credited": credited,
        "transfer_logs": [781, 784, 785],
        "sync_log": 787,
        "source_swap": source,
        "source_signed_return": signed,
        "independent_recent_pool_identity": identity,
        "gross_all_four_closed_wbnb_wei": total,
        "visible_external_native_wei": external_native[2],
        "visible_external_wbnb_wei": payout,
        "visible_external_wbnb_recipient": recipient,
    }


def capture_summary(audit):
    scripts = PinnedEvidenceScripts(audit.parent, EVIDENCE_SCRIPTS)
    forward = audit / "forward"
    env = scripts.run_path(forward / "run.py")
    intake = env["identity"]
    identity = read(audit / "identity/verified.json")
    intake.old.now = lambda: identity["state"]["head"]["receive"]
    acquired = intake.Offline()
    catalog, arrived, _ = intake.landed()
    state = intake.observe(acquired, catalog)
    if (
        json.loads(json.dumps({"state": state, "landed": arrived, "requests": acquired.seq}))
        != identity
    ):
        raise ValueError("identity byte replay differs")
    with RpcBudget(audit / "identity/original.sqlite3", intake.POLICIES, read_only=True) as ledger:
        if ledger.counts()[intake.rpc.SOURCE] != acquired.seq:
            raise ValueError("identity ledger count differs")
    saved = read(forward / "result.json")
    frozen = read(forward / "scope.json")
    planned = env["plan"]()
    # The only post-run fixture edits are the offline reporting adapter and imports.
    planned["fixture_sha256"] = digest(forward / "fixture-at-freeze.py")
    if {k: v for k, v in frozen.items() if k != "frozen_utc"} != planned:
        raise ValueError("forward scope differs")
    intake.ROOT = forward
    end = env["exposure"](intake.Offline(), "replay", saved["scored_heads"], replay=True)
    # Histogram keys normalize to JSON strings; compare structure before emitting.
    if json.loads(json.dumps(end)) != saved:
        raise ValueError("forward byte replay differs")
    with RpcBudget(forward / "original.sqlite3", env["POLICIES"], read_only=True) as ledger:
        if ledger.counts()[intake.rpc.SOURCE] != end["requests"]:
            raise ValueError("forward ledger count differs")
    landed_dir = audit / "landed"
    previous_capture = sys.modules.get("capture")
    try:
        with redirect_stdout(StringIO()) as output:
            scripts.run_path(landed_dir / "replay.py")
    finally:
        if previous_capture is None:
            sys.modules.pop("capture", None)
        else:
            sys.modules["capture"] = previous_capture
    landed = json.loads(output.getvalue())
    if landed != read(landed_dir / "result.json"):
        raise ValueError("landed byte replay differs")
    whole_dir = audit / "whole"
    whole_env = scripts.run_path(whole_dir / "run.py")
    whole = whole_env["replay"]()
    return {
        "schema": "oasis-bsc-historical-pool-expansion-v1",
        "verified_evidence_scripts_sha256": EVIDENCE_SCRIPTS,
        "identity": identity,
        "forward": saved,
        "landed": landed,
        "landed_tax_reconciliation": registered_landed_tax_closure(
            audit, saved, landed[0], whole["holdout_identity"]
        ),
        "whole_control": whole,
        "sha256": {
            str(path.relative_to(audit)): digest(path)
            for path in (
                audit / "identity/scope.json",
                audit / "identity/verified.json",
                forward / "scope.json",
                forward / "result.json",
                landed_dir / "scope.json",
                landed_dir / "result.json",
                whole_dir / "scope.json",
                whole_dir / "result.json",
            )
        },
    }


def cli():
    if not __debug__:
        raise SystemExit("run this verifier without Python optimization")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    audit = args.root / "bsc-pool-expansion-20261127"
    print(json.dumps(capture_summary(audit), sort_keys=True, indent=2))


if __name__ == "__main__":
    cli()
