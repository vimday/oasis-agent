"""Keyless worker for one bounded Base start; RPC is available only in capture."""

import argparse
import os
import re
import signal
import stat
import sys
import tarfile
import time
from contextlib import ExitStack, closing
from dataclasses import asdict
from pathlib import Path

from oasis_agent.evidence.rpc_validation import strict_json_object
from oasis_agent.evidence.store import ReadOnlyEvidenceStore
from oasis_agent.live import base_acceptance_rpc as rpc_scope
from oasis_agent.live.chain import ChainError, Rpc
from oasis_agent.live.config import LiveConfig, PublicUsdConfig
from oasis_agent.live.feed_transition import _service_lock
from oasis_agent.live.precheck import check_live
from oasis_agent.live.precheck_evidence import PrecheckEvidenceStore
from oasis_agent.live.receipts import RetainedReceiptCostReader
from oasis_agent.live.rpc_profile import LiveRpcProfile
from oasis_agent.live.runner import LiveLoop
from oasis_agent.live.startup_state import _no_network, inspect_service, paths, require_file
from oasis_agent.live.state import StateStore
from oasis_agent.live.valuation import nav_fresh_until, replay_nav_quorum
from oasis_agent.live.wallet import require_wallet_continuity
from oasis_agent.models.common import canonical_json, evidence_ref_from_json, sha256_hex
from oasis_agent.rpc.budget import BUDGETED_READ_METHODS, RpcBudget
from scripts.capture_live_recovery import (
    PAID_READ_METHODS,
    ROLES,
    RPC_ENV,
    SOURCES,
    BoundedPoster,
    CaptureControl,
)

ROOTS = {role: Path("/") / role for role in ROLES}
PROFILE_REQUESTS = 4096
PRICED_READ_METHODS = frozenset(
    {
        "eth_chainId",
        "eth_blockNumber",
        "eth_getBlockByNumber",
        "eth_getBlockByHash",
        "eth_getCode",
        "eth_getBalance",
        "eth_getTransactionCount",
        "eth_getStorageAt",
        "debug_traceTransaction",
        "eth_getLogs",
        "eth_getTransactionReceipt",
        "eth_getTransactionByHash",
        "eth_call",
        "eth_estimateGas",
    }
)
CAPTURE_LIMITS = {
    "strategy": dict.fromkeys(SOURCES, rpc_scope.CAPTURE_PER_SOURCE),
    "authority": dict.fromkeys(SOURCES, 0),
}
ARCHIVE_FILES = frozenset({"report.json", "wire.jsonl", "fresh-evidence.db"})


def require_policy(config, profile) -> None:
    if (
        not isinstance(config, PublicUsdConfig)
        or config.chain_id != 8453
        or (config.max_capital_usd, config.max_daily_loss_usd, config.max_cumulative_loss_usd)
        != (500, 200, 500)
        or config.oracle_max_age_seconds != 1800
        or config.usdc_oracle_max_age_seconds != 1800
        or profile.max_requests != PROFILE_REQUESTS
        or profile.min_interval_ms != 250
        or not BUDGETED_READ_METHODS | PAID_READ_METHODS <= PRICED_READ_METHODS
        or rpc_scope.COST_UPPER_MICROS > rpc_scope.CEILING_MICROS
    ):
        raise ValueError("approved Base risk and cumulative RPC policy required")


def endpoints(env: dict[str, str]) -> dict[str, str]:
    urls = {source: env[key] for source, key in zip(SOURCES, RPC_ENV, strict=True)}
    for source, url in urls.items():
        rpc_scope.require_endpoint(source, url)
    return urls


def _private_root(path: Path) -> None:
    info = path.lstat()
    if (
        path.resolve() != path.absolute()
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o077
    ):
        raise ValueError("existing private service volume required")


def _marker(path: Path) -> list[int]:
    info = require_file(path)
    if stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 4096:
        raise ValueError("owned private STOP marker required")
    return [info.st_dev, info.st_ino]


def _sync(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _backup(root: Path, attempt: str) -> Path:
    if re.fullmatch(r"[a-f0-9]{32}", attempt) is None:
        raise ValueError("canonical bounded attempt id required")
    return root / f".STOP.bounded-{attempt}"


def _budget(root, profile) -> rpc_scope.LedgerBudget:
    path = root / "live.receipt-budget.db"
    info = require_file(path)
    with RpcBudget(
        path, profile.policies(SOURCES), read_only=True, usage_limit=rpc_scope.LIMITS[root.name]
    ) as quota:
        return {
            "identity": [info.st_dev, info.st_ino],
            "counts": quota.counts(),
            "remaining": quota.remaining_under_limit(),
        }


def budget_readiness(profile, ticks: int, opening_settlements: int) -> dict:
    ledgers = {role: _budget(root, profile) for role, root in ROOTS.items()}
    used = sum(
        rpc_scope.LIMITS[role].additional_requests - ledger["remaining"]
        for role, ledger in ledgers.items()
    )
    required = rpc_scope.run_requests(ticks, opening_settlements, include_capture=True)
    return {
        "ceiling_micros": rpc_scope.CEILING_MICROS,
        "cumulative_reserved_upper_micros": (
            rpc_scope.PREVIOUS_COST_UPPER_MICROS + rpc_scope.PER_REQUEST_UPPER_MICROS * used
        ),
        "cumulative_scope_upper_micros": rpc_scope.COST_UPPER_MICROS,
        "ledgers": ledgers,
        "requested_ticks": ticks,
        "opening_settlement_count": opening_settlements,
        "required_requests": required,
        "enough_for_run": rpc_scope.covers_requests(ledgers, required, profile.max_requests),
    }


def _opening_settlements() -> int:
    counts = {
        len(
            StateStore(paths(root, role)[0], require_existing=True)
            .load("live", read_only=True)
            .settled_blocks
        )
        for role, root in ROOTS.items()
    }
    (count,) = counts
    return count


def _reader(evidence, config, sessions):
    return RetainedReceiptCostReader(
        evidence,
        chain_id=config.chain_id,
        account=config.account,
        native_decimals=config.decimals0,
        sources=SOURCES,
        sessions=sessions,
    )


def _snapshot(root, role, config, profile):
    inspection = inspect_service(root, role, config, profile, expect_stop=True)
    state_path, evidence_path, _ = paths(root, role)
    store = StateStore(state_path, require_existing=True)
    state = store.load("live", read_only=True)
    if store.journal is None or store.journal.revision != inspection["journal_tip"]:
        raise ValueError("original service journal changed")
    with closing(ReadOnlyEvidenceStore(evidence_path)) as evidence:
        loop = LiveLoop.recovery(config, None, store, "live", read_only=True)
        loop.receipt_costs = _reader(evidence, config, _no_network)
        anchor = loop._check_nav_checkpoint()
    if anchor is None:
        raise ValueError("original wallet anchor missing")
    marker = root / "STOP"
    record = {
        "inspection": inspection,
        "state_hash": sha256_hex(canonical_json(asdict(state))),
        "stop_identity": _marker(marker),
        "stop_hash": sha256_hex(marker.read_bytes()),
    }
    return store, state, anchor, record


class _ReadOnlyRpc(Rpc):
    def __init__(self, transport):
        self.transport = transport
        self.nonces = []

    def call(self, method, params):
        try:
            value = self.transport.request(method, params)
        except Exception:  # noqa: BLE001 - provider failures must not echo credentials
            raise ChainError("scoped startup precheck read refused") from None
        if method == "eth_getTransactionCount":
            self.nonces.append(value)
        return value

    def send_raw(self, raw):
        raise ChainError("precheck cannot broadcast")


def _deadline(_signum, _frame):
    raise TimeoutError("startup capture deadline")


def capture(config, profile, plan, out):
    result = {
        "schema": "oasis.base-bounded-fresh.v1",
        "attempt_id": plan["attempt_id"],
        "config_hash": plan["config_hash"],
        "requested_ticks": plan["requested_ticks"],
        "passed": False,
        "execution_eligible": False,
    }
    stage, control = "original_state", None
    try:
        urls = endpoints(os.environ)
        if {source: sha256_hex(url.encode()) for source, url in urls.items()} != plan[
            "endpoint_hashes"
        ]:
            raise ValueError("RPC endpoints changed")
        with ExitStack() as locks:
            checks = {}
            for role, root in ROOTS.items():
                if any(root.glob(".STOP.bounded-*")):
                    raise ValueError("previous STOP handoff unresolved")
                store = StateStore(paths(root, role)[0], require_existing=True)
                checks[role] = locks.enter_context(_service_lock(store, root / "STOP", role))
            snapshots = {
                role: _snapshot(root, role, config, profile) for role, root in ROOTS.items()
            }
            a, b = (snapshots[role][3]["inspection"] for role in ROLES)
            for field in (
                "position_identity_hash",
                "settled_history_hash",
                "financial_continuity_hash",
                "settlement_count",
            ):
                if a[field] != b[field]:
                    raise ValueError("original service financial history differs")
            ticks, opening = plan["requested_ticks"], a["settlement_count"]
            allowance = budget_readiness(profile, ticks, opening)
            if not allowance["enough_for_run"]:
                raise ValueError("insufficient cumulative RPC allowance for bounded run")
            before = allowance["ledgers"]
            result.update(
                original={r: row[3] for r, row in snapshots.items()}, budget_before=before
            )
            stage = "fresh_nav"
            budgets = {r: root / "live.receipt-budget.db" for r, root in ROOTS.items()}
            control = CaptureControl(
                {"request_limits": CAPTURE_LIMITS},
                budgets,
                profile,
                out,
                usage_limits=rpc_scope.LIMITS,
            )
            control.paid = True
            signal.setitimer(signal.ITIMER_REAL, 120)

            def sessions(source):
                return control.session("strategy", source, urls, BoundedPoster)

            primary, secondary = (_ReadOnlyRpc(sessions(source)[0]) for source in SOURCES)
            original_evidence = paths(ROOTS["strategy"], "strategy")[1]
            with closing(PrecheckEvidenceStore(out / "fresh-evidence.db", original_evidence)) as db:
                precheck = check_live(
                    config,
                    snapshots["strategy"][0],
                    primary,
                    secondary,
                    stop_file=ROOTS["strategy"] / "STOP",
                    receipt_costs=_reader(db, config, sessions),
                )
                result["precheck"] = precheck
                if precheck["blockers"] != ["stop_file_present"] or precheck["signer_contacted"]:
                    raise ValueError("fresh operational gate refused")
                refs = tuple(
                    evidence_ref_from_json(ref) for ref in precheck["observation"]["nav_evidence"]
                )
                value = replay_nav_quorum(db, refs, config, sources=SOURCES)
                wallet = value.wallet
                if primary.nonces != secondary.nonces or primary.nonces != [hex(wallet.nonce)] * 2:
                    raise ValueError("current nonce differs from block-pinned wallet")
                for _, state, anchor, _ in snapshots.values():
                    require_wallet_continuity(anchor, wallet)
                    liquidity = state.position.liquidity if state.position else 0
                    daily, cumulative = state.integer_risk().losses(value.nav_micros)
                    if (
                        state.last_units != [wallet.native, wallet.token, liquidity]
                        or not 0 <= daily < 200_000_000
                        or not 0 <= cumulative < 500_000_000
                    ):
                        raise ValueError("inventory or exact losses refused")
                deadline = nav_fresh_until(db, refs, config, sources=SOURCES)
                if time.time() >= deadline - 15:
                    raise ValueError("NAV expires before handoff")
                result.update(
                    nav_micros=value.nav_micros,
                    nav_valid_until=deadline,
                    block=wallet.header.ref.number,
                    block_hash=wallet.header.ref.block_hash,
                    block_timestamp=wallet.header.ref.timestamp,
                )
            control.remaining()
            signal.setitimer(signal.ITIMER_REAL, 0)
            after = {role: _budget(root, profile) for role, root in ROOTS.items()}
            for role, root in ROOTS.items():
                old, new = before[role], after[role]
                if old["identity"] != new["identity"] or any(
                    new["counts"][s] - old["counts"][s] != control.counts[role][s] for s in SOURCES
                ):
                    raise ValueError("original durable RPC usage differs")
                current = _snapshot(root, role, config, profile)[3]
                # RPC use is the only allowed difference.
                current["inspection"]["rpc_remaining"] = result["original"][role]["inspection"][
                    "rpc_remaining"
                ]
                if current != result["original"][role]:
                    raise ValueError("original stopped service changed during capture")
                checks[role]()
            required = rpc_scope.run_requests(ticks, opening, include_capture=False)
            if not rpc_scope.covers_requests(after, required, profile.max_requests):
                raise ValueError("remaining RPC allowance cannot cover bounded runtime")
            result.update(passed=True, budget_after=after)
    except Exception as error:  # noqa: BLE001 - retain only redacted diagnostics
        result.update(failure_stage=stage, error_type=type(error).__name__)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        if control is not None:
            result["request_counts"] = control.counts
            if control.rate_limited_sources:
                result["rpc_rate_limited_sources"] = sorted(control.rate_limited_sources)
            control.cancelled.set()
            control.wire.close()
    result["artifacts"] = {
        path.name: sha256_hex(path.read_bytes())
        for path in out.iterdir()
        if path.name in ARCHIVE_FILES - {"report.json"}
    }
    result["evidence_hash"] = sha256_hex(canonical_json(result))
    (out / "report.json").write_bytes(canonical_json(result))
    with tarfile.open(fileobj=sys.stdout.buffer, mode="w|") as archive:
        for path in sorted(out.iterdir()):
            if path.name in ARCHIVE_FILES:
                archive.add(path, arcname=path.name, recursive=False)
    return 0 if result["passed"] else 3


def open_stops(config, profile, plan, fresh) -> dict:
    proof = dict(fresh)
    digest = proof.pop("evidence_hash")
    if (
        digest != sha256_hex(canonical_json(proof))
        or proof.get("schema") != "oasis.base-bounded-fresh.v1"
        or proof.get("passed") is not True
        or proof.get("config_hash") != plan["config_hash"]
        or proof.get("attempt_id") != plan["attempt_id"]
        or proof.get("requested_ticks") != plan["requested_ticks"]
    ):
        raise ValueError("verified current capture required")
    with ExitStack() as locks:
        checks = {}
        for role, root in ROOTS.items():
            store = StateStore(paths(root, role)[0], require_existing=True)
            checks[role] = locks.enter_context(_service_lock(store, root / "STOP", role))
            row = fresh["original"][role]
            current = _snapshot(root, role, config, profile)[3]
            current["inspection"]["rpc_remaining"] = row["inspection"]["rpc_remaining"]
            if current != row or _budget(root, profile) != fresh["budget_after"][role]:
                raise ValueError("service or budget changed after capture")
        if time.time() >= fresh["nav_valid_until"] - 12:
            raise ValueError("NAV expires before handoff")
        # Keep the original inode recoverable across process death. Both backup
        # links must be durable before the first live marker can be removed.
        for role, root in ROOTS.items():
            checks[role]()
            backup = _backup(root, plan["attempt_id"])
            os.link(root / "STOP", backup, follow_symlinks=False)
            _sync(root)
            # Creating a hard link changes ctime; verify the inode instead.
            if _marker(root / "STOP") != fresh["original"][role]["stop_identity"]:
                raise ValueError("STOP changed before handoff")
        for role, root in ROOTS.items():
            marker, backup = root / "STOP", _backup(root, plan["attempt_id"])
            if (
                _marker(marker) != _marker(backup)
                or sha256_hex(backup.read_bytes()) != fresh["original"][role]["stop_hash"]
            ):
                raise ValueError("STOP changed during handoff")
            marker.unlink()
            _sync(root)
    return {"opened": True, "fresh_hash": digest}


def restore_stops(attempt: str) -> dict[str, bool]:
    restored = dict.fromkeys(ROLES, False)
    for role, root in ROOTS.items():
        try:
            _private_root(root)
            marker, backup = root / "STOP", _backup(root, attempt)
            if os.path.lexists(backup):
                previous = _marker(backup)
                if not os.path.lexists(marker):
                    os.link(backup, marker, follow_symlinks=False)
                if _marker(marker) != previous:
                    raise ValueError("different STOP present; original backup preserved")
                _sync(root)
                backup.unlink()
                _sync(root)
            _marker(marker)
            restored[role] = True
        except OSError, ValueError:
            # Try the other role even if this marker cannot be restored.
            continue
    return restored


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("budget", "capture", "open", "restore"))
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--rpc-profile", type=Path, required=True)
    args = parser.parse_args()
    try:
        if os.getuid() != 10001 or Path("/secrets").exists() or Path("/app/.env").exists():
            raise ValueError("keyless worker confinement required")
        data = strict_json_object(sys.stdin.buffer.read(256_001))
        if args.mode == "restore":
            result = restore_stops(data["attempt_id"])
            print(canonical_json(result).decode())
            raise SystemExit(0 if all(result.values()) else 3)
        config, profile = LiveConfig.load(args.config), LiveRpcProfile.load(args.rpc_profile)
        require_policy(config, profile)
        plan = data["plan"]
        if sha256_hex(canonical_json(asdict(config))) != plan["config_hash"]:
            raise ValueError("capture configuration differs")
        if args.mode == "budget":
            budget = budget_readiness(profile, plan["requested_ticks"], _opening_settlements())
            print(canonical_json(budget).decode())
            return
        if args.mode == "capture":
            signal.signal(signal.SIGALRM, _deadline)
            out = Path("/tmp/base-preflight")  # nosec B108 - dedicated container tmpfs
            out.mkdir(mode=0o700)
            raise SystemExit(capture(config, profile, plan, out))
        print(canonical_json(open_stops(config, profile, plan, data["fresh"])).decode())
    except Exception:  # noqa: BLE001 - no raw provider or local volume errors
        raise SystemExit(3) from None


if __name__ == "__main__":
    main()
