"""Operator-run full-transaction canary authority. Never run in the strategy process.

The key is loaded only in this process. Configuration, evidence and state directory
must be writable only by the authority's OS identity; give the strategy access to
the private socket through the operator's service confinement, never to the key.
"""

import argparse
import fcntl
import os
import sqlite3
from contextlib import closing
from dataclasses import asdict, dataclass
from pathlib import Path

from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.execution.lp_signer_daemon import (
    address_of,
    load_private_key,
    prepare_endpoint,
    sign_digest,
)
from oasis_agent.live.authority import TransactionAuthority
from oasis_agent.live.authority_socket import serve_connections, serve_status_connections
from oasis_agent.live.budget_rpc import BudgetedRpc
from oasis_agent.live.config import LiveConfig
from oasis_agent.live.recovery import authority_status
from oasis_agent.live.rpc_profile import LiveRpcProfile
from oasis_agent.live.runner import LiveLoop
from oasis_agent.live.state import StateStore
from oasis_agent.models.common import canonical_json, sha256_hex
from oasis_agent.reporting.live_release import require_live_release
from oasis_agent.rpc.budget import RpcBudget
from scripts.run_live_lp import receipt_cost_reader


@dataclass
class _KeyProvider:
    account: str
    key: bytes

    def sign(self, digest: bytes) -> tuple[int, int, int]:
        return sign_digest(self.key, digest)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--socket", type=Path)
    parser.add_argument(
        "--status-only",
        action="store_true",
        help="Serve public pending metadata without key or RPC",
    )
    parser.add_argument("--max-status-requests", type=int, default=12)
    parser.add_argument(
        "--reconcile-only",
        action="store_true",
        help="Reconcile existing pending without key or socket",
    )
    parser.add_argument("--rpc-profile", type=Path)
    parser.add_argument("--key-file", type=Path)
    parser.add_argument("--key-env")
    parser.add_argument("--primary-rpc-env", default="BASE_RPC_PRIMARY_ALT")
    parser.add_argument("--max-signatures", type=int, default=12)
    parser.add_argument("--ttl-seconds", type=int, default=3600)
    args = parser.parse_args()
    if args.status_only:
        if args.reconcile_only or args.key_file is not None or args.key_env is not None:
            parser.error("status-only forbids reconciliation and key options")
        if args.socket is None:
            parser.error("status-only requires --socket")
    elif args.reconcile_only:
        if args.socket is not None or args.key_file is not None or args.key_env is not None:
            parser.error("reconcile-only forbids key and socket options")
    else:
        if args.socket is None:
            parser.error("signing authority requires --socket")
        # New signing always requires release; recovery has no signing capability.
        require_live_release()
    config = LiveConfig.load(args.config)
    profile = LiveRpcProfile.load(args.rpc_profile)
    directory = args.state_dir
    if (
        directory != directory.resolve()
        or not directory.is_dir()
        or directory.stat().st_mode & 0o077
    ):
        parser.exit(3, "Authority state directory must be an existing private directory.\n")
    if directory.stat().st_uid != os.getuid():
        parser.exit(3, "Authority state directory must be owned by this service identity.\n")
    with (directory / "authority.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        store = StateStore(directory / "state.json")
        if (args.status_only or args.reconcile_only) and not store.journal_path.is_file():
            parser.exit(3, "Recovery requires an existing authority journal.\n")
        store.bind(
            sha256_hex(canonical_json(asdict(config))),
            require_existing=args.status_only or args.reconcile_only,
        )
        if args.status_only:

            def public_status():
                return authority_status(store, config)

            with prepare_endpoint(args.socket) as server:
                serve_status_connections(
                    server,
                    public_status,
                    account=config.account,
                    max_requests=args.max_status_requests,
                    ttl_seconds=args.ttl_seconds,
                    lookup=lambda nonce: authority_status(store, config, nonce),
                )
            return
        if args.reconcile_only:
            state = store.load("live")
            if state.signing_attempt is not None:
                parser.exit(3, "Authority signing attempt unresolved; state preserved.\n")
            if state.pending is None:
                print("no pending transaction")
                return
            try:
                with RpcBudget(
                    directory / "live.receipt-budget.db",
                    profile.policies(("live_primary", "live_secondary")),
                    require_existing=True,
                ):
                    pass
            except Exception:  # noqa: BLE001 - fixed diagnostic, preserve original state
                parser.exit(3, "Authority recovery RPC budget unresolved; state preserved.\n")
        primary, secondary = os.environ[args.primary_rpc_env], os.environ[config.secondary_rpc_env]
        with closing(EvidenceStore(directory / "evidence.db")) as evidence:
            loop = LiveLoop.recovery(
                config,
                BudgetedRpc(
                    primary,
                    directory,
                    "live_primary",
                    profile,
                    require_existing_budget=args.reconcile_only,
                ),
                store,
                "live",
            )
            loop.secondary = BudgetedRpc(
                secondary,
                directory,
                "live_secondary",
                profile,
                require_existing_budget=args.reconcile_only,
            )
            loop.stop_file = directory / "STOP"
            loop.receipt_costs = receipt_cost_reader(
                config,
                evidence,
                directory,
                primary,
                secondary,
                profile=profile,
                require_existing_budget=args.reconcile_only,
            )
            if args.reconcile_only:
                try:
                    result = loop._resolve_pending()
                except Exception:  # noqa: BLE001 - provider errors may contain credentials
                    parser.exit(3, "Authority reconciliation unresolved; state preserved.\n")
                store.save(loop.state)
                print(result)
                if loop.state.pending is not None:
                    raise SystemExit(3)
                return
            key = load_private_key(key_file=args.key_file, key_env=args.key_env, environ=os.environ)
            provider = _KeyProvider(address_of(key), key)
            authority = TransactionAuthority(loop, provider, ttl_seconds=args.ttl_seconds)
            with prepare_endpoint(args.socket) as server:
                serve_connections(
                    server,
                    authority,
                    account=config.account,
                    max_signatures=args.max_signatures,
                    ttl_seconds=args.ttl_seconds,
                )


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, KeyError, sqlite3.Error):
        raise SystemExit("Transaction authority refused or stopped; details redacted.") from None
