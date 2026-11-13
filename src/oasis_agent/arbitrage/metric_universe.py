"""Bounded multi-chain discovery. Directory entries are provider claims only."""

from collections import defaultdict
from typing import Annotated, Any, Literal, Self

from pydantic import Field, model_validator

from ..evidence.rpc_validation import strict_integer, strict_list, strict_object
from ..models.common import ZERO_ADDRESS
from .cycle_plan import Frozen, address
from .metric import API


class UniversePlan(Frozen):
    schema_version: Literal["oasis-metric-universe-v1"] = Field(alias="schema")
    scenario: Literal["synthetic", "provider_observation"]
    chain_ids: Annotated[
        tuple[Annotated[int, Field(gt=0, lt=1 << 64)], ...], Field(min_length=1, max_length=8)
    ]
    pools_per_chain: Annotated[int, Field(ge=1, le=32)]
    max_capture_seconds: Annotated[int, Field(ge=1, le=120)]

    @model_validator(mode="after")
    def scope(self) -> Self:
        if len(set(self.chain_ids)) != len(self.chain_ids):
            raise ValueError("duplicate_chain")
        return self


def requests(plan: UniversePlan) -> tuple[dict[str, Any], ...]:
    return tuple(
        {
            "method": "GET",
            "url": f"{API}/public/v1/evm/{chain}/metadata",
            "params": {"count": plan.pools_per_chain, "offset": 0, "minTvlFiat": 0},
        }
        for chain in plan.chain_ids
    )


def discover(plan: UniversePlan, payloads: list[dict[str, Any]]) -> dict[str, Any]:
    if len(payloads) != len(plan.chain_ids):
        raise ValueError("chain_coverage")
    chains = []
    for chain, payload in zip(plan.chain_ids, payloads, strict=True):
        rows = strict_list(payload.get("data"), "data")
        total = strict_integer(payload.get("total"), "total")
        if len(rows) > plan.pools_per_chain or total < len(rows):
            raise ValueError("metadata_coverage")
        offset = payload.get("nextOffset")
        if "nextOffset" not in payload or (
            offset is not None and strict_integer(offset, "nextOffset") != len(rows)
        ):
            raise ValueError("metadata_pagination")
        if (offset is None) != (total == len(rows)) or (not rows and total):
            raise ValueError("metadata_pagination")
        seen: set[str] = set()
        decimals: dict[str, int] = {}
        groups: dict[tuple[tuple[str, int], ...], list[dict[str, Any]]] = defaultdict(list)
        issues = []
        for entry in rows:
            row = strict_object(entry, "pool")
            pool = address(row.get("poolAddress"))
            if pool in seen or pool == ZERO_ADDRESS:
                raise ValueError("duplicate_or_zero_pool")
            seen.add(pool)
            tokens = []
            for i in (0, 1):
                token = address(row.get(f"token{i}"))
                precision = strict_integer(row.get(f"token{i}Decimals"), "decimals")
                if token == ZERO_ADDRESS or not 0 <= precision <= 36:
                    raise ValueError("unsupported_token_identity")
                if token in decimals and decimals[token] != precision:
                    raise ValueError("conflicting_token_decimals")
                decimals[token] = precision
                tokens.append((token, precision))
            if tokens[0][0] == tokens[1][0]:
                raise ValueError("identical_tokens")
            if row.get("contractVersion") != "v1":
                issues.append({"pool": pool, "reason": "unsupported_contract_version"})
                continue
            if strict_integer(row.get("pauseLevel"), "pauseLevel") != 0:
                issues.append({"pool": pool, "reason": "pool_paused"})
                continue
            # Unordered grouping finds both orientations; original orientation is retained.
            key = tuple(sorted(tokens))
            groups[key].append({"pool": pool, "token0": tokens[0][0], "token1": tokens[1][0]})
        chains.append(
            {
                "chain_id": chain,
                "observed_pools": len(rows),
                "provider_total": total,
                "coverage": "provider_directory_complete" if offset is None else "bounded_partial",
                "next_offset_not_fetched": offset,
                "issues": issues,
                "pairs": [
                    {
                        "tokens": [{"address": a, "decimals": d} for a, d in key],
                        "pools": sorted(pools, key=lambda x: x["pool"]),
                        "next_research": "depth_and_independent_dex",
                        "same_pair_pool_count": len(pools),
                    }
                    for key, pools in sorted(groups.items())
                ],
            }
        )
    return {
        "schema": "oasis-metric-universe-report-v1",
        "evidence_class": "synthetic" if plan.scenario == "synthetic" else "provider_claim",
        "chains": chains,
        "execution_eligible": False,
        "block_identity": None,
        "cross_chain_routes": [],
        "net_pnl": None,
        "limitations": [
            "directory_is_not_depth",
            "deployment_identity_unverified",
            "no_cross_chain_asset_equivalence",
            "no_chain_atomicity_claim",
        ],
    }
