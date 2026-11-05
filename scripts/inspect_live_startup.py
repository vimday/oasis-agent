"""Offline original-state inspection used before production containers are started."""

import argparse
import json
from pathlib import Path

from oasis_agent.live.config import LiveConfig
from oasis_agent.live.rpc_profile import LiveRpcProfile
from oasis_agent.live.startup_state import (
    StartupBlocker,
    StartupRefused,
    inspect_pair,
    refusal_report,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strategy-dir", type=Path, required=True)
    parser.add_argument("--authority-dir", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--rpc-profile", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = inspect_pair(
            args.strategy_dir,
            args.authority_dir,
            LiveConfig.load(args.config),
            LiveRpcProfile.load(args.rpc_profile),
        )
    except StartupRefused as exc:
        result = refusal_report(exc.code, exc.role)
    except Exception:  # noqa: BLE001 - fixed report, no local/provider diagnostics
        result = refusal_report(StartupBlocker.UNRESOLVED)
    print(json.dumps(result))
    raise SystemExit(0 if result["startup_prerequisites_passed"] else 3)


if __name__ == "__main__":
    main()
