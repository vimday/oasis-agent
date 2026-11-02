"""Owner feed ceiling is enforced at loading and cannot silently rebind history."""

from dataclasses import asdict, replace

import pytest

from oasis_agent.live.config import LiveConfig
from oasis_agent.live.state import StateStore
from oasis_agent.models.common import canonical_json, sha256_hex
from tests.unit.test_live_lp import CONFIG


@pytest.mark.parametrize("field", ["oracle_max_age_seconds", "usdc_oracle_max_age_seconds"])
@pytest.mark.parametrize("age", [1801, 5261, 90_000, 0, True, 1800.0])
def test_feed_policy_cannot_be_widened_in_an_operator_file(field, age):
    raw = asdict(CONFIG)
    raw[field] = age
    with pytest.raises(ValueError, match=field):
        LiveConfig.from_json(canonical_json(raw))


@pytest.mark.parametrize("field", ["oracle_max_age_seconds", "usdc_oracle_max_age_seconds"])
@pytest.mark.parametrize("age", [1, 60, 1800])
def test_feed_policy_allows_stricter_integer_ceilings(field, age):
    raw = asdict(CONFIG)
    raw[field] = age
    loaded = LiveConfig.from_json(canonical_json(raw))
    assert getattr(loaded, field) == age


def test_stricter_policy_does_not_reset_or_rebind_existing_live_history(tmp_path):
    # Construct only a synthetic historical digest; the old config is not admitted.
    old = replace(CONFIG, usdc_oracle_max_age_seconds=90_000)
    store = StateStore(tmp_path / "live.state.json")
    store.bind(sha256_hex(canonical_json(asdict(old))))
    state = store.load("live")
    state.halted = "observation refused"
    store.save(state)
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    strict = replace(CONFIG, usdc_oracle_max_age_seconds=1800)
    with pytest.raises(ValueError, match="configuration changed"):
        StateStore(store.path).bind(
            sha256_hex(canonical_json(asdict(strict))), require_existing=True
        )
    assert before == {p.name: p.read_bytes() for p in tmp_path.iterdir()}
