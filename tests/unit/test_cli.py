import json
import subprocess
import sys
import unittest


class CliIntegrationTests(unittest.TestCase):
    def run_cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "oasis_agent.cli", *args],
            env={"PYTHONPATH": "src"},
            text=True,
            capture_output=True,
            check=False,
        )

    def test_analyze_hook_fixture_outputs_json(self) -> None:
        result = self.run_cli("analyze-hook", "--fixture")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertIn("permissions", payload)
        self.assertEqual(payload["mode"], "fixture")
        self.assertFalse(payload["live_chain"])
        self.assertTrue(payload["permissions"]["before_swap"])

    def test_analyze_hook_requires_explicit_fixture_mode(self) -> None:
        result = self.run_cli("analyze-hook")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--fixture is required", result.stderr)

    def test_unsupported_execution_is_rejected(self) -> None:
        result = self.run_cli("execute")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("broadcast is disabled", result.stderr)


if __name__ == "__main__":
    unittest.main()
