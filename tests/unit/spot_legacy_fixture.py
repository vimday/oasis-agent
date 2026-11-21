"""Reproduce historical v2 journal records with scripted I/O for replay tests."""

import json
import re
import time
import uuid
from collections.abc import Callable
from typing import Any

from oasis_agent.launch.onchainos_source import BASE_USDC, Runner, SourceFailure
from oasis_agent.launch.spot_executor import (
    DEFAULT_SETTLE_ATTEMPTS,
    Fill,
    LiveGate,
    LiveRefused,
    LiveUnresolved,
    Side,
    balance_argv,
    parse_balance,
    parse_receipt,
    receipt_fees,
    swap_call_bound,
)
from oasis_agent.models.common import normalize_address

_HASH = re.compile(r"^0x[0-9a-f]{64}$")
Emit = Callable[[str, dict[str, Any]], object]
Mined = tuple[int, int, str] | tuple[int, int, str, dict[str, Any]]
ReceiptReader = Callable[[str], Mined | None]


def _percent(bps: int) -> str:
    return f"{bps // 100}.{bps % 100:02d}"


def swap_argv(gate: LiveGate, side: Side, token: str, amount: int) -> tuple[str, ...]:
    """The only state-changing command this module can build: one Base USDC<->token swap.

    `--force` (skip the provider's risk warning) is never emitted; MEV protection is.
    """
    token = normalize_address(token)
    if token == BASE_USDC:
        raise LiveRefused("token_is_settlement")
    if not 0 < amount < 2**256:
        raise LiveRefused("invalid_amount")
    src, dst = (BASE_USDC, token) if side == "buy" else (token, BASE_USDC)
    return (
        "swap",
        "execute",
        "--chain",
        "base",
        "--wallet",
        gate.wallet,
        "--from",
        src,
        "--to",
        dst,
        "--amount",
        str(amount),
        "--swap-mode",
        "exactIn",
        "--slippage",
        _percent(gate.slippage_bps),
        "--mev-protection",
    )


def parse_tx_hash(raw: bytes) -> str | None:
    """The swap transaction hash the CLI claims; None when it states no broadcast."""
    try:
        root = json.loads(raw, parse_float=str, parse_int=str)
    except ValueError, RecursionError:
        return None
    if not isinstance(root, dict) or root.get("ok") is not True:
        return None
    data = root.get("data")
    rows = data if isinstance(data, list) else [data]
    # `swapTxHash` names the swap itself; a reply that also reports an approve under
    # `txHash`/`approveTxHash` must not be mistaken for it. Fall back to `txHash` only
    # when no swap-specific field exists. Exactly one distinct hash, or nothing.
    hashes: set[str] = set()
    for key in ("swapTxHash", "txHash"):
        hashes = {str(r[key]).lower() for r in rows if isinstance(r, dict) and r.get(key)}
        if hashes:
            break
    if len(hashes) != 1:
        return None
    (value,) = hashes
    return value if _HASH.fullmatch(value) else None


class LiveExecutor:
    """One gated wallet. `read` may be paced/retried; `submit` must run exactly once."""

    def __init__(
        self,
        gate: LiveGate,
        *,
        read: Runner,
        submit: Runner,
        receipt: ReceiptReader,
        emit: Emit,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        receipt_timeout: float = 180.0,
        poll_seconds: float = 3.0,
        settle_attempts: int = DEFAULT_SETTLE_ATTEMPTS,
    ):
        self.gate = gate
        self._read, self._submit, self._receipt, self._emit = read, submit, receipt, emit
        self._clock, self._sleep = clock, sleep
        self._receipt_timeout, self._poll, self._settle = (
            receipt_timeout,
            poll_seconds,
            settle_attempts,
        )
        self.calls = 0
        # Swaps handed to the wallet, whatever their outcome; the loop halts on any
        # tick error after a submission rather than rolling back a real trade.
        self.submissions = 0

    def calls_per_swap(self) -> int:
        """Worst-case CLI calls of one swap (see `swap_call_bound`)."""
        return swap_call_bound(self._settle)

    def balance(self, token: str) -> int:
        self.calls += 1
        return parse_balance(self._read(balance_argv(token)), token)

    def buy(self, token: str, usdc_amount: int) -> Fill:
        if usdc_amount > self.gate.max_trade_usd_micros:
            raise LiveRefused("above_trade_cap")
        return self._swap("buy", normalize_address(token), usdc_amount)

    def sell(self, token: str, units: int) -> Fill:
        return self._swap("sell", normalize_address(token), units)

    def _journal(self, kind: str, **body: Any) -> None:
        self._emit(
            kind,
            {
                "at": int(self._clock()),
                "kind": kind,
                "mode": "live",
                "evidence_level": "wallet_balance_delta_and_chain_receipt",
                "execution_eligible": False,
                **body,
            },
        )

    def _unresolved(
        self, base: dict[str, Any], category: str, tx_hash: str | None
    ) -> LiveUnresolved:
        self._journal("live_unresolved", **base, category=category, tx_hash=tx_hash)
        return LiveUnresolved(category)

    def _balances(self, token: str) -> tuple[int, int]:
        return self.balance(BASE_USDC), self.balance(token)

    def _swap(self, side: Side, token: str, amount: int) -> Fill:
        argv = swap_argv(self.gate, side, token, amount)
        try:
            usdc0, token0 = self._balances(token)
        except SourceFailure as error:
            raise LiveRefused("balance_unavailable") from error
        if (usdc0 if side == "buy" else token0) < amount:
            raise LiveRefused("insufficient_balance")
        # The intent id is minted once, journaled before submission and carried by every
        # later record of this order, so a fill can never be matched to another intent.
        base = {
            "intent_id": uuid.uuid4().hex,
            "chain_id": 8453,
            "side": side,
            "token": token,
            "amount": str(amount),
            **self.gate.describe(),
        }
        # Journal before submission: a crash after this line leaves an intent without a
        # fill, which refuses resume instead of silently forgetting a possible trade.
        self._journal("live_intent", **base, usdc_before=str(usdc0), token_before=str(token0))
        self.calls += 1
        self.submissions += 1
        try:
            raw = self._submit(argv)
        except SourceFailure as error:
            # A timeout or non-zero exit can follow a broadcast; it is never retried.
            raise self._unresolved(base, "submit_" + error.category, None) from error
        tx_hash = parse_tx_hash(raw)
        if tx_hash is None:
            raise self._unresolved(base, "no_tx_hash", None)
        mined = self._await_receipt(tx_hash)
        if mined is None:
            raise self._unresolved(base, "receipt_unavailable", tx_hash)
        status, block, sender = mined[0], mined[1], mined[2]
        fees = mined[3] if len(mined) == 4 else None
        if sender != self.gate.wallet:
            # A status-1 receipt of someone else's transaction proves nothing about ours.
            raise self._unresolved(base, "receipt_not_from_wallet", tx_hash)
        if status != 1:
            raise self._unresolved(base, "reverted", tx_hash)
        settled = self._await_settlement(side, token, amount, usdc0, token0)
        if settled is None:
            raise self._unresolved(base, "balance_not_settled", tx_hash)
        fill = Fill(
            side,
            token,
            tx_hash,
            block,
            settled[0] - usdc0,
            settled[1] - token0,
            base["intent_id"],
            fees,
        )
        # `fill.record()` restates side/token; it wins so the journal shows the fill itself.
        self._journal("live_fill", **(base | fill.record()))
        return fill

    def _await_receipt(self, tx_hash: str) -> Mined | None:
        waited = 0.0
        while True:
            try:
                mined = self._receipt(tx_hash)
            except Exception:  # noqa: BLE001 - RPC failure is "not yet proven", keep polling
                mined = None
            if mined is not None:
                return mined
            if waited >= self._receipt_timeout:
                return None
            self._sleep(self._poll)
            waited += self._poll

    def _await_settlement(
        self, side: Side, token: str, amount: int, usdc0: int, token0: int
    ) -> tuple[int, int] | None:
        """Balances after the receipt: the spent side moved by exactly `amount`, the other rose."""
        for attempt in range(self._settle):
            if attempt:
                self._sleep(self._poll)
            try:
                usdc1, token1 = self._balances(token)
            except SourceFailure:
                continue
            spent, received = (usdc0 - usdc1, token1 - token0)
            if side == "sell":
                spent, received = token0 - token1, usdc1 - usdc0
            if spent == amount and received > 0:
                return usdc1, token1
        return None


def rpc_receipt_reader(transport: Any) -> ReceiptReader:
    """One `eth_getTransactionReceipt` per poll on a chain-pinned transport.

    Identity, status and fee items all come from that one response; no extra request.
    """

    def read(tx_hash: str) -> Mined | None:
        if not _HASH.fullmatch(tx_hash):
            raise ValueError("invalid transaction hash")
        raw = transport.request("eth_getTransactionReceipt", [tx_hash])
        mined = parse_receipt(raw, tx_hash)
        if mined is None:
            return None
        return (*mined, receipt_fees(raw))  # raw is a dict once parse_receipt accepted it

    return read
