"""Audit the retained stages of a normal head-driven Base campaign without network I/O."""

import argparse
from pathlib import Path

from oasis_agent.arbitrage.base_survival import summarize_base_shadow
from oasis_agent.arbitrage.cycle_evidence import read_bounded
from oasis_agent.evidence.rpc_validation import strict_digest, strict_json_object, strict_object
from oasis_agent.fastpath.base_mixed_snapshot import replay_mixed_snapshot
from oasis_agent.models.common import canonical_json, sha256_hex
from scripts.run_multicall_100tick_campaign import verify_campaign_attribution_chain


def replay(root: Path) -> dict[str, object]:
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
    report = summarize_base_shadow(records)
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
    return {
        **report,
        "attribution_sha256": sha256_hex(raw),
        "summary_sha256": digest,
        "campaign_stopped_early": summary["stopped_early"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("campaign", type=Path)
    args = parser.parse_args()
    print(canonical_json(replay(args.campaign)).decode())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
