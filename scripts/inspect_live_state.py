"""Inspect existing strategy/authority journals offline, without migrating or unlocking them.

Use read-only mounts of the original state volumes. No RPC, budget, signing socket
or key is opened. Exit 0 means the inspected histories agree with the supplied
config and have no listed blockers; it is never a live execution permit.
"""

import argparse
import json
import os
import sqlite3
import stat
import time
from contextlib import closing
from dataclasses import asdict
from pathlib import Path

from oasis_agent.live.config import LiveConfig
from oasis_agent.live.journal import LiveJournal
from oasis_agent.live.state import StateStore
from oasis_agent.models.common import canonical_json, sha256_hex
from oasis_agent.reporting.live_pair import PairInspection, read_pair_status
from oasis_agent.reporting.live_status import _read

ROOT = Path(__file__).resolve().parents[1]


def export_pair_status(strategy: Path, authority: Path, config: LiveConfig) -> dict:
    """Export a bounded declaration from unchanged, replayed local histories.

    No state/budget/evidence store is written. File identities span both reads;
    an advancing writer causes refusal rather than a mixed pair report. This is
    a past inspection, not a live lease, raw-RPC replay or execution admission.
    """
    config.validate()
    stores = {
        name: StateStore(path) for name, path in (("strategy", strategy), ("authority", authority))
    }
    files = [path for store in stores.values() for path in (store.path, store.journal_path)]

    def signature(path):
        info = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_nlink != 1
            or info.st_mode & 0o077
            or info.st_size > 64_000_000
        ):
            raise ValueError("private owned bounded original required")
        return tuple(
            getattr(info, key)
            for key in (
                "st_dev",
                "st_ino",
                "st_mode",
                "st_uid",
                "st_size",
                "st_mtime_ns",
                "st_ctime_ns",
            )
        )

    frozen = {path: signature(path) for path in files}
    if len(frozen) != 4 or len({value[:2] for value in frozen.values()}) != 4:
        raise ValueError("distinct original state files and journals required")

    def unchanged():
        if any(signature(path) != value for path, value in frozen.items()):
            raise ValueError("original histories changed during inspection")
        for store in stores.values():
            if any(
                Path(str(store.journal_path) + suffix).exists()
                for suffix in ("-journal", "-wal", "-shm")
            ):
                raise ValueError("journal recovery sidecars require separate inspection")

    unchanged()
    services = {}
    inspected_at = int(time.time())
    for name, store in stores.items():
        # Refuse WAL before SQLite could create or recover a sidecar on a writable mount.
        fd = os.open(store.journal_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as handle:
            header = handle.read(20)
        if header[:16] != b"SQLite format 3\x00" or header[18:20] != b"\x01\x01":
            raise ValueError("original rollback journal format required")
        projection = _read(store.path)
        store._decode(
            projection, "live"
        )  # Reject duplicate/nonfinite fields before canonicalizing.
        projection = canonical_json(json.loads(projection))
        journal = LiveJournal(store.journal_path, require_existing=True)
        # Immutable readers cannot create WAL/SHM or perform hot-journal recovery.
        # All original identities are checked again after both complete replays.
        with closing(
            sqlite3.connect(
                store.journal_path.resolve().as_uri() + "?mode=ro&immutable=1",
                uri=True,
                isolation_level=None,
                timeout=0,
            )
        ) as db:
            db.execute("BEGIN")
            tip, payload = journal._read(db)
            if (
                payload is None
                or db.execute(
                    "SELECT 1 FROM events WHERE payload = ? LIMIT 1", (projection,)
                ).fetchone()
                is None
            ):
                raise ValueError("projection must belong to a nonempty verified journal")
            epochs = []
            if db.execute(
                "SELECT 1 FROM sqlite_master WHERE name='config_transitions' AND type='table'"
            ).fetchone():
                epochs = [
                    json.loads(row[0])["schema"]
                    for row in db.execute(
                        "SELECT payload FROM config_transitions ORDER BY seq LIMIT 3"
                    )
                ]
            state = store._decode(payload, "live")
            position = state.position
            identity = (
                [position.token_id, position.lower, position.upper, position.liquidity]
                if position
                else None
            )
            services[name] = {
                "config_hash": journal.config_hash,
                "journal_tip": tip,
                "state_hash": sha256_hex(payload),
                "projection_hash": sha256_hex(projection),
                "projection_current": projection == payload,
                "runtime_epoch_present": "oasis.runtime-epoch.v1" in epochs,
                "halted": bool(state.halted),
                "has_pending": state.pending is not None,
                "has_signing_attempt": state.signing_attempt is not None,
                "legacy_gas_cost_incomplete": state.legacy_gas_cost_incomplete,
                "position_identity_hash": sha256_hex(canonical_json(identity)),
            }
            db.execute("COMMIT")
    unchanged()
    report = {
        "schema": "oasis.live-pair-inspection.v1",
        "inspected_at": inspected_at,
        "expected_config_hash": sha256_hex(canonical_json(asdict(config))),
        "execution_eligible": False,
        "historical_only": True,
        "rpc_requests": 0,
        "services": services,
    }
    report["evidence_hash"] = sha256_hex(canonical_json(report))
    PairInspection.model_validate(report)
    return report


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
    parser.add_argument(
        "--pair-status-out",
        type=Path,
        help="exclusively create a redacted console inspection artifact",
    )
    args = parser.parse_args()
    passed = False
    try:
        if args.pair_status_out is not None:
            pair_status = export_pair_status(
                args.strategy_state, args.authority_state, LiveConfig.load(args.config)
            )
            with args.pair_status_out.open("x") as handle:
                json.dump(pair_status, handle, indent=2)
            view = read_pair_status(
                args.pair_status_out, startup_config_hash=pair_status["expected_config_hash"]
            )
            report = view.model_dump(by_alias=True)
            passed = view.status == "aligned" and not view.blockers
        else:
            report = inspect(
                args.strategy_state, args.authority_state, LiveConfig.load(args.config)
            )
            passed = report["inspection_passed"] and not report["blockers"]
    except Exception:  # noqa: BLE001 - no raw journal, path or exception in refusal output
        report = {
            "schema": "oasis.live-offline-state-inspection.v1",
            "inspection_passed": False,
            "execution_eligible": False,
            "blockers": ["retained_state_unresolved"],
        }
    print(json.dumps(report, indent=2))
    raise SystemExit(0 if passed else 3)


if __name__ == "__main__":
    main()
