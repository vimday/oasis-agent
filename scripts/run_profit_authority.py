"""Operator-run confined profit-engine signing authority CLI.

Holds at most one dedicated key file in signing mode, enforces `AtomicExecutionPolicy`
before any key access, persists state atomically with `0600` permissions, and never
prints private keys or raw signed transactions.
"""

import argparse
import fcntl
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from oasis_agent.execution.lp_signer_daemon import address_of, load_private_key
from oasis_agent.live.atomic_authority_socket import (
    ProfitSigningAuthorityServer,
    build_authority_infra_proof,
    load_atomic_execution_policy,
    make_local_key_signer,
    strict_json_loads,
)
from oasis_agent.live.shutdown import run_with_term_handler
from oasis_agent.reporting.live_release import require_live_start
from oasis_agent.risk.revert_budget import RollingRevertBudgetTracker


def build_parser() -> argparse.ArgumentParser:
    """Build strict CLI parser for the isolated profit-engine authority."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy-config", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--socket", type=Path, default=None)
    parser.add_argument("--key-file", type=Path, default=None)
    parser.add_argument(
        "--status-only",
        action="store_true",
        help="Serve read-only operational status without loading any key.",
    )
    parser.add_argument(
        "--reconcile-only",
        action="store_true",
        help="Inspect or reconcile persisted state without loading a key or opening a socket.",
    )
    parser.add_argument("--max-signatures", type=int, default=12)
    parser.add_argument("--ttl-seconds", type=int, default=3600)
    parser.add_argument("--max-status-requests", type=int, default=None)
    parser.add_argument("--max-sign-requests", type=int, default=None)
    parser.add_argument("--out-proof", type=Path, default=None)
    parser.add_argument("--settlement-input", type=Path, default=None)
    parser.add_argument("--key-volume", default=None)
    parser.add_argument("--primary-rpc-provider", default=None)
    parser.add_argument("--secondary-rpc-provider", default=None)
    parser.add_argument("--secondary-rpc-verified", action="store_true")
    parser.add_argument("--allow-rehearsal-loopback-anvil", action="store_true")
    return parser


def _write_proof_once(path: Path, proof: dict[str, object]) -> None:
    if path.is_symlink() or path.exists():
        raise ValueError("refusing to overwrite existing infra proof output")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(proof, handle, indent=2, sort_keys=True)
        handle.write("\n")


def _validate_state_dir(directory: Path) -> None:
    if (
        directory.is_symlink()
        or directory != directory.resolve()
        or not directory.is_dir()
        or directory.stat().st_mode & 0o077
    ):
        raise ValueError("authority state directory must be an existing private 0700 directory")
    if directory.stat().st_uid != os.getuid():
        raise ValueError("authority state directory must be owned by this service identity")


def main(argv: Sequence[str] | None = None) -> int:
    """Run the isolated profit-engine authority daemon or recovery/status command."""
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.status_only and args.reconcile_only:
        parser.error("--status-only and --reconcile-only are mutually exclusive")
    if (args.status_only or args.reconcile_only) and args.key_file is not None:
        parser.error("--key-file is forbidden with --status-only or --reconcile-only")
    if args.status_only:
        if args.socket is None:
            parser.error("--status-only requires --socket")
        if args.settlement_input is not None:
            parser.error("--settlement-input requires --reconcile-only")
    elif args.reconcile_only:
        if args.socket is not None:
            parser.error("--reconcile-only forbids --socket")
        if args.out_proof is not None:
            parser.error("--reconcile-only forbids --out-proof")
    else:
        if args.socket is None or args.key_file is None:
            parser.error("signing mode requires both --socket and --key-file")
        if args.settlement_input is not None:
            parser.error("--settlement-input requires --reconcile-only")
        if not args.allow_rehearsal_loopback_anvil:
            require_live_start()

    if args.out_proof is not None and (args.out_proof.is_symlink() or args.out_proof.exists()):
        parser.error("--out-proof already exists or is a symlink")

    loaded = load_atomic_execution_policy(args.policy_config)
    _validate_state_dir(args.state_dir)
    if args.allow_rehearsal_loopback_anvil and not loaded.policy.rehearsal_only:
        raise ValueError(
            "--allow-rehearsal-loopback-anvil requires a rehearsal_only "
            "policy without a live release"
        )

    lock_path = args.state_dir / "profit_authority.lock"
    if lock_path.is_symlink():
        raise ValueError("authority lock file cannot be a symlink")

    with lock_path.open("a", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        tracker = RollingRevertBudgetTracker(loaded.revert_policy)

        if args.reconcile_only:
            server = ProfitSigningAuthorityServer(
                policy=loaded.policy,
                state_dir=args.state_dir,
                endpoint=None,
                key_signer=None,
                revert_tracker=tracker,
                max_signatures=args.max_signatures,
                ttl_seconds=args.ttl_seconds,
                require_existing_state=True,
                status_only=False,
            )
            if args.settlement_input is not None:
                if args.settlement_input.is_symlink() or not args.settlement_input.is_file():
                    raise ValueError("--settlement-input must be a regular non-symlink file")
                payload = strict_json_loads(args.settlement_input.read_bytes())
                if not isinstance(payload, dict):
                    raise ValueError("settlement input must be a JSON object")
                summary = server.reconcile_settlement(payload)
                print(json.dumps(summary, sort_keys=True), flush=True)
                return 3 if summary["halted"] else 0
            status = server.current_status()
            print(json.dumps(status, sort_keys=True), flush=True)
            return 3 if status["halted"] or status["signing_unresolved"] else 0

        key_signer = None
        if not args.status_only:
            private_key = load_private_key(key_file=args.key_file, key_env=None, environ={})
            if address_of(private_key) != loaded.policy.operator_account:
                raise ValueError("configured key does not control policy.operator_account")
            key_signer = make_local_key_signer(private_key, loaded.policy.operator_account)

        max_status_requests = (
            12
            if args.status_only and args.max_status_requests is None
            else args.max_status_requests
        )

        with ProfitSigningAuthorityServer(
            policy=loaded.policy,
            state_dir=args.state_dir,
            endpoint=args.socket,
            key_signer=key_signer,
            revert_tracker=tracker,
            max_signatures=args.max_signatures,
            ttl_seconds=args.ttl_seconds,
            allow_rehearsal_loopback_anvil=args.allow_rehearsal_loopback_anvil,
            status_only=args.status_only,
        ) as server:
            if args.out_proof is not None:
                key_volume = args.key_volume or loaded.isolated_key_volume or ""
                primary_rpc = args.primary_rpc_provider or loaded.primary_rpc_provider or ""
                secondary_rpc = args.secondary_rpc_provider or loaded.secondary_rpc_provider or ""
                secondary_verified = args.secondary_rpc_verified or loaded.secondary_rpc_verified
                proof = build_authority_infra_proof(
                    socket_path=args.socket,
                    account=loaded.policy.operator_account,
                    isolated_key_volume=key_volume,
                    primary_rpc_provider=primary_rpc,
                    secondary_rpc_provider=secondary_rpc,
                    secondary_rpc_verified=secondary_verified,
                    authority_status=server.current_status(),
                )
                _write_proof_once(args.out_proof, proof)

            mode_label = "status" if args.status_only else "signing"
            print(f"oasis profit authority ready ({mode_label})", flush=True)
            server.serve(
                max_status_requests=max_status_requests,
                max_sign_requests=args.max_sign_requests,
            )
    return 0


def _cli_entry() -> None:
    exit_code = main()
    if exit_code != 0:
        raise SystemExit(exit_code)


if __name__ == "__main__":
    try:
        completed = run_with_term_handler(_cli_entry)
    except (OSError, ValueError, RuntimeError, KeyError, TypeError):
        print("Profit authority refused or stopped; details redacted.", file=sys.stderr)
        raise SystemExit(3) from None
    if not completed:
        raise SystemExit(0)
