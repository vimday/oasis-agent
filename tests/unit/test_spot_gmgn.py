"""GMGN second opinion: read-only argv, age backfill, disagreement blocks entry."""

import json
from typing import Any

import pytest

from oasis_agent.launch.gmgn_source import (
    GmgnSource,
    build_argv,
    cross_check,
    parse_token_info,
)
from oasis_agent.launch.onchainos_source import OnchainosSource, SourceFailure
from oasis_agent.launch.opportunity import DecisionPolicy, assess, market_from_hot
from oasis_agent.launch.spot_agent import AgentConfig, SpotAgent
from tests.unit.test_spot_agent import NOW, TOKEN, FakeCli, hot_row


def info(**overrides: Any) -> dict[str, Any]:
    """Values as `json.loads(parse_int=str)` yields them on the real wire."""
    row = {
        "address": TOKEN,
        "creation_timestamp": NOW - 86_400,
        "liquidity": "110000",
        "holder_count": 2100,
    } | overrides
    return {k: str(v) if isinstance(v, int) else v for k, v in row.items()}


class FakeGmgn:
    def __init__(self, payload: Any) -> None:
        self.payload = payload
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, argv: Any) -> bytes:
        self.calls.append(tuple(argv))
        if isinstance(self.payload, Exception):
            raise self.payload
        return json.dumps(self.payload).encode()


def run(cli: FakeCli, gmgn: FakeGmgn) -> list[dict[str, Any]]:
    bot = SpotAgent(
        OnchainosSource(cli, clock=lambda: NOW),
        AgentConfig(),
        gmgn=GmgnSource(gmgn, clock=lambda: NOW),
    )
    return bot.tick(NOW)


@pytest.mark.parametrize(
    ("group", "command"),
    [("swap", ""), ("multi-swap", ""), ("order", "create"), ("cooking", "create"), ("config", "")],
)
def test_state_changing_gmgn_commands_are_refused(group: str, command: str) -> None:
    with pytest.raises(SourceFailure, match="command_not_read_only"):
        build_argv(group, command, 8453)


def test_gmgn_argv_is_raw_and_rejects_injection() -> None:
    assert build_argv("token", "info", 8453, address=TOKEN)[-1] == "--raw"
    with pytest.raises(SourceFailure, match="invalid_option"):
        build_argv("token", "info", 8453, address="0x1 --wallet x")


def test_gmgn_fills_unknown_age_and_unblocks() -> None:
    cli = FakeCli()
    cli.hot = [hot_row(firstTradeTime="")]
    records = run(cli, FakeGmgn(info()))
    assessed = next(r for r in records if r["kind"] == "assessment")
    assert "age_unknown" not in assessed["reasons"]
    assert any(r["kind"] == "paper_buy" for r in records)


def test_gmgn_liquidity_disagreement_blocks_entry() -> None:
    records = run(FakeCli(), FakeGmgn(info(liquidity="1000")))
    assessed = next(r for r in records if r["kind"] == "assessment")
    assert "provider_disagree:liquidity" in assessed["reasons"]
    assert not any(r["kind"] == "paper_buy" for r in records)


def test_gmgn_failure_or_foreign_token_never_blocks_or_admits() -> None:
    for payload in (SourceFailure("timeout"), info(address="0x" + "99" * 20), {"code": "1"}):
        records = run(FakeCli(), FakeGmgn(payload))
        assert any(r["kind"] == "paper_buy" for r in records)  # OKX alone still decides
    cli = FakeCli()
    cli.hot = [hot_row(firstTradeTime="")]
    records = run(cli, FakeGmgn(SourceFailure("timeout")))
    assert not any(r["kind"] == "paper_buy" for r in records)  # age stays unknown


def test_zero_creation_time_is_unknown() -> None:
    view = parse_token_info(info(creation_timestamp=0), TOKEN)
    assert view is not None and view.created_at is None


def test_cross_check_takes_the_younger_age() -> None:
    market = market_from_hot(hot_row(), NOW)  # OKX: first trade 1 day ago
    assert market is not None
    older = cross_check(market, parse_token_info(info(creation_timestamp=NOW - 30 * 86_400), TOKEN))
    assert older.first_trade_at == NOW - 86_400  # GMGN cannot make it look older
    younger = cross_check(market, parse_token_info(info(creation_timestamp=NOW - 600), TOKEN))
    assert younger.first_trade_at == NOW - 600
    assert "age_failed" in assess(younger, DecisionPolicy()).reasons
