"""One-transaction-per-tick LP loop: observe, check limits, take at most one step.

Each tick re-derives the next step from chain state, so a crash between steps
is harmless: out of range -> burn; no position and unbalanced -> swap;
no position and balanced -> mint. Shadow mode runs the same decisions on paper
balances and never signs.
"""

import re
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

from eth_hash.auto import keccak

from ..farming.positions import decode_position_info
from ..models.common import (
    BlockRef,
    EvidenceRef,
    canonical_json,
    evidence_ref_from_json,
    sha256_hex,
)
from ..reporting.live_release import require_live_action
from ..reporting.live_status import LiveObservation
from .action_cash import reconcile_action, replay_action_cash
from .action_plan import action_call
from .approval_cash import reconcile_approval, replay_approval_cash
from .chain import (
    ChainError,
    DigestSigner,
    FeePolicy,
    Rpc,
    RpcRejected,
    SubmissionUnknown,
    TransactionSigner,
    TxReverted,
    send_transaction,
)
from .config import LiveConfig
from .intent import SubmittedTransaction, pending_plan_hash
from .positions import collect_position_outcome, replay_position_outcome
from .price_guard import ExternalPriceSource, check_price_deviation
from .receipts import RetainedReceiptCostReader
from .recovery import UNKNOWN_SIGNING_HALT, release_unsigned_attempt
from .revert_cash import reconcile_revert, replay_revert_cash
from .state import Pending, Position, StateStore, day_of
from .v4lp import (
    liquidity_for_amounts,
    position_amounts,
    price_from_sqrt,
    range_ticks,
)
from .valuation import (
    MAX_HEAD_AGE_SECONDS,
    AccountValue,
    NavRefusal,
    capture_nav,
    nav_fresh_until,
    replay_nav,
    replay_nav_quorum,
)
from .wallet import WalletBalance, capture_wallet, replay_wallet

TRANSFER_TOPIC = "0x" + keccak(b"Transfer(address,address,uint256)").hex()
# FiatToken proxies keep the implementation at the zeppelinos slot.
USDC_IMPL_SLOT = "0x7050c9e0f4ca769c69bd3a8ef740bc37934f8e2c036e5a723fd8ee048ed3f8c3"
PENDING_TIMEOUT = 900
ORACLE_DECIMALS = 8  # Base ETH/USD feed decimals(), verified on two providers 2026-10-08
MAX_CONSECUTIVE_REVERTS = 2
# Each failed settlement attempt can spend ~100 requests of a 4096 lifetime budget.
MAX_SETTLEMENT_FAILURES = 5
# Refused proposals released per process before an operator must look at the cause.
MAX_RELEASED_ATTEMPTS = 3
# Holding an in-range position runs a cheap two-provider tick/liquidity check; the full
# NAV, loss-latch, runtime-pin and decision path still runs at least this often.
FULL_OBSERVATION_SECONDS = 3600
# Consecutive unresolved (transport-level) full observations before halting for review.
MAX_OBSERVATION_FAILURES = 3
RECEIPT_CONFIRMATIONS = 3
MIN_SWAP_USD = 3.0
SHADOW_GAS_USD = 0.03


@dataclass(frozen=True, slots=True)
class Market:
    block: int
    block_hash: str
    timestamp: int
    sqrt_price: int
    tick: int
    price: float  # USDC per ETH from the pool
    oracle: float  # Chainlink ETH/USD
    eth: int
    usdc: int
    position_liquidity: int
    valuation: AccountValue | None = None
    nav_evidence: tuple[EvidenceRef, ...] = ()

    @property
    def units(self) -> list[int]:
        return [self.eth, self.usdc, self.position_liquidity]

    @property
    def pool_price(self) -> Fraction:
        return Fraction(self.sqrt_price**2 * 10**12, 2**192)

    @property
    def reference_price(self) -> Fraction:
        if self.valuation is not None:
            return Fraction(self.valuation.eth_answer, self.valuation.usdc_answer)
        return Fraction(str(self.oracle))  # paper fixtures only; live requires retained NAV

    @property
    def deviation_bps(self) -> Fraction:
        return abs(self.pool_price / self.reference_price - 1) * 10_000


class LiveLoop:
    def __init__(
        self,
        config: LiveConfig,
        rpc: Rpc,
        store: StateStore,
        *,
        mode: str,
        signer: DigestSigner | TransactionSigner | None = None,
        stop_file: Path | None = None,
        log: Callable[[str], None] = print,
        secondary: Rpc | None = None,
        receipt_costs: RetainedReceiptCostReader | None = None,
        external_prices: ExternalPriceSource | None = None,
        max_external_deviation_bps: int = 50,
    ) -> None:
        if type(max_external_deviation_bps) is not int or not (
            1 <= max_external_deviation_bps <= 200
        ):
            raise ValueError("external deviation threshold must be 1-200 bps")
        if mode not in {"shadow", "live"}:
            raise ValueError("mode must be shadow or live")
        if (mode == "live") != (signer is not None):
            raise ValueError("live mode requires a signer; shadow mode forbids one")
        if signer is not None and signer.account.lower() != config.account:
            raise ValueError("signer account is not the configured dedicated account")
        if mode == "live" and secondary is None:
            raise ValueError("live mode requires a second, independent RPC provider")
        if mode == "live" and receipt_costs is None:
            raise ValueError("live mode requires retained receipt cost evidence")
        if receipt_costs is not None and (
            receipt_costs.chain_id != config.chain_id
            or receipt_costs.account != config.account
            or receipt_costs.native_decimals != config.decimals0
        ):
            raise ValueError("receipt cost scope differs from live account")
        self.c, self.rpc, self.store, self.mode = config, rpc, store, mode
        self.secondary = secondary
        self.receipt_costs = receipt_costs
        self.external_prices = external_prices
        self.max_external_deviation_bps = max_external_deviation_bps
        self.signer, self.stop_file, self.log = signer, stop_file, log
        if mode == "live":
            store.bind(sha256_hex(canonical_json(asdict(config))))
        self.state = store.load(mode)
        self.fees = FeePolicy(config.max_fee_per_gas_wei, config.priority_fee_wei)
        self.settlement_failures = 0
        self.observation_failures = 0
        self.released_attempts = 0
        # Anchors both providers proved finalized; process-local, so a restart re-verifies.
        self.final_anchors: set[bytes] = set()

    @classmethod
    def recovery(
        cls, config: LiveConfig, rpc: Rpc, store: StateStore, mode: str, *, read_only: bool = False
    ) -> "LiveLoop":
        """A loop over `mode`'s state that can read chain state but never sign."""
        loop = cls.__new__(cls)
        loop.c, loop.rpc, loop.store, loop.mode = config, rpc, store, mode
        loop.signer, loop.secondary, loop.stop_file, loop.log = None, None, None, print
        loop.receipt_costs = None
        loop.external_prices, loop.max_external_deviation_bps = None, 50
        loop.state = store.load(mode, read_only=read_only)
        loop.fees = FeePolicy(config.max_fee_per_gas_wei, config.priority_fee_wei)
        loop.settlement_failures = 0
        loop.observation_failures = 0
        loop.released_attempts = 0
        loop.final_anchors = set()
        return loop

    # ---- observation -------------------------------------------------------------

    def observe(self, pinned_header: dict[str, Any] | None = None) -> Market:
        """Read one canonical block. Every value is bound to its hash, and in live mode
        a second provider must return identical values for that same hash."""
        block = self.rpc.block() if pinned_header is None else pinned_header
        number, timestamp = int(str(block["number"]), 16), int(str(block["timestamp"]), 16)
        if not 0 <= int(time.time()) - timestamp <= MAX_HEAD_AGE_SECONDS:
            raise ChainError("observation head is stale or in the future")
        if self.mode == "live":
            reader = self.receipt_costs
            if reader is None:
                raise ChainError("live observation requires retained NAV evidence")
            try:
                value, refs = capture_nav(
                    reader.store,
                    self.c,
                    BlockRef(self.c.chain_id, number, str(block["hash"]), timestamp),
                    self.state.position,
                    sources=reader.sources,
                    sessions=reader.sessions,
                )
                deadline = nav_fresh_until(reader.store, refs, self.c, sources=reader.sources)
            except NavRefusal as exc:
                raise ChainError("account NAV refused: " + str(exc)) from None
            except Exception:  # noqa: BLE001 - raw provider failures can contain credentials
                raise ChainError("complete account NAV unresolved (details redacted)") from None
            if not timestamp <= int(time.time()) <= deadline:
                raise ChainError("retained NAV expired during acquisition")
            return Market(
                number,
                str(block["hash"]),
                timestamp,
                value.sqrt_price,
                value.tick,
                price_from_sqrt(value.sqrt_price, 18, 6),
                value.eth_answer / 10**8,
                value.wallet.native,
                value.wallet.token,
                self.state.position.liquidity if self.state.position else 0,
                value,
                refs,
            )
        ref = {"blockHash": str(block["hash"]), "requireCanonical": True}
        reads = self._reads(self.rpc, ref)
        if self.secondary is not None:
            try:
                other = self._reads(self.secondary, ref)
            except RpcRejected as exc:
                raise ChainError(f"secondary provider cannot serve block {number}") from exc
            if other != reads:
                raise ChainError(f"providers disagree on state at block {number}")
        for rpc in (self.rpc, self.secondary):
            if rpc is None:
                continue
            closing = rpc.call("eth_getBlockByNumber", [hex(number), False])
            if not isinstance(closing, dict) or any(
                closing.get(field) != block.get(field)
                for field in ("number", "hash", "timestamp", "parentHash")
            ):
                raise ChainError("observation header changed or providers disagree")
        sqrt_price, tick, answer, updated, eth, usdc, liquidity = reads
        if not 0 <= timestamp - updated <= self.c.oracle_max_age_seconds or answer <= 0:
            raise ChainError("ETH/USD oracle is stale")
        if self.mode == "shadow":
            if not self.state.history:
                self.state.paper_eth, self.state.paper_usdc = eth, usdc
            eth, usdc = self.state.paper_eth, self.state.paper_usdc
            liquidity = self.state.position.liquidity if self.state.position else 0
        return Market(
            number,
            str(block["hash"]),
            timestamp,
            sqrt_price,
            tick,
            price_from_sqrt(sqrt_price, self.c.decimals0, self.c.decimals1),
            answer / 10**ORACLE_DECIMALS,
            eth,
            usdc,
            liquidity,
        )

    def _reads(self, rpc: Rpc, ref: dict[str, Any]) -> tuple[int, ...]:
        if self.mode == "live" and rpc.uint("eth_chainId", []) != self.c.chain_id:
            raise ChainError("observation provider is on another chain")
        slot = rpc.call_abi(
            self.c.state_view,
            "getSlot0(bytes32)",
            ["bytes32"],
            [bytes.fromhex(self.c.pool_id[2:])],
            ["uint160", "int24", "uint24", "uint24"],
            ref,
        )
        _, answer, _, updated, _ = rpc.call_abi(
            self.c.eth_usd_feed,
            "latestRoundData()",
            [],
            [],
            ["uint80", "int256", "uint256", "uint256", "uint80"],
            ref,
        )
        eth = int(str(rpc.call("eth_getBalance", [self.c.account, ref])), 16)
        usdc = self._usdc(rpc, ref)
        position = self.state.position
        liquidity = 0
        if position is not None and position.token_id > 0:
            self._position_identity(rpc, position, ref)
            liquidity = self._liquidity(rpc, position.token_id, ref)
        return int(slot[0]), int(slot[1]), int(answer), int(updated), eth, usdc, liquidity

    def _light_hold(self) -> str | None:
        """Cheap two-provider in-range check between full observations; never acts.

        Returns a hold status only when a full NAV observation is under
        FULL_OBSERVATION_SECONDS old and both providers agree, at one canonical block,
        that the recorded position's liquidity is unchanged and the pool tick is inside
        its range. Anything else (no position, stale or missing observation, any
        disagreement, error or out-of-range tick) returns None so the full observation,
        loss latch, runtime pins and decision path run instead.
        """
        s, p = self.state, self.state.position
        last = s.last_observation
        if (
            self.mode != "live"
            or p is None
            or self.secondary is None
            or s.acknowledge_flow
            or not isinstance(last, dict)
            or type(last.get("observed_at")) is not int
            or not 0 <= int(time.time()) - last["observed_at"] < FULL_OBSERVATION_SECONDS
        ):
            return None
        try:
            block = self.rpc.block()
            number, timestamp = int(str(block["number"]), 16), int(str(block["timestamp"]), 16)
            if not 0 <= int(time.time()) - timestamp <= MAX_HEAD_AGE_SECONDS:
                return None
            ref = {"blockHash": str(block["hash"]), "requireCanonical": True}
            seen = []
            for rpc in (self.rpc, self.secondary):
                slot = rpc.call_abi(
                    self.c.state_view,
                    "getSlot0(bytes32)",
                    ["bytes32"],
                    [bytes.fromhex(self.c.pool_id[2:])],
                    ["uint160", "int24", "uint24", "uint24"],
                    ref,
                )
                seen.append((int(slot[1]), self._liquidity(rpc, p.token_id, ref)))
        except Exception:  # noqa: BLE001 - fall back to the full, fully checked observation
            return None
        tick, liquidity = seen[0]
        if seen[0] != seen[1] or liquidity != p.liquidity or not p.lower <= tick < p.upper:
            return None
        s.last_light_check = {
            "block": number,
            "block_timestamp": timestamp,
            "observed_at": int(time.time()),
            "tick": tick,
        }
        return (
            f"block {number} light hold in range [{p.lower},{p.upper}) tick {tick} id {p.token_id}"
        )

    def _position_identity(self, rpc: Rpc, position: Position, ref: dict[str, Any]) -> None:
        owner = rpc.call_abi(
            self.c.position_manager,
            "ownerOf(uint256)",
            ["uint256"],
            [position.token_id],
            ["address"],
            ref,
        )[0]
        key, packed = rpc.call_abi(
            self.c.position_manager,
            "getPoolAndPositionInfo(uint256)",
            ["uint256"],
            [position.token_id],
            ["(address,address,uint24,int24,address)", "uint256"],
            ref,
        )
        try:
            info = decode_position_info(packed)
        except (TypeError, ValueError):
            raise ChainError("observed position info is invalid") from None
        if (
            owner != self.c.account
            or key != self.c.pool.tuple()
            or info.pool_id_prefix != self.c.pool_id[:52]
            or (info.tick_lower, info.tick_upper) != (position.lower, position.upper)
            or info.has_subscriber
        ):
            raise ChainError("position owner, pool, range or subscription changed")

    def _usdc(self, rpc: Rpc, ref: dict[str, Any]) -> int:
        return int(
            rpc.call_abi(
                self.c.pool.currency1,
                "balanceOf(address)",
                ["address"],
                [self.c.account],
                ["uint256"],
                ref,
            )[0]
        )

    def _liquidity(self, rpc: Rpc, token_id: int, ref: dict[str, Any]) -> int:
        return int(
            rpc.call_abi(
                self.c.position_manager,
                "getPositionLiquidity(uint256)",
                ["uint256"],
                [token_id],
                ["uint128"],
                ref,
            )[0]
        )

    def nav_usd(self, m: Market) -> float:
        if m.valuation is not None:
            return m.valuation.nav_micros / 1_000_000  # display only
        if self.mode == "live":
            raise ChainError("live NAV requires replayed exact valuation")
        eth, usdc = m.eth, m.usdc
        if self.state.position is not None and m.position_liquidity:
            p = self.state.position
            a0, a1 = position_amounts(m.sqrt_price, p.lower, p.upper, m.position_liquidity)
            eth, usdc = eth + a0, usdc + a1
        return float(eth / 10**self.c.decimals0 * m.oracle + usdc / 10**self.c.decimals1)

    def runtime_pins(self, block_hash: str | None = None) -> dict[str, str]:
        ref: Any = "latest" if block_hash is None else {"blockHash": block_hash}
        pins: dict[str, str] = {}
        for rpc in (self.rpc, self.secondary):
            if rpc is None:
                continue
            seen = {}
            for name in ("state_view", "position_manager", "universal_router", "permit2"):
                code = str(rpc.call("eth_getCode", [getattr(self.c, name), ref]))
                seen[name] = "0x" + keccak(bytes.fromhex(code[2:])).hex()
            seen["usdc_implementation"] = str(
                rpc.call("eth_getStorageAt", [self.c.pool.currency1, USDC_IMPL_SLOT, ref])
            )
            if pins and seen != pins:
                raise ChainError("providers disagree on runtime code")
            pins = seen
        return pins

    # ---- one tick ----------------------------------------------------------------

    def tick(self) -> str:
        """Run one observe/decide/act step and persist state. Returns a short status."""
        try:
            status = self._tick()
        except ChainError as exc:
            status = f"skip: {exc}"
        self.store.save(self.state)
        self.log(status)
        return status

    def _tick(self) -> str:
        s = self.state
        if self.stop_file is not None and self.stop_file.exists():
            return "stopped: stop file present"
        if s.halted:
            return f"halted: {s.halted}"
        if s.signing_attempt is not None:
            # A refused proposal leaves its reservation; release it only when the authority
            # and both providers prove no signature exists, otherwise halt for reconciliation.
            attempt = s.signing_attempt
            try:
                self._release_unsigned_attempt()
            except Exception:  # noqa: BLE001 - authority/provider errors may contain secrets
                s.halted = UNKNOWN_SIGNING_HALT
                return f"HALT {s.halted}"
            return (
                f"released unsigned {attempt.get('kind')} reservation at nonce "
                f"{attempt.get('nonce')}; re-proposing next tick"
            )
        if s.pending is not None:
            try:
                status = self._resolve_pending()
            except ChainError:
                # A settlement proof that keeps failing would otherwise re-poll every tick
                # until the lifetime RPC budget is gone. Halt; recovery runs separately.
                self.settlement_failures += 1
                if self.settlement_failures >= MAX_SETTLEMENT_FAILURES:
                    s.halted = (
                        f"pending {s.pending.kind} settlement failed "
                        f"{self.settlement_failures} times; run reconcile-only recovery"
                    )
                    s.record("halt", reason=s.halted, tx=s.pending.tx_hash)
                raise
            self.settlement_failures = 0
            return status
        self._check_settlements()
        self._check_nav_checkpoint()
        light = self._light_hold()
        if light is not None:
            return light
        try:
            m = self.observe()
        except ChainError as exc:
            if self.mode == "live":
                # A dropped provider read is retried on later ticks (each tick spends its
                # own budget). A valuation refusal, or repeated failure, halts for review.
                self.observation_failures += 1
                if (
                    str(exc).startswith("account NAV refused")
                    or self.observation_failures >= MAX_OBSERVATION_FAILURES
                ):
                    self.state.halted = (
                        "live observation refused; operator review required before polling"
                    )
                    self.state.record("observation_halted", reason=self.state.halted)
            raise
        self.observation_failures = 0
        nav = self.nav_usd(m)
        flow = 0.0
        flow_micros = 0
        if self.mode == "live" and s.last_units is not None and m.units != s.last_units:
            d_eth, d_usdc, d_liq = (a - b for a, b in zip(m.units, s.last_units, strict=True))
            if not s.acknowledge_flow or d_liq:
                s.halted = (
                    f"unexplained wallet change at block {m.block}: eth {d_eth:+} wei, "
                    f"usdc {d_usdc:+}, liquidity {d_liq:+}"
                )
                s.record("halt", reason=s.halted, block=m.block)
                return f"HALT {s.halted}"
            if m.valuation is None:
                raise ChainError("external flow requires retained prices")
            exact_flow = Fraction(d_eth * m.valuation.eth_answer, 10**20) + Fraction(
                d_usdc * m.valuation.usdc_answer, 10**8
            )
            flow_micros = -(-exact_flow.numerator // exact_flow.denominator)
            flow = flow_micros / 1_000_000
            s.record("external_flow", block=m.block, usd=flow, eth=d_eth, usdc=d_usdc)
        s.acknowledge_flow = False
        if self.mode == "live":
            if m.valuation is None:
                raise ChainError("live risk requires retained integer NAV")
            risk = s.integer_risk()
            today = day_of(m.timestamp)
            if risk.day != today:
                s.actions_today = 0
            daily_micros, cumulative_micros = risk.observe(
                day=today,
                at=m.timestamp,
                nav=m.valuation.nav_micros,
                flow=flow_micros,
                checkpoint=s.checkpoint_at == 0,
            )
            s.day = risk.day
            s.initial_nav_usd = risk.basis / 1_000_000 if risk.basis is not None else None
            s.external_flow_usd = risk.external / 1_000_000
            s.daily_loss_usd = risk.daily_locked / 1_000_000
            s.checkpoint_nav_usd = (
                risk.checkpoint / 1_000_000 if risk.checkpoint is not None else None
            )
            s.checkpoint_at, s.last_nav_usd = risk.checkpoint_at, nav
            daily, cumulative = daily_micros / 1_000_000, cumulative_micros / 1_000_000
            breached = (
                daily_micros >= self.c.max_daily_loss_usd * 1_000_000
                or cumulative_micros >= self.c.max_cumulative_loss_usd * 1_000_000
            )
        else:
            s.roll_day(m.timestamp, nav, flow)
            daily, cumulative = s.losses(nav)
            breached = (
                daily >= self.c.max_daily_loss_usd or cumulative >= self.c.max_cumulative_loss_usd
            )
        s.last_units = m.units
        s.nav_evidence = m.nav_evidence
        s.last_light_check = None  # a full observation supersedes any earlier light check
        s.last_observation = LiveObservation(
            chain_id=self.c.chain_id,  # type: ignore[arg-type]  # validated Base-only config
            account=self.c.account,
            pool_id=self.c.pool_id,
            config_hash=sha256_hex(canonical_json(asdict(self.c))),
            block=m.block,
            block_hash=m.block_hash,
            block_timestamp=m.timestamp,
            observed_at=int(time.time()),
            nav_usd=nav,
            daily_loss_usd=daily,
            cumulative_loss_usd=cumulative,
            capital_limit_usd=self.c.max_capital_usd,
            daily_loss_limit_usd=self.c.max_daily_loss_usd,
            cumulative_loss_limit_usd=self.c.max_cumulative_loss_usd,
            valuation_basis="two_feed_principal_and_fees"
            if m.valuation
            else "legacy_principal_only",
            nav_micros=str(m.valuation.nav_micros) if m.valuation else None,
            uncollected_native_units=str(m.valuation.fee0) if m.valuation else None,
            uncollected_usdc_units=str(m.valuation.fee1) if m.valuation else None,
            nav_valid_until=nav_fresh_until(
                self.receipt_costs.store,
                m.nav_evidence,
                self.c,
                sources=self.receipt_costs.sources,
            )
            if m.valuation is not None and self.receipt_costs is not None
            else None,
            eth_feed_max_age_seconds=self.c.oracle_max_age_seconds,
            usdc_feed_max_age_seconds=self.c.usdc_oracle_max_age_seconds,
        ).model_dump()
        base = (
            f"block {m.block} px {m.price:.2f} oracle {m.oracle:.2f} nav ${nav:.2f} "
            f"loss d${daily:.2f}/c${cumulative:.2f}"
        )
        if breached:
            s.halted = f"loss cap breached (daily {daily:.2f}, cumulative {cumulative:.2f})"
            s.record("halt", reason=s.halted, block=m.block, nav=nav)
            return f"{base} | HALT {s.halted}"
        if m.deviation_bps > self.c.max_oracle_deviation_bps:
            return f"{base} | wait: pool/oracle deviation {float(m.deviation_bps):.0f} bps"
        action = self._decide(m)
        if s.halted:
            s.record("halt", reason=s.halted, block=m.block)
            return f"{base} | HALT {s.halted}"
        if action is None:
            return f"{base} | hold{self._range_text(m)}"
        if s.actions_today >= self.c.max_actions_per_day:
            return f"{base} | wait: daily action budget used ({action[0]} deferred)"
        refused = self._external_price_refusal(m, action[0])
        if refused is not None:
            return f"{base} | wait: {refused} ({action[0]} deferred)"
        if not self._pins_ok(m):
            return f"{base} | HALT {s.halted}"
        return f"{base} | {self._act(m, action)}"

    def _external_price_refusal(self, m: Market, kind: str) -> str | None:
        """Only new allocation (swap/mint) consults the optional independent price."""
        if self.external_prices is None or kind not in {"swap", "mint"}:
            return None
        if m.valuation is not None:
            eth = Fraction(m.valuation.eth_answer, 10**8)
            usdc = Fraction(m.valuation.usdc_answer, 10**8)
        else:
            eth, usdc = Fraction(str(m.oracle)), Fraction(1)  # paper fixtures only
        approved, reason = check_price_deviation(
            self.c.chain_id,
            self.c.pool.currency0,
            self.c.pool.currency1,
            eth,
            usdc,
            self.external_prices,
            self.max_external_deviation_bps,
        )
        # The tick log carries the reason; recording every wait would evict halts and
        # actions from the bounded history.
        return None if approved else reason

    def _check_nav_checkpoint(self) -> None:
        if self.mode != "live":
            return
        risk = self.state.exact_risk
        if not self.state.nav_evidence:
            if self.state.initial_nav_usd is not None or (
                risk is not None and (risk.basis is not None or risk.last_nav is not None)
            ):
                # A settled action clears its pre-action NAV proofs; only that action's own
                # two retained cash proofs may stand in until the next observation.
                self._check_settled_cash()
            return
        reader = self.receipt_costs
        if reader is None or len(self.state.nav_evidence) != 2:
            raise ChainError("retained NAV checkpoint missing")
        try:
            value = replay_nav_quorum(
                reader.store, self.state.nav_evidence, self.c, sources=reader.sources
            )
            if (
                risk is None
                or value.nav_micros != risk.last_nav
                or value.wallet.header.ref.timestamp != risk.last_at
            ):
                raise ValueError("risk baseline differs from retained NAV")
            risk.validate()
        except Exception:  # noqa: BLE001 - evidence diagnostics can contain provider data
            raise ChainError("retained NAV checkpoint cannot be replayed") from None

    def _release_unsigned_attempt(self) -> None:
        s = self.state
        status_of = getattr(self.signer, "pending_status", None)
        if (
            self.mode != "live"
            or self.secondary is None
            or not callable(status_of)
            or not isinstance(s.signing_attempt, dict)
            or self.released_attempts >= MAX_RELEASED_ATTEMPTS
        ):
            raise ChainError("signing outcome requires operator reconciliation")
        status = status_of(s.signing_attempt["nonce"])
        nonces = tuple(
            rpc.nonce(self.c.account, tag)
            for rpc in (self.rpc, self.secondary)
            for tag in ("latest", "pending")
        )
        release_unsigned_attempt(s, self.c, status, nonces)
        self.released_attempts += 1

    def _check_settled_cash(self) -> None:
        """Replay the last settled action's two cash proofs against the recorded units."""
        s, reader = self.state, self.receipt_costs
        last = s.history[-1] if s.history else None
        if (
            reader is None
            or last is None
            or s.checkpoint_at != 0
            or s.last_observation is not None
            or s.last_units is None
            or not isinstance(last.get("cash_evidence"), list)
            or len(last["cash_evidence"]) != 2
        ):
            raise ChainError("retained NAV checkpoint missing for existing risk baseline")
        kind = last.get("kind") if last.get("event") == "reverted" else last.get("event")
        try:
            refs = [evidence_ref_from_json(r) for r in last["cash_evidence"]]
            if [r.source for r in refs] != list(reader.sources):
                raise ValueError("cash proofs are not the declared provider pair")
            wallets: list[WalletBalance] = []
            for ref in refs:
                if last["event"] == "reverted":
                    wallets.append(
                        replay_revert_cash(reader.store, ref, self.c).reconstruct(
                            reader.store, self.c
                        )
                    )
                elif kind in {"approve_usdc", "approve_permit2"}:
                    proof = replay_approval_cash(reader.store, ref, self.c)
                    if proof.kind != kind:
                        raise ValueError("approval cash proves another action")
                    wallets.append(proof.reconstruct(reader.store, self.c)[0])
                elif kind in {"swap", "mint", "burn"}:
                    action = replay_action_cash(reader.store, ref, self.c)
                    if action.kind != kind:
                        raise ValueError("action cash proves another action")
                    wallets.append(action.reconstruct(reader.store, self.c))
                else:
                    raise ValueError("last event is not a settled action")
            a, b = wallets
            risk = s.exact_risk
            if (
                a.header != b.header
                or (a.native, a.token) != (b.native, b.token)
                or [a.native, a.token] != s.last_units[:2]
                or risk is None
                or a.header.ref.timestamp is None
                or a.header.ref.timestamp < risk.last_at
            ):
                raise ValueError("settled cash differs from recorded wallet units")
        except Exception:  # noqa: BLE001 - evidence diagnostics can contain provider data
            raise ChainError("settled action cash cannot be replayed") from None

    def _check_settlements(self) -> None:
        if self.mode != "live" or not self.state.settled_blocks:
            return
        if self.secondary is None:
            raise ChainError("settlement ancestry requires two providers")
        open_anchors = [
            a for a in self.state.settled_blocks if canonical_json(a) not in self.final_anchors
        ]
        if not open_anchors:
            return
        # An anchor at or below both providers' finalized head cannot reorg. Verify it once
        # more against both providers, then stop re-reading it in this process.
        finalized = min(self._finalized_number(rpc) for rpc in (self.rpc, self.secondary))
        for anchor in open_anchors:
            for rpc in (self.rpc, self.secondary):
                block = rpc.call("eth_getBlockByNumber", [hex(anchor["number"]), False])
                if not isinstance(block, dict) or block.get("hash") != anchor["hash"]:
                    self.state.halted = "settled transaction block reorged or unavailable"
                    raise ChainError(self.state.halted)
                receipt = rpc.receipt(anchor["transaction_hash"])
                if (
                    receipt is None
                    or receipt.get("blockHash") != anchor["hash"]
                    or receipt.get("transactionHash") != anchor["transaction_hash"]
                ):
                    self.state.halted = "settled transaction receipt changed or unavailable"
                    raise ChainError(self.state.halted)
            if anchor["number"] <= finalized:
                self.final_anchors.add(canonical_json(anchor))

    @staticmethod
    def _finalized_number(rpc: Rpc) -> int:
        block = rpc.call("eth_getBlockByNumber", ["finalized", False])
        if not isinstance(block, dict):
            return -1  # No finality claim: every anchor stays under per-tick verification.
        try:
            return int(str(block["number"]), 16)
        except (KeyError, ValueError):
            return -1

    def _range_text(self, m: Market) -> str:
        p = self.state.position
        if p is None:
            return ""
        return f" in range [{p.lower},{p.upper}) tick {m.tick} id {p.token_id}"

    def _pins_ok(self, m: Market) -> bool:
        pins = self.runtime_pins(m.block_hash)
        self.state.pins = pins
        changed = sorted(k for k, v in self.c.expected_pins.items() if pins.get(k) != v)
        if changed:
            self.state.halted = f"runtime or USDC implementation changed: {changed}"
            self.state.record("halt", reason=self.state.halted, pins=pins)
            return False
        return True

    # ---- decisions ---------------------------------------------------------------

    def _decide(self, m: Market) -> tuple[str, dict[str, Any]] | None:
        s, c = self.state, self.c
        p = s.position
        if p is not None:
            if m.position_liquidity == 0 and self.mode == "live":
                s.halted = f"position {p.token_id} disappeared without reconciled exit"
                return None
            if self.mode == "live" and m.position_liquidity != p.liquidity:
                s.halted = (
                    f"position {p.token_id} liquidity {m.position_liquidity} != recorded "
                    f"{p.liquidity}"
                )
                return None
            if p.lower <= m.tick < p.upper:
                return None
            if m.timestamp - p.opened_at < c.recenter_cooldown_seconds:
                return None
            a0, a1 = position_amounts(m.sqrt_price, p.lower, p.upper, p.liquidity)
            if a0 == 0 and a1 == 0:
                raise ChainError("burn would have no slippage bound")
            return "burn", {"amount0_min": a0 * 98 // 100, "amount1_min": a1 * 98 // 100}
        if self.mode == "live":
            approval = self._approval_needed(m)
            if approval is not None:
                return approval
        lower, upper = range_ticks(m.tick, c.half_width_ticks, c.pool.tick_spacing)
        eth_free = max(0, m.eth - c.gas_reserve_wei)
        # Size internal swaps at the pool price so one swap lands on target.
        price = m.pool_price
        eth_usd = Fraction(eth_free, 10**c.decimals0) * price
        usdc_usd = Fraction(m.usdc, 10**c.decimals1)
        quote_usd = Fraction(m.valuation.usdc_answer, 10**8) if m.valuation else Fraction(1)
        gas_usd = (
            Fraction(c.gas_reserve_wei * m.valuation.eth_answer, 10**26)
            if m.valuation
            else Fraction(0)
        )
        investment_cap = max(Fraction(0), c.max_capital_usd - gas_usd)
        budget = min(eth_usd + usdc_usd, investment_cap * Fraction(98, 100) / quote_usd)
        if budget < 20:
            return None
        probe0, probe1 = position_amounts(m.sqrt_price, lower, upper, 10**20)
        probe_value0 = Fraction(probe0 * m.sqrt_price**2, 2**192)
        share = probe_value0 / (probe_value0 + probe1)
        want_eth_usd = share * budget
        want_usdc_usd = budget - want_eth_usd
        tolerance = max(Fraction(3) / quote_usd, budget / 50)
        raw_price = Fraction(m.sqrt_price**2, 2**192)  # USDC micros per wei
        if usdc_usd < want_usdc_usd - tolerance:
            sell_usd = min(want_usdc_usd - usdc_usd, eth_usd)
            amount_in = int(sell_usd / price * 10**c.decimals0)
            return "swap", {
                "zero_for_one": True,
                "amount_in": amount_in,
                "min_out": self._min_out(
                    amount_in * raw_price * min(Fraction(1), m.reference_price / price)
                ),
            }
        if eth_usd < want_eth_usd - tolerance:
            sell_usd = min(want_eth_usd - eth_usd, usdc_usd)
            amount_in = int(sell_usd * 10**c.decimals1)
            return "swap", {
                "zero_for_one": False,
                "amount_in": amount_in,
                "min_out": self._min_out(
                    amount_in / raw_price * min(Fraction(1), price / m.reference_price)
                ),
            }
        eth_amount = min(eth_free, int(want_eth_usd / price * 10**c.decimals0 * Fraction(101, 100)))
        usdc_amount = min(m.usdc, int(want_usdc_usd * 10**c.decimals1 * Fraction(101, 100)))
        liquidity = liquidity_for_amounts(m.sqrt_price, lower, upper, eth_amount, usdc_amount)
        liquidity = liquidity * 995 // 1000  # headroom for price drift before inclusion
        if liquidity <= 0:
            return None
        return "mint", {
            "lower": lower,
            "upper": upper,
            "liquidity": liquidity,
            "amount0_max": eth_amount,
            "amount1_max": usdc_amount,
        }

    def _min_out(self, expected: Fraction) -> int:
        return int(
            expected
            * (1_000_000 - self.c.pool.fee)
            * (10_000 - self.c.swap_slippage_bps)
            / 10_000_000_000
        )

    def _approval_needed(self, m: Market) -> tuple[str, dict[str, Any]] | None:
        c = self.c
        need = c.approval_usdc
        peer = self.secondary
        if peer is None:
            raise ChainError("allowance observation requires two providers")
        pin = {"blockHash": m.block_hash, "requireCanonical": True}

        def agreed_allowance(
            target: str, signature: str, values: list[str], returns: list[str]
        ) -> tuple[Any, ...]:
            observations = tuple(
                rpc.call_abi(target, signature, ["address"] * len(values), values, returns, pin)
                for rpc in (self.rpc, peer)
            )
            if observations[0] != observations[1]:
                raise ChainError("allowance providers disagree at decision block")
            return observations[0]

        allowance = int(
            agreed_allowance(
                c.pool.currency1,
                "allowance(address,address)",
                [c.account, c.permit2],
                ["uint256"],
            )[0]
        )
        if allowance < need // 2:
            return "approve_usdc", {"amount": need}
        for spender in (c.position_manager, c.universal_router):
            amount, expiration, _ = agreed_allowance(
                c.permit2,
                "allowance(address,address,address)",
                [c.account, c.pool.currency1, spender],
                ["uint160", "uint48", "uint48"],
            )
            if int(amount) < need // 2 or int(expiration) < m.timestamp + 86_400:
                return "approve_permit2", {
                    "spender": spender,
                    "amount": need,
                    "expiration": m.timestamp + c.approval_days * 86_400,
                }
        return None

    # ---- actions -----------------------------------------------------------------

    def _transaction(self, m: Market, kind: str, a: dict[str, Any]) -> tuple[str, bytes, int]:
        return action_call(self.c, m.timestamp, kind, a, self.state.position)

    def _act(self, m: Market, action: tuple[str, dict[str, Any]]) -> str:
        kind, args = action
        s = self.state
        if self.mode == "shadow":
            return self._paper(m, kind, args)
        try:
            require_live_action(kind, m.valuation.wallet.nonce if m.valuation else None)
        except ValueError as exc:
            # Stop polling: a refused action recurs every tick and would only spend the
            # lifetime RPC budget. After an acceptance permit is consumed the operator
            # reconciles its receipt before any further release.
            s.halted = f"release gate refused {kind}; operator acceptance review required"
            s.record("halt", reason=s.halted, block=m.block)
            raise ChainError(str(exc)) from None
        self.c.validate()
        if not isinstance(self.signer, TransactionSigner):
            raise ChainError("live action requires full transaction authority")
        to, data, value = self._transaction(m, kind, args)

        # A provider on another chain must not supply simulation or nonce inputs
        # to a transaction which will be signed for the configured chain.
        if self.secondary is None or any(
            rpc.uint("eth_chainId", []) != self.c.chain_id for rpc in (self.rpc, self.secondary)
        ):
            raise ChainError("transaction preparation requires both providers on the policy chain")

        reader = self.receipt_costs
        if reader is None:
            raise ChainError("pre-sign wallet evidence unavailable")
        try:
            if m.nav_evidence:
                captures = tuple(replay_nav(reader.store, ref, self.c) for ref in m.nav_evidence)
                wallet = tuple(c.wallet for c in captures)
                if (
                    len(wallet) != 2
                    or tuple(ref.source for ref in wallet) != reader.sources
                    or any(
                        ref.block != BlockRef(self.c.chain_id, m.block, m.block_hash, m.timestamp)
                        for ref in wallet
                    )
                ):
                    raise ValueError("decision wallet proof scope differs")
            else:
                wallet = capture_wallet(
                    reader.store,
                    self.c,
                    BlockRef(self.c.chain_id, m.block, m.block_hash, m.timestamp),
                    sources=reader.sources,
                    sessions=reader.sessions,
                )
            before = replay_wallet(reader.store, wallet[0]).reconstruct()
            if before != replay_wallet(reader.store, wallet[1]).reconstruct():
                raise ValueError("decision wallet quorum differs")
            if (before.native, before.token) != (m.eth, m.usdc):
                raise ValueError("wallet differs from decision snapshot")
        except Exception:  # noqa: BLE001 - raw capture failures may contain credentials
            raise ChainError("pre-sign wallet evidence unresolved (details redacted)") from None

        if self.store.journal is None:
            raise ChainError("live signing requires configuration-bound journal")
        s.signing_attempt = {
            "nonce": before.nonce,
            "kind": kind,
            "intent": dict(args),
            "block": m.block,
            "block_hash": m.block_hash,
            "target": to,
            "value": value,
            "calldata_hash": "0x" + keccak(data).hex(),
            "wallet_evidence": [asdict(r) for r in wallet],
        }
        self.store.save(s)

        def on_signed(tx: SubmittedTransaction) -> None:
            if (
                tx.chain_id != self.c.chain_id
                or tx.account != self.c.account
                or tx.target != to
                or tx.value != value
                or tx.calldata_hash != "0x" + keccak(data).hex()
                or tx.nonce != before.nonce
            ):
                raise ChainError("signed transaction differs from the proposed action")
            s.signing_attempt = None
            s.pending = Pending(
                kind,
                tx.transaction_hash,
                tx.nonce,
                int(time.time()),
                dict(args),
                to,
                tx,
                pending_plan_hash(
                    asdict(self.c),
                    kind,
                    args,
                    tx,
                    asdict(s.position) if s.position else None,
                    tuple(asdict(r) for r in wallet),
                ),
                wallet,
            )
            self.store.save(s)

        try:
            sent = send_transaction(
                self.rpc,
                self.signer,
                chain_id=self.c.chain_id,
                to=to,
                data=data,
                value=value,
                fees=self.fees,
                on_signed=on_signed,
                authority_context={
                    "kind": kind,
                    "intent": args,
                    "block": m.block,
                    "block_hash": m.block_hash,
                },
            )
        except (TxReverted, SubmissionUnknown) as exc:
            # Pending stays recorded; only independently verified evidence resolves it.
            return f"{kind} submitted, awaiting verified receipt: {exc}"
        return f"{kind} {self._resolve_pending()} {sent.tx_hash}"

    def _receipt_cost(
        self, pending: Pending, receipt: dict[str, Any]
    ) -> tuple[int, tuple[EvidenceRef, ...]]:
        if self.mode == "shadow":
            return int(receipt["gasUsed"], 16) * int(receipt["effectiveGasPrice"], 16), ()
        if self.receipt_costs is None:
            raise ChainError("retained receipt cost evidence is unavailable")
        try:
            proof = self.receipt_costs.observe(
                receipt, transaction_hash=pending.tx_hash, target=pending.to
            )
        except Exception:  # noqa: BLE001 - provider/persistence errors may contain secrets
            raise ChainError("whole native receipt cost unresolved (details redacted)") from None
        return proof.cost.total_native, proof.evidence

    def _apply(
        self,
        kind: str,
        args: dict[str, Any],
        receipt: dict[str, Any],
        *,
        native_cost: int,
        cost_evidence: tuple[EvidenceRef, ...],
        outcome_evidence: tuple[EvidenceRef, ...] = (),
        transaction: SubmittedTransaction | None = None,
        wallet: WalletBalance | None = None,
        cash_evidence: tuple[EvidenceRef, ...] = (),
    ) -> None:
        """Commit a confirmed action. Everything that can fail runs before `pending` clears."""
        if self.mode == "live" and (wallet is None or len(cash_evidence) != 2):
            raise ChainError("successful action cash unresolved; pending retained")
        s = self.state
        position = s.position
        if kind == "mint":
            # MINT_POSITION mints exactly args["liquidity"]; the receipt names the token.
            position = Position(
                self._minted_token(receipt),
                args["lower"],
                args["upper"],
                args["liquidity"],
                int(time.time()),
            )
        elif kind == "burn":
            position = None
        if self.mode == "live":
            self._anchor_settlement(receipt)
        s.position = position
        s.gas_spent_wei += native_cost
        s.consecutive_reverts = 0
        s.actions_today += 1
        s.last_action_at = int(time.time())
        s.last_units = (
            [wallet.native, wallet.token, position.liquidity if position else 0] if wallet else None
        )
        s.nav_evidence = ()
        s.last_observation = None  # pre-action NAV cannot describe the settled account
        s.checkpoint_at = 0  # the next observation locks this action's NAV change
        s.pending = None
        s.record(
            kind,
            tx=receipt.get("transactionHash"),
            args=args,
            native_gas_units=native_cost,
            cost_evidence=[asdict(ref) for ref in cost_evidence],
            position_evidence=[asdict(ref) for ref in outcome_evidence],
            transaction=asdict(transaction) if transaction else None,
            cash_evidence=[asdict(ref) for ref in cash_evidence],
        )

    def _reverted(
        self,
        kind: str,
        receipt: dict[str, Any],
        *,
        native_cost: int,
        cost_evidence: tuple[EvidenceRef, ...],
        transaction: SubmittedTransaction | None = None,
        wallet: WalletBalance | None = None,
        cash_evidence: tuple[EvidenceRef, ...] = (),
    ) -> str:
        s = self.state
        if self.mode == "live":
            self._anchor_settlement(receipt)
        s.gas_spent_wei += native_cost
        s.nav_evidence = ()
        s.last_observation = None
        s.last_units = (
            [wallet.native, wallet.token, s.position.liquidity if s.position else 0]
            if wallet
            else None
        )
        s.checkpoint_at = 0
        s.pending = None
        s.consecutive_reverts += 1
        s.actions_today += 1
        tx = receipt.get("transactionHash")
        s.record(
            "reverted",
            kind=kind,
            tx=tx,
            native_gas_units=native_cost,
            cost_evidence=[asdict(ref) for ref in cost_evidence],
            transaction=asdict(transaction) if transaction else None,
            cash_evidence=[asdict(r) for r in cash_evidence],
        )
        if s.consecutive_reverts >= MAX_CONSECUTIVE_REVERTS:
            s.halted = f"{s.consecutive_reverts} consecutive reverts (last {kind})"
        return f"{kind} REVERTED {tx}"

    def _anchor_settlement(self, receipt: dict[str, Any]) -> None:
        anchor = {
            "number": int(receipt["blockNumber"], 16),
            "hash": receipt["blockHash"],
            "transaction_hash": receipt["transactionHash"],
        }
        if anchor in self.state.settled_blocks:
            raise ChainError("settlement already journaled")
        if len(self.state.settled_blocks) >= 10_000:
            raise ChainError("settlement anchor budget exhausted")
        self.state.settled_blocks.append(anchor)

    def _minted_token(self, receipt: dict[str, Any]) -> int:
        zero_topic = "0x" + "0" * 64
        owner_topic = "0x" + "0" * 24 + self.c.account[2:]
        found = []
        for log in receipt.get("logs", []):
            topics = [str(t).lower() for t in log.get("topics", [])]
            if (
                str(log.get("address", "")).lower() == self.c.position_manager
                and len(topics) == 4
                and topics[0] == TRANSFER_TOPIC
                and topics[1] == zero_topic
                and topics[2] == owner_topic
            ):
                found.append(int(topics[3], 16))
        if len(found) != 1 or found[0] <= 0:
            raise ChainError("mint receipt must have exactly one position NFT transfer")
        return found[0]

    def _verified_receipt(self, p: Pending) -> dict[str, Any] | None:
        """The receipt for exactly `p`, on a canonical block both providers agree on and
        at least RECEIPT_CONFIRMATIONS deep. None means not yet provable."""
        if self.mode == "live" and self.secondary is None:
            raise ChainError("receipt resolution requires a second independent provider")
        if self.mode == "live":
            tx = p.transaction
            if tx is None:
                raise ChainError("pending transaction lacks its retained signed commitment")
            if (
                tx.transaction_hash != p.tx_hash
                or tx.nonce != p.nonce
                or tx.target != p.to
                or tx.chain_id != self.c.chain_id
                or tx.account != self.c.account
                or tx.max_fee_per_gas > self.c.max_fee_per_gas_wei
                or tx.max_priority_fee_per_gas > self.c.priority_fee_wei
                or tx.gas_limit > self.fees.max_gas
                or p.plan_hash
                != pending_plan_hash(
                    asdict(self.c),
                    p.kind,
                    p.intent,
                    tx,
                    asdict(self.state.position) if self.state.position else None,
                    tuple(asdict(r) for r in p.wallet_evidence),
                )
            ):
                raise ChainError("pending commitment, intent or configuration mismatch")
        receipt = self.rpc.receipt(p.tx_hash)
        if receipt is None:
            return None
        fields = (
            "transactionHash",
            "blockHash",
            "blockNumber",
            "from",
            "to",
            "status",
            "gasUsed",
            "effectiveGasPrice",
            "logs",
        )
        if any(k not in receipt or receipt[k] is None for k in fields):
            raise ChainError("receipt lacks required identity or cost evidence")
        for name in ("blockNumber", "status", "gasUsed", "effectiveGasPrice"):
            value = receipt[name]
            if (
                not isinstance(value, str)
                or re.fullmatch(r"0x(?:0|[1-9a-f][0-9a-f]*)", value) is None
            ):
                raise ChainError("receipt contains a malformed integer")
        if receipt["status"] not in {"0x0", "0x1"} or not isinstance(receipt["logs"], list):
            raise ChainError("receipt contains malformed status or logs")
        number = int(receipt["blockNumber"], 16)
        if (
            self.mode == "live"
            and p.transaction is not None
            and (
                int(receipt["gasUsed"], 16) > p.transaction.gas_limit
                or int(receipt["effectiveGasPrice"], 16) > p.transaction.max_fee_per_gas
            )
        ):
            raise ChainError("receipt exceeds the signed gas envelope")
        if self.rpc.uint("eth_blockNumber", []) - number < RECEIPT_CONFIRMATIONS:
            return None
        for rpc in (self.rpc, self.secondary):
            if rpc is None:
                continue
            other = receipt if rpc is self.rpc else rpc.receipt(p.tx_hash)
            if other is None:
                return None
            if any(other.get(k) != receipt.get(k) for k in fields):
                raise ChainError(f"providers disagree on receipt {p.tx_hash}")
            # Chain-specific cost components must agree, including their presence.
            # This is quorum validation, not complete native-cost accounting.
            costs = (
                "l1Fee",
                "operatorFeeScalar",
                "operatorFeeConstant",
                "gasUsedForL1",
                "blobGasUsed",
                "blobGasPrice",
            )
            if any((k in other) != (k in receipt) or other.get(k) != receipt.get(k) for k in costs):
                raise ChainError(f"providers disagree on receipt costs {p.tx_hash}")
            if rpc.uint("eth_blockNumber", []) - number < RECEIPT_CONFIRMATIONS:
                return None
            canonical = rpc.call("eth_getBlockByNumber", [hex(number), False])
            if not isinstance(canonical, dict) or canonical.get("hash") != receipt["blockHash"]:
                return None
        if (
            str(receipt["transactionHash"]).lower() != p.tx_hash.lower()
            or str(receipt.get("from", "")).lower() != self.c.account
            or (p.to and str(receipt.get("to", "")).lower() != p.to.lower())
        ):
            s = self.state
            s.halted = f"receipt for {p.tx_hash} does not match the signed transaction"
            raise ChainError(s.halted)
        return receipt

    def _resolve_pending(self) -> str:
        s = self.state
        assert s.pending is not None
        p = s.pending
        receipt = self._verified_receipt(p)
        if receipt is not None:
            succeeded = receipt["status"] == "0x1"
            if succeeded and p.kind == "burn" and not self._burned_in(receipt):
                s.halted = f"burn receipt {p.tx_hash} has no position NFT burn"
                return f"HALT {s.halted}"
            native_cost, evidence = self._receipt_cost(p, receipt)
            if not succeeded:
                wallet, cash = self._revert_wallet(p, evidence)
                return "pending " + self._reverted(
                    p.kind,
                    receipt,
                    native_cost=native_cost,
                    cost_evidence=evidence,
                    transaction=p.transaction,
                    wallet=wallet,
                    cash_evidence=cash,
                )
            outcome = self._position_outcome(p, receipt, evidence)
            wallet, cash = self._approval_wallet(p, evidence)
            self._apply(
                p.kind,
                p.intent,
                receipt,
                native_cost=native_cost,
                cost_evidence=evidence,
                outcome_evidence=outcome,
                transaction=p.transaction,
                wallet=wallet,
                cash_evidence=cash,
            )
            return f"pending {p.kind} confirmed {p.tx_hash}"
        if self.rpc.nonce(self.c.account, "latest") > p.nonce:
            # The nonce moved: wait for the receipt to become provable before deciding.
            if time.time() - p.signed_at < PENDING_TIMEOUT:
                return f"waiting on receipt for {p.kind} {p.tx_hash}"
            s.halted = f"nonce {p.nonce} consumed but no provable receipt for {p.tx_hash}"
            return f"HALT {s.halted}"
        if time.time() - p.signed_at > PENDING_TIMEOUT:
            s.halted = f"pending {p.kind} {p.tx_hash} unresolved after {PENDING_TIMEOUT}s"
            return f"HALT {s.halted}"
        return f"waiting on pending {p.kind} {p.tx_hash}"

    def _approval_wallet(
        self, pending: Pending, costs: tuple[EvidenceRef, ...]
    ) -> tuple[WalletBalance | None, tuple[EvidenceRef, ...]]:
        if self.mode != "live":
            return None, ()
        reader = self.receipt_costs
        if reader is None or pending.transaction is None:
            raise ChainError("approval cash requires retained transaction and costs")
        try:
            if pending.kind not in {"approve_usdc", "approve_permit2"}:
                return reconcile_action(
                    reader.store,
                    self.c,
                    pending.kind,
                    pending.intent,
                    self.state.position,
                    pending.transaction,
                    pending.wallet_evidence,
                    costs,
                    sources=reader.sources,
                    sessions=reader.sessions,
                )
            return reconcile_approval(
                reader.store,
                self.c,
                pending.kind,
                pending.intent,
                pending.transaction,
                pending.wallet_evidence,
                costs,
                sources=reader.sources,
                sessions=reader.sessions,
            )
        except Exception:  # noqa: BLE001 - never expose raw RPC errors or credentials
            label = "approval" if pending.kind.startswith("approve") else "successful action"
            raise ChainError(f"{label} cash unresolved (details redacted)") from None

    def _revert_wallet(
        self, pending: Pending, costs: tuple[EvidenceRef, ...]
    ) -> tuple[WalletBalance | None, tuple[EvidenceRef, ...]]:
        if self.mode == "shadow":
            return None, ()
        reader = self.receipt_costs
        if reader is None or pending.transaction is None:
            raise ChainError("reverted wallet evidence unavailable")
        try:
            return reconcile_revert(
                reader.store,
                self.c,
                pending.transaction,
                pending.wallet_evidence,
                costs,
                sources=reader.sources,
                sessions=reader.sessions,
            )
        except Exception:  # noqa: BLE001 - raw provider/persistence errors are not safe to log
            raise ChainError("reverted wallet cash unresolved (details redacted)") from None

    def _position_outcome(
        self, pending: Pending, receipt: dict[str, Any], costs: tuple[EvidenceRef, ...]
    ) -> tuple[EvidenceRef, ...]:
        if self.mode == "shadow" or pending.kind not in {"mint", "burn"}:
            return ()
        reader = self.receipt_costs
        if reader is None or not costs or costs[0].block is None:
            raise ChainError("position outcome requires retained receipt block")
        position: Position | None
        if pending.kind == "mint":
            position = Position(
                self._minted_token(receipt),
                pending.intent["lower"],
                pending.intent["upper"],
                pending.intent["liquidity"],
                0,
            )
        else:
            position = self.state.position
            if position is None:
                raise ChainError("burn has no recorded position to reconcile")
        try:
            refs = collect_position_outcome(
                reader.store,
                self.c,
                costs[0].block,
                token_id=position.token_id,
                lower=position.lower,
                upper=position.upper,
                liquidity=0 if pending.kind == "burn" else position.liquidity,
                burned=pending.kind == "burn",
                sources=reader.sources,
                sessions=reader.sessions,
            )
            for ref in refs:
                replay_position_outcome(reader.store, ref)
            return refs
        except Exception:  # noqa: BLE001 - raw provider failures are not safe to log
            raise ChainError("position outcome unresolved (details redacted)") from None

    def _burned_in(self, receipt: dict[str, Any]) -> bool:
        assert self.state.position is not None
        owner = "0x" + "0" * 24 + self.c.account[2:]
        token = "0x" + f"{self.state.position.token_id:064x}"
        return any(
            str(log.get("address", "")).lower() == self.c.position_manager
            and [str(t).lower() for t in log.get("topics", [])]
            == [TRANSFER_TOPIC, owner, "0x" + "0" * 64, token]
            for log in receipt.get("logs", [])
        )

    def drop_pending(self) -> str:
        """Operator recovery: forget a submission proven absent from chain."""
        s = self.state
        if s.pending is None:
            return "no pending submission"
        p = s.pending
        if self.mode == "live":
            raise ValueError("signed live pending cannot be forgotten from nonce/receipt absence")
        if self.rpc.receipt(p.tx_hash) is not None:
            raise ValueError(f"{p.tx_hash} has a receipt; let the loop resolve it")
        if self.rpc.nonce(self.c.account, "latest") != p.nonce:
            raise ValueError(f"nonce {p.nonce} is consumed; reconcile manually")
        if self.rpc.nonce(self.c.account, "pending") != p.nonce:
            raise ValueError("a transaction is still in the mempool for this nonce")
        s.record("pending_dropped", kind=p.kind, tx=p.tx_hash, nonce=p.nonce)
        s.pending = None
        return f"dropped pending {p.kind} {p.tx_hash}"

    # ---- shadow ------------------------------------------------------------------

    def _paper(self, m: Market, kind: str, a: dict[str, Any]) -> str:
        s, c = self.state, self.c
        gas_wei = int(SHADOW_GAS_USD / m.oracle * 10**c.decimals0)
        s.paper_eth -= gas_wei
        s.gas_spent_wei += gas_wei
        s.actions_today += 1
        s.last_action_at = m.timestamp
        if kind == "swap":
            out = a["min_out"] * 10_000 // (10_000 - c.swap_slippage_bps)  # ignores impact
            if a["zero_for_one"]:
                s.paper_eth, s.paper_usdc = s.paper_eth - a["amount_in"], s.paper_usdc + out
            else:
                s.paper_usdc, s.paper_eth = s.paper_usdc - a["amount_in"], s.paper_eth + out
        elif kind == "mint":
            used0, used1 = position_amounts(m.sqrt_price, a["lower"], a["upper"], a["liquidity"])
            s.paper_eth -= used0 + 1
            s.paper_usdc -= used1 + 1
            s.position = Position(-m.block, a["lower"], a["upper"], a["liquidity"], m.timestamp)
        elif kind == "burn":
            assert s.position is not None
            got0, got1 = position_amounts(
                m.sqrt_price, s.position.lower, s.position.upper, s.position.liquidity
            )
            # Swap fees are not credited on paper, so shadow results understate LP income.
            s.paper_eth, s.paper_usdc = s.paper_eth + got0, s.paper_usdc + got1
            s.position = None
        s.record(f"paper_{kind}", block=m.block, args=a)
        return f"paper {kind} {a}"

    # ---- loop --------------------------------------------------------------------

    def run(
        self,
        *,
        max_ticks: int | None = None,
        until: float | None = None,
        poll_seconds: int | None = None,
    ) -> None:
        """`poll_seconds` may only slow polling below the configured rate; it never speeds it."""
        interval = self.c.poll_seconds if poll_seconds is None else poll_seconds
        if type(interval) is not int or not self.c.poll_seconds <= interval <= 3600:
            raise ValueError("poll interval must be an integer from the configured rate to 3600s")
        ticks = 0
        while max_ticks is None or ticks < max_ticks:
            if until is not None and time.time() >= until:
                break
            self.tick()
            ticks += 1
            if self.state.halted or (self.stop_file is not None and self.stop_file.exists()):
                break
            time.sleep(interval)

    def report(self) -> dict[str, Any]:
        s = self.state
        return {
            "mode": s.mode,
            "halted": s.halted,
            "position": asdict(s.position) if s.position else None,
            "initial_nav_usd": s.initial_nav_usd,
            "last_nav_usd": s.last_nav_usd,
            "gas_spent_eth": s.gas_spent_wei / 1e18,
            "legacy_gas_cost_incomplete": s.legacy_gas_cost_incomplete,
            "actions": [h for h in s.history if h["event"] != "pins_recorded"][-20:],
        }
