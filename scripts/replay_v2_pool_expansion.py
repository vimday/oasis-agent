"""Compare retained Ethereum or BSC V2 pool expansion and price distinct-pool routes offline."""

import argparse
import json
import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from fractions import Fraction
from hashlib import sha256
from pathlib import Path
from typing import Any

from oasis_agent.fastpath.pool_closures import PoolClosure, PoolLink, rank_pool_closures
from oasis_agent.fastpath.v2_sizing import V2Leg, V2Pool, V2Route

WETH = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
WBNB = "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c"

DEFAULT_MIN = 1
DEFAULT_MAX = 10**19
CLOSURE_LIMIT = 32
PAIR_BUDGET = 30_000


@dataclass(frozen=True, slots=True)
class _Snapshot:
    chain: int
    block: int
    block_hash: str
    anchor: str
    gas_price: int
    pools: dict[str, V2Pool]
    baseline: frozenset[str]
    digest: str


def _uint(value: object) -> int:
    if type(value) is int:
        result = value
    elif isinstance(value, str) and value.isascii() and value.isdecimal() and len(value) <= 78:
        result = int(value)
    else:
        raise ValueError("expected an unsigned integer in native units")
    if not 0 <= result < 2**256:
        raise ValueError("integer outside uint256")
    return result


def _hex(value: object, size: int) -> str:
    if not isinstance(value, str) or re.fullmatch(rf"0x[0-9a-fA-F]{{{size}}}", value) is None:
        raise ValueError("invalid EVM identifier")
    return value.lower()


def _read_snapshot(
    raw: bytes, chain: int, eth_scope: bytes | None, eth_baseline: bytes | None
) -> _Snapshot:
    obj = json.loads(raw)
    pools: dict[str, V2Pool] = {}
    identities = set()
    baseline: set[str] = set()
    prior_addresses: set[str] = set()
    if chain == 1:
        if obj["stop_reason"] is not None or eth_scope is None or eth_baseline is None:
            raise ValueError("Ethereum needs a complete snapshot, capture scope and baseline")
        prior = json.loads(eth_baseline)
        if (
            sha256(eth_scope).hexdigest() != obj["scope_sha256"]
            or sha256(eth_baseline).hexdigest() != json.loads(eth_scope)["wide_anchor_sha256"]
            or prior["stop_reason"] is not None
        ):
            raise ValueError("Ethereum baseline or capture scope did not match retained capture")
        prior_addresses = {
            _hex(pool["address"], 40)
            for sample in prior["samples"]
            for pool in sample["pool_states"]
        }
        head = obj["block"]
        block = _uint(head["number"])
        gas_price = _uint(head["base_fee_wei"])
        anchor = WETH
    elif chain == 56:
        if eth_scope is not None or eth_baseline is not None:
            raise ValueError("Ethereum baseline arguments are valid only for Ethereum")
        if obj["status"] != "complete" or obj["pin_rechecked"] is not True:
            raise ValueError("incomplete BSC snapshot")
        head = obj["head"]
        block = int(head["number"], 16)
        gas_price = _uint(obj["gas_price_wei"])
        anchor = WBNB
    else:
        raise ValueError("unsupported snapshot chain")
    if block <= 0 or gas_price == 0:
        raise ValueError("no block or no gas-price reference")
    for row in obj["pools"]:
        if chain == 1:
            if row["venue"] not in ("uni", "sushi"):
                raise ValueError("unknown Ethereum V2 venue")
            pid = _hex(row["address"], 40)
            a, b = row["tokens"]
            r0, r1 = row["reserves"]
            mul, den = 997, 1000
            in_baseline = pid in prior_addresses
            if row["stratum"] == "prior_wide_anchor" and not in_baseline:
                raise ValueError("Ethereum prior-arm identity was outside the pinned registry")
        else:
            pid = _hex(row["pool"], 40)
            a, b = row["token0"], row["token1"]
            r0, r1 = row["r0"], row["r1"]
            mul, den = _uint(row["fee_multiplier"]), _uint(row["fee_denominator"])
            if (mul, den) != {"pancake": (9975, 10000), "uniswap": (997, 1000)}.get(row["venue"]):
                raise ValueError("unknown BSC venue or fee")
            in_baseline = "named_pairs" in row["strata"]
        a, b, r0, r1 = _hex(a, 40), _hex(b, 40), _uint(r0), _uint(r1)
        if (
            int(pid, 16) == 0
            or pid in identities
            or not 0 < int(a, 16) < int(b, 16)
            or max(r0, r1) >= 2**112
        ):
            raise ValueError("invalid or duplicate V2 pool identity or reserves")
        identities.add(pid)
        if min(r0, r1) == 0:
            continue
        pools[pid] = V2Pool(pid, a, b, r0, r1, mul, den)
        if in_baseline:
            baseline.add(pid)
    if prior_addresses - identities:
        raise ValueError("Ethereum expansion omitted a baseline pool")
    if not pools or not baseline:
        raise ValueError("expected baseline and valid retained reserves")
    return _Snapshot(
        chain,
        block,
        _hex(head["hash"], 64),
        anchor,
        gas_price,
        pools,
        frozenset(baseline),
        sha256(raw).hexdigest(),
    )


def _pool_depth_links(snapshot: _Snapshot) -> Iterable[PoolLink]:
    # Mark using the deepest baseline direct pool per asset. The basis cannot shift
    # between the two arms. Marks are used for ordering research, so unknown stays unknown.
    baseline_prices: dict[str, tuple[int, int]] = {}
    for pid in sorted(snapshot.baseline):
        pool = snapshot.pools[pid]
        if snapshot.anchor == pool.token0:
            token, native, reserve = pool.token1, pool.reserve0, pool.reserve1
        elif snapshot.anchor == pool.token1:
            token, native, reserve = pool.token0, pool.reserve1, pool.reserve0
        else:
            continue
        if token not in baseline_prices or native > baseline_prices[token][0]:
            baseline_prices[token] = native, reserve
    marks = {token: Fraction(*reserves) for token, reserves in baseline_prices.items()}
    marks[snapshot.anchor] = Fraction(1)
    for row in snapshot.pools.values():
        mark0, mark1 = marks.get(row.token0), marks.get(row.token1)
        depth = (
            int(min(mark0 * row.reserve0, mark1 * row.reserve1))
            if mark0 is not None and mark1 is not None
            else None
        )
        yield PoolLink(row.pool_id, row.token0, row.token1, depth)


def _anchor_closures(rows: Iterable[PoolClosure], anchor: str) -> Iterable[tuple[str, ...]]:
    for row in rows:
        if anchor in row.tokens[:-1]:
            index = row.tokens.index(anchor)
            order = row.pools[index:] + row.pools[:index]
            yield order
            yield order[::-1]


def _build_route(snapshot: _Snapshot, route: tuple[str, ...]) -> V2Route:
    token = snapshot.anchor
    legs = []
    for address in route:
        leg = V2Leg(snapshot.pools[address], token)
        legs.append(leg)
        token = leg.token_out
    return V2Route(tuple(legs))


def _input_routes(raw: bytes, snapshot: _Snapshot) -> list[tuple[str, tuple[str, ...]]]:
    obj = json.loads(raw)
    if obj["snapshot_sha256"] != snapshot.digest:
        raise ValueError("route input is bound to a different snapshot")
    routes = obj["routes"]
    if not isinstance(routes, list) or len(routes) > 64:
        raise ValueError("expected at most 64 retained routes")
    output = []
    names = set()
    for row in routes:
        name = row["id"]
        if (
            not isinstance(name, str)
            or not 1 <= len(name) <= 100
            or name.startswith("closure-")
            or name in names
        ):
            raise ValueError("invalid or duplicate retained route name")
        if not isinstance(row["pools"], list) or not 2 <= len(row["pools"]) <= 400:
            raise ValueError("expected 2 to 400 different pools")
        names.add(name)
        output.append((name, tuple(_hex(pid, 40) for pid in row["pools"])))
    return output


def analyze(
    raw: bytes,
    *,
    chain: int,
    route_data: bytes | None,
    eth_scope: bytes | None = None,
    eth_baseline: bytes | None = None,
    minimum: int,
    maximum: int,
    gas_units: int,
) -> dict[str, Any]:
    snapshot = _read_snapshot(raw, chain, eth_scope, eth_baseline)
    if not 1 <= minimum <= maximum < 2**256 or not 21_000 <= gas_units <= 80_000_000:
        raise ValueError("invalid sizing range or gas scenario")
    links = list(_pool_depth_links(snapshot))
    closures = rank_pool_closures(
        (row for row in links if row.pool_id in snapshot.baseline),
        (row for row in links if row.pool_id not in snapshot.baseline),
        result_limit=CLOSURE_LIMIT,
        pair_budget=PAIR_BUDGET,
    )
    choices = [] if route_data is None else _input_routes(route_data, snapshot)
    choices.extend(
        (f"closure-{i}", route)
        for i, route in enumerate(_anchor_closures(closures.ranked, snapshot.anchor), start=1)
    )
    units_cost = gas_units * snapshot.gas_price
    evaluated = []
    for name, order in choices:
        route = _build_route(snapshot, order)
        bounds = route.all_input_bounds(total_cost_in_anchor=units_cost)
        sized = (
            route.size(
                minimum,
                maximum,
                total_cost_in_anchor=units_cost,
                max_quotes=128,
                max_steps=256,
            )
            if bounds.net_upper > 0
            else None
        )
        if bounds.net_upper <= 0 or (
            sized is not None and sized.net_upper is not None and sized.net_upper <= 0
        ):
            disposition = "excluded_by_cost_bound"
        elif sized is not None and sized.best_net is not None and sized.best_net > 0:
            disposition = "positive_under_cost_assumption"
        else:
            disposition = "unresolved"
        if sized is not None and sized.complete and sized.best is None:
            disposition = "no_nonzero_integer_output"
        evaluated.append(
            {
                "id": name,
                "pool_order": order,
                "hops": len(order),
                "all_input_gross_upper": bounds.gross_upper,
                "all_input_net_upper": bounds.net_upper,
                "model_net_rank": sized.best_net if sized is not None else None,
                "status": disposition,
                "finite_size": asdict(sized) if sized is not None else None,
            }
        )
    evaluated.sort(
        key=lambda item: (
            item["model_net_rank"] is None,
            -(item["model_net_rank"] or 0),
            -item["all_input_net_upper"],
            item["id"],
        )
    )
    return {
        "schema": "oasis-v2-expansion-replay-v1",
        "chain_id": chain,
        "block": snapshot.block,
        "block_hash": snapshot.block_hash,
        "snapshot_sha256": snapshot.digest,
        "baseline_sha256": sha256(eth_baseline).hexdigest() if eth_baseline is not None else None,
        "route_file_sha256": sha256(route_data).hexdigest() if route_data is not None else None,
        "unit_asset": snapshot.anchor,
        "unit_decimals": 18,
        "gas_units_assumption": gas_units,
        "gas_price_reference_wei": snapshot.gas_price,
        "cost_assumption_raw": units_cost,
        "min_amount_in": minimum,
        "max_amount_in": maximum,
        "closure_method": "single or paired additions via deterministic baseline forest",
        "closure_research": asdict(closures),
        "routes": evaluated,
        "scope": "retained snapshot model. Transfer behavior and EVM execution unverified.",
        "execution_eligible": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("--chain", type=int, required=True, choices=(1, 56))
    parser.add_argument("--routes", type=Path)
    parser.add_argument("--eth-scope", type=Path)
    parser.add_argument("--eth-baseline", type=Path)
    parser.add_argument("--min", type=int, default=DEFAULT_MIN)
    parser.add_argument("--max", type=int, default=DEFAULT_MAX)
    parser.add_argument("--gas-units", type=int, default=21_000)
    args = parser.parse_args()
    try:
        result = analyze(
            args.snapshot.read_bytes(),
            chain=args.chain,
            route_data=args.routes.read_bytes() if args.routes else None,
            eth_scope=args.eth_scope.read_bytes() if args.eth_scope else None,
            eth_baseline=args.eth_baseline.read_bytes() if args.eth_baseline else None,
            minimum=args.min,
            maximum=args.max,
            gas_units=args.gas_units,
        )
    except (KeyError, TypeError, ValueError, OSError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
