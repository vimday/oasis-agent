"""Audit the retained stages of a normal head-driven Base campaign without network I/O."""

import argparse
from pathlib import Path

from oasis_agent.arbitrage.base_survival import summarize_base_shadow
from oasis_agent.arbitrage.cycle_evidence import read_bounded
from oasis_agent.evidence.base_newheads import DIRECTORY, replay_head_schedule
from oasis_agent.evidence.rpc_validation import (
    strict_digest,
    strict_integer,
    strict_json_object,
    strict_list,
    strict_object,
)
from oasis_agent.fastpath.base_mixed_snapshot import replay_mixed_snapshot
from oasis_agent.models.common import canonical_json, sha256_hex
from scripts.run_multicall_100tick_campaign import verify_campaign_attribution_chain


def _observed_child_deadlines(
    records: list[dict[str, object]], schedule: dict[str, object]
) -> list[dict[str, object]]:
    accepted: dict[int, dict[str, object]] = {}
    dispatched = []
    for raw in strict_list(schedule.get("frames"), "scheduled heads"):
        outcome = strict_object(raw, "head outcome")
        if outcome.get("status") in ("duplicate_same_head", "rejected_frame"):
            continue
        obs = strict_object(outcome.get("observation"), "raw head observation")
        height = strict_integer(strict_object(obs["head"], "raw head")["height"], "head height")
        accepted[height] = obs
        if outcome.get("status") == "dispatched":
            dispatched.append(obs)

    out = []
    for idx, row in enumerate(records):
        life = strict_object(row["shadow_cycle_lifecycle"], "shadow lifecycle")
        mixed = strict_object(life["mixed_base_state"], "mixed Base state")
        if idx >= len(dispatched) or mixed.get("newheads_observation") != dispatched[idx]:
            raise ValueError("Base shadow published an undispatched head")
        block = strict_integer(row["block_number"], "state block")
        child = accepted.get(block + 1)
        ready = strict_integer(mixed["state_ready_monotonic_ns"], "mixed state ready")
        finished = strict_integer(life["discovery_finished_monotonic_ns"], "discovery finished")
        observed = (
            strict_integer(child["received_monotonic_ns"], "child receive time") if child else None
        )
        out.append(
            {
                "block": block,
                "observed_child": block + 1 if child else None,
                "child_receive_minus_state_ready_ns": (
                    None if observed is None else observed - ready
                ),
                "child_receive_minus_discovery_finished_ns": (
                    None if observed is None else observed - finished
                ),
            }
        )
    return out


def replay(root: Path, *, arrival_deadlines: bool = False) -> dict[str, object]:
    raw = read_bounded(root / "shadow_attribution.jsonl", 16 * 1024 * 1024)
    records = [strict_json_object(line) for line in raw.splitlines()]
    chain_head = verify_campaign_attribution_chain(records)
    summary_raw = read_bounded(root / "shadow_summary.json", 2 * 1024 * 1024)
    summary = strict_json_object(summary_raw)
    digest = strict_digest(summary.pop("summary_sha256", None), "summary checksum")
    if (
        sha256_hex(canonical_json(summary)) != digest
        or summary.get("chain_head_sha256") != chain_head
        or summary.get("ticks_completed") != len(records)
    ):
        raise ValueError("campaign summary disagrees with its attribution chain")
    report = summarize_base_shadow(records, arrival_deadlines=arrival_deadlines)
    for row in records:
        life = strict_object(row["shadow_cycle_lifecycle"], "shadow lifecycle")
        mix = strict_object(life["mixed_base_state"], "mixed Base state")
        ref = mix["manifest"]
        if not isinstance(ref, str):
            raise ValueError("invalid snapshot manifest")
        path = Path(ref)
        if (
            len(path.parts) != 3
            or path.parts[0] != "mixed_base_state"
            or path.name != "manifest.json"
            or ".." in path.parts
        ):
            raise ValueError("snapshot manifest is outside the fixed evidence directory")
        manifest = root / path
        manifest_raw = read_bounded(manifest, 512 * 1024)
        if sha256_hex(manifest_raw) != strict_digest(
            mix["manifest_sha256"], "snapshot manifest digest"
        ):
            raise ValueError("snapshot digest disagrees with the attribution chain")
        frozen = strict_json_object(manifest_raw)
        if any(
            frozen.get(key) != mix.get(key)
            for key in ("newheads_observation", "state_ready_monotonic_ns")
        ):
            raise ValueError("snapshot timing or stream header disagrees with attribution")
        state = replay_mixed_snapshot(manifest)
        if (state.block.number, state.block.block_hash) != (row["block_number"], row["block_hash"]):
            raise ValueError("replayed snapshot belongs to another block")
    stream = strict_object(summary.get("base_newheads", {}), "head session summary")
    if stream.get("selection") == "latest_queued":
        schedule = replay_head_schedule(
            root / DIRECTORY, strict_digest(stream.get("schedule_sha256"), "head schedule digest")
        )
        observed_deadlines = _observed_child_deadlines(records, schedule)
        report["head_schedule"] = schedule
        if arrival_deadlines:
            report["observed_raw_child_deadlines"] = observed_deadlines
    return {
        **report,
        "attribution_sha256": sha256_hex(raw),
        "summary_sha256": digest,
        "campaign_stopped_early": summary["stopped_early"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("campaign", type=Path)
    parser.add_argument(
        "--arrival-deadlines",
        action="store_true",
        help="Compare local state and discovery times to observed direct-child receive times.",
    )
    args = parser.parse_args()
    print(canonical_json(replay(args.campaign, arrival_deadlines=args.arrival_deadlines)).decode())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
