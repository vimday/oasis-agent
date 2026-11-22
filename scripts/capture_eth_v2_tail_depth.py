"""One-shot pinned state of two Ethereum V2 factory strata, and offline replay."""

import argparse
import json
from pathlib import Path

from eth_abi.abi import decode, encode
from eth_abi.exceptions import DecodingError

from oasis_agent.rpc.budget import BudgetPolicy, RpcBudget

if __package__:
    from . import run_eth_v2_forward_shadow as core
    from . import run_eth_v2_wide_shadow as previous
else:
    import run_eth_v2_forward_shadow as core
    import run_eth_v2_wide_shadow as previous

ROOT = Path(__file__).resolve().parents[1] / "reports/research/multichain-depth-20261123/ethereum"
MULTICALL = "0xca11bde05977b3631167028862be2a173976ca11"
LATEST_PER_FACTORY = 256
SYSTEMATIC_PER_FACTORY = 128
HTTP_CAP = 30
RPC_CAP = 52
INNER_CAP = 4_200
GROUP = 128
PLAN_SECONDS = 110
POLICY = (BudgetPolicy(core.SOURCE, HTTP_CAP, 200),)


def slots(length):
    if length <= LATEST_PER_FACTORY:
        raise ValueError("unexpected factory length")
    split = length - LATEST_PER_FACTORY
    return [
        *(
            (idx, "historical_stratum")
            for idx in sorted(
                {
                    split * (2 * i + 1) // (2 * SYSTEMATIC_PER_FACTORY)
                    for i in range(SYSTEMATIC_PER_FACTORY)
                }
            )
        ),
        *((idx, "newest_stratum") for idx in range(split, length)),
    ]


class Aggregate:
    def __init__(self, reads, bound, number):
        self.reads = reads
        self.bound = bound
        self.number = number
        self.inner_count = 0

    def fetch(self, calls):
        groups = [calls[i : i + GROUP] for i in range(0, len(calls), GROUP)]
        values = []
        for start in range(0, len(groups), 2):
            pair = groups[start : start + 2]
            self.inner_count += sum(len(xs) + 1 for xs in pair)
            if self.inner_count > INNER_CAP:
                raise ValueError("inner call cap exceeded")
            rpc_requests = []
            for group in pair:
                rows = [(address, False, bytes.fromhex(data[2:])) for address, data in group]
                rows.append((MULTICALL, False, bytes.fromhex("42cbb15c")))
                encoded = encode(["(address,bool,bytes)[]"], [rows]).hex()
                rpc_requests.append(core.abi_call(MULTICALL, "0x82ad56cb" + encoded, self.bound))
            responses, _ = self.reads.batch(rpc_requests)
            for group, response in zip(pair, responses, strict=True):
                raw = core.hex_bytes(response)
                (answers,) = decode(["(bool,bytes)[]"], raw, strict=True)
                if (
                    len(answers) != len(group) + 1
                    or not all(answer[0] for answer in answers)
                    or raw != encode(["(bool,bytes)[]"], [answers])
                    or core.words("0x" + answers[-1][1].hex(), 1)[0] != self.number
                ):
                    raise ValueError("multicall return is incomplete, unbound or noncanonical")
                values.extend("0x" + value.hex() for _, value in answers[:-1])
        return values


def collect(reads, result, previous_snapshot):
    (chain, block), clock = reads.batch(
        [
            ("eth_chainId", []),
            ("eth_getBlockByNumber", ["latest", False]),
        ]
    )
    if chain != "0x1":
        raise ValueError("unexpected chain ID")
    for key in ("hash", "parentHash", "stateRoot"):
        core.hex_bytes(block[key], 32)
    block_ms = int(block["timestamp"], 16) * 1000
    if not 0 <= clock["received_ms"] - block_ms <= 35_000:
        raise ValueError("unfresh head")
    number = int(block["number"], 16)
    bound = {"blockHash": block["hash"], "requireCanonical": True}
    result["block"] = {
        "number": number,
        "hash": block["hash"],
        "state_root": block["stateRoot"],
        "parent_hash": block["parentHash"],
        "timestamp_ms": block_ms,
        "head_received_ms": clock["received_ms"],
        "base_fee_wei": int(block["baseFeePerGas"], 16),
    }
    code_addresses = [MULTICALL, *previous.FACTORIES.values()]
    code, _ = reads.batch([("eth_getCode", [addr, bound]) for addr in code_addresses])
    result["entry_code_sha256"] = {
        addr: core.digest(core.hex_bytes(value))
        for addr, value in zip(code_addresses, code, strict=True)
    }
    agg = Aggregate(reads, bound, number)
    lengths = agg.fetch([(addr, "0x574f2ba3") for addr in previous.FACTORIES.values()])
    by_venue = dict(zip(previous.FACTORIES, (core.words(x, 1)[0] for x in lengths), strict=True))
    result["all_pairs_length"] = by_venue
    selectors = [
        (venue, idx, stratum)
        for venue, length in by_venue.items()
        for idx, stratum in slots(length)
    ]
    pairs = agg.fetch(
        [(previous.FACTORIES[venue], "0x1e3dd18b" + core.word(idx)) for venue, idx, _ in selectors]
    )
    venues = {}
    indexed = {}
    for (venue, idx, stratum), out in zip(selectors, pairs, strict=True):
        address = core.address(out)
        if not int(address, 16) or address in indexed:
            raise ValueError("invalid or repeated factory slot")
        venues[address] = venue
        indexed[address] = {"slot": idx, "stratum": stratum}
    for row in previous_snapshot["pool_states"]:
        address, venue = row["address"], row["venue"]
        if address in venues and venues[address] != venue:
            raise ValueError("prior pool factory mismatch")
        venues.setdefault(address, venue)
        indexed.setdefault(address, {"slot": None, "stratum": "prior_wide_anchor"})
    details = agg.fetch(
        [
            (addr, selector)
            for addr in venues
            for selector in ("0xc45a0155", "0x0dfe1681", "0xd21220a7", "0x0902f1ac")
        ]
    )
    pools = []
    for i, (address, venue) in enumerate(venues.items()):
        factory, token0, token1, reserves = details[i * 4 : i * 4 + 4]
        if core.address(factory) != previous.FACTORIES[venue]:
            raise ValueError("pair factory mismatch at anchor block")
        tokens = list(map(core.address, (token0, token1)))
        parsed = core.words(reserves, 3)
        if not int(tokens[0], 16) or tokens[0] >= tokens[1]:
            raise ValueError("zero or unsorted pool assets")
        if max(parsed[:2]) >= 2**112 or parsed[2] >= 2**32:
            raise ValueError("pool reserve ABI width mismatch")
        pools.append(
            {
                "address": address,
                "venue": venue,
                **indexed[address],
                "tokens": tokens,
                "reserves": parsed[:2],
                "reserve_ts": parsed[2],
            }
        )
    (again,), state_clock = reads.batch([("eth_getBlockByNumber", [block["number"], False])])
    if any(again[key] != block[key] for key in ("number", "hash", "stateRoot")):
        raise ValueError("header changed")
    result["block"]["state_ready_ms"] = state_clock["received_ms"]
    result["pools"] = pools
    result["inner_multicall_calls"] = agg.inner_count


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("capture", "replay"))
    args = parser.parse_args()
    last_ledgers = []
    for path, policies, expected in (
        (core.RESEARCH_ROOT, core.POLICY, 11),
        (previous.ROOT, previous.POLICY, 23),
    ):
        ledger_path = path / "original-budget.sqlite3"
        ledger = RpcBudget(ledger_path, policies, read_only=True)
        try:
            if ledger.counts()[core.SOURCE] != expected:
                raise ValueError("completion scope count changed")
            last_ledgers.append(
                {"count": expected, "sha256": core.digest(ledger_path.read_bytes())}
            )
        finally:
            ledger.db.close()
    previous_raw = (previous.ROOT / "result.json").read_bytes()
    scope_path = ROOT / "scope.json"
    scope = core.canonical(
        {
            "schema": 1,
            "chain_id": 1,
            "endpoint": core.ENDPOINT,
            "paid_usd": 0,
            "prior_ledgers": last_ledgers,
            "prior_http": 34,
            "new_max_http": HTTP_CAP,
            "max_cumulative_http": 34 + HTTP_CAP,
            "max_rpc": RPC_CAP,
            "max_inner": INNER_CAP,
            "deadline_seconds": PLAN_SECONDS,
            "factory_addresses": previous.FACTORIES,
            "multicall_address": MULTICALL,
            "max_calls_per_aggregate": GROUP + 1,
            "max_aggregates_per_http": 2,
            "newest_per_factory": LATEST_PER_FACTORY,
            "historic_midpoints_per_factory": SYSTEMATIC_PER_FACTORY,
            "wide_anchor_sha256": core.digest(previous_raw),
            "stop_on_first_source_failure": True,
        }
    )
    budget = None
    if args.mode == "capture":
        ROOT.mkdir(parents=True, exist_ok=False)
        core.write_new(scope_path, scope)
        budget = RpcBudget(ROOT / "original-budget.sqlite3", POLICY)
    elif scope_path.read_bytes() != scope:
        raise ValueError("scope changed")
    result = {"schema": 1, "scope_sha256": core.digest(scope)}
    reads = core.Reads(
        ROOT, budget, http_limit=HTTP_CAP, rpc_limit=RPC_CAP, deadline_seconds=PLAN_SECONDS
    )
    try:
        collect(reads, result, json.loads(previous_raw)["samples"][-1])
        result["stop_reason"] = None
    except (ValueError, KeyError, TypeError, OSError, DecodingError) as exc:
        result["stop_reason"] = str(exc)
    result.update(
        {
            "new_http": reads.http,
            "cumulative_http": 34 + reads.http,
            "rpc_count": reads.logical,
            "started_ms": reads.first_ms,
            "finished_ms": reads.last_ms,
        }
    )
    result_path = ROOT / "graph.json"
    if budget is not None:
        budget.db.close()
        core.write_new(result_path, core.canonical(result))
    else:
        ledger = RpcBudget(ROOT / "original-budget.sqlite3", POLICY, read_only=True)
        try:
            reads.verify_budget(ledger)
        finally:
            ledger.db.close()
        if result_path.read_bytes() != core.canonical(result):
            raise ValueError("replay differs from committed graph bytes")
    print(json.dumps({k: v for k, v in result.items() if k != "pools"}))
    return 3 if result["stop_reason"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
