"""Paper replay exercises GMGN/OKX rank gates and same-size OKX pricing."""

import json
from pathlib import Path

import pytest

from oasis_agent.launch.discovery import DiscoveryPolicy, DiscoveryRequest, DiscoverySignal
from oasis_agent.launch.okx_spot import QuoteCapture, SpotMarket, UsdcValue, replay_quote
from oasis_agent.launch.scoring import Candidate, ScorePolicy, TrendSignal
from oasis_agent.launch.spot_replay import SpotFrame, SpotPolicy, SpotReplay, replay_spot
from oasis_agent.live.config import BASE_USDC
from scripts import replay_base_spot as cli

TOKEN = "0x" + "12" * 20
OTHER = "0x" + "34" * 20
HASH = "a" * 64
UNITS = 1_000_000_000
MILLION = 1_000_000
FIXTURE = Path(__file__).resolve().parents[2] / "configs/meme-candidate.fixture.json"


def leg(at, token, side, usdc, units=UNITS, **overrides):
    buy = side == "buy"

    def meta(address):
        return {
            "tokenContractAddress": address,
            "decimal": "6" if address == BASE_USDC else "9",
            "isHoneyPot": False,
            "taxRate": "0",
        }

    row = {
        "chainIndex": "8453",
        "swapMode": "exactOut" if buy else "exactIn",
        "dexRouterList": [{"dexProtocol": {"dexName": "test"}}],
        "fromTokenAmount": str(usdc if buy else units),
        "toTokenAmount": str(units if buy else usdc),
        "fromToken": meta(BASE_USDC if buy else token),
        "toToken": meta(token if buy else BASE_USDC),
        "tradeFee": "0.1000004",
        "priceImpactPercent": "-0.2",
    }
    return QuoteCapture(
        retrieved_at=at - 1,
        raw_json=json.dumps({"code": "0", "data": [row | overrides]}),
        extra_cost_usd_micros=50_000,
    )


def market(at, token, ask, bid=None):
    return SpotMarket(
        token=token,
        units=UNITS,
        buy=leg(at, token, "buy", ask) if ask is not None else None,
        sell=leg(at, token, "sell", bid if bid is not None else ask - 200_000),
        usdc=UsdcValue(
            low_usd_micros=MILLION,
            high_usd_micros=MILLION,
            observed_at=at - 2,
            evidence_sha256=HASH,
        ),
    )


def frame(at, ask, bid=None, *, ranks=True, fomo=None, tokens=(TOKEN,)):
    base = Candidate.from_json(FIXTURE.read_bytes())
    signals, candidates, markets = [], [], []
    for token in tokens:
        trends = []
        if ranks:
            for provider in ("gmgn", "okx"):
                trend = TrendSignal(
                    provider=provider,
                    chain_id=8453,
                    token=token,
                    observed_at=at - 4,
                    retrieved_at=at - 3,
                    rank=1,
                    window_seconds=300,
                    raw_sha256=HASH,
                )
                trends.append(trend)
                signals.append(
                    DiscoverySignal(**trend.model_dump(), kind="trending", created_at=100)
                )
        if token == fomo:
            signals.append(
                DiscoverySignal(
                    provider="fomo",
                    chain_id=8453,
                    token=token,
                    kind="social",
                    observed_at=at - 2,
                    retrieved_at=at - 1,
                    raw_sha256=HASH,
                )
            )
        candidates.append(
            base.model_copy(
                update={
                    "token": token,
                    "evaluated_at": at,
                    "trends": tuple(trends),
                    "chain": base.chain.model_copy(update={"token": token, "observed_at": at - 2}),
                }
            )
        )
        markets.append(market(at, token, ask, bid))
    return SpotFrame(
        discovery=DiscoveryRequest(
            evaluated_at=at, policy=DiscoveryPolicy(chains=(8453,)), signals=tuple(signals)
        ),
        candidates=tuple(candidates),
        markets=tuple(markets),
    )


def entry_frames(**options):
    return tuple(
        frame(1000 + index * 30, ask * MILLION, **options)
        for index, ask in enumerate((36, 34, 28, 30))
    )


def request(frames, **overrides):
    policy = {
        "score": ScorePolicy(),
        "initial_cash_usd_micros": 100 * MILLION,
        "max_trade_usd_micros": 40 * MILLION,
        "quote_max_age_seconds": 20,
        "slippage_bps": 50,
        "pullback_bps": 800,
        "rebound_bps": 100,
        "reference_samples": 3,
        "reference_max_age_seconds": 120,
    }
    return SpotReplay(
        schema_version="oasis.spot-replay.v1",
        policy=SpotPolicy(**(policy | overrides)),
        frames=tuple(frames),
    )


def test_buy_recovery_and_sell_net_take_profit_without_popularity():
    frames = (
        *entry_frames(),
        frame(1120, None, 31 * MILLION, ranks=False),
        frame(1150, None, 38 * MILLION, ranks=False),
    )
    run = request(frames)
    report = replay_spot(run)
    assert report == replay_spot(run)
    assert [decision.action for decision in report.decisions] == [
        "watch",
        "watch",
        "watch",
        "paper_buy",
        "paper_hold",
        "paper_sell",
    ]
    buy, hold, sell = report.decisions[-3:]
    assert buy.simulated_cash_change_usd_micros == -30_300_001
    assert len(buy.quote_sha256) == 3 and not buy.fomo_observed
    assert hold.net_nav_usd_micros == 100_394_998
    assert sell.reason == "take_profit" and sell.simulated_cash_change_usd_micros == 37_659_999
    assert report.simulated_realized_pnl_usd_micros == 7_359_998
    assert report.cash_usd_micros == 107_359_998 and report.open_position is None
    assert report.mode == "paper" and not report.execution_eligible


@pytest.mark.parametrize(
    ("bids", "max_hold_seconds", "reason"),
    [((25,), 3600, "stop_loss"), ((35, 31), 3600, "trailing_stop"), ((31,), 20, "time_stop")],
)
def test_net_exit_modes(bids, max_hold_seconds, reason):
    frames = entry_frames() + tuple(
        frame(1120 + index * 30, None, bid * MILLION, ranks=False) for index, bid in enumerate(bids)
    )
    result = replay_spot(request(frames, score=ScorePolicy(max_hold_seconds=max_hold_seconds)))
    assert result.decisions[-1].action == "paper_sell" and result.decisions[-1].reason == reason


def test_fomo_breaks_equal_entry_ties_and_cannot_replace_market_sources():
    both = entry_frames(tokens=(TOKEN, OTHER), fomo=OTHER)
    result = replay_spot(request(both))
    assert result.open_position is not None and result.open_position.token == OTHER
    assert result.decisions[-1].fomo_observed
    only_fomo = frame(1090, 30 * MILLION, ranks=False, fomo=TOKEN)
    result = replay_spot(request((*both[:-1], only_fomo)))
    assert result.open_position is None and result.decisions[-1].action == "watch"


def test_exposure_requires_its_full_loss_to_fit_and_stale_exit_keeps_the_position():
    frames = entry_frames()
    no_buy = replay_spot(request(frames, max_daily_loss_usd_micros=30 * MILLION))
    assert no_buy.open_position is None
    next_frame = frame(1120, None, 38 * MILLION, ranks=False)
    later_frame = frame(1150, None, 39 * MILLION, ranks=False)
    stale = next_frame.markets[0].model_copy(update={"sell": frames[-1].markets[0].sell})
    result = replay_spot(
        request((*frames, next_frame.model_copy(update={"markets": (stale,)}), later_frame))
    )
    assert result.open_position is not None and result.remaining_frames == 1
    assert result.decisions[-1].action == "halt_review"
    assert (
        result.decisions[-1].net_nav_usd_micros is None
        and result.simulated_realized_pnl_usd_micros == 0
    )


@pytest.mark.parametrize(
    "wrong",
    [
        {"chainIndex": "1"},
        {"swapMode": "exactOut"},
        {"fromTokenAmount": str(UNITS + 1)},
        {"priceImpactPercent": None},
        {"tradeFee": "-0.2"},
    ],
)
def test_quote_rejects_wrong_chain_side_size_and_unknown_or_invalid_costs(wrong):
    sample = market(1000, TOKEN, 30 * MILLION)
    sample = sample.model_copy(update={"sell": leg(1000, TOKEN, "sell", 30 * MILLION, **wrong)})
    with pytest.raises(ValueError):
        replay_quote(sample, "sell", now=1000, max_age=20, max_impact_bps=100, slippage_bps=50)


def test_sell_fee_above_proceeds_is_unresolved_instead_of_zero():
    sample = market(1000, TOKEN, None, 1)
    with pytest.raises(ValueError, match="negative net proceeds"):
        replay_quote(sample, "sell", now=1000, max_age=20, max_impact_bps=100, slippage_bps=50)


def test_replay_refuses_out_of_order_frames_and_cli_never_overwrites(tmp_path, monkeypatch):
    frames = entry_frames()
    with pytest.raises(ValueError, match="chronological"):
        request(tuple(reversed(frames)))
    source, destination = tmp_path / "frames.json", tmp_path / "paper.json"
    source.write_text(request(frames).model_dump_json())
    monkeypatch.setattr(
        "sys.argv", ["replay_base_spot", "--input", str(source), "--output", str(destination)]
    )
    cli.main()
    original = destination.read_bytes()
    assert json.loads(original)["decisions"][-1]["action"] == "paper_buy"
    with pytest.raises(SystemExit) as failure:
        cli.main()
    assert failure.value.code == 1 and destination.read_bytes() == original
