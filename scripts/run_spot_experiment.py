"""Offline smart-wallet-bonus experiment over frozen spot decisions (research only).

Usage, strict pre-registered acceptance (the plan was bound into the capture before
its first provider call by `run_spot_agent --plan PLAN.json`):
  python -m scripts.run_spot_experiment --run RUN_DIR --plan PLAN.json \
      --evidence {synthetic,provider_claims,unknown} --out ACCEPTANCE.json

Usage, exploration of a session started without a plan (window chosen at analysis
time, so the report says `window_basis: analysis_time_arguments`):
  python -m scripts.run_spot_experiment --run RUN_DIR --start T0 --end T1 \
      --label-cutoff T2 --evidence {synthetic,provider_claims,unknown} --out REPORT.json

Decisions come from replaying RUN_DIR/capture.db (the production SpotAgent, offline),
never from a provider, wallet or executor. With --plan the window comes only from the
plan bound to the selected session: window arguments, --allow-prefix, another plan,
another session's plan and a prefix are refused, and the written acceptance record says
`accepted` or `blocked` with every observation gap (see spot_plan). Exploration accepts
only a complete session unless --allow-prefix is given, and never a session that has a
bound plan. Writes a new file outside the run directory; exits 2 with a fixed reason
when the inputs are refused, 3 when a plan's acceptance is blocked (the record is still
written). The comparator never feeds execution: nothing here can select a trade.

Reproducible synthetic examples: tests/unit/test_spot_plan.py runs the real shadow
run_loop over scripted provider fixtures with a frozen plan (accepted, and the refused
and blocked counterexamples); tests/unit/test_spot_experiment.py::
test_cli_runs_offline_and_refuses_a_prefix covers exploration. Their reports are
labelled `evidence_source: synthetic` and validate the tool only.

Evidence still missing before any real-market conclusion:
- a real shadow capture (provider claims) under a plan frozen before its window;
- an independent time proof of that plan (the archive only orders it locally);
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
from oasis_agent.launch.spot_experiment import ExperimentRefused, replay_inputs, run_experiment
from oasis_agent.launch.spot_plan import PlanRefused, accept_plan, read_plan, require_unplanned
from oasis_agent.models.common import canonical_json

BLOCKED = 3


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--session")
    parser.add_argument("--plan", type=Path, help="frozen plan file: strict acceptance mode")
    parser.add_argument("--start", type=int, help="window start, unix s (exploration only)")
    parser.add_argument("--end", type=int, help="window end, exclusive (exploration only)")
    parser.add_argument("--label-cutoff", type=int, help="exploration only")
    parser.add_argument(
        "--evidence", choices=("synthetic", "provider_claims", "unknown"), required=True
    )
    parser.add_argument("--allow-prefix", action="store_true", help="exploration only")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    window = (args.start, args.end, args.label_cutoff)
    if args.plan is not None:
        if any(v is not None for v in window) or args.allow_prefix:
            # The plan's own window is the only one acceptance can use.
            parser.error("--plan takes its window from the plan; window arguments are refused")
    elif any(v is None for v in window):
        parser.error("--start, --end and --label-cutoff are required without --plan")
    if args.out.resolve().is_relative_to(args.run.resolve()):
        parser.error("--out must be outside the run directory (it holds original evidence)")
    archive, raw = args.run / "capture.db", args.run / "raw"
    try:
        if args.plan is not None:
            document = accept_plan(
                archive,
                raw,
                read_plan(args.plan),
                session_id=args.session,
                evidence=args.evidence,
            )
        else:
            replay = replay_session(archive, raw, args.session)
            require_unplanned(replay)
            if not replay["session_complete"] and not args.allow_prefix:
                raise ExperimentRefused("capture_is_prefix_only")
            document = run_experiment(
                replay["records"],
                AgentConfig.model_validate(replay["config"]),
                start=args.start,
                end=args.end,
                label_cutoff=args.label_cutoff,
                evidence=args.evidence,
                inputs=replay_inputs(replay),
            )
    except (ReplayRefused, ExperimentRefused, PlanRefused) as refused:
        print(f"experiment refused: {refused.category}", file=sys.stderr)
        return 2
    with args.out.open("xb") as handle:
        handle.write(canonical_json(document) + b"\n")
    if args.plan is not None:
        summary = {
            "acceptance_sha256": document["acceptance_sha256"],
            "status": document["status"],
            "blocking": document["blocking"],
            "plan_sha256": document["plan_sha256"],
            "session_id": document["session"]["session_id"],
        }
        print(json.dumps(summary, sort_keys=True))
        return BLOCKED if document["status"] == "blocked" else 0
    print(json.dumps({"report_sha256": document["report_sha256"], **document["population"]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
