"""Synthetic raw NAV proofs and integer risk boundaries; no execution authority."""

import json
from contextlib import closing
from dataclasses import dataclass, replace
from pathlib import Path

import pytest
from eth_abi.abi import encode
from eth_hash.auto import keccak

from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.live import valuation
from oasis_agent.live.exact_risk import ExactRisk
from oasis_agent.live.state import LoopState, Position, StateStore
from oasis_agent.live.valuation import capture_nav, replay_nav
from oasis_agent.models.common import BlockRef, canonical_json
from tests.unit.test_live_lp import CONFIG, SQRT, T0, TICK
from tests.unit.test_live_receipt_costs import position_config
from tests.unit.test_live_revert_cash import WalletFixture
from tests.unit.test_lp_receipts import PRIMARY, SECONDARY, sessions


@dataclass
class NavFixture(WalletFixture):
    position_enabled: bool = True
    nft_count: int | None = None
    eth_price: int = 2500 * 10**8
    usdc_price: int = 99_000_000
    age: int = 10
    eth_age: int | None = None
    usdc_age: int | None = None
    round_id: int = 2
    answered: int = 2
    feed_decimals: int = 8
    description: str | None = None
    last0: int = 2**256 - 2**128
    growth0: int = 2**128
    growth1: int = 3 * 2**128
    sqrt: int = SQRT
    tick: int = TICK

    def post(self, payload: bytes) -> bytes:
        req = json.loads(payload)
        method, params = req["method"], req["params"]
        value = None
        if method == "eth_call":
            target, selector = params[0]["to"], params[0]["data"][:10]
            if target == CONFIG.position_manager and selector == "0x70a08231":
                count = int(self.position_enabled) if self.nft_count is None else self.nft_count
                return canonical_json(
                    {
                        "jsonrpc": "2.0",
                        "id": req["id"],
                        "result": "0x" + encode(["uint256"], [count]).hex(),
                    }
                )
            if target in {valuation.ETH_USD_FEED, valuation.USDC_USD_FEED}:
                eth = target == valuation.ETH_USD_FEED
                age = self.eth_age if eth else self.usdc_age
                age = self.age if age is None else age
                answers = {
                    "decimals()": (["uint8"], [self.feed_decimals]),
                    "description()": (
                        ["string"],
                        [self.description or ("ETH / USD" if eth else "USDC / USD")],
                    ),
                    "latestRoundData()": (
                        ["uint80", "int256", "uint256", "uint256", "uint80"],
                        [
                            self.round_id,
                            self.eth_price if eth else self.usdc_price,
                            self.timestamp - age - 1,
                            self.timestamp - age,
                            self.answered,
                        ],
                    ),
                }
            else:
                answers = {
                    "getSlot0(bytes32)": (
                        ["uint160", "int24", "uint24", "uint24"],
                        [self.sqrt, self.tick, 0, 500],
                    ),
                    "getPositionInfo(bytes32,bytes32)": (
                        ["uint128", "uint256", "uint256"],
                        [self.manager_liquidity, self.last0, 0],
                    ),
                    "getFeeGrowthInside(bytes32,int24,int24)": (
                        ["uint256", "uint256"],
                        [self.growth0, self.growth1],
                    ),
                }
            for sig, (types, values) in answers.items():
                if selector == "0x" + keccak(sig.encode())[:4].hex():
                    value = "0x" + encode(types, values).hex()
        if value is None:
            return super().post(payload)
        return canonical_json({"jsonrpc": "2.0", "id": req["id"], "result": value})


def run_nav(
    store: EvidenceStore, f: NavFixture, peer: NavFixture | None = None, position: bool = True
):
    f = replace(f, position_enabled=position)
    if peer is not None:
        peer = replace(peer, position_enabled=position)
    return capture_nav(
        store,
        position_config(),
        BlockRef(CONFIG.chain_id, f.block_number, f.block_hash, f.timestamp),
        Position(123, -100, 100, 77, 0) if position else None,
        sources=(PRIMARY, SECONDARY),
        sessions=sessions({PRIMARY: f, SECONDARY: peer or replace(f)}),
    )


@pytest.fixture(autouse=True)
def synthetic_feed_pin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(valuation, "FEED_RUNTIME", "0x" + keccak(bytes.fromhex("6000")).hex())


@pytest.mark.parametrize("position", [False, True])
def test_raw_nav_includes_both_feeds_and_modular_uncollected_fees(
    tmp_path: Path, position: bool
) -> None:
    with closing(EvidenceStore(tmp_path / "proof.db")) as store:
        value, refs = run_nav(store, NavFixture(), position=position)
        assert (value.fee0, value.fee1) == ((154, 231) if position else (0, 0))
        expected = (
            value.wallet.native * 2500 * 10**8 // 10**20
            + value.wallet.token * 99 // 100
            + (value.principal0 + value.fee0) * 2500 * 10**8 // 10**20
            + (value.principal1 + value.fee1) * 99 // 100
        )
        assert value.nav_micros == expected
        assert value.usdc_answer == 99_000_000  # never substitute a one-dollar peg
        for ref in refs:
            assert (
                replay_nav(store, ref, position_config()).reconstruct(store, position_config())
                == value
            )


@pytest.mark.parametrize(
    "fault",
    [
        "age",
        "future",
        "round",
        "answered",
        "zero",
        "negative",
        "decimals",
        "description",
        "owner",
        "liquidity",
        "tick",
        "peer",
        "peer_round",
        "signed_fee_overflow",
    ],
)
def test_incomplete_or_disagreeing_nav_never_emits_valuation(tmp_path: Path, fault: str) -> None:
    fixture = NavFixture()
    peer = None
    if fault == "age":
        fixture.age = CONFIG.oracle_max_age_seconds + 1
    if fault == "future":
        fixture.age = -1
    if fault == "round":
        fixture.round_id = 0
    if fault == "answered":
        fixture.answered = 1
    if fault == "zero":
        fixture.usdc_price = 0
    if fault == "negative":
        fixture.eth_price = -1
    if fault == "decimals":
        fixture.feed_decimals = 6
    if fault == "description":
        fixture.description = "unrelated feed"
    if fault == "owner":
        fixture.position_owner = "0x" + "11" * 20
    if fault == "liquidity":
        fixture.manager_liquidity = 76
    if fault == "tick":
        fixture.tick += 2
    if fault == "peer":
        peer = replace(fixture, usdc_price=99_000_001)
    if fault == "peer_round":
        peer = replace(fixture, round_id=3, answered=3)
    if fault == "signed_fee_overflow":
        fixture.last0 = 0
        fixture.growth0 = (2**255 + 76) // 77
    with closing(EvidenceStore(tmp_path / "proof.db")) as store, pytest.raises(ValueError):
        run_nav(store, fixture, peer)


@pytest.mark.parametrize("feed", ["eth_age", "usdc_age"])
def test_both_feeds_enforce_owner_freshness_ceiling(tmp_path: Path, feed: str) -> None:
    c = position_config()
    assert c.usdc_oracle_max_age_seconds == c.oracle_max_age_seconds == 1800
    fixture = NavFixture(**{feed: 1800})
    with closing(EvidenceStore(tmp_path / "ok.db")) as store:
        value, refs = run_nav(store, fixture, position=False)
        assert value.usdc_answer == fixture.usdc_price
        deadline = valuation.nav_fresh_until(store, refs, c, sources=(PRIMARY, SECONDARY))
        assert deadline == fixture.timestamp
    # Include the previously refused live round age and the superseded 25-hour ceiling.
    for age in (1801, 5261, 90_000):
        with (
            closing(EvidenceStore(tmp_path / f"stale-{age}.db")) as store,
            pytest.raises(ValueError, match="stale"),
        ):
            run_nav(store, NavFixture(**{feed: age}), position=False)


@pytest.mark.parametrize(
    ("price", "accepted"),
    [(98_000_000, True), (102_000_000, True), (97_999_999, False), (102_000_001, False)],
)
def test_usdc_answer_outside_stable_band_refuses_nav(
    tmp_path: Path, price: int, accepted: bool
) -> None:
    fixture = NavFixture(usdc_price=price)
    with closing(EvidenceStore(tmp_path / "proof.db")) as store:
        if accepted:
            assert run_nav(store, fixture, position=False)[0].usdc_answer == price
        else:
            with pytest.raises(ValueError, match="stable band"):
                run_nav(store, fixture, position=False)


def test_integer_loss_one_micro_boundary_and_recovery(tmp_path: Path) -> None:
    state = LoopState("live", exact_risk=ExactRisk())
    risk = state.integer_risk()
    risk.observe(day="2026-10-15", at=T0, nav=500_000_000)
    assert risk.observe(day="2026-10-15", at=T0 + 1, nav=300_000_001) == (199_999_999, 199_999_999)
    assert risk.observe(day="2026-10-15", at=T0 + 2, nav=300_000_000) == (200_000_000, 200_000_000)
    store = StateStore(tmp_path / "state.json")
    store.save(state)
    assert store.load("live").integer_risk().losses(300_000_000) == (200_000_000, 200_000_000)


def test_integer_risk_preserves_daily_gross_cumulative_net_and_external_flows() -> None:
    risk = ExactRisk()
    for i, nav in enumerate((500, 350, 500, 350)):
        result = risk.observe(day="2026-10-15", at=T0 + i * 3600, nav=nav * 1_000_000)
    assert result == (300_000_000, 150_000_000)
    assert risk.observe(day="2026-10-16", at=T0 + 4 * 3600, nav=250_000_000) == (
        100_000_000,
        250_000_000,
    )
    assert risk.observe(
        day="2026-10-16", at=T0 + 4 * 3600 + 1, nav=350_000_000, flow=100_000_000
    ) == (100_000_000, 250_000_000)
    assert risk.observe(
        day="2026-10-16", at=T0 + 4 * 3600 + 2, nav=300_000_000, flow=-50_000_000
    ) == (100_000_000, 250_000_000)


@pytest.mark.parametrize("bad", [True, 0.1, -1, 2**256])
def test_exact_risk_rejects_noninteger_or_invalid_state(bad: object) -> None:
    with pytest.raises(ValueError):
        ExactRisk(daily_locked=bad).validate()


def test_legacy_migration_rounds_loss_basis_up_and_does_not_erase_loss() -> None:
    state = LoopState(
        "live",
        initial_nav_usd=500.0000001,
        checkpoint_nav_usd=350.0000001,
        daily_loss_usd=150.0000001,
        checkpoint_at=T0,
        day="2026-10-15",
    )
    risk = state.integer_risk()
    assert risk.migrated_legacy
    assert risk.losses(300_000_000) == (200_000_002, 200_000_001)
