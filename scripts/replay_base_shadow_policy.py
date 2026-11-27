"""Compare Base shadow policies using normalized, verified same-envelope state observations."""

import argparse
from pathlib import Path

from oasis_agent.arbitrage.cycle_evidence import read_bounded
from oasis_agent.fastpath.base_policy import BasePolicyWindow
from oasis_agent.models.common import canonical_json, sha256_hex


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("observations", type=Path)
    args = parser.parse_args()
    raw = read_bounded(args.observations, 16 * 1024 * 1024)
    result = BasePolicyWindow.model_validate_json(raw).report()
    print(canonical_json({**result, "normalized_input_sha256": sha256_hex(raw)}).decode())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
