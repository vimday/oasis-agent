import unittest
from decimal import Decimal
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
            config = load_config(
                path,
                {
                    "OASIS_CHAIN_ID": "4663",
                    "OASIS_RPC_URL": "https://example.invalid",
                    "OASIS_POOL_MANAGER": "0x" + "1" * 40,
                    "OASIS_ALLOW_FIXTURE_ENDPOINT": "1",
                },
            )
            self.assertEqual(config.execution.mode.value, "observe")

    def test_config_rejects_private_key(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text('private_key = "0xdeadbeef"', encoding="utf-8")
            with self.assertRaises(ConfigurationError):
                load_config(path, {})

    def test_config_rejects_string_boolean(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text('[execution]\nallow_broadcast = "false"', encoding="utf-8")
            with self.assertRaisesRegex(ConfigurationError, "TOML boolean"):
                load_config(
                    path,
                    {
                        "OASIS_CHAIN_ID": "4663",
                        "OASIS_RPC_URL": "https://example.invalid",
                        "OASIS_POOL_MANAGER": "0x" + "1" * 40,
                        "OASIS_ALLOW_FIXTURE_ENDPOINT": "1",
                    },
                )

    def test_config_parses_finite_nonnegative_limits(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text('[execution]\nmax_capital = "12.5"\nmax_gas = "0.01"', encoding="utf-8")
            config = load_config(
                path,
                {
                    "OASIS_CHAIN_ID": "4663",
                    "OASIS_RPC_URL": "https://example.invalid",
                    "OASIS_POOL_MANAGER": "0x" + "1" * 40,
                    "OASIS_ALLOW_FIXTURE_ENDPOINT": "1",
                },
            )
            self.assertEqual(config.execution.max_capital, Decimal("12.5"))
            self.assertEqual(config.execution.max_gas, Decimal("0.01"))

    def test_config_rejects_invalid_limits(self) -> None:
        for value in ("-1", "NaN", "Infinity"):
            with self.subTest(value=value), TemporaryDirectory() as directory:
                path = Path(directory) / "config.toml"
                path.write_text(f'[execution]\nmax_capital = "{value}"', encoding="utf-8")
                with self.assertRaises(ConfigurationError):
                    load_config(
                        path,
                        {
                            "OASIS_CHAIN_ID": "4663",
                            "OASIS_RPC_URL": "https://example.invalid",
                            "OASIS_POOL_MANAGER": "0x" + "1" * 40,
                            "OASIS_ALLOW_FIXTURE_ENDPOINT": "1",
                        },
                    )

    def test_config_rejects_unknown_keys(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text("unknown = true", encoding="utf-8")
            with self.assertRaisesRegex(ConfigurationError, "unknown root"):
                load_config(
                    path,
                    {
                        "OASIS_CHAIN_ID": "4663",
                        "OASIS_RPC_URL": "https://rpc.example",
                        "OASIS_POOL_MANAGER": "0x" + "1" * 40,
                        "OASIS_ALLOW_FIXTURE_ENDPOINT": "1",
                    },
                )

    def test_config_rejects_mixed_flat_and_nested_chain_forms(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(
                'chain_id = 1\n[chain]\nrpc_url = "https://rpc.example"\n'
                'pool_manager = "0x1111111111111111111111111111111111111111"',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ConfigurationError, r"either flat chain keys or \[chain\]"):
                load_config(path, {})

    def test_config_rejects_non_string_chain_identity_fields(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(
                '[chain]\nrpc_url = 1\npool_manager = "0x1111111111111111111111111111111111111111"',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ConfigurationError, "rpc_url must be a string"):
                load_config(path, {})

    def test_config_resolves_rpc_url_from_named_environment_variable(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(
                '[chain]\nchain_id = 4663\nrpc_url_env = "ROBINHOOD_RPC_URL"\n'
                'pool_manager = "0x1111111111111111111111111111111111111111"',
                encoding="utf-8",
            )
            config = load_config(
                path, {"ROBINHOOD_RPC_URL": "https://rpc.mainnet.chain.robinhood.com"}
            )
            self.assertEqual(
                config.chain.rpc_url,
                "https://rpc.mainnet.chain.robinhood.com",
            )

    def test_config_rejects_ambiguous_or_missing_rpc_environment(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(
                '[chain]\nchain_id = 4663\nrpc_url = "https://rpc.example"\n'
                'rpc_url_env = "ROBINHOOD_RPC_URL"\n'
                'pool_manager = "0x1111111111111111111111111111111111111111"',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ConfigurationError, "either rpc_url or rpc_url_env"):
                load_config(path, {"ROBINHOOD_RPC_URL": "https://rpc.example"})
            path.write_text(
                '[chain]\nchain_id = 4663\nrpc_url_env = "ROBINHOOD_RPC_URL"\n'
                'pool_manager = "0x1111111111111111111111111111111111111111"',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ConfigurationError, "environment variable is missing"):
                load_config(path, {})

    def test_config_rejects_placeholder_endpoint_without_fixture_opt_in(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text("", encoding="utf-8")
            with self.assertRaisesRegex(ConfigurationError, "placeholder RPC"):
                load_config(
                    path,
                    {
                        "OASIS_CHAIN_ID": "4663",
                        "OASIS_RPC_URL": "https://example.invalid",
                        "OASIS_POOL_MANAGER": "0x" + "1" * 40,
                    },
                )

    def test_config_allows_placeholder_endpoint_for_explicit_fixture(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text("", encoding="utf-8")
            config = load_config(
                path,
                {
                    "OASIS_CHAIN_ID": "4663",
                    "OASIS_RPC_URL": "https://example.invalid",
                    "OASIS_POOL_MANAGER": "0x" + "1" * 40,
                    "OASIS_ALLOW_FIXTURE_ENDPOINT": "1",
                },
            )
            self.assertEqual(config.chain.chain_id, 4663)

        self.assertEqual(render_markdown(benchmark_payload()), render_markdown(benchmark_payload()))
        text = render_markdown(benchmark_payload())
        self.assertIn("confirmed", text)
        self.assertIn("inferred", text)
        self.assertIn("fixture_only", text)
        self.assertIn("not live-chain evidence", text)
        self.assertIn("0x800000", text)
        self.assertIn("Tick spacing: `60`", text)
        self.assertIn("## Fixture flow-attribution example", text)
        self.assertIn("Gross LP fees: `300 USDG base units`", text)
        self.assertIn("Confirmed external fee alpha: `90 USDG base units`", text)
        self.assertIn("Affiliated/self-classified LP fees: `100 USDG base units`", text)
        self.assertIn("Unknown-flow LP fees: `100 USDG base units`", text)
        self.assertIn("Self-trade cost: `95 USDG base units`", text)
        self.assertIn("synthetic-fixture", text)
        self.assertIn(
            "Request hash: `4444444444444444444444444444444444444444444444444444444444444444`", text
        )
        self.assertIn(
            "Raw response hash: `5555555555555555555555555555555555555555555555555555555555555555`",
            text,
        )
        self.assertIn(
            "Interpretation hash: `6666666666666666666666666666666666666666666666666666666666666666`",
            text,
        )
        self.assertIn("Unknown flow remains unresolved", text)
        self.assertNotIn("Annual percentage", text)
        self.assertNotIn("Pool initialization fee: `60`", text)


if __name__ == "__main__":
    unittest.main()
