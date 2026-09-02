import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from oasis_agent.config import load_config
from oasis_agent.errors import ConfigurationError
from oasis_agent.reporting import benchmark_payload, render_markdown


class ConfigAndReportTests(unittest.TestCase):
    def test_config_defaults_to_observe(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text("", encoding="utf-8")
            config = load_config(path, {"OASIS_RPC_URL": "https://example.invalid", "OASIS_POOL_MANAGER": "0x" + "1" * 40})
            self.assertEqual(config.execution.mode.value, "observe")

    def test_config_rejects_private_key(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text('private_key = "0xdeadbeef"', encoding="utf-8")
            with self.assertRaises(ConfigurationError):
                load_config(path, {})

    def test_report_render_is_deterministic(self) -> None:
        self.assertEqual(render_markdown(benchmark_payload()), render_markdown(benchmark_payload()))
        text = render_markdown(benchmark_payload())
        self.assertIn("confirmed", text)
        self.assertIn("inferred", text)
        self.assertIn("unresolved", text)


if __name__ == "__main__":
    unittest.main()
