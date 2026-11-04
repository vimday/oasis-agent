"""Read-only preflight of a five-to-eight-pin policy upgrade; never applies it.

Reads existing stopped-service journals and retained proofs only. No budget,
RPC, key, configuration write, epoch transition, rearm or startup is performed.
"""

import argparse
import json
import os
import stat
import time
from contextlib import ExitStack, closing
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path

from oasis_agent.evidence.exact_capture import successful_results
from oasis_agent.evidence.rpc_validation import strict_json_object
from oasis_agent.evidence.store import ReadOnlyEvidenceStore
from oasis_agent.live.config import LiveConfig
from oasis_agent.live.config_epochs import retained_config
from oasis_agent.live.gas_oracle import GAS_PIN_NAMES, LEGACY_PIN_NAMES, require_gas_oracle_pins
from oasis_agent.live.gas_oracle_evidence import replay_discovery_observation
from oasis_agent.live.startup_state import _position_identity, paths, require_file, startup_locks
from oasis_agent.live.state import StateStore, day_of
from oasis_agent.live.valuation import nav_fresh_until, replay_nav, replay_nav_quorum
from oasis_agent.models.common import canonical_json, evidence_ref_from_json, sha256_hex
from scripts.capture_live_recovery import review_checkpoint
from scripts.review_live_gas_oracle import _read

ROLES = ("strategy", "authority")


def _signature(path):
    info = require_file(path)
    return tuple(
        getattr(info, name)
        for name in (
            "st_dev",
            "st_ino",
            "st_mode",
            "st_uid",
            "st_size",
            "st_mtime_ns",
            "st_ctime_ns",
        )
    )


def review(strategy, authority, target, references, gas_evidence, *, now):
    """A local snapshot preflight, not an application or release permit."""
    target.validate()
    require_gas_oracle_pins(target.expected_pins)
    if target.oracle_max_age_seconds != 1800 or target.usdc_oracle_max_age_seconds != 1800:
        raise ValueError("both owner feed ceilings must remain 1800 seconds")
    if type(now) is not int or now < 0:
        raise ValueError("explicit integer review clock required")
    if (
        not isinstance(references, dict)
        or set(references) != {"schema", "nav", "gas"}
        or references["schema"] != "oasis.runtime-transition-inputs.v1"
        or not isinstance(references["nav"], dict)
        or set(references["nav"]) != set(ROLES)
        or any(not isinstance(v, list) or len(v) != 2 for v in references["nav"].values())
        or not isinstance(references["gas"], list)
        or len(references["gas"]) != 2
    ):
        raise ValueError("exact per-service NAV and shared gas reference pairs required")
    gas_refs = tuple(evidence_ref_from_json(r) for r in references["gas"])
    sources = ("live_primary", "live_secondary")
    directories = dict(zip(ROLES, (strategy, authority), strict=True))
    files, database_paths, frozen_directories = [], [gas_evidence], {}
    for role, directory in directories.items():
        info = directory.lstat()
        if (
            directory.resolve() != directory.absolute()
            or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077
        ):
            raise ValueError("existing private owned service directories required")
        frozen_directories[directory] = (
            info.st_dev,
            info.st_ino,
            info.st_mode,
            info.st_uid,
            info.st_mtime_ns,
            info.st_ctime_ns,
        )
        state, evidence, lock = paths(directory, role)
        journal = StateStore(state, require_existing=True).journal_path
        files.extend([state, journal, evidence, lock, directory / "STOP"])
        database_paths.extend([journal, evidence])
    files.append(gas_evidence)
    frozen = {p: _signature(p) for p in files}
    if len(frozen) != len(files) or len({v[:2] for v in frozen.values()}) != len(files):
        raise ValueError("distinct original service and gas artifacts required")

    def unchanged():
        for directory, signature in frozen_directories.items():
            info = directory.lstat()
            if (
                info.st_dev,
                info.st_ino,
                info.st_mode,
                info.st_uid,
                info.st_mtime_ns,
                info.st_ctime_ns,
            ) != signature:
                raise ValueError("original service directory changed during review")
        if any(_signature(p) != signature for p, signature in frozen.items()) or any(
            os.path.lexists(str(p) + suffix)
            for p in database_paths
            for suffix in ("-journal", "-wal", "-shm")
        ):
            raise ValueError("original artifact changed or recovery sidecar requires inspection")

    report = {
        "schema": "oasis.runtime-transition-review.v1",
        "review_passed": False,
        "execution_eligible": False,
        "runtime_approved": False,
        "migration_applied": False,
        "state_write_supported": False,
        "rpc_requests": 0,
        "reviewed_at": now,
        "target_config_hash": sha256_hex(canonical_json(asdict(target))),
        "references_hash": sha256_hex(canonical_json(references)),
        "services": {},
        "limitations": [
            "not_an_application_rearm_or_release_permit",
            "gas_observation_not_all_runtime_or_source_approval",
            "provider_independence_is_declared_by_existing_aliases",
            "current_full_receipt_history_simulation_and_unwind_still_required",
            "no_budget_or_cost_scope_admission",
            "same_block_gas_and_nav_acquisition_must_be_supplied_separately",
            "history_preserving_runtime_epoch_writer_still_required",
        ],
    }
    with startup_locks(directories), ExitStack() as resources:
        unchanged()
        gas = resources.enter_context(closing(ReadOnlyEvidenceStore(gas_evidence)))
        gas.validate_existing_schema()
        header, pins = replay_discovery_observation(
            gas, gas_refs, gas_refs[0].block, sources=sources
        )
        if pins != {name: target.expected_pins[name] for name in GAS_PIN_NAMES}:
            raise ValueError("raw gas observation differs from declared target")
        views, invariants, stores = [], [], []
        deadline = header.ref.timestamp + 1800
        for role, directory in directories.items():
            state_path, evidence_path, _ = paths(directory, role)
            store = StateStore(state_path, require_existing=True)
            state = store.load("live", read_only=True)
            binding, tip = store.journal.config_hash, store.journal.revision
            previous = retained_config(target, binding)
            if set(previous.expected_pins) != LEGACY_PIN_NAMES or not state.halted:
                raise ValueError("halted five-pin original journal required")
            evidence = resources.enter_context(closing(ReadOnlyEvidenceStore(evidence_path)))
            evidence.validate_existing_schema()
            refs = tuple(evidence_ref_from_json(r) for r in references["nav"][role])
            review_checkpoint(target, state, binding, evidence, fresh=refs, now=now)
            value = replay_nav_quorum(evidence, refs, target, sources=sources)
            if value.wallet.header != header:
                raise ValueError("gas and NAV full headers differ")
            capture = replay_nav(evidence, refs[0], target)
            wire_hash = sha256_hex(
                canonical_json(
                    successful_results(capture.exchanges, capture._requests(target, header.ref))
                )
            )
            views.append((value, wire_hash))
            risk = deepcopy(state.exact_risk)
            invariants.append(
                (
                    risk.basis,
                    risk.external,
                    state.gas_spent_wei,
                    state.settled_blocks,
                    _position_identity(state.position),
                )
            )
            risk.observe(
                day=day_of(header.ref.timestamp),
                at=header.ref.timestamp,
                nav=value.nav_micros,
                checkpoint=True,
            )
            deadline = min(deadline, nav_fresh_until(evidence, refs, target, sources=sources))
            report["services"][role] = {
                "journal_tip": tip,
                "source_config_hash": binding,
                "state_hash": sha256_hex(canonical_json(asdict(state))),
                "nav_usd_micros": value.nav_micros,
                "nav_raw_results_hash": wire_hash,
                "risk_preview": asdict(risk),
                "halt_preserved": True,
            }
            stores.append((store, tip, binding))
        if views[0] != views[1] or invariants[0] != invariants[1]:
            raise ValueError("services disagree on current NAV or original capital/history")
        if not header.ref.timestamp <= now <= deadline:
            raise ValueError("runtime transition review is stale")
        for store, tip, binding in stores:
            store.load("live", read_only=True)
            if (store.journal.revision, store.journal.config_hash) != (tip, binding):
                raise ValueError("original journal changed during review")
        unchanged()
        if report["target_config_hash"] != sha256_hex(canonical_json(asdict(target))) or report[
            "references_hash"
        ] != sha256_hex(canonical_json(references)):
            raise ValueError("declared review inputs changed")
        report.update(
            review_passed=True,
            block=asdict(header.ref),
            gas_observed_pins=pins,
            valid_until=deadline,
        )
    report["evidence_hash"] = sha256_hex(canonical_json(report))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for role in ROLES:
        parser.add_argument(f"--{role}-dir", required=True, type=Path)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--references", required=True, type=Path)
    parser.add_argument("--gas-evidence", required=True, type=Path)
    args = parser.parse_args()
    try:
        report = review(
            args.strategy_dir,
            args.authority_dir,
            LiveConfig.from_json(_read(args.config, 65_536)),
            strict_json_object(_read(args.references, 32_768)),
            args.gas_evidence,
            now=int(time.time()),
        )
        if not report["block"]["timestamp"] <= int(time.time()) <= report["valid_until"]:
            raise ValueError("review expired before publication")
    except Exception:  # noqa: BLE001 - never echo private state, paths or parser/provider content
        report = {
            "schema": "oasis.runtime-transition-review.v1",
            "review_passed": False,
            "execution_eligible": False,
            "runtime_approved": False,
            "migration_applied": False,
            "state_write_supported": False,
            "rpc_requests": 0,
            "blockers": ["retained_runtime_transition_inputs_unresolved"],
        }
    print(json.dumps(report, indent=2))
    raise SystemExit(0 if report["review_passed"] else 3)


if __name__ == "__main__":
    main()
