import json
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

from oasis_agent.farming.backtest import WalkForwardSpec
from tests.unit.test_lp_backtest import lock, observation


def test_lp_backtest_cli_writes_deterministic_fail_closed_result(tmp_path):
    input_path = tmp_path / "input.json"
    output_path = tmp_path / "result.json"
    observations = tuple(observation(number) for number in range(10, 22))
    input_path.write_text(
        json.dumps(
            {
                "schema": "oasis.lp-backtest-input.v1",
                "experiment": asdict(lock(observations)),
                "walk_forward": asdict(WalkForwardSpec(2, 2, 1, 2)),
                "observations": [asdict(item) for item in observations],
            }
        )
    )
    completed = subprocess.run(
        [
            sys.executable,
            "scripts/run_lp_backtest.py",
            str(input_path),
            "--output",
            str(output_path),
        ],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    result = json.loads(output_path.read_text())
    assert result["schema"] == "oasis.lp-backtest-result.v1"
    assert result["result"]["accepted"] is True
    assert result["result"]["execution_eligible"] is False
    assert result["result"]["metrics"]["fold_count"] == 2


def test_lp_backtest_cli_rejects_duplicate_keys(tmp_path):
    input_path = tmp_path / "duplicate.json"
    input_path.write_text('{"schema":"oasis.lp-backtest-input.v1","schema":"bad"}')
    completed = subprocess.run(
        [
            sys.executable,
            "scripts/run_lp_backtest.py",
            str(input_path),
            "--output",
            str(tmp_path / "x"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode != 0
    assert "duplicate JSON key" in completed.stderr
