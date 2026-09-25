"""WP7 operator CLI for the isolated LP signer sidecar.

This CLI holds no key material. With no `--signer-socket` it runs the static
policy and fee caps and journals a denial recording that no key authority is
wired. With a socket it asks a *separate* signing authority
(`scripts/run_lp_signer_daemon.py`) for the exact EIP-1559 digest only, then
verifies the signature by recovering the signer address; the authority can be an
owner-only local process, an HSM, or a remote service.

An independent reconciled-account risk guard is still not configurable here, so
the sidecar denies even with a key authority wired: a live signature needs an
operator-supplied guard that reconciles complete inventory and durably reserves
capital first. Producing a signature is never an execution permit, and this CLI
never broadcasts.
"""

import argparse
import json
from pathlib import Path

from oasis_agent.execution.lp_signer_sidecar import (
    SIDE_CAR_DECISION_SCHEMA,
    LpSignerSidecar,
    sidecar_config_from_json,
    sidecar_request_from_json,
)
from oasis_agent.execution.socket_key_provider import SocketKeyProvider
from oasis_agent.models.common import canonical_json


def _key_authority(args: argparse.Namespace) -> SocketKeyProvider | None:
    if args.signer_socket is None and args.signer_account is None:
        return None
    if args.signer_socket is None or args.signer_account is None:
        raise ValueError("a signer socket needs its account")
    return SocketKeyProvider(
        args.signer_socket, args.signer_account, timeout_ms=args.signer_timeout_ms
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--journal", type=Path, default=None)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--signer-socket", type=Path, default=None)
    parser.add_argument("--signer-account", default=None)
    parser.add_argument("--signer-timeout-ms", type=int, default=3000)
    args = parser.parse_args()

    journal = args.journal or args.out.with_suffix(".db")
    try:
        config = sidecar_config_from_json(
            args.config.read_bytes(), journal_path=journal, provider=_key_authority(args)
        )
        request = sidecar_request_from_json(args.request.read_bytes())
    except (OSError, TypeError, ValueError):
        parser.exit(1, "Invalid sidecar configuration or request; no key material is logged.\n")
    sidecar = LpSignerSidecar(config)
    try:
        decision = sidecar.handle(request)
    finally:
        sidecar.journal.close()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_bytes(
        canonical_json(
            {
                "schema": SIDE_CAR_DECISION_SCHEMA,
                "outcome": decision.outcome,
                "reasons": list(decision.reasons),
                "request_hash": decision.request_hash,
                "digest": decision.digest,
                "signature_hash": decision.signature_hash,
                "transaction_hash": decision.transaction_hash,
                "raw_transaction_hex": decision.raw_transaction_hex,
            }
        )
        + b"\n"
    )
    print(json.dumps({"outcome": decision.outcome, "reasons": list(decision.reasons)}))


if __name__ == "__main__":
    main()
