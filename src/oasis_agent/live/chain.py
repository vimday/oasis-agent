"""Minimal JSON-RPC client and EIP-1559 send path for the live LP loop.

Signing happens by digest in the isolated signer daemon. A transaction that was
signed but whose outcome cannot be established raises `SubmissionUnknown`; the
caller must latch and never retry it automatically.
"""

import json
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

import certifi
from eth_abi.abi import decode, encode
from eth_account import Account
from eth_hash.auto import keccak
from rlp import encode as rlp_encode  # type: ignore[import-untyped]

from ..models.common import normalize_address
from .intent import SubmittedTransaction

USER_AGENT = "oasis-agent/0.1"
SECP256K1_HALF = 0x7FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF5D576E7357A4501DDFE92F46681B20A0


class ChainError(RuntimeError):
    """Nothing was signed, or a read failed: safe to retry on a later poll."""


class RpcRejected(ChainError):
    """The node answered with a JSON-RPC error."""


class TxReverted(RuntimeError):
    """A signed transaction was mined and reverted: count it, never resend it."""

    def __init__(self, message: str, receipt: dict[str, Any]) -> None:
        super().__init__(message)
        self.receipt = receipt


class SubmissionUnknown(RuntimeError):
    """A signed transaction may or may not be on chain: latch, do not retry."""


def selector(signature: str) -> bytes:
    return keccak(signature.encode())[:4]


def calldata(signature: str, types: list[str], values: list[Any]) -> bytes:
    return selector(signature) + encode(types, values)


class Rpc:
    """JSON-RPC over HTTPS with bounded retries for reads only."""

    def __init__(self, url: str, *, timeout: float = 20.0, retries: int = 3) -> None:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("RPC requires an HTTP(S) endpoint")
        self._url, self._timeout, self._retries = url, timeout, retries
        self._context = ssl.create_default_context(cafile=certifi.where())
        self._id = 0

    def _post(self, method: str, params: list[Any]) -> Any:
        self._id += 1
        body = json.dumps(
            {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params}
        ).encode()
        request = urllib.request.Request(
            self._url,
            body,
            {"Content-Type": "application/json", "User-Agent": USER_AGENT},
        )
        with urllib.request.urlopen(  # nosec B310
            request, timeout=self._timeout, context=self._context
        ) as response:
            data = json.loads(response.read())
        if not isinstance(data, dict):
            raise ChainError(f"{method}: malformed response")
        if "error" in data:
            raise RpcRejected(f"{method}: {str(data['error'])[:200]}")
        return data.get("result")

    def call(self, method: str, params: list[Any]) -> Any:
        last: Exception | None = None
        for attempt in range(self._retries):
            try:
                return self._post(method, params)
            except RpcRejected:
                raise
            except (OSError, ValueError, urllib.error.URLError) as exc:
                last = exc
                time.sleep(0.5 * (attempt + 1))
        raise ChainError(f"{method}: {type(last).__name__}")

    def send_raw(self, raw: bytes) -> Any:
        """Exactly one attempt: a lost response must not trigger a resend."""
        return self._post("eth_sendRawTransaction", ["0x" + raw.hex()])

    def eth_call(self, to: str, data: bytes, block: str | dict[str, Any] = "latest") -> bytes:
        result = self.call("eth_call", [{"to": to, "data": "0x" + data.hex()}, block])
        return bytes.fromhex(str(result)[2:])

    def call_abi(
        self,
        to: str,
        signature: str,
        types: list[str],
        values: list[Any],
        returns: list[str],
        block: str | dict[str, Any] = "latest",
    ) -> tuple[Any, ...]:
        data = self.eth_call(to, calldata(signature, types, values), block)
        return tuple(decode(returns, data))

    def uint(self, method: str, params: list[Any]) -> int:
        return int(str(self.call(method, params)), 16)

    def balance(self, account: str) -> int:
        return self.uint("eth_getBalance", [account, "latest"])

    def erc20_balance(self, token: str, account: str) -> int:
        return int(
            self.call_abi(token, "balanceOf(address)", ["address"], [account], ["uint256"])[0]
        )

    def nonce(self, account: str, block: str = "pending") -> int:
        return self.uint("eth_getTransactionCount", [account, block])

    def receipt(self, tx_hash: str) -> dict[str, Any] | None:
        value = self.call("eth_getTransactionReceipt", [tx_hash])
        return value if isinstance(value, dict) else None

    def block(self) -> dict[str, Any]:
        value = self.call("eth_getBlockByNumber", ["latest", False])
        if not isinstance(value, dict):
            raise ChainError("latest block missing")
        return value


class DigestSigner(Protocol):
    account: str

    def sign(self, digest: bytes) -> tuple[int, int, int]: ...


@runtime_checkable
class TransactionSigner(Protocol):
    account: str

    def sign_envelope(self, fields: list[object], context: dict[str, Any]) -> bytes: ...


@dataclass(frozen=True, slots=True)
class FeePolicy:
    max_fee_per_gas_cap: int  # wei, hard ceiling
    priority_fee: int  # wei
    gas_buffer_bps: int = 3000
    max_gas: int = 1_500_000


def _envelope(
    chain_id: int,
    nonce: int,
    priority: int,
    max_fee: int,
    gas: int,
    to: str,
    value: int,
    data: bytes,
) -> list[object]:
    return [chain_id, nonce, priority, max_fee, gas, bytes.fromhex(to[2:]), value, data, []]


def sign_transaction(signer: DigestSigner, fields: list[object]) -> bytes:
    digest = keccak(b"\x02" + rlp_encode(fields))
    try:
        r, s, parity = signer.sign(digest)
    except (OSError, RuntimeError, ValueError) as exc:
        # Nothing was broadcast; a signature the caller never received is inert.
        raise ChainError("signing authority unavailable") from exc
    if not (0 < s <= SECP256K1_HALF) or parity not in (0, 1):
        raise ChainError("signer returned a non-canonical signature")
    raw = b"\x02" + bytes(rlp_encode([*fields, parity, r, s]))
    if normalize_address(str(Account.recover_transaction(raw))) != normalize_address(
        signer.account
    ):
        raise ChainError("signature does not recover to the configured account")
    return raw


@dataclass(frozen=True, slots=True)
class Sent:
    tx_hash: str
    nonce: int
    receipt: dict[str, Any]


def send_transaction(
    rpc: Rpc,
    signer: DigestSigner | TransactionSigner,
    *,
    chain_id: int,
    to: str,
    data: bytes,
    value: int,
    fees: FeePolicy,
    on_signed: Callable[[SubmittedTransaction], None],
    receipt_timeout: float = 120.0,
    authority_context: dict[str, Any] | None = None,
) -> Sent:
    """Simulate, sign, broadcast once and wait for the receipt.

    `on_signed(commitment)` runs before broadcast with fields recovered from
    the actual signed bytes. It must persist pending or raise before submission.
    """
    account = normalize_address(signer.account)
    to = normalize_address(to)
    tx = {"from": account, "to": to, "data": "0x" + data.hex(), "value": hex(value)}
    try:
        estimate = rpc.uint("eth_estimateGas", [tx])
    except RpcRejected as exc:
        raise ChainError(f"simulation reverted: {exc}") from exc
    gas = estimate * (10_000 + fees.gas_buffer_bps) // 10_000
    if gas > fees.max_gas:
        raise ChainError(f"gas {gas} exceeds cap {fees.max_gas}")
    base_fee = int(str(rpc.block()["baseFeePerGas"]), 16)
    max_fee = min(fees.max_fee_per_gas_cap, 2 * base_fee + fees.priority_fee)
    if max_fee <= base_fee:
        raise ChainError("base fee is above the configured gas price cap")
    nonce = rpc.nonce(account, "pending")
    if nonce != rpc.nonce(account, "latest"):
        raise ChainError("account has an unmined transaction in the mempool")
    fields = _envelope(chain_id, nonce, fees.priority_fee, max_fee, gas, to, value, data)
    if isinstance(signer, TransactionSigner):
        if authority_context is None:
            raise ChainError("transaction authority requires decision context")
        try:
            raw = signer.sign_envelope(fields, authority_context)
        except (OSError, RuntimeError, ValueError):
            raise ChainError("transaction authority outcome unresolved") from None
    else:
        if authority_context is not None:
            raise ChainError("digest signer cannot authorize a live action")
        raw = sign_transaction(signer, fields)
    commitment = SubmittedTransaction.from_signed(raw)
    tx_hash = commitment.transaction_hash
    on_signed(commitment)
    try:
        rpc.send_raw(raw)
    except RpcRejected as exc:
        # The node refused the bytes; confirm it is not on chain anyway.
        if rpc.receipt(tx_hash) is None and rpc.nonce(account, "latest") == nonce:
            raise SubmissionUnknown(f"broadcast rejected: {exc}") from exc
    except (OSError, ValueError, urllib.error.URLError) as exc:
        raise SubmissionUnknown(f"broadcast outcome unknown: {type(exc).__name__}") from exc
    deadline = time.monotonic() + receipt_timeout
    while time.monotonic() < deadline:
        try:
            receipt = rpc.receipt(tx_hash)
        except ChainError:
            receipt = None
        if receipt is not None:
            if int(str(receipt["status"]), 16) != 1:
                raise TxReverted(f"transaction {tx_hash} reverted", receipt)
            return Sent(tx_hash, nonce, receipt)
        time.sleep(1.0)
    raise SubmissionUnknown(f"no receipt for {tx_hash} within {receipt_timeout:.0f}s")


__all__ = [
    "ChainError",
    "DigestSigner",
    "FeePolicy",
    "Rpc",
    "RpcRejected",
    "Sent",
    "SubmissionUnknown",
    "TxReverted",
    "calldata",
    "selector",
    "send_transaction",
    "sign_transaction",
]
