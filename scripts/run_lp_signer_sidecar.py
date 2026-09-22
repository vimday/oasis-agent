"""WP7 operator CLI for the isolated LP signer sidecar.

This CLI intentionally has neither a key provider nor an independent account
risk guard, so it can never produce a signature: it runs the static policy and
fee caps and journals a denial recording both missing capabilities. Wiring an
external key authority and a reconciled-account guard are operator actions
outside this repository; even then the sidecar only ever hands the exact
EIP-1559 digest to the provider and verifies the recovered signer.
"""

import argparse
import json
from pathlib import Path

from oasis_agent.execution.lp_signer_sidecar import (
    LpSignerSidecar,
    sidecar_config_from_json,
    sidecar_request_from_json,
)
from oasis_agent.models.common import canonical_json


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--journal", type=Path, default=None)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    journal = args.journal or args.out.with_suffix(".db")
    config = sidecar_config_from_json(args.config.read_bytes(), journal_path=journal)
    request = sidecar_request_from_json(args.request.read_bytes())
    sidecar = LpSignerSidecar(config)
    try:
        decision = sidecar.handle(request)
    finally:
        sidecar.journal.close()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_bytes(
        canonical_json(
            {
                "schema": "oasis.lp-signer-decision.v1",
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
