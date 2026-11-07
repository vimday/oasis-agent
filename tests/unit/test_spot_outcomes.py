"""Forward outcome labels: every assessment is labelled at each horizon or `unobserved`."""

import json
from fractions import Fraction

from oasis_agent.launch.outcomes import HORIZONS, MAX_LATENESS_SECONDS, OutcomeTracker, parse_prices
from oasis_agent.launch.spot_agent import AgentState, state_from_json, state_to_json
from oasis_agent.models.common import canonical_json

TOKEN = "0x" + "ab" * 20
OTHER = "0x" + "cd" * 20
T0 = 1_800_000_000


def test_each_horizon_labels_once_with_exact_return() -> None:
    tracker = OutcomeTracker()
    assert tracker.register(TOKEN, "T", "flip", 6000, T0, Fraction(1, 3))
    assert tracker.due(T0 + HORIZONS[0] - 1) == []
    assert tracker.due(T0 + HORIZONS[0]) == [TOKEN]
    labels = tracker.label(T0 + 900, {TOKEN: (Fraction(2, 5), T0 + 900)})
    assert len(labels) == 1
    # (2/5 - 1/3) / (1/3) = +20.00%
    assert labels[0]["forward_return_bps"] == 2000 and labels[0]["horizon_seconds"] == 900
    assert labels[0]["action"] == "flip" and labels[0]["status"] == "observed"
    later = tracker.label(T0 + 3600, {TOKEN: (Fraction(1, 6), T0 + 3600)})
    assert [x["forward_return_bps"] for x in later] == [-5000]
    final = tracker.label(T0 + 14_400, {TOKEN: (Fraction(1, 3), T0 + 14_400)})
    assert final[0]["forward_return_bps"] == 0
    assert tracker.pending == {}


def test_missing_stale_or_early_price_is_unobserved_not_zero() -> None:
    for observed in (
        None,
        (Fraction(1), T0 + 900 + MAX_LATENESS_SECONDS + 1),
        (Fraction(1), T0 + 899),
    ):
        tracker = OutcomeTracker()
        tracker.register(TOKEN, "T", "watch", 0, T0, Fraction(1))
        prices = {} if observed is None else {TOKEN: observed}
        labels = tracker.label(T0 + 900, prices)
        assert labels[0]["status"] == "unobserved" and "forward_return_bps" not in labels[0]


def test_late_tick_labels_every_overdue_horizon_and_drops_finished_samples() -> None:
    tracker = OutcomeTracker()
    tracker.register(TOKEN, "T", "skip", 0, T0, Fraction(1))
    labels = tracker.label(T0 + 20_000, {TOKEN: (Fraction(1), T0 + 20_000)})
    assert [x["horizon_seconds"] for x in labels] == list(HORIZONS)
    assert all(x["status"] == "unobserved" for x in labels)  # far past each due time
    assert tracker.pending == {}


def test_register_dedups_and_refuses_unpriced() -> None:
    tracker = OutcomeTracker()
    assert not tracker.register(TOKEN, "T", "watch", 0, T0, None)
    assert not tracker.register(TOKEN, "T", "watch", 0, T0, Fraction(0))
    assert tracker.register(TOKEN, "T", "watch", 0, T0, Fraction(1))
    assert not tracker.register(TOKEN, "T", "watch", 0, T0 + 120, Fraction(1))
    assert tracker.register(OTHER, "O", "watch", 0, T0 + 120, Fraction(1))
    assert tracker.register(TOKEN, "T", "watch", 0, T0 + 300, Fraction(1))


def test_parse_prices_drops_malformed_and_foreign_rows() -> None:
    rows = [
        {"chainIndex": "8453", "tokenContractAddress": TOKEN, "price": "0.5", "time": "1000999"},
        {"chainIndex": "1", "tokenContractAddress": OTHER, "price": "1", "time": "1"},
        {"chainIndex": "8453", "tokenContractAddress": OTHER, "price": "0", "time": "1"},
        {"chainIndex": "8453", "tokenContractAddress": "bad", "price": "1", "time": "1"},
        {"chainIndex": "8453", "tokenContractAddress": OTHER, "price": 1.5, "time": "1"},
    ]
    assert parse_prices(rows, 8453) == {TOKEN: (Fraction(1, 2), 1000)}
    assert parse_prices({"x": 1}, 8453) == {}


def test_state_snapshot_round_trips_exact_prices() -> None:
    state = AgentState(cash_usd_micros=5)
    state.outcomes.register(TOKEN, "T", "flip", 1, T0, Fraction(1, 7))
    restored = state_from_json(json.loads(canonical_json(state_to_json(state))))
    assert restored == state
    assert next(iter(restored.outcomes.pending.values())).price == Fraction(1, 7)
