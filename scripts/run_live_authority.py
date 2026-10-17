"""Operator-run full-transaction canary authority. Never run in the strategy process.

The key is loaded only in this process. Configuration, evidence and state directory
must be writable only by the authority's OS identity; give the strategy access to
the private socket through the operator's service confinement, never to the key.
"""

import argparse
import fcntl
import os
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.execution.lp_signer_daemon import (
    address_of,
    load_private_key,
    prepare_endpoint,
    sign_digest,
)
from oasis_agent.live.authority import TransactionAuthority
from oasis_agent.live.authority_socket import serve_connections
from oasis_agent.live.budget_rpc import BudgetedRpc
from oasis_agent.live.config import LiveConfig
from oasis_agent.live.rpc_profile import LiveRpcProfile
from oasis_agent.live.runner import LiveLoop
from oasis_agent.live.state import StateStore
from oasis_agent.reporting.live_release import require_live_release
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
    parser.add_argument("--socket", type=Path, required=True)
    parser.add_argument("--rpc-profile", type=Path)
    parser.add_argument("--key-file", type=Path)
    parser.add_argument("--key-env")
    parser.add_argument("--primary-rpc-env", default="BASE_RPC_PRIMARY_ALT")
    parser.add_argument("--max-signatures", type=int, default=12)
    parser.add_argument("--ttl-seconds", type=int, default=3600)
    args = parser.parse_args()
    # No CLI switch bypasses unfinished release controls; refusal precedes key loading.
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
        primary, secondary = os.environ[args.primary_rpc_env], os.environ[config.secondary_rpc_env]
        with closing(EvidenceStore(directory / "evidence.db")) as evidence:
            loop = LiveLoop.recovery(
                config,
                BudgetedRpc(primary, directory, "live_primary", profile),
                StateStore(directory / "state.json"),
                "live",
            )
            loop.secondary = BudgetedRpc(secondary, directory, "live_secondary", profile)
            loop.stop_file = directory / "STOP"
            loop.receipt_costs = receipt_cost_reader(
                config,
                evidence,
                directory,
                primary,
                secondary,
                profile=profile,
            )
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
    except (OSError, ValueError, RuntimeError, KeyError):
        raise SystemExit("Transaction authority refused or stopped; details redacted.") from None
