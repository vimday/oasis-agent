"""Keyless, offline admission of existing production state. Never initializes files."""

import fcntl
import os
import stat
from collections.abc import Iterator
from contextlib import ExitStack, closing, contextmanager
from dataclasses import asdict
from enum import StrEnum
from pathlib import Path
from typing import Any, BinaryIO, Literal, NoReturn

from ..evidence.store import ReadOnlyEvidenceStore
from ..models.common import canonical_json, sha256_hex
from ..rpc.budget import RpcBudget
from .config import LiveConfig
from .rearm_history import replay_history
from .receipts import RetainedReceiptCostReader
from .rpc_profile import LiveRpcProfile
from .runner import LiveLoop
from .state import LoopState, Position, StateStore
from .valuation import replay_nav, replay_nav_quorum

SOURCES = ("live_primary", "live_secondary")
STARTUP_LIMITATIONS = (
    "no_current_chain_or_nav_acceptance",
    "no_service_start_or_execution_permit",
    "host_operator_and_original_volume_identity_remain_trusted",
    "journaled_history_only_no_account_transaction_discovery",
)
Role = Literal["strategy", "authority"]


class StartupBlocker(StrEnum):
    UNRESOLVED = "original_runtime_state_unresolved"
    STOP_PRESENT = "stop_present"
    STOP_UNVERIFIED = "stop_missing_or_changed"
    SQLITE_RECOVERY = "sqlite_recovery_required"
    CONFIGURATION_MISMATCH = "configuration_mismatch"
    RECONCILIATION_REQUIRED = "halt_or_reconciliation_unresolved"


class StartupRefused(ValueError):
    def __init__(self, code: StartupBlocker, role: Role, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.role = role


def refusal_report(code: StartupBlocker, role: Role | None = None) -> dict[str, Any]:
    report: dict[str, Any] = {
        "schema": "oasis.live-startup-state.v1",
        "startup_prerequisites_passed": False,
        "execution_eligible": False,
        "historical_only": True,
        "blockers": [code.value],
    }
    if role is not None:
        report["blocked_service"] = role
    return report


def _position_identity(position: Position | None) -> tuple[int, int, int, int] | None:
    return (
        (position.token_id, position.lower, position.upper, position.liquidity)
        if position
        else None
    )


def require_file(path: Path) -> os.stat_result:
    info = path.lstat()
    if (
        path.resolve() != path.absolute()
        or not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o022
    ):
        raise ValueError("existing owned regular runtime file required")
    return info


def paths(directory: Path, role: Role) -> tuple[Path, Path, Path]:
    if role not in {"strategy", "authority"}:
        raise ValueError("explicit service role required")
    return (
        directory / ("live.state.json" if role == "strategy" else "state.json"),
        directory / ("live.receipts.db" if role == "strategy" else "evidence.db"),
        directory / ("live.lock" if role == "strategy" else "authority.lock"),
    )


@contextmanager
def startup_locks(directories: dict[Role, Path]) -> Iterator[None]:
    """Hold both locks shared; refuse service writers, permit concurrent readers."""
    with ExitStack() as resources:
        for role in ("strategy", "authority"):
            lock_path = paths(directories[role], role)[2]
            resources.enter_context(acquire_existing_lock(lock_path, exclusive=False))
        yield


def acquire_existing_lock(path: Path, *, exclusive: bool) -> BinaryIO:
    expected = require_file(path)
    lock = os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb")
    try:
        fcntl.flock(lock, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        actual, current = os.fstat(lock.fileno()), require_file(path)
        if len({(info.st_dev, info.st_ino) for info in (expected, actual, current)}) != 1:
            raise ValueError("service lock changed while opening")
        return lock
    except BaseException:
        lock.close()
        raise


def _no_network(*_: object) -> NoReturn:
    raise ValueError("startup inspection cannot acquire data")


def _require_clean_databases(paths: tuple[Path, ...], role: Role) -> None:
    """Require rollback-format SQLite files before opening a SQLite connection."""
    for path in paths:
        if any(os.path.lexists(str(path) + suffix) for suffix in ("-wal", "-shm", "-journal")):
            raise StartupRefused(
                StartupBlocker.SQLITE_RECOVERY,
                role,
                "SQLite recovery artifacts require separate inspection",
            )
        expected = require_file(path)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as handle:
            opened = os.fstat(handle.fileno())
            if (opened.st_dev, opened.st_ino) != (expected.st_dev, expected.st_ino):
                raise ValueError("SQLite startup file changed while opening")
            header = handle.read(20)
        if header[:16] != b"SQLite format 3\x00" or header[18:20] != b"\x01\x01":
            raise StartupRefused(
                StartupBlocker.SQLITE_RECOVERY, role, "SQLite rollback format required for startup"
            )


def _journal_history(store: StateStore, state: LoopState) -> list[tuple[str, bytes]]:
    """Return only rows verified at the checkpoint's own unchanged journal tip."""
    journal = store.journal
    if journal is None or journal.revision is None:
        raise ValueError("loaded original journal required")
    expected = journal.revision
    with closing(journal._connect(read_only=True)) as db:
        db.execute("BEGIN")
        tip, payload = journal._read(db)
        if (
            tip != expected
            or payload is None
            or canonical_json(asdict(store._decode(payload, "live")))
            != canonical_json(asdict(state))
        ):
            raise ValueError("original checkpoint changed before history replay")
        rows = db.execute("SELECT config, payload FROM events ORDER BY seq").fetchall()
        db.execute("COMMIT")
    return rows


def inspect_service(
    directory: Path,
    role: Role,
    config: LiveConfig,
    profile: LiveRpcProfile,
    *,
    expect_stop: bool = False,
) -> dict[str, Any]:
    """Caller holds the service lock. Reads original evidence without migration.

    Historical checkpoint replay is a prerequisite, not fresh chain acceptance.
    Every live action still requires its independent current on-chain checks.
    """
    config.validate()
    info = directory.lstat()
    if (
        directory.resolve() != directory.absolute()
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o077
    ):
        raise ValueError("existing private owned service directory required")
    state_path, evidence_path, lock_path = paths(directory, role)
    store = StateStore(state_path, require_existing=True)
    for path in (state_path, lock_path):
        require_file(path)
    budget_path = directory / "live.receipt-budget.db"
    if expect_stop:
        require_file(directory / "STOP")
    elif os.path.lexists(directory / "STOP"):
        raise StartupRefused(
            StartupBlocker.STOP_PRESENT, role, "service STOP must be resolved before startup"
        )
    databases = (store.journal_path, evidence_path, budget_path)
    _require_clean_databases(databases, role)
    state = store.load("live", read_only=True)
    expected = sha256_hex(canonical_json(asdict(config)))
    if store.journal is None or store.journal.config_hash != expected:
        raise StartupRefused(
            StartupBlocker.CONFIGURATION_MISMATCH, role, "service configuration migration required"
        )
    if (
        state.halted
        or state.pending is not None
        or state.signing_attempt is not None
        or state.acknowledge_flow
        or state.legacy_gas_cost_incomplete
    ):
        raise StartupRefused(
            StartupBlocker.RECONCILIATION_REQUIRED,
            role,
            "service halt or reconciliation unresolved",
        )
    risk = state.exact_risk
    if risk is None or risk.basis is None or risk.last_nav is None or state.last_units is None:
        raise ValueError("existing complete integer risk baseline required")
    daily, cumulative = risk.losses(risk.last_nav)
    if (
        daily >= config.max_daily_loss_usd * 1_000_000
        or cumulative >= config.max_cumulative_loss_usd * 1_000_000
    ):
        raise ValueError("existing service loss budget exhausted")
    policies = profile.policies(SOURCES)
    with RpcBudget(budget_path, policies, read_only=True) as budget:
        counts = budget.counts()
        if any(not 0 < counts[p.source] < p.max_requests for p in policies):
            raise ValueError("original nonempty unexhausted RPC history required")
    with closing(ReadOnlyEvidenceStore(evidence_path)) as evidence:
        evidence.validate_existing_schema()
        loop = object.__new__(LiveLoop)
        loop.mode, loop.c, loop.state = "live", config, state
        loop.receipt_costs = RetainedReceiptCostReader(
            evidence,
            chain_id=config.chain_id,
            account=config.account,
            native_decimals=config.decimals0,
            sources=SOURCES,
            sessions=_no_network,
        )
        anchor = loop._check_nav_checkpoint()
        if anchor is None:
            raise ValueError("verified original wallet anchor required")
        if state.nav_evidence:
            value = replay_nav_quorum(evidence, state.nav_evidence, config, sources=SOURCES)
            position = replay_nav(evidence, state.nav_evidence[0], config).position

            if _position_identity(position) != _position_identity(
                state.position
            ) or state.last_units != [
                value.wallet.native,
                value.wallet.token,
                position.liquidity if position else 0,
            ]:
                raise ValueError("retained inventory differs from checkpoint")
        rows = _journal_history(store, state)
        try:
            historical = replay_history(
                rows,
                state,
                config,
                evidence,
                SOURCES,
                current_nonce=anchor.nonce,
            )
        except Exception:  # noqa: BLE001 - retained wire diagnostics can contain provider data
            raise ValueError("retained complete settlement history cannot be replayed") from None
        history_hash = sha256_hex(canonical_json(historical))
        continuity_hash = sha256_hex(
            canonical_json(
                {
                    "schema": "oasis.startup-financial-continuity.v1",
                    "config_hash": expected,
                    "basis": risk.basis,
                    "external": risk.external,
                    "native_gas_units": state.gas_spent_wei,
                    "settled_history_hash": history_hash,
                    "wallet_nonce": anchor.nonce,
                    "last_units": state.last_units,
                    "position": _position_identity(state.position),
                }
            )
        )
    _require_clean_databases(databases, role)
    return {
        "config_hash": expected,
        "journal_tip": store.journal.revision,
        "position_identity_hash": sha256_hex(
            canonical_json(
                [
                    state.position.token_id,
                    state.position.lower,
                    state.position.upper,
                    state.position.liquidity,
                ]
                if state.position
                else None
            )
        ),
        "rpc_remaining": {p.source: p.max_requests - counts[p.source] for p in policies},
        "checkpoint_verified": True,
        "settled_history_replayed": True,
        "settlement_count": len(historical),
        "settled_history_hash": history_hash,
        "financial_continuity_hash": continuity_hash,
    }


def _stopped_marker(directory: Path, role: Role) -> os.stat_result:
    try:
        marker = require_file(directory / "STOP")
        if stat.S_IMODE(marker.st_mode) == 0o600 and marker.st_size <= 4096:
            return marker
    except OSError, ValueError:
        pass
    raise StartupRefused(StartupBlocker.STOP_UNVERIFIED, role, "private STOP required")


def inspect_pair(
    strategy: Path,
    authority: Path,
    config: LiveConfig,
    profile: LiveRpcProfile,
    *,
    expect_stop: bool = False,
) -> dict[str, Any]:
    """Replay the two original histories; stopped mode also binds both STOP markers."""
    if strategy.samefile(authority):
        raise ValueError("distinct original state volumes required")
    directories: dict[Role, Path] = {"strategy": strategy, "authority": authority}
    with startup_locks(directories):
        stops = (
            {role: _stopped_marker(directory, role) for role, directory in directories.items()}
            if expect_stop
            else {}
        )
        identities = set()
        for role, directory in directories.items():
            state_path, evidence_path, _ = paths(directory, role)
            for path in (
                state_path,
                StateStore(state_path).journal_path,
                evidence_path,
                directory / "live.receipt-budget.db",
            ):
                info = require_file(path)
                identity = (info.st_dev, info.st_ino)
                if identity in identities:
                    raise ValueError("distinct original service files required")
                identities.add(identity)
        services = {
            role: inspect_service(directory, role, config, profile, expect_stop=expect_stop)
            for role, directory in directories.items()
        }
        if (
            services["strategy"]["position_identity_hash"]
            != services["authority"]["position_identity_hash"]
        ):
            raise ValueError("service positions disagree")
        for field in ("settlement_count", "settled_history_hash", "financial_continuity_hash"):
            if services["strategy"][field] != services["authority"][field]:
                raise ValueError("services disagree on original capital or settled history")
        for role, marker in stops.items():
            if _stopped_marker(directories[role], role) != marker:
                raise StartupRefused(StartupBlocker.STOP_UNVERIFIED, role, "STOP changed")
    return {
        **({"stop_markers_present": True} if expect_stop else {}),
        "schema": "oasis.live-startup-state.v1",
        "startup_prerequisites_passed": True,
        "execution_eligible": False,
        "historical_only": True,
        "limitations": list(STARTUP_LIMITATIONS),
        "services": services,
    }
