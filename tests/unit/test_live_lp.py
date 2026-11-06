"""Live LP loop: calldata shape, range math, loss latch, pending recovery and halts.

The full sign -> broadcast -> mint -> burn -> recenter path is exercised against an
Anvil fork of Base in `.scratch/fork_live_e2e.py`; these tests pin the pure parts
and the fail-closed state machine with a scripted RPC.
"""

import time
from dataclasses import asdict, replace
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest
from eth_abi.abi import decode

from oasis_agent.live.chain import ChainError, sign_transaction
from oasis_agent.live.config import LiveConfig
from oasis_agent.live.intent import SubmittedTransaction, pending_plan_hash
from oasis_agent.live.price_guard import ExternalPriceSource
from oasis_agent.live.runner import (
    MAX_SETTLEMENT_FAILURES,
    LiveLoop,
    Market,
)
from oasis_agent.live.state import LoopState, Pending, Position, StateStore, day_of
from oasis_agent.live.v4lp import (
    PoolKey,
    burn_call,
    eth_value_share,
    liquidity_for_amounts,
    mint_call,
    position_amounts,
    price_from_sqrt,
    range_ticks,
    swap_call,
)
from oasis_agent.live.valuation import AccountValue
from oasis_agent.live.wallet import WalletBalance
from oasis_agent.models.chain import BlockSnapshot
from oasis_agent.models.common import BlockRef
from oasis_agent.uniswap_v4.liquidity_math import sqrt_price_at_tick

ROOT = Path(__file__).resolve().parents[2]
CONFIG = LiveConfig.load(ROOT / "configs/live.base-eth-usdc.json")
KEY = CONFIG.pool
TICK = -198_080
SQRT = sqrt_price_at_tick(TICK) + 12345


def test_mint_plan_settles_pair_and_sweeps_native_change() -> None:
    data = mint_call(KEY, -198_480, -197_500, 10**15, 10**17, 250 * 10**6, CONFIG.account, 99)
    unlock, deadline = decode(["bytes", "uint256"], data[4:])
    actions, params = decode(["bytes", "bytes[]"], unlock)
    assert deadline == 99
    assert list(actions) == [0x02, 0x0D, 0x14]  # MINT_POSITION, SETTLE_PAIR, SWEEP
    sweep = decode(["address", "address"], params[2])
    assert sweep == ("0x0000000000000000000000000000000000000000", CONFIG.account)


def test_burn_plan_takes_both_currencies_to_owner() -> None:
    actions, params = decode(
        ["bytes", "bytes[]"],
        decode(["bytes", "uint256"], burn_call(KEY, 7, 1, 2, CONFIG.account, 5)[4:])[0],
    )
    assert list(actions) == [0x03, 0x11]
    assert decode(["uint256", "uint128", "uint128", "bytes"], params[0])[:3] == (7, 1, 2)
    assert decode(["address", "address", "address"], params[1])[2] == CONFIG.account


def test_swap_binds_minimum_output_in_both_swap_and_take() -> None:
    data = swap_call(KEY, True, 10**17, 240 * 10**6, 77)
    commands, inputs, deadline = decode(["bytes", "bytes[]", "uint256"], data[4:])
    assert commands == bytes([0x10]) and deadline == 77
    actions, params = decode(["bytes", "bytes[]"], inputs[0])
    assert list(actions) == [0x06, 0x0C, 0x0F]
    assert decode(["address", "uint256"], params[2])[1] == 240 * 10**6


def test_hooked_or_erc20_currency0_pools_are_refused() -> None:
    with pytest.raises(ValueError, match="hooked"):
        PoolKey(KEY.currency0, KEY.currency1, 500, 10, "0x" + "11" * 20)
    with pytest.raises(ValueError, match="native"):
        PoolKey("0x" + "22" * 20, KEY.currency1, 500, 10)


def test_range_is_spacing_aligned_and_contains_the_price() -> None:
    lower, upper = range_ticks(TICK, 490, 10)
    assert lower % 10 == 0 and upper % 10 == 0 and lower <= TICK < upper
    assert upper - lower == 980


def test_liquidity_never_exceeds_either_amount() -> None:
    lower, upper = range_ticks(TICK, 490, 10)
    eth, usdc = 10**17, 250 * 10**6
    liquidity = liquidity_for_amounts(SQRT, lower, upper, eth, usdc)
    used0, used1 = position_amounts(SQRT, lower, upper, liquidity)
    assert 0 < used0 <= eth and 0 < used1 <= usdc
    assert max(used0 / eth, used1 / usdc) > 0.999
    assert 0.4 < eth_value_share(SQRT, lower, upper) < 0.6


def test_loss_day_rolls_at_utc_plus_eight() -> None:
    assert day_of(1_791_475_199) == "2026-10-08"  # 23:59:59 UTC+8
    assert day_of(1_791_475_200) == "2026-10-09"


class FakeSigner:
    account = "0x" + "ab" * 20

    def sign(self, digest: bytes) -> tuple[int, int, int]:
        raise RuntimeError("socket down")


def test_signer_failure_is_a_retryable_chain_error() -> None:
    with pytest.raises(ChainError, match="signing authority unavailable"):
        sign_transaction(FakeSigner(), [8453, 0, 1, 2, 21000, b"\x00" * 20, 0, b"", []])


BLOCK_HASH = "0x" + "77" * 32


def mined(tx_hash: str, status: str = "0x1", **fields: Any) -> dict[str, Any]:
    """A receipt for `tx_hash` from the configured account, mined in block 100."""
    receipt = {
        "transactionHash": tx_hash,
        "blockHash": BLOCK_HASH,
        "blockNumber": "0x64",
        "from": CONFIG.account,
        "to": CONFIG.position_manager,
        "status": status,
        "gasUsed": "0x1",
        "effectiveGasPrice": "0x1",
        "logs": [],
    }
    return receipt | fields


class ScriptedRpc:
    """Answers only what a halted/pending tick may touch. Head is block 110."""

    def __init__(
        self,
        *,
        receipt: dict[str, Any] | None = None,
        latest_nonce: int = 0,
        canonical: str = BLOCK_HASH,
    ) -> None:
        self._receipt, self._latest_nonce, self._canonical = receipt, latest_nonce, canonical

    def receipt(self, tx_hash: str) -> dict[str, Any] | None:
        return self._receipt

    def uint(self, method: str, params: list[Any]) -> int:
        assert method == "eth_blockNumber"
        return 110

    def call(self, method: str, params: list[Any]) -> Any:
        assert method == "eth_getBlockByNumber"
        return {"hash": self._canonical}

    def nonce(self, account: str, block: str = "pending") -> int:
        return self._latest_nonce


def loop_with(tmp_path: Path, rpc: Any, **state: Any) -> LiveLoop:
    store = StateStore(tmp_path / "shadow.state.json")
    loop = LiveLoop(CONFIG, rpc, store, mode="shadow", log=lambda _line: None)
    for key, value in state.items():
        setattr(loop.state, key, value)
    return loop


def market(**overrides: Any) -> Market:
    values = {
        "block": 10,
        "block_hash": "0x" + "12" * 32,
        "timestamp": 1_791_470_000,
        "sqrt_price": SQRT,
        "tick": TICK,
        "price": 2500.0,
        "oracle": 2500.0,
        "eth": 2 * 10**17,
        "usdc": 0,
        "position_liquidity": 0,
    }
    values.update(overrides)
    if "sqrt_price" in overrides and "oracle" not in overrides:
        values["oracle"] = price_from_sqrt(values["sqrt_price"], 18, 6)
    answer = int(values["oracle"] * 10**8)
    wallet = WalletBalance(
        BlockSnapshot(
            BlockRef(CONFIG.chain_id, values["block"], values["block_hash"], values["timestamp"]),
            "0x" + "00" * 32,
        ),
        3,
        values["eth"],
        values["usdc"],
        "0x" + "11" * 32,
    )
    values.setdefault(
        "valuation",
        AccountValue(
            wallet,
            values["sqrt_price"],
            values["tick"],
            answer,
            10**8,
            0,
            0,
            0,
            0,
            values["eth"] * answer // 10**20 + values["usdc"],
        ),
    )
    return Market(**values)


def test_halted_state_takes_no_action_and_persists(tmp_path: Path) -> None:
    loop = loop_with(tmp_path, ScriptedRpc(), halted="loss cap breached")
    assert loop.tick().startswith("halted")
    assert StateStore(tmp_path / "shadow.state.json").load("shadow").halted == "loss cap breached"


def test_pending_submission_is_resolved_before_anything_else(tmp_path: Path) -> None:
    pending = Pending("swap", "0x" + "cd" * 32, 3, 0)
    loop = loop_with(tmp_path, ScriptedRpc(latest_nonce=4), pending=pending)
    assert "HALT" in loop.tick()
    assert loop.state.halted is not None and "nonce 3 consumed" in loop.state.halted


def test_reverted_pending_counts_and_clears(tmp_path: Path) -> None:
    tx = "0x" + "ef" * 32
    receipt = mined(tx, "0x0", gasUsed="0x5208")
    loop = loop_with(tmp_path, ScriptedRpc(receipt=receipt), pending=Pending("mint", tx, 3, 0))
    assert "REVERTED" in loop.tick()
    assert loop.state.pending is None and loop.state.consecutive_reverts == 1
    assert loop.state.gas_spent_wei == 21000


def test_loss_cap_breach_latches(tmp_path: Path) -> None:
    loop = loop_with(tmp_path, ScriptedRpc())
    loop.observe = lambda: market(eth=10**17)  # type: ignore[method-assign]
    loop.state.initial_nav_usd = 800.0
    loop.state.day, loop.state.checkpoint_nav_usd = day_of(1_791_470_000), 800.0
    loop.state.checkpoint_at = 1_791_470_000
    assert "HALT" in loop.tick()
    assert loop.state.halted is not None and "loss cap" in loop.state.halted


def test_oracle_deviation_blocks_action(tmp_path: Path) -> None:
    loop = loop_with(tmp_path, ScriptedRpc())
    loop.observe = lambda: market(oracle=2600.0)  # type: ignore[method-assign]
    assert "deviation" in loop.tick()
    assert loop.state.position is None


def test_unbalanced_wallet_swaps_then_mints_on_paper(tmp_path: Path) -> None:
    loop = loop_with(tmp_path, ScriptedRpc(), pins={"x": "y"})
    loop.runtime_pins = lambda _h=None: dict(CONFIG.expected_pins)  # type: ignore[method-assign]
    loop.state.paper_eth = 2 * 10**17
    loop.observe = lambda: market(
        eth=loop.state.paper_eth,  # type: ignore[method-assign]
        usdc=loop.state.paper_usdc,
    )
    assert "paper swap" in loop.tick()
    assert "paper mint" in loop.tick()
    position = loop.state.position
    assert isinstance(position, Position) and position.lower <= TICK < position.upper
    assert loop.state.paper_eth >= CONFIG.gas_reserve_wei - 10**14


class FixedExternalPrice(ExternalPriceSource):
    def __init__(self, eth: Fraction | None) -> None:
        self.eth, self.calls = eth, 0

    def fetch_usd_price(self, chain_id: int, currency: str) -> Fraction | None:
        self.calls += 1
        return self.eth if currency == CONFIG.pool.currency0 else Fraction(1)


def guarded_loop(tmp_path: Path, external: ExternalPriceSource, **state: Any) -> LiveLoop:
    store = StateStore(tmp_path / "shadow.state.json")
    loop = LiveLoop(
        CONFIG, ScriptedRpc(), store, mode="shadow", log=lambda _l: None, external_prices=external
    )
    for key, value in state.items():
        setattr(loop.state, key, value)
    loop.state.pins = {"x": "y"}
    loop.runtime_pins = lambda _h=None: dict(CONFIG.expected_pins)  # type: ignore[method-assign]
    return loop


@pytest.mark.parametrize("eth", [Fraction(2400), None])
def test_external_price_divergence_or_absence_defers_new_allocation(
    tmp_path: Path, eth: Fraction | None
) -> None:
    loop = guarded_loop(tmp_path, FixedExternalPrice(eth))
    loop.observe = lambda: market(eth=2 * 10**17)  # type: ignore[method-assign]
    history = len(loop.state.history)
    line = loop.tick()
    assert "wait: external price guard" in line and "swap deferred" in line
    assert loop.state.halted is None and loop.state.position is None
    assert len(loop.state.history) == history  # waits never evict halts/actions


def test_agreeing_external_price_allows_allocation(tmp_path: Path) -> None:
    external = FixedExternalPrice(Fraction(2501))
    loop = guarded_loop(tmp_path, external, paper_eth=2 * 10**17)
    loop.observe = lambda: market(eth=2 * 10**17)  # type: ignore[method-assign]
    assert "paper swap" in loop.tick()
    assert external.calls == 2


def test_exit_never_consults_external_price(tmp_path: Path) -> None:
    lower, upper = range_ticks(TICK, 490, 10)
    external = FixedExternalPrice(None)
    loop = guarded_loop(
        tmp_path, external, position=Position(-1, lower, upper, 10**14, 1_791_469_000)
    )
    loop.observe = lambda: market(  # type: ignore[method-assign]
        tick=lower - 50, sqrt_price=sqrt_price_at_tick(lower - 50), position_liquidity=10**14
    )
    assert "paper burn" in loop.tick()
    assert external.calls == 0


@pytest.mark.parametrize("bps", [0, 201, True])
def test_external_deviation_threshold_is_bounded(tmp_path: Path, bps: Any) -> None:
    with pytest.raises(ValueError, match="1-200 bps"):
        LiveLoop(
            CONFIG,
            ScriptedRpc(),
            StateStore(tmp_path / "s.json"),
            mode="shadow",
            max_external_deviation_bps=bps,
        )


def test_out_of_range_position_burns_after_cooldown(tmp_path: Path) -> None:
    lower, upper = range_ticks(TICK, 490, 10)
    position = Position(-1, lower, upper, 10**14, 1_791_469_000)
    loop = loop_with(tmp_path, ScriptedRpc(), position=position, pins={"x": "y"})
    loop.runtime_pins = lambda _h=None: dict(CONFIG.expected_pins)  # type: ignore[method-assign]
    moved = market(
        tick=lower - 50, sqrt_price=sqrt_price_at_tick(lower - 50), position_liquidity=10**14
    )
    loop.observe = lambda: moved  # type: ignore[method-assign]
    assert "paper burn" in loop.tick()
    assert loop.state.position is None


def test_runtime_pin_change_halts(tmp_path: Path) -> None:
    lower, upper = range_ticks(TICK, 490, 10)
    loop = loop_with(
        tmp_path,
        ScriptedRpc(),
        position=Position(-1, lower, upper, 10**14, 0),
    )
    loop.runtime_pins = lambda _h=None: (
        CONFIG.expected_pins
        | {  # type: ignore[method-assign]
            "usdc_implementation": "0x" + "00" * 31 + "01"
        }
    )
    loop.observe = lambda: market(
        tick=upper + 50,  # type: ignore[method-assign]
        sqrt_price=sqrt_price_at_tick(upper + 50),
        position_liquidity=10**14,
    )
    assert "HALT" in loop.tick()
    assert loop.state.halted is not None and "usdc_implementation" in loop.state.halted


def test_live_mode_requires_the_dedicated_signer(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "live.state.json")
    with pytest.raises(ValueError, match="requires a signer"):
        LiveLoop(CONFIG, ScriptedRpc(), store, mode="live")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="dedicated account"):
        LiveLoop(
            CONFIG,
            ScriptedRpc(),
            store,
            mode="live",  # type: ignore[arg-type]
            signer=FakeSigner(),
        )


def test_config_refuses_capital_above_owner_cap() -> None:
    with pytest.raises(ValueError, match="USD 500"):
        replace(CONFIG, max_capital_usd=501).validate()


class MintedRpc(ScriptedRpc):
    def __init__(self, receipt: dict[str, Any]) -> None:
        super().__init__(receipt=receipt, latest_nonce=4)


def test_confirmed_mint_without_nft_log_keeps_pending(tmp_path: Path) -> None:
    tx = "0x" + "aa" * 32
    pending = Pending("mint", tx, 3, 0, {"lower": 0, "upper": 10, "liquidity": 5})
    loop = loop_with(tmp_path, MintedRpc(mined(tx)), pending=pending)
    assert loop.tick().startswith("skip")
    assert loop.state.pending is pending and loop.state.position is None


def test_confirmed_mint_records_exact_liquidity_and_token(tmp_path: Path) -> None:
    owner = "0x" + "0" * 24 + CONFIG.account[2:]
    log = {
        "address": CONFIG.position_manager,
        "topics": [
            "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef",
            "0x" + "0" * 64,
            owner,
            "0x" + f"{4242:064x}",
        ],
    }
    tx = "0x" + "bb" * 32
    receipt = mined(tx, gasUsed="0x2", effectiveGasPrice="0x3", logs=[log])
    intent = {"lower": -100, "upper": 100, "liquidity": 77}
    loop = loop_with(tmp_path, MintedRpc(receipt), pending=Pending("mint", tx, 3, 0, intent))
    assert "confirmed" in loop.tick()
    assert loop.state.pending is None and loop.state.gas_spent_wei == 6
    assert loop.state.position is not None
    assert (loop.state.position.token_id, loop.state.position.liquidity) == (4242, 77)


def test_live_liquidity_mismatch_halts_instead_of_burning_unbounded(tmp_path: Path) -> None:
    lower, upper = range_ticks(TICK, 490, 10)
    loop = loop_with(tmp_path, ScriptedRpc(), position=Position(9, lower, upper, 10**14, 0))
    loop.mode = "live"
    loop.observe = lambda: market(  # type: ignore[method-assign]
        tick=upper + 50, sqrt_price=sqrt_price_at_tick(upper + 50), position_liquidity=10**13
    )
    assert "HALT" in loop.tick()
    assert loop.state.halted is not None and "liquidity" in loop.state.halted


class DropRpc(ScriptedRpc):
    def __init__(self, *, receipt: dict[str, Any] | None, latest: int, pending: int) -> None:
        super().__init__(receipt=receipt, latest_nonce=latest)
        self._pending = pending

    def nonce(self, account: str, block: str = "pending") -> int:
        return self._latest_nonce if block == "latest" else self._pending


@pytest.mark.parametrize(("latest", "pending"), [(4, 4), (3, 4), (3, 3)])
def test_live_pending_cannot_be_forgotten_from_provider_absence(
    tmp_path: Path, latest: int, pending: int
) -> None:
    store = StateStore(tmp_path / "live.state.json")
    state = store.load("live")
    state.pending = Pending("swap", "0x" + "cc" * 32, 3, 0)
    store.save(state)
    before = store.path.read_bytes()
    loop = LiveLoop.recovery(
        CONFIG, DropRpc(receipt=None, latest=latest, pending=pending), store, "live"
    )
    with pytest.raises(ValueError, match="cannot be forgotten"):
        loop.drop_pending()
    assert loop.state.pending is not None and store.path.read_bytes() == before


def test_live_mode_requires_a_second_provider(tmp_path: Path) -> None:
    signer = FakeSigner()
    signer.account = CONFIG.account
    with pytest.raises(ValueError, match="second"):
        LiveLoop(
            CONFIG,
            ScriptedRpc(),
            StateStore(tmp_path / "l.json"),
            mode="live",  # type: ignore[arg-type]
            signer=signer,
        )


T0 = 1_791_470_000  # 2026-10-08 22:33:20 UTC+8; midnight is T0 + 5_200


def test_recovery_does_not_erase_locked_daily_loss() -> None:
    # Review counterexample: 500 -> 350 -> 500 -> 350 within one day, one checkpoint apart.
    state = LoopState(mode="live")
    for i, nav in enumerate((500.0, 350.0, 500.0, 350.0)):
        state.roll_day(T0 - 20_000 + i * 3600, nav)
    daily, cumulative = state.losses(350.0)
    assert daily == pytest.approx(300.0) and cumulative == pytest.approx(150.0)


def test_missing_midnight_observation_charges_the_new_day() -> None:
    state = LoopState(mode="live")
    state.roll_day(T0 + 5_200 - 60, 500.0)  # 23:59 UTC+8
    state.roll_day(T0 + 5_200 + 60, 250.0)  # 00:01, no midnight observation
    assert state.day == day_of(T0 + 5_260) != day_of(T0)
    assert state.losses(250.0) == (pytest.approx(250.0), pytest.approx(250.0))


def test_intra_checkpoint_drawdown_counts_without_compounding_noise() -> None:
    state = LoopState(mode="live")
    state.roll_day(T0, 500.0)
    for i, nav in enumerate((499.0, 500.0, 499.0, 500.0, 300.0), start=1):
        state.roll_day(T0 + i * 30, nav)
    assert state.losses(300.0)[0] == pytest.approx(200.0)
    state.roll_day(T0 + 200, 500.0)
    assert state.losses(500.0)[0] == 0.0


def test_acknowledged_flow_is_not_profit_or_loss() -> None:
    state = LoopState(mode="live")
    state.roll_day(T0, 500.0)
    state.roll_day(T0 + 30, 400.0, flow_usd=-100.0)  # operator withdrawal
    assert state.losses(400.0) == (0.0, 0.0)
    state.roll_day(T0 + 60, 900.0, flow_usd=+500.0)  # deposit cannot mask a loss
    state.roll_day(T0 + 90, 800.0)
    assert state.losses(800.0) == (pytest.approx(100.0), pytest.approx(100.0))


def live_loop(tmp_path: Path, rpc: Any, secondary: Any = None, **state: Any) -> LiveLoop:
    loop = loop_with(tmp_path, rpc, **state)
    loop.mode, loop.secondary = "live", secondary if secondary is not None else rpc
    bind_pending(loop.state.pending, loop.state.position)
    return loop


def bind_pending(
    pending: Pending | None, position: Position | None = None, config: LiveConfig = CONFIG
) -> None:
    """Synthetic metadata for receipt-state tests; no signature or real submission."""
    if pending is None:
        return
    pending.to = pending.to or config.position_manager
    pending.transaction = SubmittedTransaction(
        pending.tx_hash,
        config.chain_id,
        config.account,
        pending.nonce,
        pending.to,
        0,
        "0x" + "11" * 32,
        1_000_000,
        config.max_fee_per_gas_wei,
        config.priority_fee_wei,
    )
    pending.plan_hash = pending_plan_hash(
        asdict(config),
        pending.kind,
        pending.intent,
        pending.transaction,
        asdict(position) if position else None,
        tuple(asdict(r) for r in pending.wallet_evidence),
    )


def test_unexplained_wallet_change_halts(tmp_path: Path) -> None:
    loop = live_loop(tmp_path, ScriptedRpc(), last_units=[2 * 10**17, 0, 0])
    loop.observe = lambda: market(eth=10**17)  # type: ignore[method-assign]
    assert "HALT" in loop.tick()
    assert loop.state.halted is not None and "unexplained wallet change" in loop.state.halted


def test_flow_acknowledgement_cannot_bypass_missing_legacy_nav_proofs(tmp_path: Path) -> None:
    loop = live_loop(tmp_path, ScriptedRpc(), last_units=[10**17, 0, 0], acknowledge_flow=True)
    loop.state.roll_day(T0 - 30, 250.0)
    loop.observe = lambda: market(eth=2 * 10**17, oracle=2500.0)  # type: ignore[method-assign]
    loop._decide = lambda _m: None  # type: ignore[method-assign]
    assert "retained NAV checkpoint missing" in loop.tick()
    assert loop.state.external_flow_usd == 0.0 and loop.state.acknowledge_flow
    assert loop.state.initial_nav_usd == 250.0


def test_receipt_for_another_transaction_never_clears_a_burn(tmp_path: Path) -> None:
    # Review counterexample: pending burn A, provider answers with successful receipt B.
    position = Position(123, -10, 10, 5, 0)
    a, b = "0x" + "0a" * 32, "0x" + "0b" * 32
    loop = live_loop(
        tmp_path,
        ScriptedRpc(receipt=mined(b), latest_nonce=4),
        pending=Pending("burn", a, 3, 0, {}, CONFIG.position_manager),
        position=position,
    )
    assert "skip" in loop.tick()
    assert loop.state.pending is not None and loop.state.position == position
    assert loop.state.halted is not None and "does not match" in loop.state.halted


def test_successful_burn_without_nft_burn_log_halts(tmp_path: Path) -> None:
    position = Position(123, -10, 10, 5, 0)
    a = "0x" + "0a" * 32
    loop = live_loop(
        tmp_path,
        ScriptedRpc(receipt=mined(a), latest_nonce=4),
        pending=Pending("burn", a, 3, 0, {}, CONFIG.position_manager),
        position=position,
    )
    assert "HALT" in loop.tick()
    assert loop.state.position == position and loop.state.pending is not None


def test_receipt_waits_for_depth_canonicality_and_provider_agreement(tmp_path: Path) -> None:
    a = "0x" + "0a" * 32
    pending = Pending("swap", a, 3, 10**10, {}, CONFIG.position_manager)
    shallow = mined(a, blockNumber=hex(109))
    loop = live_loop(tmp_path, ScriptedRpc(receipt=shallow, latest_nonce=4), pending=pending)
    assert "waiting" in loop.tick() and loop.state.pending is not None
    orphan = ScriptedRpc(receipt=mined(a), latest_nonce=4, canonical="0x" + "99" * 32)
    loop = live_loop(tmp_path, orphan, pending=pending)
    assert "waiting" in loop.tick() and loop.state.pending is not None
    loop = live_loop(
        tmp_path,
        ScriptedRpc(receipt=mined(a), latest_nonce=4),
        secondary=ScriptedRpc(receipt=mined(a, status="0x0")),
        pending=pending,
    )
    assert "disagree" in loop.tick() and loop.state.pending is not None
    loop = live_loop(
        tmp_path,
        ScriptedRpc(receipt=mined(a), latest_nonce=4),
        secondary=ScriptedRpc(receipt=mined(a)),
        pending=pending,
    )
    # Quorum/depth alone no longer substitutes for retained whole-cost evidence.
    assert "cost evidence is unavailable" in loop.tick() and loop.state.pending is pending


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("effectiveGasPrice", "0x2"),
        ("from", "0x" + "ab" * 20),
        ("to", "0x" + "ab" * 20),
        ("blockNumber", "0x65"),
        ("l1Fee", "0x10"),
    ],
)
def test_receipt_quorum_retains_pending_on_cost_or_identity_disagreement(
    tmp_path: Path, field: str, value: str
) -> None:
    tx = "0x" + "0a" * 32
    pending = Pending("swap", tx, 3, 10**10, {}, CONFIG.position_manager)
    loop = live_loop(
        tmp_path,
        ScriptedRpc(receipt=mined(tx)),
        secondary=ScriptedRpc(receipt=mined(tx, **{field: value})),
        pending=pending,
    )
    assert "disagree" in loop.tick()
    restored = loop.store.load("shadow")
    assert restored.pending == pending
    assert restored.gas_spent_wei == 0 and restored.actions_today == 0


def test_live_recovery_cannot_resolve_pending_without_quorum(tmp_path: Path) -> None:
    tx = "0x" + "0a" * 32
    loop = live_loop(tmp_path, ScriptedRpc(receipt=mined(tx)), pending=Pending("swap", tx, 3, 0))
    loop.secondary = None  # recovery() intentionally has no second provider
    assert "second" in loop.tick()
    assert loop.state.pending is not None and loop.state.actions_today == 0


def test_receipt_depth_is_required_from_both_providers(tmp_path: Path) -> None:
    class LaggingRpc(ScriptedRpc):
        def uint(self, method: str, params: list[Any]) -> int:
            return 101

    tx = "0x" + "0a" * 32
    loop = live_loop(
        tmp_path,
        ScriptedRpc(receipt=mined(tx)),
        secondary=LaggingRpc(receipt=mined(tx)),
        pending=Pending("swap", tx, 3, 10**10),
    )
    assert "waiting" in loop.tick()
    assert loop.state.pending is not None and loop.state.gas_spent_wei == 0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("blockNumber", None),
        ("effectiveGasPrice", "-0x1"),
        ("gasUsed", "0x00"),
        ("status", "0x2"),
        ("logs", {}),
    ],
)
def test_malformed_agreed_receipt_cannot_change_state(
    tmp_path: Path, field: str, value: Any
) -> None:
    tx = "0x" + "0a" * 32
    loop = live_loop(
        tmp_path,
        ScriptedRpc(receipt=mined(tx, **{field: value})),
        pending=Pending("swap", tx, 3, 10**10),
    )
    assert "skip" in loop.tick()
    assert loop.state.pending is not None and loop.state.gas_spent_wei == 0


def test_repeated_settlement_failure_halts_instead_of_spending_budget(tmp_path: Path) -> None:
    tx = "0x" + "ab" * 32
    loop = loop_with(tmp_path, ScriptedRpc(), pending=Pending("approve_usdc", tx, 3, 0))
    calls = []

    def failing() -> str:
        calls.append(1)
        raise ChainError("approval cash unresolved (details redacted)")

    loop._resolve_pending = failing  # type: ignore[method-assign]
    limit = MAX_SETTLEMENT_FAILURES
    for _ in range(limit - 1):
        assert loop.tick().startswith("skip: approval cash unresolved")
        assert loop.state.halted is None
    assert loop.tick().startswith("skip:")
    assert loop.state.halted is not None and "settlement failed" in loop.state.halted
    assert loop.state.history[-1]["event"] == "halt"
    # Halted: later ticks no longer attempt settlement or spend RPC.
    assert loop.tick().startswith("halted:")
    assert len(calls) == limit
    assert loop.state.pending is not None  # recovery, not the loop, clears it


def test_settlement_success_resets_failure_count(tmp_path: Path) -> None:
    tx = "0x" + "ab" * 32
    loop = loop_with(tmp_path, ScriptedRpc(), pending=Pending("approve_usdc", tx, 3, 0))
    outcomes = iter([ChainError("transient"), "waiting on receipt"])

    def flaky() -> str:
        result = next(outcomes)
        if isinstance(result, Exception):
            raise result
        return result

    loop._resolve_pending = flaky  # type: ignore[method-assign]
    loop.tick()
    assert loop.settlement_failures == 1
    assert loop.tick() == "waiting on receipt"
    assert loop.settlement_failures == 0 and loop.state.halted is None


@pytest.mark.parametrize("interval", [None, 30, 300, 3600])
def test_run_polls_at_or_slower_than_the_configured_rate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, interval: int | None
) -> None:
    loop = loop_with(tmp_path, ScriptedRpc())
    sleeps: list[float] = []
    monkeypatch.setattr("oasis_agent.live.runner.time.sleep", sleeps.append)
    monkeypatch.setattr(loop, "tick", lambda: "hold")
    loop.run(max_ticks=2, poll_seconds=interval)
    assert sleeps == [CONFIG.poll_seconds if interval is None else interval] * 2


@pytest.mark.parametrize("interval", [0, 1, CONFIG.poll_seconds - 1, 3601, 300.0, True, "300"])
def test_run_refuses_faster_or_non_integer_polling_before_any_tick(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, interval: Any
) -> None:
    loop = loop_with(tmp_path, ScriptedRpc())
    monkeypatch.setattr(loop, "tick", lambda: pytest.fail("refused interval must not tick"))
    with pytest.raises(ValueError, match="poll interval"):
        loop.run(max_ticks=1, poll_seconds=interval)


class LightRpc:
    """Serves only the light hold's head, slot0 and liquidity reads."""

    def __init__(self, tick: int, liquidity: int, *, age: int = 2, fail: bool = False) -> None:
        self.tick, self.liquidity, self.age, self.fail, self.calls = tick, liquidity, age, fail, 0

    def block(self) -> dict[str, Any]:
        self.calls += 1
        return {
            "number": hex(500),
            "hash": "0x" + "ab" * 32,
            "timestamp": hex(int(time.time()) - self.age),
        }

    def call_abi(self, to, signature, types, values, returns, block="latest"):
        self.calls += 1
        if self.fail:
            raise ChainError("provider dropped")
        if signature == "getSlot0(bytes32)":
            return (SQRT, self.tick, 0, 500)
        assert signature == "getPositionLiquidity(uint256)"
        return (self.liquidity,)


HELD = Position(77, TICK - 100, TICK + 100, 5_000, 0)
