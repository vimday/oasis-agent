"""Shadow spot agent: read-only source, fail-closed gates, paper entry and exits."""

import json
from typing import Any

import pytest

from oasis_agent.launch.onchainos_source import (
    BASE_USDC,
    OnchainosSource,
    SourceFailure,
    build_argv,
)
from oasis_agent.launch.opportunity import RISK_FLAGS, DecisionPolicy, assess, market_from_hot
from oasis_agent.launch.spot_agent import (
    AgentConfig,
    AgentState,
    SpotAgent,
    reconcile_with_journal,
)
from oasis_agent.launch.spot_book import Position, check_exit, parse_quote
from oasis_agent.launch.spot_executor import Fill, LiveRefused
from oasis_agent.models.common import sha256_hex
from scripts.run_spot_agent import load_state, save_state

TOKEN = "0x" + "ab" * 20
NOW = 1_800_000_000
USD = 1_000_000


def hot_row(**overrides: str) -> dict[str, str]:
    row = {
        "tokenContractAddress": TOKEN,
        "tokenSymbol": "TKN",
        "liquidity": "120000.5",
        "marketCap": "900000",
        "holders": "2000",
        "top10HoldPercent": "30.5",
        "uniqueTraders": "80",
        "txsBuy": "120",
        "txsSell": "60",
        "firstTradeTime": str((NOW - 86_400) * 1000),
    }
    return row | overrides


def security_row(**flags: bool) -> dict[str, Any]:
    return dict.fromkeys(RISK_FLAGS, False) | {"buyTaxes": "0", "sellTaxes": "0"} | flags


def price_row(**overrides: str) -> dict[str, str]:
    return {
        "priceChange5M": "-0.8",
        "priceChange1H": "12.5",
        "priceChange24H": "40",
        "volume24H": "50000",
        "liquidity": "120000.5",
        "price": "0.95",
        "maxPrice": "1",
    } | overrides


def quote_row(src: str, dst: str, amount_in: int, amount_out: int, **extra: Any) -> dict[str, Any]:
    def meta(address: str) -> dict[str, Any]:
        return {"tokenContractAddress": address, "isHoneyPot": False, "taxRate": "0"}

    return {
        "chainIndex": "8453",
        "swapMode": "exactIn",
        "dexRouterList": [{"dexProtocol": {"dexName": "test"}}],
        "fromToken": meta(src),
        "toToken": meta(dst),
        "fromTokenAmount": str(amount_in),
        "toTokenAmount": str(amount_out),
        "priceImpactPercent": "-0.2",
        "tradeFee": "0.01",
    } | extra


class FakeCli:
    """Scripted responses keyed by (group, command); the sell price is mutable per tick."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.hot = [hot_row()]
        self.price = price_row()
        self.security = security_row()
        self.signals: list[Any] = [
            {
                "token": {"tokenAddress": TOKEN},
                "timestamp": str((NOW - 300) * 1000),
                "walletType": "1",
                "triggerWalletAddress": "0x01,0x02",
                "soldRatioPercent": "0",
            }
        ]
        self.token_per_usdc = 1_000  # token base units per USDC base unit
        self.sell_ratio: tuple[int, int] = (99, 100)  # unwind value as a fraction of buy-equivalent
        self.fail: set[tuple[str, str]] = set()
        self.now = NOW  # wall time reported by batch price rows

    def __call__(self, argv: Any) -> bytes:
        argv = tuple(argv)
        self.calls.append(argv)
        key = (argv[0], argv[1])
        if key in self.fail:
            return b'{"ok":false,"error":"x"}'
        options = dict(zip(argv[2::2], argv[3::2], strict=True))
        data: Any
        if key == ("token", "hot-tokens"):
            data = self.hot
        elif key == ("token", "price-info"):
            data = [self.price]
        elif key == ("token", "advanced-info"):
            data = {"devRugPullTokenCount": "0"}
        elif key == ("security", "token-scan"):
            data = [self.security]
        elif key == ("signal", "list"):
            data = self.signals
        elif key == ("market", "prices"):
            tokens = [t.split(":", 1)[1] for t in options["--tokens"].split(",")]
            data = [
                {
                    "chainIndex": "8453",
                    "tokenContractAddress": t,
                    "price": self.price["price"],
                    "time": str(self.now * 1000),
                }
                for t in tokens
            ]
        elif key == ("swap", "quote"):
            src, dst, amount = options["--from"], options["--to"], int(options["--amount"])
            if src == BASE_USDC:
                out = amount * self.token_per_usdc
            else:
                n, d = self.sell_ratio
                out = amount // self.token_per_usdc * n // d
            data = [quote_row(src, dst, amount, out)]
        else:
            raise AssertionError(argv)
        return json.dumps({"ok": True, "data": data}).encode()


def agent(cli: FakeCli, **config: Any) -> SpotAgent:
    return SpotAgent(OnchainosSource(cli, clock=lambda: NOW), AgentConfig(**config))


def test_only_read_only_commands_can_be_built() -> None:
    for group, command in (
        ("swap", "execute"),
        ("swap", "swap"),
        ("wallet", "send"),
        ("strategy", "create-limit"),
    ):
        with pytest.raises(SourceFailure, match="command_not_read_only"):
            build_argv(group, command, 8453)
    with pytest.raises(SourceFailure, match="invalid_option"):
        build_argv("token", "price-info", 8453, address="0x1; rm -rf /")
    with pytest.raises(SourceFailure, match="invalid_option"):
        build_argv("token", "price-info", 8453, address="--wallet")
    with pytest.raises(SourceFailure, match="chain_not_supported"):
        build_argv("token", "hot-tokens", 1)


def test_provider_error_and_bad_json_fail_closed() -> None:
    source = OnchainosSource(lambda argv: b'{"ok":false}', clock=lambda: NOW)
    with pytest.raises(SourceFailure, match="provider_error"):
        source.hot_tokens(8453)
    source = OnchainosSource(lambda argv: b"not json", clock=lambda: NOW)
    with pytest.raises(SourceFailure, match="invalid_json"):
        source.hot_tokens(8453)


def test_quote_parser_refuses_identity_amount_and_honeypot_mismatches() -> None:
    good = quote_row(BASE_USDC, TOKEN, 10, 20)
    kwargs = {
        "chain": 8453,
        "src": BASE_USDC,
        "dst": TOKEN,
        "amount": 10,
        "raw_sha256": "x",
        "at": NOW,
    }
    assert parse_quote([good], **kwargs).amount_out == 20
    for bad in (
        good | {"fromTokenAmount": "11"},
        good | {"chainIndex": "1"},
        good | {"swapMode": "exactOut"},
        good | {"dexRouterList": []},
        good | {"toToken": good["toToken"] | {"isHoneyPot": True}},
        good | {"toToken": good["toToken"] | {"taxRate": "5"}},
        good | {"toToken": good["fromToken"]},
        good | {"tradeFee": 0.01},
    ):
        with pytest.raises(ValueError):
            parse_quote([bad], **kwargs)
    with pytest.raises(ValueError):
        parse_quote([good, good], **kwargs)


@pytest.mark.parametrize(
    ("override", "reason"),
    [
        ({"liquidity": "1000"}, "liquidity_failed"),
        ({"holders": "10"}, "holders_failed"),
        ({"txsBuy": "1"}, None),
        ({"top10HoldPercent": "80"}, "top10_failed"),
        ({"firstTradeTime": str(NOW * 1000)}, "age_failed"),
    ],
)
def test_listing_blockers_skip_without_enrichment(
    override: dict[str, str], reason: str | None
) -> None:
    cli = FakeCli()
    cli.hot = [hot_row(**override)]
    cli.signals = []
    records = agent(cli).tick(NOW)
    assessed = [r for r in records if r["kind"] == "assessment"]
    if reason is None:  # a non-gating field change still enriches
        assert any(c[:2] == ("token", "price-info") for c in cli.calls)
        return
    assert assessed[0]["action"] == "skip" and reason in assessed[0]["reasons"]
    assert not any(c[:2] in (("token", "price-info"), ("swap", "quote")) for c in cli.calls)


@pytest.mark.parametrize("value", ["", "1e9", "-5", "NaN", "12.5.1"])
def test_malformed_liquidity_is_unknown_and_blocks(value: str) -> None:
    cli = FakeCli()
    cli.hot = [hot_row(liquidity=value)]
    cli.price = price_row(liquidity=value)
    records = agent(cli).tick(NOW)
    assessed = next(r for r in records if r["kind"] == "assessment")
    assert assessed["action"] == "skip" and "liquidity_unknown" in assessed["reasons"]
    assert not any(r["kind"] == "paper_buy" for r in records)


def test_signal_only_token_is_enriched_and_needs_age_and_liquidity() -> None:
    other = "0x" + "cd" * 20
    cli = FakeCli()
    cli.hot = []
    cli.signals = [
        {
            "token": {
                "tokenAddress": other,
                "symbol": "SIG",
                "holders": "5000",
                "top10HolderPercent": "20",
                "marketCapUsd": "1000000",
            },
            "timestamp": str((NOW - 60) * 1000),
            "walletType": "1",
            "triggerWalletAddress": "0x01,0x02,0x03,0x04",
            "soldRatioPercent": "0",
        }
    ]
    records = agent(cli).tick(NOW)
    assessed = next(r for r in records if r["kind"] == "assessment")
    # price-info supplies liquidity but advanced-info gives no createTime: age unknown blocks.
    assert any(c[:2] == ("token", "price-info") for c in cli.calls)
    assert assessed["action"] == "skip" and "age_unknown" in assessed["reasons"]
    assert not any(r["kind"] == "paper_buy" for r in records)


def test_no_smart_money_confluence_only_watches() -> None:
    cli = FakeCli()
    cli.signals = []
    records = agent(cli).tick(NOW)
    assessed = next(r for r in records if r["kind"] == "assessment")
    assert assessed["action"] == "watch" and "score_below_threshold" in assessed["reasons"]
    assert not any(r["kind"] == "paper_buy" for r in records)


def test_unknown_security_is_a_blocker() -> None:
    market = market_from_hot(hot_row(), NOW)
    assert market is not None
    result = assess(market, DecisionPolicy())
    assert result.action == "skip" and "security_unknown" in result.reasons
    assert "tax_unknown" in result.reasons


@pytest.mark.parametrize("flag", ["isHoneypot", "isMintable", "isLiquidityRemoval", "isWash"])
def test_risk_flag_blocks_entry(flag: str) -> None:
    cli = FakeCli()
    cli.security = security_row(**{flag: True})
    records = agent(cli).tick(NOW)
    assert not any(r["kind"] == "paper_buy" for r in records)
    assert any("risk:" + flag in r.get("reasons", []) for r in records)
    assert not any(c[:2] == ("swap", "quote") for c in cli.calls)


def test_non_boolean_security_flag_is_unknown_not_safe() -> None:
    cli = FakeCli()
    cli.security = security_row() | {"isHoneypot": "false"}
    records = agent(cli).tick(NOW)
    assert any("security_unknown" in r.get("reasons", []) for r in records)
    assert not any(r["kind"] == "paper_buy" for r in records)


def test_momentum_gates_reject_chasing_and_dumping() -> None:
    for change, reason in (("9", "chasing_spike"), ("-7", "dumping_now")):
        cli = FakeCli()
        cli.price = price_row(priceChange5M=change)
        records = agent(cli).tick(NOW)
        assert any(reason in r.get("reasons", []) for r in records)
        assert not any(r["kind"] == "paper_buy" for r in records)


def test_entry_measures_round_trip_and_sizes_against_wallet_and_depth() -> None:
    cli = FakeCli()
    bot = agent(cli, initial_cash_usd_micros=1_000 * USD)
    records = bot.tick(NOW)
    buy = next(r for r in records if r["kind"] == "paper_buy")
    # min(20% of 1000 USD, 0.5% of 120000.5 USD liquidity) = 200 USD.
    position = bot.state.positions[TOKEN]
    assert position.units == 200 * USD * 1_000
    # 200 USD in + 0.05 gas + 0.50% slippage = 201.05; unwind 198 * 0.995 - 0.05 = 196.96.
    assert buy["cost_usd_micros"] == 201_050_000
    assert buy["immediate_exit_usd_micros"] == 196_960_000
    assert buy["round_trip_cost_bps"] == 205  # ceil(4.09 / 200 USD)
    assert buy["execution_eligible"] is False and buy["mode"] == "shadow"
    assert bot.state.cash_usd_micros == 1_000 * USD - 201_050_000


def test_expensive_round_trip_refuses_entry() -> None:
    cli = FakeCli()
    cli.sell_ratio = (95, 100)
    records = agent(cli).tick(NOW)
    skip = next(r for r in records if r["kind"] == "skip_entry")
    assert skip["reason"] == "round_trip_too_expensive"
    assert not any(r["kind"] == "paper_buy" for r in records)


def opened(cli: FakeCli) -> SpotAgent:
    bot = agent(cli, initial_cash_usd_micros=1_000 * USD)
    bot.tick(NOW)
    assert TOKEN in bot.state.positions
    return bot


@pytest.mark.parametrize(
    ("ratio", "reason", "won"),
    [((90, 100), "stop_loss", False), ((130, 100), "take_profit", True)],
)
def test_exit_rules_close_paper_position(ratio: tuple[int, int], reason: str, won: bool) -> None:
    cli = FakeCli()
    bot = opened(cli)
    cli.sell_ratio = ratio
    records = bot.tick(NOW + 60)
    sell = next(r for r in records if r["kind"] == "paper_sell")
    assert sell["reason"] == reason
    assert TOKEN not in bot.state.positions
    assert bot.state.closed_trades == 1 and bot.state.winning_trades == int(won)
    assert bot.state.cash_usd_micros == 1_000 * USD + bot.state.realized_pnl_usd_micros
    # Cooldown prevents an immediate re-entry into the same token.
    assert not any(r["kind"] == "paper_buy" for r in records)


def test_trailing_stop_after_gain() -> None:
    cli = FakeCli()
    bot = opened(cli)
    cli.sell_ratio = (115, 100)
    assert any(r["kind"] == "hold" for r in bot.tick(NOW + 60))
    cli.sell_ratio = (102, 100)
    sell = next(r for r in bot.tick(NOW + 120) if r["kind"] == "paper_sell")
    assert sell["reason"] == "trailing_stop" and sell["pnl_usd_micros"] > 0


def test_time_stop() -> None:
    cli = FakeCli()
    bot = opened(cli)
    sell = next(r for r in bot.tick(NOW + 4 * 3600) if r["kind"] == "paper_sell")
    assert sell["reason"] == "time_stop"


def test_new_risk_flag_forces_exit_of_held_position() -> None:
    cli = FakeCli()
    bot = opened(cli)
    cli.security = security_row(isLiquidityRemoval=True)
    sell = next(r for r in bot.tick(NOW + 1_000) if r["kind"] == "paper_sell")
    assert sell["reason"] == "risk_flag"


def test_unpriceable_position_halts_new_entries() -> None:
    cli = FakeCli()
    bot = opened(cli)
    cli.fail.add(("swap", "quote"))
    records: list[dict[str, Any]] = []
    for step in range(1, 4):
        records = bot.tick(NOW + step * 60)
    assert any(r["kind"] == "halt_review" for r in records)
    assert records[-1]["entries_blocked"] == "held_position_unpriceable"
    assert TOKEN in bot.state.positions  # never silently written off


def test_discovery_failure_is_recorded_not_traded() -> None:
    cli = FakeCli()
    cli.fail.add(("token", "hot-tokens"))
    records = agent(cli).tick(NOW)
    assert [r["kind"] for r in records] == ["discovery_failed", "tick_summary"]


def test_steady_high_turnover_token_routes_to_lp_not_flip() -> None:
    cli = FakeCli()
    cli.hot = [hot_row(liquidity="500000")]
    cli.price = price_row(volume24H="2000000", priceChange24H="5", liquidity="500000")
    records = agent(cli).tick(NOW)
    assessed = next(r for r in records if r["kind"] == "assessment")
    assert assessed["action"] == "lp_candidate"
    assert not any(r["kind"] == "paper_buy" for r in records)


def test_smart_money_signals_count_distinct_recent_wallets() -> None:
    cli = FakeCli()
    signal = {
        "token": {"tokenAddress": TOKEN},
        "timestamp": str((NOW - 60) * 1000),
        "walletType": "1",
        "triggerWalletAddress": "0xa,0xb,0xc",
        "soldRatioPercent": "10",
    }
    stale = signal | {"timestamp": str((NOW - 7200) * 1000), "triggerWalletAddress": "0xd"}
    whale = signal | {"walletType": "3", "triggerWalletAddress": "0xe"}
    cli.signals = [signal, stale, whale]
    assessed = next(r for r in agent(cli).tick(NOW) if r["kind"] == "assessment")
    assert assessed["smart_wallets"] == 3


def test_smart_money_exiting_blocks_flip() -> None:
    cli = FakeCli()
    cli.signals = [
        {
            "token": {"tokenAddress": TOKEN},
            "timestamp": str((NOW - 60) * 1000),
            "walletType": "2",
            "triggerWalletAddress": "0xa",
            "soldRatioPercent": "90",
        }
    ]
    records = agent(cli).tick(NOW)
    assert any("smart_money_exiting" in r.get("reasons", []) for r in records)


def test_check_exit_boundaries_are_exact() -> None:
    policy = DecisionPolicy()
    position = Position(TOKEN, "T", 1, 10_000, NOW, 10_000, 10_000, "x")
    assert check_exit(position, 9_200, NOW, policy) == "stop_loss"
    assert check_exit(position, 9_201, NOW, policy) is None
    assert check_exit(position, 12_500, NOW, policy) == "take_profit"
    assert check_exit(position, 12_499, NOW, policy) is None


def test_raw_responses_are_retained_content_addressed(tmp_path: Any) -> None:
    cli = FakeCli()
    source = OnchainosSource(cli, clock=lambda: NOW, retain=tmp_path)
    capture = source.hot_tokens(8453)
    stored = tmp_path / (capture.raw_sha256 + ".json")
    assert sha256_hex(stored.read_bytes()) == capture.raw_sha256


def test_state_round_trips_through_snapshot(tmp_path: Any) -> None:
    bot = opened(FakeCli())
    save_state(tmp_path / "s.json", bot.state)
    restored = load_state(tmp_path / "s.json", bot.config)
    assert restored == bot.state


@pytest.mark.parametrize(
    ("price", "high", "reason"),
    [
        ("0.99", "1", "at_high_no_pullback"),
        ("0.6", "1", "trend_broken"),
        ("1.2", "1", "pullback_unknown"),
        ("0.95", "", "pullback_unknown"),
    ],
)
def test_entry_requires_a_real_pullback_from_the_high(price: str, high: str, reason: str) -> None:
    cli = FakeCli()
    cli.price = price_row(price=price, maxPrice=high)
    records = agent(cli).tick(NOW)
    assessed = next(r for r in records if r["kind"] == "assessment")
    assert reason in assessed["reasons"]
    assert not any(r["kind"] == "paper_buy" for r in records)


def test_flip_reason_records_the_measured_pullback() -> None:
    records = agent(FakeCli()).tick(NOW)
    assessed = next(r for r in records if r["kind"] == "assessment")
    assert assessed["action"] == "flip" and "pullback_bps:500" in assessed["reasons"]


class FakeExecutor:
    """Stands in for LiveExecutor: scripted fills or refusals, never a real wallet."""

    def __init__(self, refuse: str | None = None) -> None:
        self.calls = 0
        self.orders: list[tuple[str, str, int]] = []
        self.refuse = refuse

    def _fill(self, side: str, token: str, amount: int, usdc: int, units: int) -> Any:
        self.calls += 1
        self.orders.append((side, token, amount))
        if self.refuse is not None:
            raise LiveRefused(self.refuse)
        return Fill(side, token, "0x" + "cd" * 32, 77, usdc, units)  # type: ignore[arg-type]

    def buy(self, token: str, usdc_amount: int) -> Any:
        # Fill one percent fewer units than quoted.
        return self._fill("buy", token, usdc_amount, -usdc_amount, usdc_amount * 990)

    def sell(self, token: str, units: int) -> Any:
        return self._fill("sell", token, units, 150 * USD, -units)


def live_agent(cli: FakeCli, executor: FakeExecutor) -> SpotAgent:
    config = AgentConfig(initial_cash_usd_micros=1_000 * USD)
    return SpotAgent(OnchainosSource(cli, clock=lambda: NOW), config, executor=executor)  # type: ignore[arg-type]


def test_live_buy_books_the_observed_fill_not_the_quote() -> None:
    cli, ex = FakeCli(), FakeExecutor()
    bot = live_agent(cli, ex)
    buy = next(r for r in bot.tick(NOW) if r["kind"] == "live_buy")
    assert ex.orders == [("buy", TOKEN, 200 * USD)]
    position = bot.state.positions[TOKEN]
    assert position.units == 200 * USD * 990
    # 200 USD spent plus 0.05 declared gas; no modelled slippage on an observed fill.
    assert buy["cost_usd_micros"] == 200_050_000
    assert buy["mode"] == "live" and buy["tx_hash"] == "0x" + "cd" * 32
    assert buy["execution_eligible"] is False


def test_live_buy_refusal_skips_without_booking() -> None:
    cli = FakeCli()
    bot = live_agent(cli, FakeExecutor(refuse="insufficient_balance"))
    records = bot.tick(NOW)
    skip = next(r for r in records if r["kind"] == "skip_entry")
    assert skip["reason"] == "live_buy_refused" and skip["category"] == "insufficient_balance"
    assert bot.state.positions == {} and bot.state.cash_usd_micros == 1_000 * USD


def test_live_sell_realizes_observed_usdc_and_refusal_halts() -> None:
    cli, ex = FakeCli(), FakeExecutor()
    bot = live_agent(cli, ex)
    bot.tick(NOW)
    units = bot.state.positions[TOKEN].units
    cli.sell_ratio = (60, 100)
    sell = next(r for r in bot.tick(NOW + 60) if r["kind"] == "live_sell")
    assert ex.orders[-1] == ("sell", TOKEN, units)
    assert sell["proceeds_usd_micros"] == 150 * USD - 50_000
    assert sell["pnl_usd_micros"] == 150 * USD - 50_000 - 200_050_000

    cli2, ex2 = FakeCli(), FakeExecutor()
    bot2 = live_agent(cli2, ex2)
    bot2.tick(NOW)
    ex2.refuse = "insufficient_balance"
    cli2.sell_ratio = (60, 100)
    records = bot2.tick(NOW + 60)
    assert any(r["kind"] == "halt_review" and r["reason"] == "live_sell_refused" for r in records)
    assert TOKEN in bot2.state.positions
    assert not any(r["kind"] == "live_buy" for r in records)


@pytest.mark.parametrize("kind", ["live_intent", "live_unresolved", "live_buy"])
def test_resume_refuses_journal_with_unsummarized_live_activity(kind: str) -> None:
    summary = {
        "kind": "tick_summary",
        "cash_usd_micros": 1,
        "positions": [],
        "realized_pnl_usd_micros": 0,
        "closed_trades": 0,
        "winning_trades": 0,
    }
    with pytest.raises(ValueError):
        reconcile_with_journal(AgentState(1), [summary, {"kind": kind}])
    with pytest.raises(ValueError):
        reconcile_with_journal(AgentState(1), [{"kind": kind}])


class Nominator:
    name = "fomo"

    def __init__(self, addresses: list[str], fail: bool = False) -> None:
        self.calls, self.addresses, self.fail = 0, addresses, fail

    def nominate(self, chain_id: int) -> list[str]:
        self.calls += 1
        if self.fail:
            raise SourceFailure("authorization_required")
        return self.addresses


def test_nominated_token_is_enriched_but_never_admitted_on_its_own() -> None:
    other = "0x" + "cd" * 20
    cli = FakeCli()
    cli.hot, cli.signals = [], []
    nominator = Nominator([other, other, "not-an-address"])
    bot = SpotAgent(OnchainosSource(cli, clock=lambda: NOW), AgentConfig(), nominators=[nominator])
    records = bot.tick(NOW)
    assessed = [r for r in records if r["kind"] == "assessment"]
    assert [r["token"] for r in assessed] == [other]
    # Enrichment ran (price-info was queried for it) but no smart money: no entry.
    assert any(c[:2] == ("token", "price-info") and other in c for c in cli.calls)
    assert assessed[0]["action"] != "flip"
    assert not any(r["kind"] in ("paper_buy", "live_buy") for r in records)
    assert records[-1]["provider_calls"] >= nominator.calls == 1


def test_failing_nominator_does_not_block_other_discovery() -> None:
    cli = FakeCli()
    bot = SpotAgent(
        OnchainosSource(cli, clock=lambda: NOW),
        AgentConfig(initial_cash_usd_micros=1_000 * USD),
        nominators=[Nominator([], fail=True)],
    )
    assert any(r["kind"] == "paper_buy" for r in bot.tick(NOW))
