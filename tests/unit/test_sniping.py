"""Focused unit tests for the Base launch sniper's money-critical pure logic.

Covered: strict config validation, exit-trigger evaluation, Universal Router
calldata encoding, and the append-only hash-chained execution journal. Network
and broadcasting paths are exercised separately by the dry-run smoke test.
"""

import json
import sqlite3
import warnings

import pytest
from eth_abi import decode

from oasis_agent.sniping.calldata import (
    EXECUTE_SELECTOR,
    UNWRAP_WETH,
    V3_SWAP_EXACT_IN,
    buy_calldata,
    execute_calldata,
    path_bytes,
    sell_calldata,
    unwrap_weth_input,
    v3_swap_exact_in_input,
)
from oasis_agent.sniping.config import ExecutionConfig
from oasis_agent.sniping.exit_state import ExitEvaluation, evaluate_exit
from oasis_agent.sniping.journal import ExecutionJournal

WETH = "0x4200000000000000000000000000000000000006"
LAPTOP = "0xb095274743941e953c746f9c228da9c18bb6ec29"
ROUTER = "0x3fc91a3afd70395cd496c647d5a6cc9d4b2b7fad"
POLICY = {"schema": "oasis.launch-execution-policy.v1", "wallet": WETH}


def default_config() -> ExecutionConfig:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        return ExecutionConfig()


# --------------------------------------------------------------- config


class TestExecutionConfig:
    def test_defaults_freeze_fundamental_thresholds(self) -> None:
        cfg = default_config()
        assert cfg.chain_id == 8453
        assert cfg.target_ca == LAPTOP
        assert cfg.buy_tranches_wei == (100_000_000_000_000_000, 200_000_000_000_000_000)
        assert cfg.max_fdv_usd_micros == 200_000_000_000_000
        assert cfg.stop_loss_value_pct == 30
        assert cfg.take_profit_multiple == 10
        assert cfg.time_stop_seconds == 30 * 24 * 3600
        assert cfg.slippage_bps == 1000

    def test_rejects_binary_floats(self) -> None:
        with pytest.raises(ValueError, match="binary floats"):
            default_config().model_validate(
                {"poll_interval_ms": 2.0},
            )

    def test_rejects_unknown_keys(self) -> None:
        with pytest.raises(ValueError):
            default_config().model_validate(
                {"poll_interval_seconds": 2},
            )

    def test_rejects_non_ascending_tranches(self) -> None:
        with pytest.raises(ValueError, match="strictly ascending"):
            default_config().model_validate(
                {"buy_tranches_wei": [200_000_000_000_000_000, 100_000_000_000_000_000]}
            )

    def test_rejects_duplicate_tranches(self) -> None:
        with pytest.raises(ValueError, match="duplicate"):
            default_config().model_validate(
                {"buy_tranches_wei": [100_000_000_000_000_000, 100_000_000_000_000_000]}
            )

    def test_from_json_file(self) -> None:
        raw = default_config().model_dump(mode="json", by_alias=True)
        cfg = ExecutionConfig.from_json(json.dumps(raw))
        assert cfg == default_config()

    def test_uppercase_checksum_addresses_are_rejected(self) -> None:
        with pytest.raises(ValueError, match=r"target_ca|address"):
            default_config().model_validate(
                {"target_ca": "0xB095274743941E953C746F9C228DA9C18BB6EC29"}
            )


# ------------------------------------------------------------- exit state


class TestEvaluateExit:
    def test_stop_loss_sells_all(self) -> None:
        cfg = default_config()
        basis = 100_000_000  # $100 in micros
        value = basis * 69 // 100  # -31%
        result = evaluate_exit(
            cost_basis_usd_micros=basis,
            value_usd_micros=value,
            started_at=1_000,
            now=2_000,
            config=cfg,
            half_sold=False,
        )
        assert result.action == "sell_all"
        assert result.reason.startswith("stop_loss_value_pct")

    def test_take_profit_sells_half_before_any_half_sell(self) -> None:
        cfg = default_config()
        basis = 100_000_000
        result = evaluate_exit(
            cost_basis_usd_micros=basis,
            value_usd_micros=basis * 10,  # exactly 10x
            started_at=1_000,
            now=2_000,
            config=cfg,
            half_sold=False,
        )
        assert result.action == "sell_half"
        assert result.reason.startswith("take_profit_multiple")
        assert result.multiple == 10

    def test_take_profit_does_not_repeat_after_half_sell(self) -> None:
        cfg = default_config()
        basis = 100_000_000
        result = evaluate_exit(
            cost_basis_usd_micros=basis,
            value_usd_micros=basis * 10,
            started_at=1_000,
            now=2_000,
            config=cfg,
            half_sold=True,
        )
        assert result.action == "hold"

    def test_post_half_floor_exits_rest(self) -> None:
        cfg = default_config()
        basis = 100_000_000
        result = evaluate_exit(
            cost_basis_usd_micros=basis,
            value_usd_micros=basis * 2,  # exactly 2x floor
            started_at=1_000,
            now=2_000,
            config=cfg,
            half_sold=True,
        )
        assert result.action == "sell_all"
        assert result.reason.startswith("post_half_sell_floor_multiple")

    def test_time_stop_fires_after_window(self) -> None:
        cfg = default_config()
        basis = 100_000_000
        result = evaluate_exit(
            cost_basis_usd_micros=basis,
            value_usd_micros=basis,  # flat, no other trigger
            started_at=1_000,
            now=1_000 + cfg.time_stop_seconds,
            config=cfg,
            half_sold=False,
        )
        assert result.action == "time_stop"

    def test_hold_before_any_trigger(self) -> None:
        cfg = default_config()
        basis = 100_000_000
        result = evaluate_exit(
            cost_basis_usd_micros=basis,
            value_usd_micros=basis * 3,
            started_at=1_000,
            now=2_000,
            config=cfg,
            half_sold=False,
        )
        assert result.action == "hold"
        assert isinstance(result, ExitEvaluation)

    def test_clock_regression_rejected(self) -> None:
        cfg = default_config()
        with pytest.raises(ValueError, match="clock regression"):
            evaluate_exit(
                cost_basis_usd_micros=1,
                value_usd_micros=1,
                started_at=2_000,
                now=1_000,
                config=cfg,
                half_sold=False,
            )

    def test_non_positive_basis_rejected(self) -> None:
        cfg = default_config()
        with pytest.raises(ValueError, match="positive cost basis"):
            evaluate_exit(
                cost_basis_usd_micros=0,
                value_usd_micros=1,
                started_at=1,
                now=2,
                config=cfg,
                half_sold=False,
            )


# -------------------------------------------------------------- calldata


class TestCalldata:
    def test_path_encoding(self) -> None:
        path = path_bytes((WETH, LAPTOP), 3000)
        assert len(path) == 43  # 20 + 3 + 20
        assert path[20:23] == (3000).to_bytes(3, "big")

    def test_buy_calldata_uses_weth_to_laptop_v3_exact_in(self) -> None:
        cfg = default_config()
        data, value = buy_calldata(
            cfg,
            amount_in_wei=100_000_000_000_000_000,
            amount_out_min_laptop=1_000_000_000_000_000_000,
            fee=3000,
            recipient=WETH,
        )
        assert data.startswith(EXECUTE_SELECTOR)
        assert value == 100_000_000_000_000_000

    def test_sell_calldata_swaps_then_unwraps_to_wallet(self) -> None:
        cfg = default_config()
        data = sell_calldata(
            cfg,
            amount_in_laptop=1_000_000_000_000_000_000,
            amount_out_min_weth=50_000_000_000_000_000,
            fee=3000,
            wallet=WETH,
        )
        assert data.startswith(EXECUTE_SELECTOR)
        commands, inputs = decode(
            ["bytes", "bytes[]"],
            bytes.fromhex(data[len(EXECUTE_SELECTOR) :]),
        )
        assert commands == bytes([V3_SWAP_EXACT_IN, UNWRAP_WETH])
        assert len(inputs) == 2

    def test_v3_input_rejects_non_positive_amounts(self) -> None:
        with pytest.raises(ValueError, match="positive amounts"):
            v3_swap_exact_in_input(
                recipient=WETH,
                amount_in=0,
                amount_out_min=1,
                path=path_bytes((WETH, LAPTOP), 3000),
                payer_is_user=False,
            )

    def test_execute_rejects_mismatched_commands(self) -> None:
        with pytest.raises(ValueError, match="equal length"):
            execute_calldata((V3_SWAP_EXACT_IN,), (b"", b""))

    def test_unwrap_rejects_negative_min(self) -> None:
        with pytest.raises(ValueError, match="non-negative"):
            unwrap_weth_input(recipient=WETH, amount_min=-1)


# ---------------------------------------------------------------- journal


class TestExecutionJournal:
    def test_append_and_read_roundtrip(self, tmp_path) -> None:
        with ExecutionJournal(tmp_path / "j.db", POLICY) as j:
            first = j.append("buy_confirmed", {"amount_wei": 1, "tx_hash": "0x" + "a" * 64})
            second = j.append("sell_confirmed", {"amount_laptop": 1})
            assert first["seq"] == 1
            assert second["seq"] == 2
            assert second["previous"] == first["digest"]
            records = j.read()
            assert [r["kind"] for r in records] == ["buy_confirmed", "sell_confirmed"]

    def test_tampered_payload_raises(self, tmp_path) -> None:
        path = tmp_path / "j.db"
        with ExecutionJournal(path, POLICY) as j:
            j.append("buy_confirmed", {"amount_wei": 1, "tx_hash": "0x" + "a" * 64})
        db = sqlite3.connect(path)
        db.execute(
            "UPDATE execution_events SET payload = ? WHERE seq = 1",
            (b'{"tampered": true}',),
        )
        db.commit()
        db.close()
        with pytest.raises(ValueError, match="corrupted"):
            ExecutionJournal(path, POLICY)

    def test_policy_cannot_change_on_restart(self, tmp_path) -> None:
        path = tmp_path / "j.db"
        with ExecutionJournal(path, POLICY):
            pass
        with pytest.raises(ValueError, match="policy cannot change"):
            ExecutionJournal(path, {"schema": "oasis.launch-execution-policy.v1", "wallet": LAPTOP})

    def test_append_rejects_empty_kind(self, tmp_path) -> None:
        with (
            ExecutionJournal(tmp_path / "j.db", POLICY) as j,
            pytest.raises(ValueError, match="non-empty string"),
        ):
            j.append("", {"a": 1})
