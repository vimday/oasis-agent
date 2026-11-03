"""Inspect existing strategy/authority journals offline, without migrating or unlocking them.

Use read-only mounts of the original state volumes. No RPC, budget, signing socket
or key is opened. Exit 0 means the inspected histories agree with the supplied
config and have no listed blockers; it is never a live execution permit.
"""

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from oasis_agent.live.config import LiveConfig
from oasis_agent.live.journal import LiveJournal
from oasis_agent.live.state import StateStore
from oasis_agent.models.common import canonical_json, sha256_hex

ROOT = Path(__file__).resolve().parents[1]


def inspect(strategy: Path, authority: Path, config: LiveConfig) -> dict:
    config.validate()
    expected = sha256_hex(canonical_json(asdict(config)))
    stores = {"strategy": StateStore(strategy), "authority": StateStore(authority)}
    if stores["strategy"].journal_path.samefile(stores["authority"].journal_path):
        raise ValueError("distinct service journals required")
    report: dict = {
        "schema": "oasis.live-offline-state-inspection.v1",
        "inspection_passed": False,
        "execution_eligible": False,
        "historical_only": True,
        "expected_config_hash": expected,
        "services": {},
        "blockers": [],
        "limitations": [
            "no_current_chain_or_nav_observation",
            "receipt_payloads_not_replayed",
            "no_configuration_migration_or_halt_clear",
            "separate_journal_snapshots_not_a_cross_service_atomic_read",
        ],
    }
    for name, store in stores.items():
        journal = LiveJournal(store.journal_path, require_existing=True)
        store.journal = journal
        state = store.load("live", read_only=True)
        binding = journal.config_hash
        if (
            not isinstance(binding, str)
            or len(binding) != 64
            or any(c not in "0123456789abcdef" for c in binding)
        ):
            raise ValueError("canonical journal configuration hash required")
        position = state.position
        identity = (
            [position.token_id, position.lower, position.upper, position.liquidity]
            if position
            else None
        )
        service = {
            "journal_tip": journal.revision,
            "config_hash": binding,
            "configuration_matches": binding == expected,
            "halted": bool(state.halted),
            "has_pending": state.pending is not None,
            "has_signing_attempt": state.signing_attempt is not None,
            "legacy_gas_cost_incomplete": state.legacy_gas_cost_incomplete,
            "position_identity_hash": sha256_hex(canonical_json(identity)),
            "last_recorded_nav_usd": state.last_nav_usd,
        }
        report["services"][name] = service
        for flag in ("halted", "has_pending", "has_signing_attempt", "legacy_gas_cost_incomplete"):
            if service[flag]:
                report["blockers"].append(f"{name}_{flag}")
        if binding != expected:
            report["blockers"].append(f"{name}_configuration_mismatch")
    a, b = (report["services"][name] for name in ("strategy", "authority"))
    if a["position_identity_hash"] != b["position_identity_hash"]:
        report["blockers"].append("service_position_disagreement")
    report["inspection_passed"] = True
    report["evidence_hash"] = sha256_hex(canonical_json(report))
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strategy-state", type=Path, required=True)
    parser.add_argument("--authority-state", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=ROOT / "configs/live.base-eth-usdc.json")
    args = parser.parse_args()
    try:
        report = inspect(args.strategy_state, args.authority_state, LiveConfig.load(args.config))
    except Exception:  # noqa: BLE001 - no raw journal, path or exception in refusal output
        report = {
            "schema": "oasis.live-offline-state-inspection.v1",
            "inspection_passed": False,
            "execution_eligible": False,
            "blockers": ["retained_state_unresolved"],
        }
    print(json.dumps(report, indent=2))
    raise SystemExit(0 if report["inspection_passed"] and not report["blockers"] else 3)


if __name__ == "__main__":
    main()
