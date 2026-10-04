"""One-command bounded public-RPC acquisition and retained LP market diagnostics.

This worker never loads a wallet, signer or broadcaster. A successful process
means reports were produced, never that a strategy passed live release gates.
"""

import argparse
import os
import signal
from dataclasses import asdict
from pathlib import Path

from oasis_agent.models.common import canonical_json
from oasis_agent.rpc.transport import HttpPoster
from oasis_agent.shadow.observer_config import Config
from scripts.check_lp_dataset_coverage import check
from scripts.lp_workflow import run as run_workflow
from scripts.report_lp_market_economics import report
from scripts.run_pool_state_observer import Http, sample


def _status(output: Path, stage: str, **fields: object) -> None:
    payload = canonical_json(
        {
            "schema": "oasis.lp-research-run.v1",
            "stage": stage,
            "execution_eligible": False,
            "out_of_sample_accepted": False,
            **fields,
        }
    )
    temporary = output / "status.tmp"
    temporary.write_bytes(payload + b"\n")
    temporary.replace(output / "status.json")


def run(
    config: Config, posters: dict[str, HttpPoster], output: Path, *, now: int | None = None
) -> bool:
    if config.market is None or set(posters) != {"public", "paid"}:
        raise ValueError("market configuration and two providers required")
    output.mkdir(parents=True, exist_ok=False)
    (output / "config.json").write_text(config.model_dump_json(indent=2) + "\n")
    _status(output, "acquiring")
    try:
        captured = sample(
            config, posters, budget_path=output / "budget.db", output=output / "capture", now=now
        )
        if captured["status"] != "observed":
            _status(
                output, "halted", reason="market_capture_refused", capture="capture/report.json"
            )
            return False
        window = captured["market_window"]
        if not isinstance(window, dict):
            raise ValueError("market window missing")
        refs = window["input_references"]
        request = {
            "pool": config.market.pool,
            "pool_manager": config.pool_manager,
            "horizon_seconds": config.market.horizon_seconds,
            "max_lateness_seconds": config.market.max_lateness_seconds,
            "mature_prefix": True,
            "price_references": [r for r in refs if r["kind"] == "slot0_window"],
            "swap_references": [r for r in refs if r["kind"] != "slot0_window"],
        }
        request["pool"] = asdict(config.market.pool)
        request_path = output / "market-request.json"
        request_path.write_bytes(canonical_json(request))
        _status(output, "analyzing", capture="capture/report.json")
        report(request_path, output / "capture/evidence.db", output / "economics.json")
        dataset = output / "coverage-input.json"
        dataset.write_bytes(
            canonical_json(
                {
                    "schema": "oasis.lp-coverage-input.v1",
                    "windows": [
                        {"request": "market-request.json", "evidence": "capture/evidence.db"}
                    ],
                }
            )
        )
        coverage = check(dataset, output / "coverage.json")
        _status(
            output,
            "completed",
            economics="economics.json",
            coverage="coverage.json",
            coverage_passed=coverage,
            live_blockers=[
                "sustained_dataset",
                "costed_lp_share_estimator",
                "economic_acceptance",
                "production_release",
            ],
        )
        return True
    except KeyboardInterrupt:
        _status(output, "stopped", reason="operator_stop")
        return False
    except Exception:  # noqa: BLE001 - do not expose endpoint or raw response strings
        _status(output, "halted", reason="acquisition_or_analysis_refused")
        return False


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--config", type=Path)
    mode.add_argument("--workflow", type=Path)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    def stop_requested(signum: int, frame: object) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop_requested)
    if args.workflow is not None:
        try:
            result = run_workflow(args.workflow, args.output, resume=args.resume)
        except KeyboardInterrupt:
            parser.exit(130, "Workflow stopped; partial artifacts retained.\n")
        except Exception:  # noqa: BLE001 - never echo hostile config, paths or endpoints
            parser.exit(
                2, "Workflow integrity/configuration refused; existing evidence retained.\n"
            )
        print(f"Workflow {result['status']}; report: {args.output / 'report.html'}")
        if result["status"] != "completed":
            parser.exit(3, "Required inputs blocked; inspect report.json.\n")
        return
    if args.resume:
        parser.error("resume is available only for offline workflow; acquisition never restarts")
    posters = {}
    try:
        config = Config.from_json(args.config.read_bytes())
        primary, secondary = os.environ["BASE_RPC_PRIMARY"], os.environ["BASE_RPC_SECONDARY"]
        if primary == secondary:
            raise ValueError("distinct RPC endpoints required")
        posters = {"public": Http(primary), "paid": Http(secondary)}
        succeeded = run(config, posters, args.output)
    except Exception:  # noqa: BLE001 - redact endpoints and operator configuration
        parser.exit(2, "Research configuration refused; no execution permitted.\n")
    finally:
        for poster in posters.values():
            poster.client.close()
    if not succeeded:
        parser.exit(3, "Research halted; inspect retained status and capture evidence.\n")
    print("Research reports completed; live investment remains unavailable.")


if __name__ == "__main__":
    main()
