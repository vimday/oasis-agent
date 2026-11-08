"""Live spot execution through the OKX Agentic Wallet (`onchainos swap execute`).

Nothing here runs unless an operator constructs a `LiveGate`: the approved wallet, a hard
per-trade USD cap, a bounded slippage and an exact confirmation phrase. The wallet's own
TEE signer signs and broadcasts; this process never holds a key.

A swap is not idempotent, so it is submitted exactly once and never retried. The fill is
derived from the wallet's own balance deltas plus a chain receipt with status 1, never from
the CLI's claims. The intent is journaled before submission; any outcome that cannot be
proven (no receipt, reverted, balances that did not move as expected) is journaled
`live_unresolved` and raises, which halts the agent for human review.
"""

import json
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from ..evidence.rpc_validation import rpc_quantity
from ..models.common import normalize_address
from .onchainos_source import BASE_USDC, Runner, SourceFailure
from .receipt_fee_contract import FEE_SOURCE, execution_fee_in_bounds, l1_fee_in_bounds

APPROVED_WALLET = "0x95a91b0afbc5fb3dbbffa1be8aaff5c14772a900"
CONFIRMATION = "LIVE SPOT ON BASE FROM {wallet}"
USD = 1_000_000
# The wallet's own policy rejects single transactions above USD 3000.
MAX_TRADE_USD_MICROS = 3_000 * USD
MIN_SLIPPAGE_BPS, MAX_SLIPPAGE_BPS = 10, 300
_HASH = re.compile(r"^0x[0-9a-f]{64}$")
DEFAULT_SETTLE_ATTEMPTS = 6


def swap_call_bound(settle_attempts: int = DEFAULT_SETTLE_ATTEMPTS) -> int:
    """Worst-case logical calls of one swap: two balances, submit, settle balance reads."""
    return 3 + 2 * settle_attempts


Side = Literal["buy", "sell"]
Emit = Callable[[str, dict[str, Any]], object]
# (status, block_number, sender) once mined, None while unavailable. Raises on RPC failure.
# A reader may append a fourth element, the fee items of that same receipt (`receipt_fees`);
# a three-element reader (older injection) leaves the fees `not_captured`.
Mined = tuple[int, int, str] | tuple[int, int, str, dict[str, Any]]
ReceiptReader = Callable[[str], Mined | None]


class LiveRefused(Exception):
    """Refused before anything was submitted; no funds moved."""

    def __init__(self, category: str):
        super().__init__(f"live order refused: {category}")
        self.category = category


class LiveUnresolved(Exception):
    """Submitted (or possibly submitted) without a proven fill; requires human review."""

    def __init__(self, category: str):
        super().__init__(f"live order unresolved: {category}")
        self.category = category


@dataclass(frozen=True, slots=True)
class LiveGate:
    wallet: str
    max_trade_usd_micros: int
    slippage_bps: int
    confirmation: str

    def __post_init__(self) -> None:
        wallet = normalize_address(self.wallet)
        object.__setattr__(self, "wallet", wallet)
        if wallet != APPROVED_WALLET:
            raise ValueError("wallet is not the owner-approved Agentic Wallet")
        if not USD <= self.max_trade_usd_micros <= MAX_TRADE_USD_MICROS:
            raise ValueError("per-trade cap must be within USD 1..3000")
        if not MIN_SLIPPAGE_BPS <= self.slippage_bps <= MAX_SLIPPAGE_BPS:
            raise ValueError("slippage must be within 10..300 bps")
        if self.confirmation != CONFIRMATION.format(wallet=wallet):
            raise ValueError("live confirmation phrase does not match")

    def describe(self) -> dict[str, Any]:
        return {
            "wallet": self.wallet,
            "max_trade_usd_micros": self.max_trade_usd_micros,
            "slippage_bps": self.slippage_bps,
        }


@dataclass(frozen=True, slots=True)
class Fill:
    side: Side
    token: str
    tx_hash: str
    block_number: int
    usdc_delta: int  # signed USDC base units observed in the wallet
    token_delta: int  # signed token base units observed in the wallet
    intent_id: str = ""  # the journaled live_intent this fill answers
    # Fee items read from the same receipt that proved this fill (see `receipt_fees`);
    # None when the reader did not capture them. Evidence only: never booked as cash.
    receipt_fees: dict[str, Any] | None = None

    def record(self) -> dict[str, Any]:
        return {
            # What this fill proves: the CLI-reported tx mined with status 1 from the gated
            # wallet, and the wallet's balances moved as expected. The two balance reads are
            # not block-pinned and are not decoded from the receipt's Transfer logs, so other
            # wallet activity in between could be misattributed. It is not verified settlement.
            "settlement": "unverified_balance_delta_plus_sender_receipt",
            "side": self.side,
            "token": self.token,
            "tx_hash": self.tx_hash,
            "block_number": self.block_number,
            "usdc_delta": str(self.usdc_delta),
            "token_delta": str(self.token_delta),
            "intent_id": self.intent_id,
            "receipt_fees": (
                self.receipt_fees if self.receipt_fees is not None else dict(NOT_CAPTURED_FEES)
            ),
        }


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


def balance_argv(token: str) -> tuple[str, ...]:
    # `--force` here only bypasses the wallet's balance cache, so deltas are not stale.
    address = normalize_address(token)
    return ("wallet", "balance", "--chain", "base", "--token-address", address, "--force")


def parse_balance(raw: bytes, token: str) -> int:
    """Raw base-unit balance of one token; a missing row is zero, any ambiguity refuses."""
    token = normalize_address(token)
    try:
        root = json.loads(raw, parse_float=str, parse_int=str)
    except (ValueError, RecursionError) as error:
        raise SourceFailure("invalid_json") from error
    if not isinstance(root, dict) or root.get("ok") is not True:
        raise SourceFailure("provider_error")
    details = root.get("data", {}).get("details") if isinstance(root.get("data"), dict) else None
    rows: list[Any] = []
    for detail in details if isinstance(details, list) else [None]:
        if not isinstance(detail, dict) or not isinstance(detail.get("tokenAssets"), list):
            raise SourceFailure("balance_shape")
        rows.extend(detail["tokenAssets"])
    # The query names one token, so every row must be that token on Base, at most once.
    if len(rows) > 1 or any(
        not isinstance(r, dict) or str(r.get("tokenAddress", "")).lower() != token for r in rows
    ):
        raise SourceFailure("balance_identity")
    if not rows:
        return 0
    if rows[0].get("chainIndex") != "8453":
        raise SourceFailure("balance_chain")
    value = rows[0].get("rawBalance")
    if not isinstance(value, str) or not value.isdigit() or len(value) > 78:
        raise SourceFailure("balance_value")
    return int(value)


def parse_tx_hash(raw: bytes) -> str | None:
    """The swap transaction hash the CLI claims; None when it states no broadcast."""
    try:
        root = json.loads(raw, parse_float=str, parse_int=str)
    except (ValueError, RecursionError):
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


def parse_receipt(raw: object, tx_hash: str) -> tuple[int, int, str] | None:
    """(status, block, sender) from ONE raw `eth_getTransactionReceipt` result.

    All three fields come from the same response so they cannot describe different
    transactions; None means not yet mined. Any malformed field raises.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("receipt is not an object")
    if str(raw.get("transactionHash", "")).lower() != tx_hash.lower():
        raise ValueError("receipt is for a different transaction")
    status, block, sender = raw.get("status"), raw.get("blockNumber"), raw.get("from")
    if status not in ("0x0", "0x1"):
        raise ValueError("receipt status is not 0x0/0x1")
    if not isinstance(block, str) or not re.fullmatch(r"0x(0|[1-9a-f][0-9a-f]*)", block):
        raise ValueError("receipt block number is not a canonical quantity")
    if not isinstance(sender, str):
        raise ValueError("receipt has no sender")
    return int(status, 16), int(block, 16), normalize_address(sender)


# The reader returned no fee items (an older three-element injection or log).
NOT_CAPTURED_FEES: dict[str, Any] = {"status": "not_captured"}


def _fee_quantity(raw: dict[str, Any], field: str) -> int | None:
    """A canonical hex quantity, or None; `missing` and `invalid` are told apart by caller."""
    try:
        return rpc_quantity(raw[field])
    except (KeyError, ValueError):
        return None


def receipt_fees(raw: dict[str, Any]) -> dict[str, Any]:
    """Native-fee items stated by the same receipt object that `parse_receipt` accepted.

    Never raises: a missing or malformed item is recorded as such and can never turn a
    proven fill into an unresolved one or cause a resubmission.
    - execution: gasUsed x effectiveGasPrice in wei (the L2 execution fee only);
    - l1_data: the receipt's own `l1Fee` (OP-stack L1 data fee), as stated, in wei.
    The OP operator fee is not in the receipt and is not captured, so no total native
    debit is derived here, and nothing is converted to USD.
    """

    def item(fields: tuple[str, ...]) -> tuple[dict[str, Any], list[int]]:
        absent = [f for f in fields if f not in raw]
        if absent:
            return {"status": "missing", "fields": absent}, []
        values = [_fee_quantity(raw, f) for f in fields]
        if any(v is None for v in values):
            bad = [f for f, v in zip(fields, values, strict=True) if v is None]
            return {"status": "invalid", "fields": bad}, []
        return {"status": "observed"}, [v for v in values if v is not None]

    execution, values = item(("gasUsed", "effectiveGasPrice"))
    if values:
        gas, price = values
        fee = gas * price
        if not execution_fee_in_bounds(gas, price, fee):
            execution = {"status": "invalid", "fields": ["gasUsed", "effectiveGasPrice"]}
        else:
            execution |= {
                "gas_used": str(gas),
                "effective_gas_price_wei": str(price),
                "fee_wei": str(fee),
            }
    l1, values = item(("l1Fee",))
    if values:
        if not l1_fee_in_bounds(values[0]):
            l1 = {"status": "invalid", "fields": ["l1Fee"]}
        else:
            l1 |= {"fee_wei": str(values[0])}
    # The block this same receipt names, so a later cost proof can be bound to that exact
    # block (a height alone is not a block). Optional: missing or malformed only makes
    # the binding unavailable; it never touches the fill or its gates.
    block_hash = raw.get("blockHash")
    if "blockHash" not in raw:
        binding: dict[str, Any] = {"status": "missing"}
    elif isinstance(block_hash, str) and _HASH.fullmatch(block_hash.lower()):
        binding = {"status": "observed", "block_hash": block_hash.lower()}
    else:
        binding = {"status": "invalid"}
    return {
        "status": "captured",
        "source": FEE_SOURCE,
        "receipt_block": binding,
        "execution": execution,
        "l1_data": l1,
        "operator": {"status": "not_captured"},
        "total_native_wei": "not_derived",
    }


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


def parse_active_base_address(raw: bytes) -> str:
    """The CLI's active-account Base EVM address; balances and swaps act on this account."""
    try:
        root = json.loads(raw, parse_float=str, parse_int=str)
    except (ValueError, RecursionError) as error:
        raise SourceFailure("invalid_json") from error
    data = root.get("data") if isinstance(root, dict) and root.get("ok") is True else None
    rows = data.get("evm") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise SourceFailure("addresses_shape")
    base = {
        str(r.get("address", "")).lower()
        for r in rows
        if isinstance(r, dict) and r.get("chainIndex") == "8453"
    }
    if len(base) != 1:
        raise SourceFailure("addresses_identity")
    try:
        return normalize_address(base.pop())
    except ValueError as error:
        raise SourceFailure("addresses_identity") from error


# Native ETH for gas. The unfunded wallet showed no row, so the native row's address is
# unobserved: accept the two conventional sentinels; anything else reads as zero (refuses).
NATIVE_SENTINELS = frozenset({"", "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"})
MIN_GAS_WEI = 10**14  # 0.0001 ETH, a few Base swaps at normal fees


def parse_native_balance(raw: bytes) -> int:
    try:
        root = json.loads(raw, parse_float=str, parse_int=str)
    except (ValueError, RecursionError) as error:
        raise SourceFailure("invalid_json") from error
    if not isinstance(root, dict) or root.get("ok") is not True:
        raise SourceFailure("provider_error")
    details = root.get("data", {}).get("details") if isinstance(root.get("data"), dict) else None
    if not isinstance(details, list):
        raise SourceFailure("balance_shape")
    native = [
        r
        for d in details
        if isinstance(d, dict) and isinstance(d.get("tokenAssets"), list)
        for r in d["tokenAssets"]
        if isinstance(r, dict)
        and r.get("chainIndex") == "8453"
        and str(r.get("tokenAddress", "x")).lower() in NATIVE_SENTINELS
    ]
    if len(native) > 1:
        raise SourceFailure("balance_identity")
    if not native:
        return 0
    value = native[0].get("rawBalance")
    if not isinstance(value, str) or not value.isdigit() or len(value) > 78:
        raise SourceFailure("balance_value")
    return int(value)


def preflight(gate: LiveGate, read: Runner) -> dict[str, Any]:
    """Read-only checks before a live run; raises LiveRefused, never submits anything.

    The active CLI account must be the gated wallet (balances and swaps act on it), and
    the wallet must hold USDC to trade and native ETH for gas.
    """
    try:
        active = parse_active_base_address(read(("wallet", "addresses", "--chain", "base")))
        usdc = parse_balance(read(balance_argv(BASE_USDC)), BASE_USDC)
        gas = parse_native_balance(read(("wallet", "balance", "--chain", "base", "--force")))
    except SourceFailure as error:
        raise LiveRefused("preflight_" + error.category) from error
    if active != gate.wallet:
        raise LiveRefused("active_account_not_gated_wallet")
    if usdc < USD:
        raise LiveRefused("wallet_usdc_below_1_usd")
    if gas < MIN_GAS_WEI:
        raise LiveRefused("wallet_gas_below_minimum")
    return {"wallet": active, "usdc_base_units": str(usdc), "native_wei": str(gas)}
