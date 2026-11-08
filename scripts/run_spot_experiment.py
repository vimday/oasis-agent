"""Offline smart-wallet-bonus experiment over frozen spot decisions (research only).

Usage:
  python -m scripts.run_spot_experiment --run RUN_DIR --start T0 --end T1 \
      --label-cutoff T2 --evidence {synthetic,provider_claims,unknown} --out REPORT.json

Decisions come from replaying RUN_DIR/capture.db (the production SpotAgent, offline),
never from a provider, wallet or executor. Only a complete session is accepted unless
--allow-prefix is given, in which case the report says it covers a prefix. Writes a new
file outside the run directory; exits 2 with a fixed reason when the inputs are refused.
The comparator never feeds execution: nothing here can select a trade.

Reproducible synthetic example: tests/unit/test_spot_experiment.py::
test_cli_runs_offline_and_refuses_a_prefix runs the real shadow run_loop over scripted
provider fixtures, replays the capture and runs this command; its report is labelled
`evidence_source: synthetic` and validates the tool only.

Evidence still missing before any real-market conclusion:
- a real shadow capture (provider claims) over a pre-declared window, complete session;
- enough independent tokens per group (the report counts them; nothing is assumed);
- executable exit evidence: fills, fees, gas, slippage and depth are all unobserved;
- an untouched later window for the same fixed comparator (no fitting happens here);
- ablation of the signal source itself (this measures only the score term).
"""

import argparse
import json
import sys
from pathlib import Path

from oasis_agent.launch.spot_agent import AgentConfig
from oasis_agent.launch.spot_capture import ReplayRefused, replay_session
from oasis_agent.launch.spot_experiment import ExperimentRefused, run_experiment
from oasis_agent.models.common import canonical_json


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--session")
    parser.add_argument("--start", type=int, required=True, help="window start, unix s")
    parser.add_argument("--end", type=int, required=True, help="window end (exclusive)")
    parser.add_argument("--label-cutoff", type=int, required=True)
    parser.add_argument(
        "--evidence", choices=("synthetic", "provider_claims", "unknown"), required=True
    )
    parser.add_argument("--allow-prefix", action="store_true")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.out.resolve().is_relative_to(args.run.resolve()):
        parser.error("--out must be outside the run directory (it holds original evidence)")
    try:
        replay = replay_session(args.run / "capture.db", args.run / "raw", args.session)
        if not replay["session_complete"] and not args.allow_prefix:
            raise ExperimentRefused("capture_is_prefix_only")
        report = run_experiment(
            replay["records"],
            AgentConfig.model_validate(replay["config"]),
            start=args.start,
            end=args.end,
            label_cutoff=args.label_cutoff,
            evidence=args.evidence,
            inputs={
                "capture_session_id": replay["session_id"],
                "capture_archive_head_digest": replay["archive_head_digest"],
                "code": replay["code"],
                "run_policy_sha256": replay["run_policy_sha256"],
                "session_complete": replay["session_complete"],
                "termination": replay["termination"],
                "cycles_replayed": replay["cycles_replayed"],
            },
        )
    except (ReplayRefused, ExperimentRefused) as refused:
        print(f"experiment refused: {refused.category}", file=sys.stderr)
        return 2
    with args.out.open("xb") as handle:
        handle.write(canonical_json(report) + b"\n")
    print(json.dumps({"report_sha256": report["report_sha256"], **report["population"]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
