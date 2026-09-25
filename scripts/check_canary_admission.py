"""Decide whether one retained shadow window admits a canary cycle. Observe-only.

Reads the `window.json` written by `run_account_risk_shadow.py` together with the
manifest that window observed, re-derives the window record byte for byte, and
reports the gate's refusal reasons if there are any. It reserves no capital,
opens no risk journal, holds no key and cannot sign or broadcast:
`execution_eligible` stays false, and an admitted precondition still requires
capital authorization, a signing policy, simulation and the operator broadcast
gate.

Exit codes: 0 = admitted, 2 = refused, 1 = invalid configuration.
"""

import argparse
import time
from pathlib import Path

from oasis_agent.models.common import canonical_json
from oasis_agent.risk.canary_instructions import usd_micros
from oasis_agent.risk.portfolio_manifest import PortfolioManifest
from oasis_agent.risk.shadow_acceptance import canary_admission, load_shadow_window


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--window", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--cap-usd", required=True, help="declared canary cap, plain decimal")
    parser.add_argument("--now", type=int, help="integer wall clock (default: the system clock)")
    args = parser.parse_args()
    try:
        manifest = PortfolioManifest.model_validate_json(args.manifest.read_bytes())
        if len(manifest.keys) != 1 or len(manifest.chains) != 1:
            raise ValueError("a shadow window describes exactly one declared account")
        window = load_shadow_window(args.window)
        decision = canary_admission(
            window,
            policy=manifest.policy,
            manifest_digest=manifest.digest,
            cap_usd_micros=usd_micros(args.cap_usd),
            now=int(time.time()) if args.now is None else args.now,
        )
    except (OSError, KeyError, TypeError, ValueError):
        parser.exit(1, "Invalid shadow window or manifest; no signing is available.\n")
    result = {
        "schema": "oasis.account-canary-admission.v1",
        "account_key": window.account_key,
        "manifest_digest": manifest.digest,
        "policy_digest": manifest.policy.digest,
        "window_digest": decision.window_digest,
        "cap_usd_micros": decision.cap_usd_micros,
        "runs": len(window.runs),
        "span_seconds": window.span_seconds,
        "first_block": window.first_block,
        "last_block": window.last_block,
        "last_timestamp": window.last_timestamp,
        "cumulative_loss_usd_micros": window.cumulative_loss_usd_micros,
        "admitted": decision.admitted,
        "reasons": list(decision.reasons),
        "execution_eligible": False,
    }
    print(canonical_json(result).decode())
    if not decision.admitted:
        parser.exit(2, "The shadow window does not admit a canary cycle.\n")


if __name__ == "__main__":
    main()
