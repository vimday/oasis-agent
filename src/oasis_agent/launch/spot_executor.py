"""Spot diagnostics and historical fill types. This module has no transaction runner."""

import json
import re
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from ..evidence.rpc_validation import rpc_quantity
from ..models.common import normalize_address
from .onchainos_source import BASE_USDC, Runner, SourceFailure
from .receipt_fee_contract import FEE_SOURCE, execution_fee_in_bounds, l1_fee_in_bounds

APPROVED_WALLET = "0x95a91b0afbc5fb3dbbffa1be8aaff5c14772a900"
CONFIRMATION = "LIVE SPOT ON BASE FROM {wallet}"
USD = 1_000_000
# Diagnostics cannot declare a per-trade cap above the account's total capital ceiling.
MAX_TRADE_USD_MICROS = 500 * USD
MIN_SLIPPAGE_BPS, MAX_SLIPPAGE_BPS = 10, 300
_HASH = re.compile(r"^0x[0-9a-f]{64}$")
DEFAULT_SETTLE_ATTEMPTS = 6


def swap_call_bound(settle_attempts: int = DEFAULT_SETTLE_ATTEMPTS) -> int:
    """Historical v2 bound for reading old logical-call ledgers."""
    return 3 + 2 * settle_attempts


Side = Literal["buy", "sell"]


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
        if (
            type(self.max_trade_usd_micros) is not int
            or not USD <= self.max_trade_usd_micros <= MAX_TRADE_USD_MICROS
        ):
            raise ValueError("per-trade cap must be within USD 1..500")
        if (
            type(self.slippage_bps) is not int
            or not MIN_SLIPPAGE_BPS <= self.slippage_bps <= MAX_SLIPPAGE_BPS
        ):
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


class SpotExecutor(Protocol):
    """Strategy-side port. Production CLI constructs no implementation."""

    gate: LiveGate
    calls: int
    submissions: int

    def calls_per_swap(self) -> int: ...

    def buy(self, token: str, usdc_amount: int) -> Fill: ...

    def sell(self, token: str, units: int) -> Fill: ...


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
    except KeyError, ValueError:
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
