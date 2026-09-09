"""Real-time launch monitor: detect executable liquidity, buy, then watch sell.

All decision inputs are dual-provider and fail closed. Broadcasting only
happens through an explicit, user-authorized execution path; every attempt is
journaled before and after. The private key lives in the process environment
and is never printed or logged.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from eth_account import Account
from eth_utils import to_checksum_address  # type: ignore[attr-defined]

from ..launch.entry_price import below_fdv, minimum_received
from ..models.common import sha256_hex
from .calldata import buy_calldata, sell_calldata
from .config import ExecutionConfig
from .exit_state import ExitEvaluation, evaluate_exit
from .journal import ExecutionJournal
from .rpc import JsonRpc, ProviderDisagreement, RpcError, _selector, addr_arg

TOKEN_DECIMALS = 18


@dataclass(frozen=True)
class LivePool:
    fee: int
    pool: str


@dataclass
class Position:
    spent_wei: int = 0
    received_base: int = 0
    spent_usd_micros: int = 0
    half_sold: bool = False
    started_at: int = 0
    buys: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class BuyResult:
    bought: bool
    fdv_skip: bool = False
    reason: str = ""


class Monitor:
    def __init__(
        self,
        config: ExecutionConfig,
        rpc: JsonRpc,
        wallet: str,
        account: Account,
        journal: ExecutionJournal,
        stop_file: Path,
        *,
        log: Callable[[str], None] = print,
        dry_run: bool = False,
    ):
        self.config = config
        self.rpc = rpc
        self.wallet = to_checksum_address(wallet)
        self.account = account
        self.journal = journal
        self.stop_file = Path(stop_file)
        self.log = log
        self.dry_run = dry_run
        self.position = Position()
        self._code_hash: str | None = None
        self._cycle = 0
        self._consecutive_errors = 0
        self.tranches_done = 0
        self.tranche_2_abandoned = False

    # ------------------------------------------------------------------ env

    @staticmethod
    def load_env(path: str | Path) -> dict[str, str]:
        env: dict[str, str] = {}
        for raw_line in Path(path).read_text().splitlines():
            line = raw_line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                value = value.strip()
                if value[:1] in ('"', "'") and value[-1:] == value[:1]:
                    value = value[1:-1]
                env[key.strip()] = value
        return env

    # ------------------------------------------------------------ primitives

    def _wallet_laptop_balance(self, provider: str = "primary") -> int:
        data = _selector("balanceOf(address)") + addr_arg(self.wallet)
        return int(self.rpc.eth_call(self.config.target_ca, data, provider=provider), 16)

    def _token_code_hash(self) -> str:
        code = self.rpc.get_code(self.config.target_ca)
        return sha256_hex(bytes.fromhex(code[2:])) if code and code != "0x" else "0x"

    def _token_owner(self) -> str:
        return "0x" + self.rpc.eth_call(self.config.target_ca, _selector("owner()"))[-40:]

    def _eth_usd_feed(self) -> tuple[int, int, int]:
        """Return (price_usd_micros, updated_at, block) from dual fresh reads."""
        data = _selector("latestRoundData()")
        dual = self.rpc.dual_eth_call(self.config.eth_usd_feed, data)
        left = dual.require_agreement("ETH/USD feed")
        parts = [int(str(left)[2 + 64 * i : 2 + 64 * (i + 1)], 16) for i in range(5)]
        answer, updated_at = parts[1], parts[3]
        decimals = int(self.rpc.eth_call(self.config.eth_usd_feed, _selector("decimals()")), 16)
        if decimals != 8 or answer <= 0:
            raise ProviderDisagreement(f"invalid ETH/USD feed: decimals={decimals} answer={answer}")
        now = int(time.time())
        if not 0 <= now - updated_at <= self.config.feed_max_age_seconds:
            raise ProviderDisagreement(f"stale ETH/USD feed: updated {now - updated_at}s ago")
        return answer * 100, updated_at, self.rpc.block_number()

    def _live_v3_pools(self) -> list[LivePool]:
        pools: list[LivePool] = []
        data = _selector("getPool(address,address,uint24)")
        for fee in self.config.v3_fees:
            calldata = (
                data
                + addr_arg(self.config.target_ca)
                + addr_arg(self.config.weth)
                + format(fee, "x").rjust(64, "0")
            )
            raw = self.rpc.eth_call(self.config.v3_factory, calldata)
            pool = "0x" + raw[-40:]
            if int(pool, 16):
                pools.append(LivePool(fee=fee, pool=to_checksum_address(pool)))
        return pools

    def _quote_buy(self, pool: LivePool, amount_wei: int) -> tuple[int, int]:
        """Dual-provider exact-in quote WETH -> LAPTOP at the real tranche size.

        Returns (received_base_units, block). Raises if providers disagree or
        the pool cannot fill the amount.
        """
        inner = (
            addr_arg(self.config.weth)
            + addr_arg(self.config.target_ca)
            + format(amount_wei, "x").rjust(64, "0")
            + format(pool.fee, "x").rjust(64, "0")
            + "0" * 64
        )
        data = (
            _selector("quoteExactInputSingle((address,address,uint256,uint24,uint160))")
            + format(len(inner) // 2, "x").rjust(64, "0")
            + inner
        )
        dual = self.rpc.dual_eth_call(self.config.quoter_v2, data)
        received = int(str(dual.require_agreement(f"buy quote fee={pool.fee}")), 16)
        if received <= 0:
            raise RpcError("zero buy quote")
        return received, self.rpc.block_number()

    def _quote_sell(self, pool: LivePool, amount_laptop: int) -> int:
        """Dual-provider exact-in quote LAPTOP -> WETH for a given amount."""
        inner = (
            addr_arg(self.config.target_ca)
            + addr_arg(self.config.weth)
            + format(amount_laptop, "x").rjust(64, "0")
            + format(pool.fee, "x").rjust(64, "0")
            + "0" * 64
        )
        data = (
            _selector("quoteExactInputSingle((address,address,uint256,uint24,uint160))")
            + format(len(inner) // 2, "x").rjust(64, "0")
            + inner
        )
        dual = self.rpc.dual_eth_call(self.config.quoter_v2, data)
        out = int(str(dual.require_agreement(f"sell quote fee={pool.fee}")), 16)
        if out <= 0:
            raise RpcError("zero sell quote")
        return out

    def _check_token_baseline(self) -> None:
        code_hash = self._token_code_hash()
        if code_hash != self._code_hash:
            if self._code_hash is not None:
                raise ProviderDisagreement("LAPTOP runtime code hash changed - stand down buys")
            self._code_hash = code_hash
        owner = self._token_owner()
        self.log(f"token baseline ok code_hash={code_hash[:16]}... owner={owner}")

    # --------------------------------------------------------------- gating

    def _buy_gate(
        self, pool: LivePool, amount_wei: int, price_usd_micros: int
    ) -> tuple[int, int, bool]:
        """Return (min_received, quoted_received, fdv_ok). Fail closed."""
        quoted, _ = self._quote_buy(pool, amount_wei)
        spend_usd = amount_wei * price_usd_micros // 10**18
        fdv_ok = below_fdv(
            spend_usd_micros=spend_usd,
            received_base_units=quoted,
            token_decimals=TOKEN_DECIMALS,
            max_fdv_usd_micros=self.config.max_fdv_usd_micros,
            reference_supply=self.config.reference_supply,
        )
        minimum = minimum_received(
            native_wei=amount_wei,
            eth_usd_micros=price_usd_micros,
            token_decimals=TOKEN_DECIMALS,
            quoted_base_units=quoted,
            slippage_bps=self.config.slippage_bps,
            max_fdv_usd_micros=self.config.max_fdv_usd_micros,
            reference_supply=self.config.reference_supply,
        )
        return minimum, quoted, fdv_ok

    def _next_nonce(self) -> int:
        left = self.rpc.get_transaction_count(self.wallet, "latest", "primary")
        right = self.rpc.get_transaction_count(self.wallet, "latest", "secondary")
        if left != right:
            raise ProviderDisagreement(f"nonce disagreement {left} != {right}")
        return left

    def _broadcast(
        self, payload: dict[str, Any], receipt_timeout: float | None = None
    ) -> dict[str, Any]:
        signed = self.account.sign_transaction(payload)
        raw = signed.raw_transaction.hex()
        errors: list[str] = []
        for provider in ("primary", "secondary"):
            for _ in range(self.config.broadcast_tries):
                try:
                    tx_hash = self.rpc.send_raw_transaction(raw, provider)
                    receipt = self.rpc.wait_receipt(tx_hash, receipt_timeout)
                    status = int(receipt["status"], 16)
                    if status != 1:
                        raise RpcError(f"tx {tx_hash} reverted on chain")
                    return {
                        "tx_hash": tx_hash,
                        "block": int(receipt["blockNumber"], 16),
                        "status": status,
                        "gas_used": int(receipt["gasUsed"], 16),
                        "nonce": payload["nonce"],
                    }
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{provider}:{type(exc).__name__}:{exc}")
        raise RpcError(f"broadcast failed on both providers: {errors}")

    # ---------------------------------------------------------------- buying

    def _try_buy_tranche(self, pool: LivePool, amount_wei: int, tranche_index: int) -> BuyResult:
        price_usd_micros, _, _ = self._eth_usd_feed()
        minimum, quoted, fdv_ok = self._buy_gate(pool, amount_wei, price_usd_micros)
        self.log(
            f"tranche {tranche_index}: pool={pool.pool} fee={pool.fee} "
            f"quote={quoted / 10**18:.4f} LAPTOP min={minimum / 10**18:.4f} "
            f"fdv_ok={fdv_ok} eth_usd={price_usd_micros / 1e6:.2f}"
        )
        if not fdv_ok:
            self._journal_event(
                "buy_skip",
                {"reason": "fdv_at_or_above_cap", "pool": pool.pool, "fee": pool.fee},
            )
            return BuyResult(bought=False, fdv_skip=True, reason="fdv_at_or_above_cap")
        data, value = buy_calldata(
            self.config,
            amount_in_wei=amount_wei,
            amount_out_min_laptop=minimum,
            fee=pool.fee,
            recipient=self.wallet,
        )
        simulation = self.rpc.simulate(
            from_address=self.wallet, to=self.config.universal_router, data=data, value=value
        )
        if len(simulation) < 64:
            raise RpcError(f"unexpected buy simulation output {simulation[:16]}")
        simulated_out = int(simulation[-64:], 16)
        if simulated_out < minimum:
            raise RpcError(f"simulated output {simulated_out} below minimum {minimum}")
        self.log(f"simulation ok output={simulated_out / 10**18:.4f} >= min={minimum / 10**18:.4f}")
        if self.dry_run:
            self._journal_event(
                "buy_dry_run",
                {
                    "tranche": tranche_index,
                    "amount_wei": amount_wei,
                    "min_received": minimum,
                    "quoted": quoted,
                    "simulated_out": simulated_out,
                    "pool": pool.pool,
                    "fee": pool.fee,
                    "price_usd_micros": price_usd_micros,
                },
            )
            self.tranches_done += 1
            return BuyResult(bought=True, reason="dry_run")
        gas_estimate = self.rpc.estimate_gas(
            {
                "from": self.wallet,
                "to": self.config.universal_router,
                "data": data,
                "value": hex(value),
            }
        )
        gas = min(
            gas_estimate * (10_000 + self.config.gas_buffer_bps) // 10_000,
            self.config.max_gas,
        )
        gas_price = self.rpc.gas_price() * self.config.gas_price_multiplier
        nonce = self._next_nonce()
        payload = {
            "nonce": nonce,
            "to": self.config.universal_router,
            "data": data,
            "value": value,
            "gas": gas,
            "gasPrice": gas_price,
            "chainId": self.config.chain_id,
        }
        before = self._wallet_laptop_balance()
        self._journal_event(
            "buy_broadcast",
            {
                "tranche": tranche_index,
                "amount_wei": amount_wei,
                "min_received": minimum,
                "quoted": quoted,
                "simulated_out": simulated_out,
                "pool": pool.pool,
                "fee": pool.fee,
                "price_usd_micros": price_usd_micros,
                "nonce": nonce,
                "gas": gas,
                "gas_price": gas_price,
            },
        )
        result = self._broadcast(payload)
        after = self._wallet_laptop_balance()
        received = after - before
        if received < minimum:
            raise RpcError(f"post-trade received {received} below minimum {minimum}")
        self.position.spent_wei += amount_wei
        self.position.received_base += received
        self.position.spent_usd_micros += amount_wei * price_usd_micros // 10**18
        if self.position.started_at == 0:
            self.position.started_at = int(time.time())
        self.position.buys.append(
            {
                "tranche": tranche_index,
                "tx_hash": result["tx_hash"],
                "block": result["block"],
                "amount_wei": amount_wei,
                "received_base": received,
                "price_usd_micros": price_usd_micros,
            }
        )
        self._journal_event(
            "buy_confirmed",
            {
                "tranche": tranche_index,
                "tx_hash": result["tx_hash"],
                "block": result["block"],
                "amount_wei": amount_wei,
                "received_base": received,
                "min_received": minimum,
                "price_usd_micros": price_usd_micros,
                "spent_usd_micros_total": self.position.spent_usd_micros,
                "received_base_total": self.position.received_base,
            },
        )
        self.log(
            f"BUY {tranche_index} confirmed: {amount_wei / 10**18} ETH -> "
            f"{received / 10**18:.6f} LAPTOP tx={result['tx_hash']}"
        )
        self.tranches_done += 1
        return BuyResult(bought=True, reason="confirmed")

    # -------------------------------------------------------------- selling

    def _evaluate_position(self, pool: LivePool) -> ExitEvaluation:
        balance = self._wallet_laptop_balance()
        if balance <= 0:
            raise RpcError("position empty")
        weth_per_token = self._quote_sell(pool, 10**TOKEN_DECIMALS)
        price_usd_micros, _, _ = self._eth_usd_feed()
        value_usd_micros = weth_per_token * balance * price_usd_micros // 10**36
        cost_basis = self.position.spent_usd_micros
        if cost_basis <= 0:
            raise RpcError("position cost basis missing")
        return evaluate_exit(
            cost_basis_usd_micros=cost_basis,
            value_usd_micros=value_usd_micros,
            started_at=self.position.started_at,
            now=int(time.time()),
            config=self.config,
            half_sold=self.position.half_sold,
        )

    def _try_sell(self, pool: LivePool, amount_laptop: int, reason: str) -> bool:
        weth_per_token = self._quote_sell(pool, 10**TOKEN_DECIMALS)
        expected_weth = weth_per_token * amount_laptop // 10**TOKEN_DECIMALS
        min_weth = expected_weth * (10_000 - self.config.slippage_bps) // 10_000
        if min_weth <= 0:
            raise RpcError("sell minimum below one wei")
        data = sell_calldata(
            self.config,
            amount_in_laptop=amount_laptop,
            amount_out_min_weth=min_weth,
            fee=pool.fee,
            wallet=self.wallet,
        )
        simulation = self.rpc.simulate(
            from_address=self.wallet, to=self.config.universal_router, data=data, value=0
        )
        if len(simulation) < 64:
            raise RpcError("unexpected sell simulation output")
        simulated_out = int(simulation[-64:], 16)
        if simulated_out < min_weth:
            raise RpcError(f"simulated sell output {simulated_out} below minimum {min_weth}")
        if self.dry_run:
            self._journal_event(
                "sell_dry_run",
                {"reason": reason, "amount_laptop": amount_laptop, "min_weth": min_weth},
            )
            return True
        gas_estimate = self.rpc.estimate_gas(
            {"from": self.wallet, "to": self.config.universal_router, "data": data, "value": "0x0"}
        )
        gas = min(
            gas_estimate * (10_000 + self.config.gas_buffer_bps) // 10_000,
            self.config.max_gas,
        )
        gas_price = self.rpc.gas_price() * self.config.gas_price_multiplier
        nonce = self._next_nonce()
        payload = {
            "nonce": nonce,
            "to": self.config.universal_router,
            "data": data,
            "value": 0,
            "gas": gas,
            "gasPrice": gas_price,
            "chainId": self.config.chain_id,
        }
        self._journal_event(
            "sell_broadcast",
            {
                "reason": reason,
                "amount_laptop": amount_laptop,
                "min_weth": min_weth,
                "nonce": nonce,
            },
        )
        result = self._broadcast(payload)
        balance_after = self._wallet_laptop_balance()
        if balance_after > self.position.received_base - amount_laptop + 1:
            raise RpcError("sell did not reduce the position as expected")
        self.position.received_base = max(0, self.position.received_base - amount_laptop)
        if reason == "sell_half":
            self.position.half_sold = True
        if reason in {"sell_all", "time_stop"}:
            self.tranche_2_abandoned = True
        self._journal_event(
            "sell_confirmed",
            {
                "reason": reason,
                "tx_hash": result["tx_hash"],
                "block": result["block"],
                "amount_laptop": amount_laptop,
                "min_weth": min_weth,
                "remaining_base": self.position.received_base,
            },
        )
        self.log(
            f"SELL({reason}) confirmed: {amount_laptop / 10**18} LAPTOP tx={result['tx_hash']}"
        )
        return True

    # --------------------------------------------------------------- journal

    def _journal_event(self, kind: str, payload: dict[str, Any]) -> None:
        record = self.journal.append(kind, payload)
        self.log(f"journal {kind} seq={record['seq']}")

    # ----------------------------------------------------------------- loop

    def _recover_position(self) -> None:
        balance = self._wallet_laptop_balance()
        for record in self.journal.read():
            if record.get("kind") == "buy_confirmed":
                self.position.spent_wei += record["amount_wei"]
                self.position.received_base += record["received_base"]
                self.position.spent_usd_micros += (
                    record["price_usd_micros"] * record["amount_wei"] // 10**18
                )
                self.position.started_at = self.position.started_at or int(time.time())
                self.position.buys.append(record)
                self.tranches_done += 1
            if record.get("kind") == "sell_confirmed":
                self.position.received_base = max(
                    0, self.position.received_base - record["amount_laptop"]
                )
                if record["reason"] == "sell_half":
                    self.position.half_sold = True
                if record["reason"] in {"sell_all", "time_stop"}:
                    self.tranche_2_abandoned = True
        self.log(
            f"recovered position: spent_wei={self.position.spent_wei} "
            f"received_base={self.position.received_base} half_sold={self.position.half_sold} "
            f"tranches_done={self.tranches_done} wallet_balance={balance}"
        )

    def run(self) -> None:
        self._check_token_baseline()
        self._recover_position()
        self.log(f"monitor started dry_run={self.dry_run} stop_file={self.stop_file}")
        while True:
            if self.stop_file.exists():
                self.log("stop file present - exiting")
                break
            self._cycle += 1
            try:
                self._cycle_once()
                self._consecutive_errors = 0
            except (ProviderDisagreement, RpcError, TimeoutError, ValueError) as exc:
                self._consecutive_errors += 1
                self._journal_event(
                    "monitor_error",
                    {"cycle": self._cycle, "error": f"{type(exc).__name__}: {exc}"},
                )
                self.log(f"cycle {self._cycle} error: {type(exc).__name__}: {exc}")
                if self._consecutive_errors >= 5:
                    self.log("too many consecutive errors - halting buys, monitoring only")
                    self._consecutive_errors = 0
            except KeyboardInterrupt:
                self.log("interrupted - exiting")
                break
            time.sleep(self.config.poll_interval_ms / 1000)

    def _cycle_once(self) -> None:
        left_block, _ = self.rpc.dual_block_number()
        if self._cycle % 10 == 1:
            self._check_token_baseline()
        pools = self._live_v3_pools()
        if not pools:
            self.log(f"cycle {self._cycle} block={left_block}: no live v3 pool yet")
            return
        balance = self._wallet_laptop_balance()
        buys_pending = (
            self.tranches_done < len(self.config.buy_tranches_wei) and not self.tranche_2_abandoned
        )
        if buys_pending:
            self._buy_cycle(pools, left_block)
        if (self.position.received_base > 0 or balance > 0) and (
            not buys_pending
            or self.tranches_done >= len(self.config.buy_tranches_wei)
            or self.tranche_2_abandoned
        ):
            self._sell_cycle(pools)

    def _buy_cycle(self, pools: list[LivePool], block: int) -> None:
        index = self.tranches_done
        amount = self.config.buy_tranches_wei[index] - (
            self.config.buy_tranches_wei[index - 1] if index > 0 else 0
        )
        for pool in pools:
            try:
                result = self._try_buy_tranche(pool, amount, index + 1)
                if result.bought:
                    self.log(f"tranche {index + 1} done at block {block}")
                    return
                if result.fdv_skip and index == len(self.config.buy_tranches_wei) - 1:
                    self.tranche_2_abandoned = True
                    self.log("tranche 2 abandoned: FDV at or above cap")
                    return
            except (ProviderDisagreement, RpcError, TimeoutError, ValueError) as exc:
                self._journal_event(
                    "buy_failed",
                    {
                        "cycle": self._cycle,
                        "tranche": index + 1,
                        "pool": pool.pool,
                        "fee": pool.fee,
                        "error": f"{type(exc).__name__}: {exc}",
                    },
                )
                self.log(f"buy tranche {index + 1} failed: {type(exc).__name__}: {exc}")

    def _sell_cycle(self, pools: list[LivePool]) -> None:
        pool = pools[0]
        try:
            evaluation = self._evaluate_position(pool)
        except (ProviderDisagreement, RpcError, TimeoutError, ValueError) as exc:
            self._journal_event(
                "sell_eval_failed",
                {"cycle": self._cycle, "error": f"{type(exc).__name__}: {exc}"},
            )
            self.log(f"sell evaluation failed: {type(exc).__name__}: {exc}")
            return
        balance = self._wallet_laptop_balance()
        self.log(
            f"position value={evaluation.value_usd_micros / 1e6:.2f} usd "
            f"basis={evaluation.cost_basis_usd_micros / 1e6:.2f} usd "
            f"x{evaluation.multiple} action={evaluation.action} balance={balance / 10**18:.6f}"
        )
        if evaluation.action == "hold":
            return
        amount = balance if evaluation.action != "sell_half" else balance // 2
        if amount <= 0:
            return
        try:
            self._try_sell(pool, amount, evaluation.action)
        except (ProviderDisagreement, RpcError, TimeoutError, ValueError) as exc:
            self._journal_event(
                "sell_failed",
                {
                    "cycle": self._cycle,
                    "action": evaluation.action,
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )
            self.log(f"sell {evaluation.action} failed: {type(exc).__name__}: {exc}")
