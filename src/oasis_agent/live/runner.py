"""One-transaction-per-tick LP loop: observe, check limits, take at most one step.

Each tick re-derives the next step from chain state, so a crash between steps
is harmless: out of range -> burn; no position and unbalanced -> swap;
no position and balanced -> mint. Shadow mode runs the same decisions on paper
balances and never signs.
"""

import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from eth_hash.auto import keccak

from .chain import (
    ChainError,
    DigestSigner,
    FeePolicy,
    Rpc,
    RpcRejected,
    SubmissionUnknown,
    TxReverted,
    send_transaction,
)
from .config import LiveConfig
from .state import Pending, Position, StateStore
from .v4lp import (
    UINT160_MAX,
    burn_call,
    erc20_approve_call,
    eth_value_share,
    liquidity_for_amounts,
    mint_call,
    permit2_approve_call,
    position_amounts,
    price_from_sqrt,
    range_ticks,
    swap_call,
)

TRANSFER_TOPIC = "0x" + keccak(b"Transfer(address,address,uint256)").hex()
# FiatToken proxies keep the implementation at the zeppelinos slot.
USDC_IMPL_SLOT = "0x7050c9e0f4ca769c69bd3a8ef740bc37934f8e2c036e5a723fd8ee048ed3f8c3"
PENDING_TIMEOUT = 900
ORACLE_DECIMALS = 8  # Base ETH/USD feed decimals(), verified on two providers 2026-10-08
MAX_CONSECUTIVE_REVERTS = 2
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

    @property
    def units(self) -> list[int]:
        return [self.eth, self.usdc, self.position_liquidity]

    @property
    def deviation_bps(self) -> float:
        return abs(self.price / self.oracle - 1) * 10_000


class LiveLoop:
    def __init__(
        self,
        config: LiveConfig,
        rpc: Rpc,
        store: StateStore,
        *,
        mode: str,
        signer: DigestSigner | None = None,
        stop_file: Path | None = None,
        log: Callable[[str], None] = print,
        secondary: Rpc | None = None,
    ) -> None:
        if mode not in {"shadow", "live"}:
            raise ValueError("mode must be shadow or live")
        if (mode == "live") != (signer is not None):
            raise ValueError("live mode requires a signer; shadow mode forbids one")
        if signer is not None and signer.account.lower() != config.account:
            raise ValueError("signer account is not the configured dedicated account")
        if mode == "live" and secondary is None:
            raise ValueError("live mode requires a second, independent RPC provider")
        self.c, self.rpc, self.store, self.mode = config, rpc, store, mode
        self.secondary = secondary
        self.signer, self.stop_file, self.log = signer, stop_file, log
        self.state = store.load(mode)
        self.fees = FeePolicy(config.max_fee_per_gas_wei, config.priority_fee_wei)

    @classmethod
    def recovery(cls, config: LiveConfig, rpc: Rpc, store: StateStore, mode: str) -> "LiveLoop":
        """A loop over `mode`'s state that can read chain state but never sign."""
        loop = cls.__new__(cls)
        loop.c, loop.rpc, loop.store, loop.mode = config, rpc, store, mode
        loop.signer, loop.secondary, loop.stop_file, loop.log = None, None, None, print
        loop.state = store.load(mode)
        loop.fees = FeePolicy(config.max_fee_per_gas_wei, config.priority_fee_wei)
        return loop

    # ---- observation -------------------------------------------------------------

    def observe(self) -> Market:
        """Read one canonical block. Every value is bound to its hash, and in live mode
        a second provider must return identical values for that same hash."""
        block = self.rpc.block()
        number, timestamp = int(str(block["number"]), 16), int(str(block["timestamp"]), 16)
        ref = {"blockHash": str(block["hash"]), "requireCanonical": True}
        reads = self._reads(self.rpc, ref)
        if self.secondary is not None:
            try:
                other = self._reads(self.secondary, ref)
            except RpcRejected as exc:
                raise ChainError(f"secondary provider cannot serve block {number}") from exc
            if other != reads:
                raise ChainError(f"providers disagree on state at block {number}")
        sqrt_price, tick, answer, updated, eth, usdc, liquidity = reads
        if timestamp - updated > self.c.oracle_max_age_seconds or answer <= 0:
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
            liquidity = self._liquidity(rpc, position.token_id, ref)
        return int(slot[0]), int(slot[1]), int(answer), int(updated), eth, usdc, liquidity

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
        if s.pending is not None:
            return self._resolve_pending()
        m = self.observe()
        nav = self.nav_usd(m)
        flow = 0.0
        if self.mode == "live" and s.last_units is not None and m.units != s.last_units:
            d_eth, d_usdc, d_liq = (a - b for a, b in zip(m.units, s.last_units, strict=True))
            if not s.acknowledge_flow or d_liq:
                s.halted = (
                    f"unexplained wallet change at block {m.block}: eth {d_eth:+} wei, "
                    f"usdc {d_usdc:+}, liquidity {d_liq:+}"
                )
                s.record("halt", reason=s.halted, block=m.block)
                return f"HALT {s.halted}"
            flow = d_eth / 10**self.c.decimals0 * m.oracle + d_usdc / 10**self.c.decimals1
            s.record("external_flow", block=m.block, usd=flow, eth=d_eth, usdc=d_usdc)
        s.acknowledge_flow = False
        s.roll_day(m.timestamp, nav, flow)
        s.last_units = m.units
        daily, cumulative = s.losses(nav)
        base = (
            f"block {m.block} px {m.price:.2f} oracle {m.oracle:.2f} nav ${nav:.2f} "
            f"loss d${daily:.2f}/c${cumulative:.2f}"
        )
        if daily >= self.c.max_daily_loss_usd or cumulative >= self.c.max_cumulative_loss_usd:
            s.halted = f"loss cap breached (daily {daily:.2f}, cumulative {cumulative:.2f})"
            s.record("halt", reason=s.halted, block=m.block, nav=nav)
            return f"{base} | HALT {s.halted}"
        if m.deviation_bps > self.c.max_oracle_deviation_bps:
            return f"{base} | wait: pool/oracle deviation {m.deviation_bps:.0f} bps"
        action = self._decide(m)
        if s.halted:
            s.record("halt", reason=s.halted, block=m.block)
            return f"{base} | HALT {s.halted}"
        if action is None:
            return f"{base} | hold{self._range_text(m)}"
        if s.actions_today >= self.c.max_actions_per_day:
            return f"{base} | wait: daily action budget used ({action[0]} deferred)"
        if not self._pins_ok(m):
            return f"{base} | HALT {s.halted}"
        return f"{base} | {self._act(m, action)}"

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
                s.record("position_gone", token_id=p.token_id)
                s.position = None
                return self._decide(m)
            if p.lower <= m.tick < p.upper:
                return None
            if m.timestamp - p.opened_at < c.recenter_cooldown_seconds:
                return None
            if self.mode == "live" and m.position_liquidity != p.liquidity:
                s.halted = (
                    f"position {p.token_id} liquidity {m.position_liquidity} != recorded "
                    f"{p.liquidity}"
                )
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
        eth_usd = eth_free / 10**c.decimals0 * m.price
        usdc_usd = m.usdc / 10**c.decimals1
        budget = min(eth_usd + usdc_usd, c.max_capital_usd * 0.98)
        if budget < 20:
            return None
        share = eth_value_share(m.sqrt_price, lower, upper)
        want_eth_usd = share * budget
        want_usdc_usd = budget - want_eth_usd
        tolerance = max(MIN_SWAP_USD, budget * 0.02)
        raw_price = (m.sqrt_price / (1 << 96)) ** 2  # USDC micros per wei
        if usdc_usd < want_usdc_usd - tolerance:
            sell_usd = min(want_usdc_usd - usdc_usd, eth_usd)
            amount_in = int(sell_usd / m.price * 10**c.decimals0)
            return "swap", {
                "zero_for_one": True,
                "amount_in": amount_in,
                "min_out": self._min_out(amount_in * raw_price * min(1.0, m.oracle / m.price)),
            }
        if eth_usd < want_eth_usd - tolerance:
            sell_usd = min(want_eth_usd - eth_usd, usdc_usd)
            amount_in = int(sell_usd * 10**c.decimals1)
            return "swap", {
                "zero_for_one": False,
                "amount_in": amount_in,
                "min_out": self._min_out(amount_in / raw_price * min(1.0, m.price / m.oracle)),
            }
        eth_amount = min(eth_free, int(want_eth_usd / m.price * 10**c.decimals0 * 1.01))
        usdc_amount = min(m.usdc, int(want_usdc_usd * 10**c.decimals1 * 1.01))
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

    def _min_out(self, expected: float) -> int:
        fee = self.c.pool.fee / 1_000_000
        return int(expected * (1 - fee) * (10_000 - self.c.swap_slippage_bps) / 10_000)

    def _approval_needed(self, m: Market) -> tuple[str, dict[str, Any]] | None:
        c = self.c
        need = c.approval_usdc
        allowance = int(
            self.rpc.call_abi(
                c.pool.currency1,
                "allowance(address,address)",
                ["address", "address"],
                [c.account, c.permit2],
                ["uint256"],
            )[0]
        )
        if allowance < need // 2:
            return "approve_usdc", {"amount": need}
        for spender in (c.position_manager, c.universal_router):
            amount, expiration, _ = self.rpc.call_abi(
                c.permit2,
                "allowance(address,address,address)",
                ["address", "address", "address"],
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
        c, deadline = self.c, m.timestamp + 300
        if kind == "approve_usdc":
            return c.pool.currency1, erc20_approve_call(c.permit2, a["amount"]), 0
        if kind == "approve_permit2":
            return (
                c.permit2,
                permit2_approve_call(
                    c.pool.currency1, a["spender"], min(a["amount"], UINT160_MAX), a["expiration"]
                ),
                0,
            )
        if kind == "swap":
            data = swap_call(c.pool, a["zero_for_one"], a["amount_in"], a["min_out"], deadline)
            return c.universal_router, data, a["amount_in"] if a["zero_for_one"] else 0
        if kind == "mint":
            data = mint_call(
                c.pool,
                a["lower"],
                a["upper"],
                a["liquidity"],
                a["amount0_max"],
                a["amount1_max"],
                c.account,
                deadline,
            )
            return c.position_manager, data, a["amount0_max"]
        if kind == "burn":
            assert self.state.position is not None
            data = burn_call(
                c.pool,
                self.state.position.token_id,
                a["amount0_min"],
                a["amount1_min"],
                c.account,
                deadline,
            )
            return c.position_manager, data, 0
        raise ValueError(f"unknown action {kind}")

    def _act(self, m: Market, action: tuple[str, dict[str, Any]]) -> str:
        kind, args = action
        s = self.state
        if self.mode == "shadow":
            return self._paper(m, kind, args)
        assert self.signer is not None
        to, data, value = self._transaction(m, kind, args)

        def on_signed(tx_hash: str, nonce: int) -> None:
            s.pending = Pending(kind, tx_hash, nonce, int(time.time()), args, to)
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
            )
        except (TxReverted, SubmissionUnknown) as exc:
            # Pending stays recorded; only independently verified evidence resolves it.
            return f"{kind} submitted, awaiting verified receipt: {exc}"
        return f"{kind} {self._resolve_pending()} {sent.tx_hash}"

    def _charge_gas(self, receipt: dict[str, Any]) -> None:
        used = int(str(receipt["gasUsed"]), 16) * int(str(receipt["effectiveGasPrice"]), 16)
        self.state.gas_spent_wei += used

    def _apply(self, kind: str, args: dict[str, Any], receipt: dict[str, Any]) -> None:
        """Commit a confirmed action. Everything that can fail runs before `pending` clears."""
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
        used = int(str(receipt["gasUsed"]), 16) * int(str(receipt["effectiveGasPrice"]), 16)
        s.position = position
        s.gas_spent_wei += used
        s.consecutive_reverts = 0
        s.actions_today += 1
        s.last_action_at = int(time.time())
        s.last_units = None
        s.checkpoint_at = 0  # the next observation locks this action's NAV change
        s.pending = None
        s.record(kind, tx=receipt.get("transactionHash"), args=args)

    def _reverted(self, kind: str, receipt: dict[str, Any]) -> str:
        s = self.state
        self._charge_gas(receipt)
        s.last_units = None
        s.checkpoint_at = 0
        s.pending = None
        s.consecutive_reverts += 1
        s.actions_today += 1
        tx = receipt.get("transactionHash")
        s.record("reverted", kind=kind, tx=tx)
        if s.consecutive_reverts >= MAX_CONSECUTIVE_REVERTS:
            s.halted = f"{s.consecutive_reverts} consecutive reverts (last {kind})"
        return f"{kind} REVERTED {tx}"

    def _minted_token(self, receipt: dict[str, Any]) -> int:
        zero_topic = "0x" + "0" * 64
        owner_topic = "0x" + "0" * 24 + self.c.account[2:]
        for log in receipt.get("logs", []):
            topics = [str(t).lower() for t in log.get("topics", [])]
            if (
                str(log.get("address", "")).lower() == self.c.position_manager
                and len(topics) == 4
                and topics[0] == TRANSFER_TOPIC
                and topics[1] == zero_topic
                and topics[2] == owner_topic
            ):
                return int(topics[3], 16)
        raise ChainError("mint receipt has no position NFT transfer")

    def _verified_receipt(self, p: Pending) -> dict[str, Any] | None:
        """The receipt for exactly `p`, on a canonical block both providers agree on and
        at least RECEIPT_CONFIRMATIONS deep. None means not yet provable."""
        receipt = self.rpc.receipt(p.tx_hash)
        if receipt is None:
            return None
        number = int(str(receipt["blockNumber"]), 16)
        if self.rpc.uint("eth_blockNumber", []) - number < RECEIPT_CONFIRMATIONS:
            return None
        for rpc in (self.rpc, self.secondary):
            if rpc is None:
                continue
            other = receipt if rpc is self.rpc else rpc.receipt(p.tx_hash)
            if other is None:
                return None
            fields = ("transactionHash", "blockHash", "status", "gasUsed", "logs")
            if any(other.get(k) != receipt.get(k) for k in fields):
                raise ChainError(f"providers disagree on receipt {p.tx_hash}")
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
            if int(str(receipt["status"]), 16) != 1:
                return "pending " + self._reverted(p.kind, receipt)
            if p.kind == "burn" and not self._burned_in(receipt):
                s.halted = f"burn receipt {p.tx_hash} has no position NFT burn"
                return f"HALT {s.halted}"
            self._apply(p.kind, p.intent, receipt)
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

    def run(self, *, max_ticks: int | None = None, until: float | None = None) -> None:
        ticks = 0
        while max_ticks is None or ticks < max_ticks:
            if until is not None and time.time() >= until:
                break
            self.tick()
            ticks += 1
            if self.state.halted or (self.stop_file is not None and self.stop_file.exists()):
                break
            time.sleep(self.c.poll_seconds)

    def report(self) -> dict[str, Any]:
        s = self.state
        return {
            "mode": s.mode,
            "halted": s.halted,
            "position": asdict(s.position) if s.position else None,
            "initial_nav_usd": s.initial_nav_usd,
            "last_nav_usd": s.last_nav_usd,
            "gas_spent_eth": s.gas_spent_wei / 1e18,
            "actions": [h for h in s.history if h["event"] != "pins_recorded"][-20:],
        }
