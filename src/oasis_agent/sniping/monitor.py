"""Real-time launch monitor: detect executable liquidity, buy, then watch sell.

All decision inputs are dual-provider and fail closed. Broadcasting only
happens through an explicit, user-authorized execution path; every attempt is
journaled before and after. The private key lives in the process environment
and is never printed or logged.
"""

import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from eth_abi import decode  # type: ignore[attr-defined]
from eth_account import Account
from eth_utils import to_checksum_address  # type: ignore[attr-defined]

from ..launch.entry_price import minimum_received
from ..models.common import sha256_hex
from .calldata import (
    buy_calldata,
    buy_calldata_usdc,
    encode_v3_path,
    quote_exact_input_calldata,
    sell_calldata,
    sell_calldata_usdc,
)
from .config import ExecutionConfig
from .discovery import VenueScanner
from .event_wakeup import EventWakeup
from .exit_state import ExitEvaluation, evaluate_exit
from .journal import ExecutionJournal
from .rpc import JsonRpc, ProviderDisagreement, RpcError, _selector, addr_arg
from .scheduling import PoolSchedule
from .venues import VenuePool, call_data, swap_call
from .venues import quote as venue_quote


@dataclass(frozen=True)
class LivePool:
    fee: int
    pool: str
    route: VenuePool | None = None
    base: str = ""  # v3 route=None counterparty: config.weth (native) or config.usdc


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
        account: Account | None,
        journal: ExecutionJournal,
        stop_file: Path,
        *,
        log: Callable[[str], None] = print,
        dry_run: bool = False,
        venue_scanner: VenueScanner | None = None,
        wakeup: EventWakeup | None = None,
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
        self.halt_buys = False
        self.submission_unknown = False
        self.venue_scanner = venue_scanner
        self.wakeup = wakeup
        self._pool_schedule = PoolSchedule()
        self._sell_schedule = PoolSchedule()

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

    def _settled_token_balance(self, block_hash: str) -> int:
        """Read settlement state without reusing the pre-trade decision snapshot."""
        data = _selector("balanceOf(address)") + addr_arg(self.wallet)
        raw = self.rpc.dual(
            "eth_call",
            [
                {"to": self.config.target_ca, "data": data},
                {"blockHash": block_hash, "requireCanonical": True},
            ],
        ).require_agreement("settled token balance")
        return int(raw, 16)

    def _token_code_hash(self) -> str:
        code = self.rpc.get_code(self.config.target_ca)
        return sha256_hex(bytes.fromhex(code[2:])) if code and code != "0x" else "0x"

    def _token_owner(self) -> str:
        return "0x" + self.rpc.eth_call(self.config.target_ca, _selector("owner()"))[-40:]

    def _eth_usd_feed(self) -> tuple[int, int, int]:
        """Return (price_usd_micros, updated_at, block) from dual fresh reads."""
        snapshot = getattr(self.rpc, "snapshot", None)
        key = snapshot.get("blockHash") if isinstance(snapshot, dict) else None
        cached = getattr(self, "_feed_cache", None)
        if key is not None and cached is not None and cached[0] == key:
            price, updated_at, block = cached[1]
            if not 0 <= int(time.time()) - updated_at <= self.config.feed_max_age_seconds:
                raise ProviderDisagreement("cached ETH/USD feed expired")
            return price, updated_at, block
        data = _selector("latestRoundData()")
        dual = self.rpc.dual_eth_call(self.config.eth_usd_feed, data)
        left = dual.require_agreement("ETH/USD feed")
        parts = decode(
            ["uint80", "int256", "uint256", "uint256", "uint80"],
            bytes.fromhex(str(left)[2:]),
            strict=True,
        )
        if parts[4] < parts[0]:
            raise ProviderDisagreement("incomplete oracle round")
        answer, updated_at = parts[1], parts[3]
        decimals = int(self.rpc.eth_call(self.config.eth_usd_feed, _selector("decimals()")), 16)
        if decimals != 8 or answer <= 0:
            raise ProviderDisagreement(f"invalid ETH/USD feed: decimals={decimals} answer={answer}")
        now = int(time.time())
        if not 0 <= now - updated_at <= self.config.feed_max_age_seconds:
            raise ProviderDisagreement(f"stale ETH/USD feed: updated {now - updated_at}s ago")
        value = ((answer + 99) // 100, updated_at, self._snapshot_block())
        if key is not None:
            self._feed_cache = (key, value)
        return value

    def _snapshot_block(self) -> int:
        number = getattr(self.rpc, "snapshot_number", None)
        return number if type(number) is int else self.rpc.block_number()

    def _live_v3_pools(self) -> list[LivePool]:
        pools: list[LivePool] = []
        data = _selector("getPool(address,address,uint24)")
        # All lookups share the already pinned snapshot. Parallel discovery
        # avoids one network round trip per fee while checking both sources.
        slots = 2 * len(self.config.base_tokens) * len(self.config.v3_fees)
        with ThreadPoolExecutor(
            max_workers=max(8, slots), thread_name_prefix="launch-pools"
        ) as workers:
            pending = []
            for base in self.config.base_tokens:
                for fee in self.config.v3_fees:
                    calldata = (
                        data
                        + addr_arg(self.config.target_ca)
                        + addr_arg(base)
                        + format(fee, "x").rjust(64, "0")
                    )
                    left = workers.submit(
                        self.rpc.eth_call, self.config.v3_factory, calldata, provider="primary"
                    )
                    right = workers.submit(
                        self.rpc.eth_call, self.config.v3_factory, calldata, provider="secondary"
                    )
                    pending.append((fee, base, left, right))
            for fee, base, left, right in pending:
                raw = left.result()
                if raw != right.result():
                    raise ProviderDisagreement(f"pool identity disagreement fee={fee} base={base}")
                (pool,) = decode(["address"], bytes.fromhex(raw[2:]), strict=True)
                if int(pool, 16):
                    pools.append(LivePool(fee=fee, pool=to_checksum_address(pool), base=base))
        return pools

    def _multi_hop_quote(self, pool: LivePool, amount: int, *, buy: bool) -> int:
        """Dual-provider two-hop quote through the canonical WETH/USDC tier.

        Buy quotes WETH -> USDC -> LAPTOP; sell quotes LAPTOP -> USDC -> WETH.
        The returned amount for a sell is therefore in WETH base units, keeping
        downstream cost-basis valuation consistent with the direct-WETH path.
        """
        if pool.base != self.config.usdc:
            raise RpcError("multi-hop quote requires a USDC-denominated pool")
        if buy:
            path = encode_v3_path(
                (self.config.weth, self.config.usdc, self.config.target_ca),
                (self.config.usdc_weth_fee, pool.fee),
            )
        else:
            path = encode_v3_path(
                (self.config.target_ca, self.config.usdc, self.config.weth),
                (pool.fee, self.config.usdc_weth_fee),
            )
        data = quote_exact_input_calldata(path, amount)
        dual = self.rpc.dual_eth_call(self.config.quoter_v2, data)
        out, _, _, _ = decode(
            ["uint256", "uint160[]", "uint32[]", "uint256"],
            bytes.fromhex(str(dual.require_agreement("usdc hop quote"))[2:]),
            strict=True,
        )
        if out <= 0:
            raise RpcError("zero usdc hop quote")
        return int(out)

    def _quote_buy(self, pool: LivePool, amount_wei: int) -> tuple[int, int]:
        """Dual-provider exact-in quote base -> LAPTOP at the real tranche size.

        Returns (received_base_units, block). Raises if providers disagree or
        the pool cannot fill the amount.
        """
        if pool.route is not None:
            if self.rpc.snapshot_number is None:
                raise RpcError("venue quote requires a pinned block")
            return (
                venue_quote(
                    self.rpc,
                    pool.route,
                    self.config.target_ca,
                    amount_wei,
                    buy=True,
                    usdc=self.config.usdc,
                ),
                self.rpc.snapshot_number,
            )
        if pool.base == self.config.usdc:
            return self._multi_hop_quote(pool, amount_wei, buy=True), self._snapshot_block()
        inner = (
            addr_arg(self.config.weth)
            + addr_arg(self.config.target_ca)
            + format(amount_wei, "x").rjust(64, "0")
            + format(pool.fee, "x").rjust(64, "0")
            + "0" * 64
        )
        data = _selector("quoteExactInputSingle((address,address,uint256,uint24,uint160))") + inner
        dual = self.rpc.dual_eth_call(self.config.quoter_v2, data)
        received, _, _, _ = decode(
            ["uint256", "uint160", "uint32", "uint256"],
            bytes.fromhex(str(dual.require_agreement(f"buy quote fee={pool.fee}"))[2:]),
            strict=True,
        )
        if received <= 0:
            raise RpcError("zero buy quote")
        return int(received), self._snapshot_block()

    def _quote_sell(self, pool: LivePool, amount_laptop: int) -> int:
        """Dual-provider exact-in quote LAPTOP -> base for a given amount."""
        if pool.route is not None:
            return venue_quote(
                self.rpc,
                pool.route,
                self.config.target_ca,
                amount_laptop,
                buy=False,
                usdc=self.config.usdc,
            )
        if pool.base == self.config.usdc:
            return self._multi_hop_quote(pool, amount_laptop, buy=False)
        inner = (
            addr_arg(self.config.target_ca)
            + addr_arg(self.config.weth)
            + format(amount_laptop, "x").rjust(64, "0")
            + format(pool.fee, "x").rjust(64, "0")
            + "0" * 64
        )
        data = _selector("quoteExactInputSingle((address,address,uint256,uint24,uint160))") + inner
        dual = self.rpc.dual_eth_call(self.config.quoter_v2, data)
        out, _, _, _ = decode(
            ["uint256", "uint160", "uint32", "uint256"],
            bytes.fromhex(str(dual.require_agreement(f"sell quote fee={pool.fee}"))[2:]),
            strict=True,
        )
        if out <= 0:
            raise RpcError("zero sell quote")
        return int(out)

    def _check_token_baseline(self) -> None:
        raw = self.rpc.dual_eth_call(self.config.target_ca, _selector("decimals()"))
        encoded = str(raw.require_agreement("target token decimals"))
        if len(encoded) != 66 or not encoded.startswith("0x"):
            raise ProviderDisagreement("invalid target token decimals encoding")
        (decimals,) = decode(["uint8"], bytes.fromhex(encoded[2:]), strict=True)
        if decimals != self.config.token_decimals:
            raise ProviderDisagreement("target token decimals differ from configured policy")
        code_hash = self._token_code_hash()
        if code_hash != self._code_hash:
            if self._code_hash is not None:
                raise ProviderDisagreement("target runtime code hash changed - stand down buys")
            self._code_hash = code_hash
        owner = self._token_owner()
        self.log(f"token baseline ok code_hash={code_hash[:16]}... owner={owner}")

    # --------------------------------------------------------------- gating

    def _buy_gate(
        self, pool: LivePool, amount_wei: int, price_usd_micros: int
    ) -> tuple[int, int, bool]:
        """Return (min_received, quoted_received, fdv_ok). Fail closed."""
        quoted, _ = self._quote_buy(pool, amount_wei)
        minimum = minimum_received(
            native_wei=amount_wei,
            eth_usd_micros=price_usd_micros,
            token_decimals=self.config.token_decimals,
            quoted_base_units=quoted,
            slippage_bps=self.config.slippage_bps,
            max_fdv_usd_micros=self.config.max_fdv_usd_micros,
            reference_supply=self.config.reference_supply,
        )
        fdv_ok = (
            amount_wei
            * price_usd_micros
            * self.config.reference_supply
            * 10**self.config.token_decimals
            < self.config.max_fdv_usd_micros * quoted * 10**18
        )
        return minimum, quoted, fdv_ok

    def _next_nonce(self) -> int:
        return int(
            self.rpc.dual("eth_getTransactionCount", [self.wallet, "pending"]).require_agreement(
                "pending nonce"
            ),
            16,
        )

    def _transaction_preflight(self, tx: dict[str, Any]) -> tuple[int, int, int]:
        """Independent reads run together; signing/broadcast remain serial."""
        started = time.monotonic()
        with ThreadPoolExecutor(max_workers=3, thread_name_prefix="launch-preflight") as workers:
            pending = {
                "estimate_gas": workers.submit(self.rpc.estimate_gas, tx),
                "gas_price": workers.submit(self.rpc.gas_price),
                "pending_nonce": workers.submit(self._next_nonce),
            }
            values = {}
            for stage, future in pending.items():
                try:
                    values[stage] = future.result()
                except (RpcError, ProviderDisagreement, TimeoutError, ValueError) as exc:
                    raise RpcError(f"preflight {stage}: {exc}") from exc
        estimate = values["estimate_gas"]
        if type(estimate) is not int or not 0 < estimate <= self.config.max_gas:
            raise RpcError("preflight gas estimate exceeds cap or is invalid")
        gas = min(estimate * (10_000 + self.config.gas_buffer_bps) // 10_000, self.config.max_gas)
        gas_price = values["gas_price"] * self.config.gas_price_multiplier
        nonce = values["pending_nonce"]
        if type(gas_price) is not int or gas_price <= 0 or type(nonce) is not int or nonce < 0:
            raise RpcError("preflight invalid gas price or nonce")
        self._journal_event(
            "preflight_complete",
            {
                "elapsed_ms": int((time.monotonic() - started) * 1000),
                "block": self._snapshot_block(),
                "nonce": nonce,
                "gas": gas,
            },
        )
        return gas, gas_price, nonce

    def _sign_and_bind(self, payload: dict[str, Any]) -> tuple[str, str]:
        """Sign the payload and return (tx_hash, raw_hex) with a durable intent.

        The tx_hash is derived deterministically from the signed bytes, so the
        journal can bind intent -> tx_hash -> nonce even if the process dies
        between broadcast and confirmation.
        """
        if self.account is None:
            raise RpcError("no signing key configured; live execution requires --live")
        if self.dry_run:
            raise RpcError("dry-run mode cannot sign or broadcast")
        normalized = payload | {"to": to_checksum_address(payload["to"])}
        signed = self.account.sign_transaction(normalized)
        raw_hex = signed.raw_transaction.hex()
        tx_hash = "0x" + signed.hash.hex()
        self._journal_event(
            "broadcast_sent",
            {
                "nonce": payload["nonce"],
                "tx_hash": tx_hash,
                "chain_id": payload["chainId"],
                "to": payload.get("to"),
            },
        )
        return tx_hash, raw_hex

    def _broadcast(
        self, payload: dict[str, Any], receipt_timeout: float | None = None
    ) -> dict[str, Any]:
        if self.stop_file.exists() or self.submission_unknown:
            raise RpcError("stop requested or previous submission unresolved")
        tx_hash, raw_hex = self._sign_and_bind(payload)
        # Keep this latch set through receipt AND accounting. A successful
        # broadcast followed by a balance/journal error must never retry a buy.
        self.submission_unknown = True
        try:
            self.rpc.send_raw_transaction(raw_hex)
            receipt = self.rpc.wait_receipt(tx_hash, receipt_timeout)
        except (RpcError, TimeoutError) as exc:
            # Broadcast outcome unknown: the tx may have landed after all.
            # Halt new buys immediately (not only on the next restart) and
            # persist the halt so recovery stays fail-closed.
            self.submission_unknown = True
            self._journal_event(
                "halt_buys", {"reason": "broadcast_outcome_unknown", "tx_hash": tx_hash}
            )
            raise RpcError(f"broadcast outcome unknown for {tx_hash}") from exc
        if receipt.get("transactionHash") != tx_hash or receipt.get("status") not in {"0x0", "0x1"}:
            raise RpcError("invalid transaction receipt identity or status")
        status = int(receipt["status"], 16)
        if status != 1:
            self.submission_unknown = False
            raise RpcError(f"transaction {tx_hash} reverted on-chain")
        block_hash = receipt.get("blockHash")
        if not isinstance(block_hash, str) or len(block_hash) != 66:
            raise RpcError("invalid receipt block hash")
        return {
            "tx_hash": tx_hash,
            "block": int(receipt["blockNumber"], 16),
            "block_hash": block_hash,
            "status": status,
            "gas_used": int(receipt["gasUsed"], 16) if receipt.get("gasUsed") else 0,
            "nonce": payload["nonce"],
        }

    # ---------------------------------------------------------------- buying

    def _require_venue_sell_allowance(self, route: VenuePool, amount: int) -> None:
        spender = self.config.permit2 if route.venue == "uniswap_v4" else route.router
        raw = self.rpc.dual_eth_call(
            self.config.target_ca,
            call_data("allowance(address,address)", ["address", "address"], [self.wallet, spender]),
        ).require_agreement("venue sell allowance")
        if decode(["uint256"], bytes.fromhex(raw[2:]))[0] < amount:
            raise RpcError("new venue requires a separate token approval before entry")
        if route.venue == "uniswap_v4":
            raw = self.rpc.dual_eth_call(
                self.config.permit2,
                call_data(
                    "allowance(address,address,address)",
                    ["address", "address", "address"],
                    [self.wallet, self.config.target_ca, route.router],
                ),
            ).require_agreement("v4 router allowance")
            allowed, expiry, _ = decode(["uint160", "uint48", "uint48"], bytes.fromhex(raw[2:]))
            if allowed < amount or expiry <= int(time.time()) + 60:
                raise RpcError("v4 requires a separate unexpired Permit2 router allowance")

    def _venue_swap(
        self, pool: LivePool, amount: int, minimum: int, *, buy: bool
    ) -> tuple[str, int, str]:
        if pool.route is None:
            if pool.base == self.config.usdc:
                if buy:
                    data, value = buy_calldata_usdc(
                        self.config,
                        amount_in_wei=amount,
                        amount_out_min_laptop=minimum,
                        laptop_usdc_fee=pool.fee,
                        recipient=self.wallet,
                    )
                else:
                    data = sell_calldata_usdc(
                        self.config,
                        amount_in_laptop=amount,
                        amount_out_min_weth=minimum,
                        laptop_usdc_fee=pool.fee,
                        wallet=self.wallet,
                    )
                    value = 0
            elif buy:
                data, value = buy_calldata(
                    self.config,
                    amount_in_wei=amount,
                    amount_out_min_laptop=minimum,
                    fee=pool.fee,
                    recipient=self.wallet,
                )
            else:
                data = sell_calldata(
                    self.config,
                    amount_in_laptop=amount,
                    amount_out_min_weth=minimum,
                    fee=pool.fee,
                    wallet=self.wallet,
                )
                value = 0
            return data, value, self.config.universal_router
        if self.venue_scanner is None:
            raise RpcError("venue scanner missing")
        self.venue_scanner.verify_pool(pool.route)
        self._require_venue_sell_allowance(pool.route, minimum if buy else amount)
        tx = swap_call(
            pool.route,
            self.config.target_ca,
            self.wallet,
            amount,
            minimum,
            int(time.time()) + 60,
            buy=buy,
            usdc=self.config.usdc,
        )
        return str(tx["data"]), int(tx["value"], 16), str(tx["to"])

    @staticmethod
    def _check_swap_result(pool: LivePool, simulation: str, minimum: int) -> None:
        if pool.route is None or pool.route.venue == "uniswap_v4":
            if simulation != "0x":
                raise RpcError("unexpected execute return data")
        elif pool.route.venue == "aerodrome":
            (amounts,) = decode(["uint256[]"], bytes.fromhex(simulation[2:]))
            if len(amounts) < 2 or amounts[-1] < minimum:
                raise RpcError("Aerodrome simulation below minimum")
        else:
            (results,) = decode(["bytes[]"], bytes.fromhex(simulation[2:]))
            if not results or decode(["uint256"], results[0])[0] < minimum:
                raise RpcError("Slipstream simulation below minimum")

    def _try_buy_tranche(self, pool: LivePool, amount_wei: int, tranche_index: int) -> BuyResult:
        price_usd_micros, _, _ = self._eth_usd_feed()
        minimum, quoted, fdv_ok = self._buy_gate(pool, amount_wei, price_usd_micros)
        self.log(
            f"tranche {tranche_index}: pool={pool.pool} fee={pool.fee} "
            f"quote_base={quoted} minimum_base={minimum} "
            f"fdv_ok={fdv_ok} eth_usd={price_usd_micros / 1e6:.2f}"
        )
        if not fdv_ok:
            self._journal_event(
                "buy_skip",
                {
                    "reason": "fdv_at_or_above_cap",
                    "pool": pool.pool,
                    "fee": pool.fee,
                    "native_wei": amount_wei,
                    "quote_base_units": quoted,
                    "min_base_units": minimum,
                    "eth_usd_micros": price_usd_micros,
                    "fdv_ok": False,
                },
            )
            return BuyResult(bought=False, fdv_skip=True, reason="fdv_at_or_above_cap")
        data, value, router = self._venue_swap(pool, amount_wei, minimum, buy=True)
        simulation = self.rpc.simulate(from_address=self.wallet, to=router, data=data, value=value)
        self._check_swap_result(pool, simulation, minimum)
        simulated_out = None
        self.log(
            "buy eth_call succeeded; actual receipt and continuous sellability still unverified"
        )
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
            return BuyResult(bought=False, reason="dry_run")
        gas, gas_price, nonce = self._transaction_preflight(
            {"from": self.wallet, "to": router, "data": data, "value": hex(value)}
        )
        payload = {
            "nonce": nonce,
            "to": router,
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
        after = self._settled_token_balance(result["block_hash"])
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
            f"received_base={received} target={self.config.target_ca} tx={result['tx_hash']}"
        )
        self.tranches_done += 1
        self.submission_unknown = False
        # Subsequent exit checks must observe at least the settled buy state.
        self.rpc.snapshot = {"blockHash": result["block_hash"], "requireCanonical": True}
        self.rpc.snapshot_number = result["block"]
        return BuyResult(bought=True, reason="confirmed")

    # -------------------------------------------------------------- selling

    def _evaluate_position(self, pool: LivePool) -> ExitEvaluation:
        balance = self._wallet_laptop_balance()
        if balance <= 0:
            raise RpcError("position empty")
        quoted_weth = self._quote_sell(pool, balance)
        price_usd_micros, _, _ = self._eth_usd_feed()
        value_usd_micros = quoted_weth * price_usd_micros // 10**18
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
        expected_weth = self._quote_sell(pool, amount_laptop)
        min_weth = expected_weth * (10_000 - self.config.slippage_bps) // 10_000
        if min_weth <= 0:
            raise RpcError("sell minimum below one wei")
        data, _, router = self._venue_swap(pool, amount_laptop, min_weth, buy=False)
        simulation = self.rpc.simulate(from_address=self.wallet, to=router, data=data, value=0)
        self._check_swap_result(pool, simulation, min_weth)
        if self.dry_run:
            self._journal_event(
                "sell_dry_run",
                {"reason": reason, "amount_laptop": amount_laptop, "min_weth": min_weth},
            )
            return True
        gas, gas_price, nonce = self._transaction_preflight(
            {"from": self.wallet, "to": router, "data": data, "value": "0x0"}
        )
        payload = {
            "nonce": nonce,
            "to": router,
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
        balance_after = self._settled_token_balance(result["block_hash"])
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
        self.log(f"SELL({reason}) confirmed: sold_base={amount_laptop} tx={result['tx_hash']}")
        self.submission_unknown = False
        self.rpc.snapshot = {"blockHash": result["block_hash"], "requireCanonical": True}
        self.rpc.snapshot_number = result["block"]
        return True

    # --------------------------------------------------------------- journal

    def _journal_event(self, kind: str, payload: dict[str, Any]) -> None:
        record = self.journal.append(kind, payload | {"observed_at": int(time.time())})
        self.log(f"journal {kind} seq={record['seq']}")

    # ----------------------------------------------------------------- loop

    def _peek_receipt(self, tx_hash: str) -> dict[str, Any] | None:
        """Dual-provider receipt lookup for an unresolved intent.

        Returns None when both providers agree the transaction is not yet
        mined. A disagreement (one found, one missing) is treated as unknown
        by the caller, never resolved.
        """
        try:
            primary = self.rpc.get_transaction_receipt(tx_hash, "primary")
        except RpcError:
            primary = None
        try:
            secondary = self.rpc.get_transaction_receipt(tx_hash, "secondary")
        except RpcError:
            secondary = None
        if primary is None and secondary is None:
            return None
        if primary is None or secondary is None:
            raise ProviderDisagreement(f"receipt disagreement for {tx_hash}")
        return primary

    def _recover_position(self) -> None:
        balance = self._wallet_laptop_balance()
        records = self.journal.read()
        sent = {
            r["tx_hash"]
            for r in records
            if r.get("kind") == "broadcast_sent" and isinstance(r.get("tx_hash"), str)
        }
        confirmed = {
            r["tx_hash"]
            for r in records
            if r.get("kind") in {"buy_confirmed", "sell_confirmed"}
            and isinstance(r.get("tx_hash"), str)
        }
        unresolved = sent - confirmed
        for tx_hash in sorted(unresolved):
            receipt = self._peek_receipt(tx_hash)
            if receipt is None:
                self.submission_unknown = True
                self.log(f"unresolved broadcast {tx_hash}: no receipt - halting buys")
            elif int(str(receipt.get("status", "0x1")), 16) != 1:
                self.log(f"unresolved broadcast {tx_hash}: reverted on-chain")
            else:
                self.submission_unknown = True
                self.log(
                    f"unresolved broadcast {tx_hash}: confirmed but not journaled - halting buys"
                )
        self.halt_buys = (
            any(r.get("kind") == "halt_buys" for r in records) or self.submission_unknown
        )
        for record in records:
            if record.get("kind") == "buy_confirmed":
                self.position.spent_wei += record["amount_wei"]
                self.position.received_base += record["received_base"]
                self.position.spent_usd_micros += (
                    record["price_usd_micros"] * record["amount_wei"] // 10**18
                )
                self.position.started_at = self.position.started_at or record.get("observed_at", 0)
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
            f"tranches_done={self.tranches_done} wallet_balance={balance} "
            f"submission_unknown={self.submission_unknown}"
        )

    def run(self) -> None:
        if not self.dry_run and self.account is None:
            raise RpcError("live run requires a signing account")
        self.rpc.begin_snapshot()
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
                    self.halt_buys = True
                    self._journal_event("halt_buys", {"reason": "repeated_monitor_errors"})
            except KeyboardInterrupt:
                self.log("interrupted - exiting")
                break
            if self.wakeup is not None:
                self.wakeup.wait(self.config.poll_interval_ms / 1000)
            else:
                time.sleep(self.config.poll_interval_ms / 1000)

    def _cycle_once(self) -> None:
        left_block = self.rpc.begin_snapshot()
        if self._cycle % 10 == 1:
            self._check_token_baseline()
        pools = self._live_v3_pools()
        signaled_pools: set[str] = set()
        if self.venue_scanner is not None:
            signals = self.venue_scanner.poll(left_block, {p.pool for p in pools})
            for signal in signals:
                signaled_pools.add(signal.pool.lower())
                self.log(f"liquidity event kind={signal.kind} pool={signal.pool}")
            pools.extend(
                LivePool(p.fee, p.pool, p)
                for p in self.venue_scanner.pools.values()
                if p.tradeable(self.config.target_ca, self.config.usdc)
            )
            if self.wakeup is not None:
                self.wakeup.add_pools([p.pool for p in pools if len(p.pool) == 42])
        if not pools:
            self.log(f"cycle {self._cycle} block={left_block}: no live v3 pool yet")
            return
        by_id = {pool.pool.lower(): pool for pool in pools}
        selected = self._pool_schedule.select(list(by_id), signaled_pools)
        batch = [by_id[pool] for pool in selected]
        # Discovery may be slow. Start a new coherent snapshot for this bounded
        # batch instead of quoting hundreds of pools at a minutes-old head.
        left_block = self.rpc.begin_snapshot()
        self._journal_event(
            "scan_batch",
            {
                "cycle": self._cycle,
                "block": left_block,
                "candidate_count": len(by_id),
                "selected_pools": selected,
                "liquidity_signal_count": len(signaled_pools),
            },
        )
        if self.dry_run:
            self._observe_quotes(batch, left_block)
            return
        balance = self._wallet_laptop_balance()
        buys_pending = (
            self.tranches_done < len(self.config.buy_tranches_wei)
            and not self.tranche_2_abandoned
            and not self.halt_buys
            and not self.submission_unknown
        )
        if buys_pending:
            self._buy_cycle(batch, left_block)
        if (self.position.received_base > 0 or balance > 0) and not self.submission_unknown:
            self._sell_cycle(pools)

    def _observe_quotes(self, pools: list[LivePool], block: int) -> None:
        price, _, _ = self._eth_usd_feed()
        for pool in pools:
            for amount in self.config.buy_tranches_wei:
                try:
                    minimum, received, fdv_ok = self._buy_gate(pool, amount, price)
                    fdv_micros = (
                        amount
                        * price
                        * self.config.reference_supply
                        * 10**self.config.token_decimals
                        // (received * 10**18)
                    )
                    self._journal_event(
                        "quote_observation",
                        {
                            "block": block,
                            "snapshot": self.rpc.snapshot,
                            "pool": pool.pool,
                            "fee": pool.fee,
                            "native_wei": amount,
                            "received_base": received,
                            "minimum_base": minimum,
                            "eth_usd_micros": price,
                            "fdv_usd_micros": fdv_micros,
                            "fdv_ok": fdv_ok,
                            "execution_eligible": False,
                            "reason": "continuous_simulation_and_signer_unverified",
                        },
                    )
                    self.log(
                        f"OBSERVE block={block} fee={pool.fee} native_wei={amount} "
                        f"received_base={received} fdv_usd_micros={fdv_micros} "
                        f"fdv_ok={fdv_ok} execution_eligible=False"
                    )
                except (ProviderDisagreement, RpcError, TimeoutError, ValueError) as exc:
                    self._journal_event(
                        "quote_unavailable",
                        {
                            "block": block,
                            "pool": pool.pool,
                            "fee": pool.fee,
                            "native_wei": amount,
                            "error_type": type(exc).__name__,
                            "error": f"{type(exc).__name__}: {exc}",
                            "execution_eligible": False,
                        },
                    )

    def _buy_cycle(self, pools: list[LivePool], block: int) -> None:
        index = self.tranches_done
        if (
            index >= len(self.config.buy_tranches_wei)
            or self.tranche_2_abandoned
            or self.halt_buys
            or self.submission_unknown
        ):
            return
        amount = self.config.buy_tranches_wei[index] - (
            self.config.buy_tranches_wei[index - 1] if index > 0 else 0
        )
        for pool in pools:
            if self.submission_unknown or self.stop_file.exists():
                return
            try:
                result = self._try_buy_tranche(pool, amount, index + 1)
                if result.bought:
                    self.log(f"tranche {index + 1} done at block {block}")
                    return
                # A high quote only disqualifies this pool at this snapshot.
                # Continue other pools and future snapshots within the budget.
            except (ProviderDisagreement, RpcError, TimeoutError, ValueError) as exc:
                self._journal_event(
                    "buy_failed",
                    {
                        "cycle": self._cycle,
                        "tranche": index + 1,
                        "block": block,
                        "snapshot": self.rpc.snapshot,
                        "pool": pool.pool,
                        "fee": pool.fee,
                        "error": f"{type(exc).__name__}: {exc}",
                    },
                )
                self.log(f"buy tranche {index + 1} failed: {type(exc).__name__}: {exc}")

    def _sell_cycle(self, pools: list[LivePool]) -> None:
        if self.submission_unknown or self.stop_file.exists():
            return
        by_id = {pool.pool.lower(): pool for pool in pools}
        for pool_id in self._sell_schedule.select(list(by_id), set()):
            pool = by_id[pool_id]
            try:
                evaluation = self._evaluate_position(pool)
                break
            except (ProviderDisagreement, RpcError, TimeoutError, ValueError) as exc:
                self._journal_event(
                    "sell_eval_failed",
                    {
                        "cycle": self._cycle,
                        "pool": pool.pool,
                        "block": self._snapshot_block(),
                        "error": f"{type(exc).__name__}: {exc}",
                    },
                )
                self.log(f"sell evaluation failed: {type(exc).__name__}: {exc}")
        else:
            return
        balance = self._wallet_laptop_balance()
        self.log(
            f"position value={evaluation.value_usd_micros / 1e6:.2f} usd "
            f"basis={evaluation.cost_basis_usd_micros / 1e6:.2f} usd "
            f"x{evaluation.multiple} action={evaluation.action} balance_base={balance}"
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
