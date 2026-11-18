"""End-to-end integration tests: EQN discovery -> SpotAgent -> LaunchpadSafetyGate."""

import json
from typing import Any

from oasis_agent.intelligence import (
    EqnDiscoveryProvider,
    VerifiedSymbolEntry,
    VerifiedSymbolRegistry,
    parse_news_item,
)
from oasis_agent.launch.launchpad_safety import (
    DecisionStatus,
    LaunchpadArchetype,
    LaunchpadSafetyDecision,
    LaunchpadSafetyEvidence,
    LaunchpadSafetyPolicy,
)
from oasis_agent.launch.onchainos_source import BASE_USDC, OnchainosSource, SourceFailure
from oasis_agent.launch.opportunity import RISK_FLAGS, Market
from oasis_agent.launch.spot_agent import (
    AgentConfig,
    SpotAgent,
    make_launchpad_safety_evaluator,
)
from oasis_agent.models.common import sha256_hex

BASE_CHAIN_ID = 8453
TOKEN = "0x" + "11" * 20
TOKEN_ALT = "0x" + "22" * 20
POOL = "0x" + "33" * 20
BLOCK_HASH = "0x" + "44" * 32
CODE_HASH_TOKEN = sha256_hex(b"launchpad-token-runtime-bytecode")
CODE_HASH_ALT = sha256_hex(b"alt-token-runtime-bytecode")
GENESIS_S = 1_800_000_000
GENESIS_MS = GENESIS_S * 1_000
USD = 1_000_000


class _ScriptedOnchainosCli:
    """Deterministic OnchainOS stub supporting enrichment, quotes, and price checks."""

    def __init__(self, *, now_s: int = GENESIS_S, emit_signals: bool = True) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.now_s = now_s
        self.emit_signals = emit_signals
        self.tokens_data: dict[str, dict[str, Any]] = {
            TOKEN: self._default_token_data(TOKEN, "LPT"),
            TOKEN_ALT: self._default_token_data(TOKEN_ALT, "ALT"),
        }

    def _default_token_data(self, token: str, symbol: str) -> dict[str, Any]:
        return {
            "symbol": symbol,
            "price_info": {
                "priceChange5M": "-0.8",
                "priceChange1H": "20",
                "priceChange24H": "40",
                "volume24H": "50000",
                "liquidity": "250000.5",
                "price": "0.95",
                "maxPrice": "1",
            },
            "security": dict.fromkeys(RISK_FLAGS, False) | {"buyTaxes": "0", "sellTaxes": "0"},
            "advanced": {
                "devRugPullTokenCount": "0",
                "createTime": str((GENESIS_S - 86_400) * 1000),
            },
            "token_address": token,
        }

    def __call__(self, argv: Any) -> bytes:
        cmd = tuple(argv)
        self.calls.append(cmd)
        key = (cmd[0], cmd[1])
        options = dict(zip(cmd[2::2], cmd[3::2], strict=True))
        data: Any
        if key == ("token", "hot-tokens"):
            data = []
        elif key == ("signal", "list"):
            if not self.emit_signals:
                data = []
            else:
                data = [
                    {
                        "token": {
                            "tokenAddress": token,
                            "symbol": entry["symbol"],
                            "holders": "2000",
                            "top10HolderPercent": "30.5",
                            "marketCapUsd": "900000",
                        },
                        "timestamp": str((self.now_s - 60) * 1000),
                        "walletType": "1",
                        "triggerWalletAddress": "0x01,0x02,0x03,0x04",
                        "soldRatioPercent": "0",
                    }
                    for token, entry in self.tokens_data.items()
                ]
        elif key == ("token", "price-info"):
            token = options["--address"].lower()
            entry = self.tokens_data[token]
            data = [
                entry["price_info"]
                | {
                    "holders": "2000",
                    "top10HoldPercent": "30.5",
                }
            ]
        elif key == ("security", "token-scan"):
            token = options["--tokens"].split(":", 1)[1].lower()
            data = [self.tokens_data[token]["security"]]
        elif key == ("token", "advanced-info"):
            token = options["--address"].lower()
            data = self.tokens_data[token]["advanced"]
        elif key == ("market", "prices"):
            data = []
        elif key == ("swap", "quote"):
            src, dst, amount = options["--from"], options["--to"], int(options["--amount"])
            out = amount * 1_000 if src == BASE_USDC else amount // 1_000 * 99 // 100
            data = [
                {
                    "chainIndex": "8453",
                    "swapMode": "exactIn",
                    "dexRouterList": [{"dexProtocol": {"dexName": "test"}}],
                    "fromToken": {"tokenContractAddress": src, "isHoneyPot": False, "taxRate": "0"},
                    "toToken": {"tokenContractAddress": dst, "isHoneyPot": False, "taxRate": "0"},
                    "fromTokenAmount": str(amount),
                    "toTokenAmount": str(out),
                    "priceImpactPercent": "-0.2",
                    "tradeFee": "0.01",
                }
            ]
        else:
            raise AssertionError(f"unexpected CLI command: {cmd!r}")
        return json.dumps({"ok": True, "data": data}).encode()


def _make_evidence(now_s: int, **overrides: object) -> LaunchpadSafetyEvidence:
    defaults: dict[str, object] = {
        "chain_id": BASE_CHAIN_ID,
        "token": TOKEN,
        "pool": POOL,
        "archetype": LaunchpadArchetype.DOPPLER_MULTICURVE,
        "block_number": 58_000_000 + (now_s - GENESIS_S) * 5,
        "block_hash": BLOCK_HASH,
        "block_timestamp_s": now_s,
        "block_timestamp_ms": now_s * 1_000,
        "observed_at_ms": now_s * 1_000 + 200,
        "source_count": 2,
        "buy_tax_bps": 120,
        "sell_tax_bps": 120,
        "sell_enabled": True,
        "unlock_timestamp_ms": GENESIS_MS,
        "genesis_timestamp_ms": GENESIS_MS,
        "decay_start_fee_bps": 8_000,
        "decay_end_fee_bps": 120,
        "decay_duration_ms": 10_000,
        "simulated_buy_in": 1_000_000,
        "simulated_buy_out": 988_000,
        "simulated_sell_out": 970_000,
        "exit_liquidity_usd_micros": 50_000_000_000,
        "creator_same_block_buy_bps": 200,
        "bundled_cluster_bps": 800,
        "creator_prior_rug_count": 0,
        "raw_sha256": sha256_hex(f"lp-evidence:{now_s}".encode()),
    }
    defaults.update(overrides)
    return LaunchpadSafetyEvidence(**defaults)  # type: ignore[arg-type]


def _make_eqn_provider(now_s: int, *, freeze_token: bool = False) -> EqnDiscoveryProvider:
    registry = VerifiedSymbolRegistry(
        [
            VerifiedSymbolEntry(
                chain_id=BASE_CHAIN_ID,
                symbol="LPT",
                token_address=TOKEN,
                code_sha256=CODE_HASH_TOKEN,
            ),
            VerifiedSymbolEntry(
                chain_id=BASE_CHAIN_ID,
                symbol="ALT",
                token_address=TOKEN_ALT,
                code_sha256=CODE_HASH_ALT,
            ),
        ]
    )
    items = [
        parse_news_item(
            {
                "id": "cat-1",
                "published_ts": now_s * 1_000 - 2_000,
                "channels": ["binance"],
                "i18n": {"en": "Binance Alpha lists LPT on Base"},
                "coins": [{"symbol": "LPT"}],
            },
            received_ts_ms=now_s * 1_000,
            raw_sha256=sha256_hex(b"eqn-listing"),
        )
    ]
    if freeze_token:
        items.append(
            parse_news_item(
                {
                    "id": "cat-freeze",
                    "published_ts": now_s * 1_000 - 1_000,
                    "channels": ["main"],
                    "i18n": {"en": "LPT bridge exploit detected and halted"},
                    "coins": [{"symbol": "LPT"}],
                },
                received_ts_ms=now_s * 1_000,
                raw_sha256=sha256_hex(b"eqn-freeze"),
            )
        )
    return EqnDiscoveryProvider(
        registry,
        items=items,
        max_age_ms=300_000,
        clock_ms=lambda: now_s * 1_000,
    )


def test_eqn_spot_launchpad_defer_during_decay_then_allow_after_decay() -> None:
    cli = _ScriptedOnchainosCli(now_s=GENESIS_S + 5)
    del cli.tokens_data[TOKEN_ALT]
    current_time_s = GENESIS_S + 5
    eqn_provider = _make_eqn_provider(GENESIS_S + 5)

    def lookup(market: Market, now: int) -> LaunchpadSafetyEvidence | None:
        if market.token != TOKEN:
            return None
        if now == GENESIS_S + 5:
            return _make_evidence(
                now,
                buy_tax_bps=4_060,
                simulated_buy_out=594_000,
                simulated_sell_out=580_000,
            )
        return _make_evidence(now)

    evaluator = make_launchpad_safety_evaluator(LaunchpadSafetyPolicy(), lookup)
    bot = SpotAgent(
        OnchainosSource(cli, clock=lambda: current_time_s),
        AgentConfig(initial_cash_usd_micros=1_000 * USD),
        nominators=[eqn_provider],
        launchpad_safety_evaluator=evaluator,
    )

    # Tick 1 (during anti-snipe decay at +5s): defers without calling swap/quote.
    records_defer = bot.tick(GENESIS_S + 5)
    assessed = next(r for r in records_defer if r["kind"] == "assessment")
    assert assessed["action"] == "flip"
    skip = next(r for r in records_defer if r["kind"] == "skip_entry")
    assert skip["reason"] == "launchpad_defer_until_decay"
    assert skip["earliest_safe_entry_ms"] == GENESIS_MS + 10_000
    assert skip["launchpad_reasons"] == ["active_decay_fee"]
    assert skip["execution_eligible"] is False
    assert not any(c[:2] == ("swap", "quote") for c in cli.calls)
    assert bot.state.positions == {}
    assert bot.state.cash_usd_micros == 1_000 * USD
    assert TOKEN not in bot.state.cooldown_until

    # Tick 2 (after decay at +10s): passes launchpad safety gate and opens paper_buy.
    current_time_s = GENESIS_S + 10
    cli.now_s = GENESIS_S + 10
    cli.calls.clear()
    records_buy = bot.tick(GENESIS_S + 10)
    buy = next(r for r in records_buy if r["kind"] == "paper_buy")
    assert buy["token"] == TOKEN
    assert buy["execution_eligible"] is False
    assert buy["mode"] == "shadow"
    assert any(c[:2] == ("swap", "quote") for c in cli.calls)
    assert TOKEN in bot.state.positions


def test_soft_rug_and_excessive_tax_reject_without_quote_calls() -> None:
    cases: tuple[tuple[dict[str, object], str], ...] = (
        (
            {
                "buy_tax_bps": 4_060,
                "simulated_buy_out": 594_000,
                "simulated_sell_out": 580_000,
                "creator_same_block_buy_bps": 900,
            },
            "excessive_creator_snipe",
        ),
        ({"bundled_cluster_bps": 2_500}, "excessive_bundled_cluster"),
        ({"creator_prior_rug_count": 1}, "creator_prior_rug"),
        ({"sell_tax_bps": 500, "simulated_sell_out": 900_000}, "excessive_sell_tax"),
        ({"sell_enabled": False, "simulated_sell_out": 0}, "sell_disabled"),
        ({"source_count": 1}, "insufficient_sources"),
        ({"observed_at_ms": (GENESIS_S + 10) * 1_000 + 5_001}, "stale_evidence"),
        ({"archetype": LaunchpadArchetype.UNKNOWN_CUSTOM}, "unknown_launchpad_archetype"),
    )
    for overrides, expected_reason in cases:
        cli = _ScriptedOnchainosCli(now_s=GENESIS_S + 10)
        del cli.tokens_data[TOKEN_ALT]

        def lookup(
            _market: Market,
            now: int,
            ov: dict[str, object] = overrides,
        ) -> LaunchpadSafetyEvidence:
            return _make_evidence(now, **ov)

        evaluator = make_launchpad_safety_evaluator(None, lookup)
        bot = SpotAgent(
            OnchainosSource(cli, clock=lambda: GENESIS_S + 10),
            AgentConfig(initial_cash_usd_micros=1_000 * USD),
            nominators=[_make_eqn_provider(GENESIS_S + 10)],
            launchpad_safety_evaluator=evaluator,
        )
        records = bot.tick(GENESIS_S + 10)
        skip = next(r for r in records if r["kind"] == "skip_entry")
        assert skip["reason"] == "launchpad_safety_rejected"
        assert expected_reason in skip["launchpad_reasons"]
        assert not any(c[:2] == ("swap", "quote") for c in cli.calls)
        assert not any(r["kind"] == "paper_buy" for r in records)


def test_fail_closed_missing_evidence_token_mismatch_and_source_failure() -> None:
    # 1. fail_closed_on_missing=True blocks entry when evidence lookup returns None.
    cli1 = _ScriptedOnchainosCli(now_s=GENESIS_S + 10)
    bot1 = SpotAgent(
        OnchainosSource(cli1, clock=lambda: GENESIS_S + 10),
        AgentConfig(initial_cash_usd_micros=1_000 * USD),
        launchpad_safety_evaluator=make_launchpad_safety_evaluator(
            LaunchpadSafetyPolicy(),
            lambda _m, _now: None,
            fail_closed_on_missing=True,
        ),
    )
    rec1 = bot1.tick(GENESIS_S + 10)
    skip1 = next(r for r in rec1 if r["kind"] == "skip_entry")
    assert skip1["reason"] == "launchpad_safety_rejected"
    assert skip1["launchpad_reasons"] == ["missing_launchpad_evidence"]
    assert not any(c[:2] == ("swap", "quote") for c in cli1.calls)

    # 2. Evidence returning a mismatched token address fails closed.
    cli2 = _ScriptedOnchainosCli(now_s=GENESIS_S + 10)
    bot2 = SpotAgent(
        OnchainosSource(cli2, clock=lambda: GENESIS_S + 10),
        AgentConfig(initial_cash_usd_micros=1_000 * USD),
        launchpad_safety_evaluator=make_launchpad_safety_evaluator(
            LaunchpadSafetyPolicy(),
            lambda _m, now: _make_evidence(now, token=TOKEN_ALT),
        ),
    )
    rec2 = bot2.tick(GENESIS_S + 10)
    skip2 = next(r for r in rec2 if r["kind"] == "skip_entry" and r["token"] == TOKEN)
    assert skip2["reason"] == "launchpad_safety_rejected"
    assert skip2["launchpad_reasons"] == ["launchpad_evidence_token_mismatch"]

    # 3. SourceFailure in lookup fails closed as launchpad_safety_rejected, not quote_refused.
    cli3 = _ScriptedOnchainosCli(now_s=GENESIS_S + 10)

    def broken_lookup(_m: Market, _now: int) -> LaunchpadSafetyEvidence | None:
        raise SourceFailure("launchpad_rpc_unavailable")

    bot3 = SpotAgent(
        OnchainosSource(cli3, clock=lambda: GENESIS_S + 10),
        AgentConfig(initial_cash_usd_micros=1_000 * USD),
        launchpad_safety_evaluator=make_launchpad_safety_evaluator(
            LaunchpadSafetyPolicy(),
            broken_lookup,
        ),
    )
    rec3 = bot3.tick(GENESIS_S + 10)
    skip3 = next(r for r in rec3 if r["kind"] == "skip_entry")
    assert skip3["reason"] == "launchpad_safety_rejected"
    assert skip3["launchpad_reasons"] == ["launchpad_evidence_unavailable"]
    assert not any(c[:2] == ("swap", "quote") for c in cli3.calls)


def test_skipped_launchpad_candidate_does_not_consume_open_slot_and_none_preserves_legacy() -> None:
    cli = _ScriptedOnchainosCli(now_s=GENESIS_S + 10)

    def mixed_evaluator(market: Market, now: int) -> LaunchpadSafetyDecision | None:
        if market.token == TOKEN:
            return LaunchpadSafetyDecision(
                status=DecisionStatus.DEFER_UNTIL_DECAY,
                earliest_safe_entry_ms=GENESIS_MS + 20_000,
                effective_buy_tax_bps=4_000,
                schedule_fee_bps=4_000,
                round_trip_loss_bps=4_200,
                reasons=("active_decay_fee",),
                raw_sha256=sha256_hex(b"defer-first"),
            )
        return LaunchpadSafetyDecision(
            status=DecisionStatus.APPROVED,
            earliest_safe_entry_ms=now * 1_000,
            effective_buy_tax_bps=100,
            schedule_fee_bps=100,
            round_trip_loss_bps=250,
            reasons=(),
            raw_sha256=sha256_hex(b"allow-second"),
        )

    bot = SpotAgent(
        OnchainosSource(cli, clock=lambda: GENESIS_S + 10),
        AgentConfig(initial_cash_usd_micros=1_000 * USD, max_positions=1),
        launchpad_safety_evaluator=mixed_evaluator,
    )
    records = bot.tick(GENESIS_S + 10)
    assert any(
        r["kind"] == "skip_entry"
        and r["token"] == TOKEN
        and r["reason"] == "launchpad_defer_until_decay"
        for r in records
    )
    # The single open slot was NOT consumed by the deferred token, so TOKEN_ALT can enter.
    buys = [r for r in records if r["kind"] == "paper_buy"]
    assert len(buys) == 1 and buys[0]["token"] == TOKEN_ALT

    # Legacy mode (launchpad_safety_evaluator=None) permits TOKEN without launchpad evidence.
    cli_legacy = _ScriptedOnchainosCli(now_s=GENESIS_S + 10)
    legacy_bot = SpotAgent(
        OnchainosSource(cli_legacy, clock=lambda: GENESIS_S + 10),
        AgentConfig(initial_cash_usd_micros=1_000 * USD),
    )
    legacy_records = legacy_bot.tick(GENESIS_S + 10)
    assert any(r["kind"] == "paper_buy" and r["token"] == TOKEN for r in legacy_records)
    assert not any(
        r["kind"] == "skip_entry" and r.get("reason", "").startswith("launchpad_")
        for r in legacy_records
    )


def test_eqn_freeze_blocks_nomination_before_enrichment_or_launchpad_gate() -> None:
    cli = _ScriptedOnchainosCli(now_s=GENESIS_S + 10, emit_signals=False)
    frozen_provider = _make_eqn_provider(GENESIS_S + 10, freeze_token=True)
    calls = 0

    def spy_lookup(_market: Market, now: int) -> LaunchpadSafetyEvidence | None:
        nonlocal calls
        calls += 1
        return _make_evidence(now)

    bot = SpotAgent(
        OnchainosSource(cli, clock=lambda: GENESIS_S + 10),
        AgentConfig(initial_cash_usd_micros=1_000 * USD),
        nominators=[frozen_provider],
        launchpad_safety_evaluator=make_launchpad_safety_evaluator(
            LaunchpadSafetyPolicy(),
            spy_lookup,
        ),
    )
    records = bot.tick(GENESIS_S + 10)
    assert frozen_provider.frozen_symbols == ("LPT",)
    assert calls == 0
    assert records[-1]["coverage"]["discovered"] == 0
    assert not any(r["kind"] == "paper_buy" for r in records)
