"""Operator CLI for the isolated digest-signing authority.

Run this as a separate process from the strategy, server or agent process, and
point the sidecar at its socket:

```bash
python -m scripts.run_lp_signer_daemon \
  --socket /Users/you/.oasis/signer.sock --account 0x... \
  --key-env OASIS_CANARY_KEY --audit /Users/you/.oasis/signer-audit.jsonl \
  --max-signatures 50 --ttl-seconds 3600

python scripts/run_lp_signer_sidecar.py --config ... --request ... --out ... \
  --signer-socket /Users/you/.oasis/signer.sock --signer-account 0x...
```

The key is read inside this process only, from one owner-only `0600` file or one
environment variable of this process, and is never printed, journaled, or handed
to any other process. Only digest signatures leave the socket. This CLI never
builds, signs or broadcasts a transaction, and producing a signature is not an
execution permit: the sidecar still requires its static policy, fee caps and an
independent reconciled-account risk guard.
"""

import argparse
import os
from pathlib import Path

from oasis_agent.execution.lp_signer_daemon import (
    SignerLimits,
    SigningAudit,
    load_private_key,
    serve,
)
from oasis_agent.models.common import canonical_json, normalize_address

RUN_SCHEMA = "oasis.digest-signer-run.v1"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", type=Path, required=True)
    parser.add_argument("--account", required=True)
    parser.add_argument("--key-file", type=Path, default=None)
    parser.add_argument("--key-env", default=None)
    parser.add_argument("--audit", type=Path, required=True)
    parser.add_argument("--max-signatures", type=int, default=100)
    parser.add_argument("--ttl-seconds", type=int, default=3600)
    args = parser.parse_args()
    try:
        private_key = load_private_key(
            key_file=args.key_file, key_env=args.key_env, environ=os.environ
        )
        limits = SignerLimits(args.max_signatures, args.ttl_seconds)
        audit = SigningAudit(args.audit)
        account = normalize_address(args.account)
        signed = serve(
            endpoint=args.socket,
            account=account,
            private_key=private_key,
            limits=limits,
            audit=audit,
        )
    except KeyboardInterrupt:
        parser.exit(130, "Signing authority interrupted.\n")
    except (OSError, TypeError, ValueError):
        # Never echo the underlying message: a path or key error can carry material.
        parser.exit(1, "Invalid signing authority configuration; no key material is logged.\n")
    print(canonical_json({"schema": RUN_SCHEMA, "account": account, "signatures": signed}).decode())


if __name__ == "__main__":
    main()
