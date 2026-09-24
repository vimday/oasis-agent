"""Operator CLI: verify or broadcast one sidecar-signed LP transaction.

The endpoint URL is read from an environment variable by name, never from a
file, so no credential is written down here. Without ``--broadcast`` this tool
only decodes, verifies, and reports: it opens no endpoint, writes no journal,
and sends nothing. With ``--broadcast`` every attempt is journaled append-only.

Exit codes: 0 = authorized or broadcast, 2 = denied, 3 = outcome unknown
(the transaction may have landed; reconcile it by receipt, never by re-sending).
"""

import argparse
import json
import os
from pathlib import Path
from typing import Any

from oasis_agent.execution.lp_broadcast import (
    BroadcastPolicy,
    HttpBroadcastRpc,
    LpBroadcaster,
    authorize_broadcast,
    broadcast_policy_from_json,
    decode_signed_transaction,
    expectation_from_sidecar,
    raw_bytes_from_decision,
)
from oasis_agent.execution.lp_signer_sidecar import (
    sidecar_decision_from_json,
    sidecar_request_from_json,
)
from oasis_agent.models.common import canonical_json

DECISION_SCHEMA = "oasis.lp-broadcast-decision.v1"


def _report(
    out: Path,
    *,
    mode: str,
    outcome: str,
    reasons: tuple[str, ...],
    request_hash: str | None,
    transaction_hash: str | None,
    signer: str | None,
) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(
        canonical_json(
            {
                "schema": DECISION_SCHEMA,
                "mode": mode,
                "outcome": outcome,
                "reasons": list(reasons),
                "request_hash": request_hash,
                "transaction_hash": transaction_hash,
                "signer": signer,
            }
        )
        + b"\n"
    )


def _refuse(out: Path, *, mode: str, reason: str) -> int:
    _report(
        out,
        mode=mode,
        outcome="denied",
        reasons=(reason,),
        request_hash=None,
        transaction_hash=None,
        signer=None,
    )
    print(json.dumps({"outcome": "denied", "reasons": [reason]}))
    return 2


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--decision", type=Path, required=True)
    parser.add_argument("--journal", type=Path, default=None)
    parser.add_argument("--endpoint-env", default="OASIS_LP_BROADCAST_RPC")
    parser.add_argument("--broadcast", action="store_true")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    mode = "broadcast" if args.broadcast else "verify"

    policy: BroadcastPolicy = broadcast_policy_from_json(args.policy.read_bytes())
    request = sidecar_request_from_json(args.request.read_bytes())
    decision = sidecar_decision_from_json(args.decision.read_bytes())
    try:
        expectation = expectation_from_sidecar(request, decision)
    except (TypeError, ValueError) as exc:
        raise SystemExit(
            _refuse(args.out, mode=mode, reason=f"sidecar decision is not broadcastable: {exc}")
        ) from None

    try:
        transaction = decode_signed_transaction(raw_bytes_from_decision(decision))
    except (TypeError, ValueError) as exc:
        raise SystemExit(
            _refuse(args.out, mode=mode, reason=f"raw transaction is refused: {exc}")
        ) from None

    if not args.broadcast:
        authorization = authorize_broadcast(policy, expectation, transaction)
        outcome = "authorized" if authorization.authorized else "denied"
        _report(
            args.out,
            mode=mode,
            outcome=outcome,
            reasons=authorization.reasons,
            request_hash=expectation.request_hash,
            transaction_hash=expectation.transaction_hash,
            signer=transaction.signer,
        )
        print(json.dumps({"outcome": outcome, "reasons": list(authorization.reasons)}))
        raise SystemExit(0 if authorization.authorized else 2)

    endpoint = os.environ.get(args.endpoint_env)
    if not endpoint:
        raise SystemExit(
            _refuse(
                args.out,
                mode=mode,
                reason=f"environment variable {args.endpoint_env} is not set",
            )
        )
    journal = args.journal or args.out.with_suffix(".db")
    broadcaster = LpBroadcaster(policy, journal, rpc=HttpBroadcastRpc(endpoint))
    try:
        broadcast = broadcaster.broadcast(transaction.raw, expectation=expectation)
    finally:
        broadcaster.journal.close()
    report: dict[str, Any] = {
        "outcome": broadcast.outcome,
        "reasons": list(broadcast.reasons),
    }
    _report(
        args.out,
        mode=mode,
        outcome=broadcast.outcome,
        reasons=broadcast.reasons,
        request_hash=broadcast.request_hash,
        transaction_hash=broadcast.transaction_hash,
        signer=broadcast.signer,
    )
    print(json.dumps(report))
    raise SystemExit({"broadcast": 0, "denied": 2, "unresolved": 3}[broadcast.outcome])


if __name__ == "__main__":
    main()
