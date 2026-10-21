"""Run local release-development checks with explicit per-command exit evidence.

This is a development quality gate, not live trading release authorization.
It never starts production services or acquires chain data.
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path

from oasis_agent.models.common import canonical_json, sha256_hex

ROOT = Path(__file__).resolve().parents[1]
CHECKS = (
    ("python_frontend", ("make", f"PYTHON={sys.executable}", "python-check", "frontend-check")),
    ("secrets", (sys.executable, "-m", "scripts.check_secrets")),
    ("bandit", (sys.executable, "-m", "bandit", "-r", "src", "scripts", "-ll")),
    ("compose", (sys.executable, "-m", "scripts.verify_live_compose")),
)


def run_checks(directory: Path, *, timeout: int = 600) -> dict:
    if type(timeout) is not int or not 1 <= timeout <= 600:
        raise ValueError("bounded check timeout required")
    directory.parent.mkdir(parents=True, exist_ok=True)
    directory.mkdir(mode=0o700)  # Exclusive: preserve previous pass/failure evidence.
    report: dict = {
        "schema": "oasis.live-development-quality.v1",
        "passed": False,
        "execution_eligible": False,
        "planned_checks": [name for name, _ in CHECKS],
        "checks": [],
    }
    for name, command in CHECKS:
        log = directory / f"{name}.log"
        started = time.monotonic()
        timed_out = False
        launch_failed = False
        code = None
        with log.open("xb") as handle:
            try:
                process = subprocess.Popen(  # nosec B603 - fixed development checks, no shell
                    command,
                    cwd=ROOT,
                    stdout=handle,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            except OSError:
                launch_failed = True
            else:
                try:
                    code = process.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    # Kill the entire check group, including make's test children.
                    with suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
                    code = process.wait(timeout=10)
        result = {
            "name": name,
            "command": list(command),
            "exit_code": code,
            "timed_out": timed_out,
            "launch_failed": launch_failed,
            "passed": code == 0 and not timed_out and not launch_failed,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "log": log.name,
            "log_hash": sha256_hex(log.read_bytes()),
        }
        report["checks"].append(result)
        if not result["passed"]:
            break
    report["passed"] = len(report["checks"]) == len(CHECKS) and all(
        check["passed"] for check in report["checks"]
    )
    report["evidence_hash"] = sha256_hex(canonical_json(report))
    with (directory / "report.json").open("x") as handle:
        json.dump(report, handle, indent=2)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    try:
        report = run_checks(args.out)
    except (OSError, ValueError):
        parser.exit(3, "Quality check setup refused; previous evidence preserved.\n")
    print(json.dumps({key: report[key] for key in ("passed", "execution_eligible", "checks")}))
    raise SystemExit(0 if report["passed"] else 3)


if __name__ == "__main__":
    main()
