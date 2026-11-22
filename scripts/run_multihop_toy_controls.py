"""Exhaustively compare two bounded multihop controls on synthetic integer V2 pools."""

import json
from collections import Counter
from collections.abc import Iterator
from dataclasses import asdict, dataclass, replace

CAPITAL_TOKEN = "A"
FEE_BPS = 30
COST_PER_SWAP = 3


@dataclass(frozen=True, slots=True)
class Pool:
    label: str
    token0: str
    token1: str
    reserve0: int
    reserve1: int

    def other_token(self, token: str) -> str:
        if token == self.token0:
            return self.token1
        if token == self.token1:
            return self.token0
        raise ValueError(f"{token} is outside pool {self.label}")

    def swap(self, token: str, amount: int) -> tuple[Pool, int]:
        self.other_token(token)
        if amount < 0:
            raise ValueError("negative swap input")
        forward = token == self.token0
        reserve_in, reserve_out = (
            (self.reserve0, self.reserve1) if forward else (self.reserve1, self.reserve0)
        )
        after_fee = amount * (10_000 - FEE_BPS)
        output = after_fee * reserve_out // (reserve_in * 10_000 + after_fee)
        reserve_in += amount
        reserve_out -= output
        if forward:
            return replace(self, reserve0=reserve_in, reserve1=reserve_out), output
        return replace(self, reserve0=reserve_out, reserve1=reserve_in), output


@dataclass(frozen=True, slots=True)
class Step:
    pool: str
    token_in: str
    amount_in: int | None = None


@dataclass(frozen=True, slots=True)
class Outcome:
    amount_in: int
    amount_out: int
    swap_count: int
    cost: int
    net: int
    final_pools: tuple[Pool, ...]


def inventory(pools: tuple[Pool, ...], wallet: Counter[str]) -> Counter[str]:
    total = wallet.copy()
    for pool in pools:
        total[pool.token0] += pool.reserve0
        total[pool.token1] += pool.reserve1
    return total


def simulate(pools: tuple[Pool, ...], amount: int, steps: tuple[Step, ...]) -> Outcome | None:
    """Run one fully funded atomic plan, returning None if a leg rounds its output to zero."""
    wallet = Counter({CAPITAL_TOKEN: amount})
    initial = inventory(pools, wallet)
    states = {pool.label: pool for pool in pools}
    for step in steps:
        spend = wallet[step.token_in] if step.amount_in is None else step.amount_in
        if not 0 < spend <= wallet[step.token_in]:
            raise ValueError("swap input exceeds funded token balance or is zero")
        pool = states[step.pool]
        updated, output = pool.swap(step.token_in, spend)
        if output == 0:
            return None
        wallet[step.token_in] -= spend
        wallet[pool.other_token(step.token_in)] += output
        states[pool.label] = updated

    final_pools = tuple(states.values())
    if inventory(final_pools, wallet) != initial or set(+wallet) - {CAPITAL_TOKEN}:
        raise ValueError("nonconserving execution or unsettled intermediate token")
    cost = COST_PER_SWAP * len(steps)
    return Outcome(
        amount,
        wallet[CAPITAL_TOKEN],
        len(steps),
        cost,
        wallet[CAPITAL_TOKEN] - amount - cost,
        final_pools,
    )


RING_POOLS = (
    Pool("AB", "A", "B", 20_000, 20_000),
    Pool("BC", "B", "C", 20_000, 20_000),
    Pool("CD", "C", "D", 20_000, 20_000),
    Pool("DE", "D", "E", 20_000, 20_000),
    Pool("EF", "E", "F", 20_000, 20_000),
    Pool("FA", "F", "A", 20_000, 30_000),
    Pool("AB-parallel", "A", "B", 20_000, 20_000),
)
RING_MAX_INPUT = 1_200


def cycles(pools: tuple[Pool, ...], hop_cap: int) -> Iterator[tuple[Step, ...]]:
    """Enumerate simple pool and token cycles anchored to A, preserving directions."""
    if hop_cap < 2:
        return

    def walk(
        token: str, path: tuple[Step, ...], seen_tokens: frozenset[str], seen_pools: frozenset[str]
    ) -> Iterator[tuple[Step, ...]]:
        for pool in pools:
            if pool.label in seen_pools or token not in (pool.token0, pool.token1):
                continue
            next_token = pool.other_token(token)
            extended = (*path, Step(pool.label, token))
            if next_token == CAPITAL_TOKEN:
                if len(extended) >= 2:
                    yield extended
            elif next_token not in seen_tokens and len(extended) < hop_cap:
                yield from walk(
                    next_token, extended, seen_tokens | {next_token}, seen_pools | {pool.label}
                )

    yield from walk(CAPITAL_TOKEN, (), frozenset({CAPITAL_TOKEN}), frozenset())


def outcome_row(outcome: Outcome) -> dict[str, int]:
    return {
        "amount_in": outcome.amount_in,
        "amount_out": outcome.amount_out,
        "swaps": outcome.swap_count,
        "cost": outcome.cost,
        "net": outcome.net,
    }


def ring_control(hop_cap: int) -> dict[str, object]:
    path_rows = []
    for path in cycles(RING_POOLS, hop_cap):
        best = max(
            (
                outcome
                for amount in range(1, RING_MAX_INPUT + 1)
                if (outcome := simulate(RING_POOLS, amount, path)) is not None
            ),
            key=lambda outcome: outcome.net,
        )
        path_rows.append({"path": [step.pool for step in path], **outcome_row(best)})
    return {
        "hop_cap": hop_cap,
        "routes": path_rows,
        "positive_routes": sum(row["net"] > 0 for row in path_rows),
        "best_net_including_hold": max(0, max((row["net"] for row in path_rows), default=0)),
    }


MERGE_POOLS = (
    Pool("AB", "A", "B", 4_000, 4_400),
    Pool("BM", "B", "M", 4_000, 4_400),
    Pool("AC", "A", "C", 4_200, 4_550),
    Pool("CM", "C", "M", 4_100, 4_500),
    Pool("MA-shared", "M", "A", 15_000, 18_500),
)
MERGE_INPUT = 600
EXIT = Step("MA-shared", "M")


def branch_steps(left: int, right: int) -> Iterator[tuple[Step, ...]]:
    if left:
        yield Step("AB", "A", left), Step("BM", "B")
    if right:
        yield Step("AC", "A", right), Step("CM", "C")


def merge_control() -> dict[str, object]:
    successful: dict[int, Outcome] = {}
    for left in range(MERGE_INPUT + 1):
        branches = branch_steps(left, MERGE_INPUT - left)
        merged_steps = [step for branch in branches for step in branch]
        outcome = simulate(MERGE_POOLS, MERGE_INPUT, (*merged_steps, EXIT))
        if outcome is not None:
            successful[left] = outcome
    best_single = max((0, MERGE_INPUT), key=lambda left: successful[left].net)
    best_split = max(
        (left for left in successful if 0 < left < MERGE_INPUT),
        key=lambda left: successful[left].net,
    )
    right = MERGE_INPUT - best_split
    merged = successful[best_split]
    repeated_plan = tuple(
        step for branch in branch_steps(best_split, right) for step in (*branch, EXIT)
    )
    repeated = simulate(MERGE_POOLS, MERGE_INPUT, repeated_plan)
    if repeated is None:
        raise ValueError("expected shared-exit sequence to be executable in the toy")

    standalone_outputs = []
    for allocation, branch in zip(
        (best_split, right), branch_steps(best_split, right), strict=True
    ):
        standalone = simulate(MERGE_POOLS, allocation, (*branch, EXIT))
        if standalone is None:
            raise ValueError("expected standalone branch quote to succeed in the toy")
        standalone_outputs.append(standalone.amount_out)
    stale_output = sum(standalone_outputs)

    return {
        "input_capital": MERGE_INPUT,
        "allocation_points": MERGE_INPUT + 1,
        "feasible_points": len(successful),
        "best_single": {
            "left": best_single,
            "right": MERGE_INPUT - best_single,
            **outcome_row(successful[best_single]),
        },
        "best_merged": {"left": best_split, "right": right, **outcome_row(merged)},
        "same_allocation_two_mutating_exit_swaps": outcome_row(repeated),
        "fresh_reserve_per_branch_incorrect_output": stale_output,
        "fresh_reserve_overstatement_vs_mutating_two_exits": stale_output - repeated.amount_out,
        "shared_exit_initial_reserves": asdict(MERGE_POOLS[-1]),
        "shared_exit_reserves_after_one_merge": asdict(merged.final_pools[-1]),
        "shared_exit_reserves_after_two_swaps": asdict(repeated.final_pools[-1]),
    }


def report() -> dict[str, object]:
    return {
        "evidence_kind": "synthetic_offline",
        "execution_eligible": False,
        "release_permitted": False,
        "currency": "A",
        "all_assets_decimals": 6,
        "unit": "integer asset base unit",
        "fee_bps_per_swap": FEE_BPS,
        "cost_base_units_per_swap": COST_PER_SWAP,
        "ring_amount_bounds_inclusive": [1, RING_MAX_INPUT],
        "ring": [ring_control(4), ring_control(6)],
        "merge": merge_control(),
    }


if __name__ == "__main__":
    print(json.dumps(report(), indent=2, sort_keys=True))
