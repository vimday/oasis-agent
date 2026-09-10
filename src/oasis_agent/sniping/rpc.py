"""Dual-provider JSON-RPC client with fail-closed agreement checks.

Two independent Base endpoints are required for every decision input: chain
id, block, wallet nonce/balance, token code hash, quotes and simulations.
Disagreement raises instead of returning a permissive default.
"""

import ssl
import time
import urllib.parse
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import Any

import certifi
import httpx
from eth_utils import keccak, to_checksum_address  # type: ignore[attr-defined]

from .config import ExecutionConfig

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
)


def _redact_url(url: str) -> str:
    """Strip credentials and path (API keys) from a URL before logging."""
    parts = urllib.parse.urlsplit(url)
    host = parts.netloc.rpartition("@")[-1]
    return f"{parts.scheme}://{host}"


def _selector(signature: str) -> str:
    return "0x" + keccak(signature.encode())[:4].hex()


def addr_arg(address: str) -> str:
    return to_checksum_address(address)[2:].lower().rjust(64, "0")


def uint_arg(value: int, width: int = 64) -> str:
    if type(value) is not int or value < 0:
        raise ValueError("non-negative integer argument required")
    return format(value, "x").rjust(width, "0")


@dataclass(frozen=True)
class DualValue:
    primary: Any
    secondary: Any

    def agree(self) -> bool:
        return bool(self.primary == self.secondary)

    def require_agreement(self, what: str) -> Any:
        if not self.agree():
            raise ProviderDisagreement(f"{what} disagreement: {self.primary} != {self.secondary}")
        return self.primary


class RpcError(RuntimeError):
    pass


class ProviderDisagreement(RuntimeError):
    pass


def rejection_summary(error: Any) -> str:
    """Retain diagnostic categories, never arbitrary provider text or calldata."""
    if not isinstance(error, dict):
        return "node_rejected code=unknown"
    message = str(error.get("message", "")).lower()
    code = error.get("code")
    category = "node_rejected"
    for terms, label in (
        (("rate limit", "too many requests", "compute units"), "rate_limited"),
        (("execution reverted", "revert"), "execution_reverted"),
        (("insufficient funds",), "insufficient_funds"),
        (("missing trie", "historical state", "header not found"), "state_unavailable"),
        (("timeout", "timed out"), "timeout"),
        (("not supported", "method not found"), "unsupported_method"),
        (("invalid param",), "invalid_params"),
    ):
        if any(term in message for term in terms):
            category = label
            break
    suffix = ""
    # Only retain known protocol reason literals; arbitrary revert strings can
    # include provider credentials or caller input.
    data = error.get("data")
    if isinstance(data, str) and data.startswith("0x08c379a0"):
        try:
            raw = bytes.fromhex(data[10:])
            if len(raw) >= 64 and int.from_bytes(raw[:32], "big") == 32:
                size = int.from_bytes(raw[32:64], "big")
                reason = raw[64 : 64 + size].decode("ascii")
                if size == len(reason) and reason in {"SPL", "LOK", "STF", "TF", "IIA", "AS"}:
                    suffix = f" revert={reason}"
        except (ValueError, UnicodeDecodeError):
            pass
    return f"{category} code={code if type(code) is int else 'unknown'}{suffix}"


class JsonRpc:
    def __init__(self, primary_url: str, secondary_url: str, config: ExecutionConfig):
        if not primary_url or not secondary_url or primary_url == secondary_url:
            raise ValueError("both provider URLs are required")
        self.primary_url = primary_url
        self.secondary_url = secondary_url
        self.config = config
        self.snapshot: dict[str, Any] | None = None
        self.snapshot_number: int | None = None
        self._workers = ThreadPoolExecutor(max_workers=2, thread_name_prefix="launch-rpc")
        self._client = httpx.Client(
            verify=ssl.create_default_context(cafile=certifi.where()),
            timeout=config.rpc_timeout_ms / 1000,
            headers={"User-Agent": UA},
        )

    def _post(self, url: str, method: str, params: list[Any]) -> Any:
        last: Exception = TimeoutError("RPC time budget exhausted")
        retries = 0 if method == "eth_sendRawTransaction" else self.config.rpc_retries
        deadline = time.monotonic() + self.config.rpc_timeout_ms / 1000
        for attempt in range(retries + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                resp = self._client.post(
                    url,
                    json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                    timeout=remaining,
                )
                resp.raise_for_status()
                data = resp.json()
                if data.get("error"):
                    # A node-level rejection (revert, bad params, nonce error)
                    # is a definitive answer; retrying cannot change it and
                    # only delays the next cycle.
                    provider = "primary" if url == self.primary_url else "secondary"
                    raise RpcError(f"RPC {provider} {method}: {rejection_summary(data['error'])}")
                if (
                    data.get("jsonrpc") != "2.0"
                    or type(data.get("id")) is not int
                    or data["id"] != 1
                ):
                    raise RpcError("invalid JSON-RPC envelope")
                return data["result"]
            except RpcError:
                raise
            except Exception as exc:  # noqa: BLE001
                last = exc
                if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in {
                    400,
                    401,
                    403,
                    404,
                    429,
                }:
                    break
                remaining = deadline - time.monotonic()
                if attempt < retries and remaining > 0:
                    time.sleep(min(0.3 * (attempt + 1), remaining))
        provider = "primary" if url == self.primary_url else "secondary"
        status = last.response.status_code if isinstance(last, httpx.HTTPStatusError) else None
        raise RpcError(
            f"RPC {provider} {method}: {type(last).__name__}"
            + (f" HTTP={status}" if status is not None else "")
        ) from None

    def call(self, method: str, params: list[Any], provider: str = "primary") -> Any:
        url = self.primary_url if provider == "primary" else self.secondary_url
        return self._post(url, method, params)

    def dual(self, method: str, params: list[Any]) -> DualValue:
        primary = self._workers.submit(self._post, self.primary_url, method, params)
        secondary = self._workers.submit(self._post, self.secondary_url, method, params)
        # Submit both before awaiting either; one slow source cannot delay the
        # start of the other. A failure never substitutes the remaining source.
        return DualValue(primary.result(), secondary.result())

    def close(self) -> None:
        self._workers.shutdown(wait=True, cancel_futures=True)
        self._client.close()

    def begin_snapshot(self) -> int:
        if self.chain_id() != self.config.chain_id:
            raise ProviderDisagreement("wrong chain")
        p, s = self.dual_block_number()
        number = min(p, s)
        header = self.dual("eth_getBlockByNumber", [hex(number), False]).require_agreement(
            "block header"
        )
        if not isinstance(header, dict) or int(header["number"], 16) != number:
            raise ProviderDisagreement("invalid block header")
        if not 0 <= int(time.time()) - int(header["timestamp"], 16) <= self.config.max_age_seconds:
            raise ProviderDisagreement("stale block")
        self.snapshot = {"blockHash": header["hash"], "requireCanonical": True}
        self.snapshot_number = number
        return number

    def _pinned(self, block: str) -> str | dict[str, Any]:
        snapshot = getattr(self, "snapshot", None)
        return snapshot if block == "latest" and snapshot is not None else block

    # --- typed helpers -------------------------------------------------

    def chain_id(self) -> int:
        return int(self.dual("eth_chainId", []).require_agreement("chainId"), 16)

    def block_number(self, provider: str = "primary") -> int:
        return int(self.call("eth_blockNumber", [], provider), 16)

    def dual_block_number(self) -> tuple[int, int]:
        heads = self.dual("eth_blockNumber", [])
        p = int(heads.primary, 16)
        s = int(heads.secondary, 16)
        if abs(p - s) > 1:
            raise ProviderDisagreement(f"block divergence {p} != {s}")
        return p, s

    def gas_price(self) -> int:
        return int(self.dual("eth_gasPrice", []).require_agreement("gasPrice"), 16)

    def get_balance(self, address: str, block: str = "latest", provider: str = "primary") -> int:
        return int(self.call("eth_getBalance", [address, self._pinned(block)], provider), 16)

    def get_transaction_count(
        self, address: str, block: str = "latest", provider: str = "primary"
    ) -> int:
        return int(self.call("eth_getTransactionCount", [address, block], provider), 16)

    def get_code(self, address: str, block: str = "latest", provider: str = "primary") -> str:
        return str(self.call("eth_getCode", [address, self._pinned(block)], provider))

    def eth_call(self, to: str, data: str, block: str = "latest", provider: str = "primary") -> str:
        return str(self.call("eth_call", [{"to": to, "data": data}, self._pinned(block)], provider))

    def dual_eth_call(self, to: str, data: str, block: str = "latest") -> DualValue:
        return self.dual("eth_call", [{"to": to, "data": data}, self._pinned(block)])

    def estimate_gas(self, tx: dict[str, Any], provider: str = "primary") -> int:
        return int(self.call("eth_estimateGas", [tx], provider), 16)

    def send_raw_transaction(self, raw: str, provider: str = "primary") -> str:
        return str(self.call("eth_sendRawTransaction", ["0x" + raw], provider))

    def get_transaction_receipt(
        self, tx_hash: str, provider: str = "primary"
    ) -> dict[str, Any] | None:
        result = self.call("eth_getTransactionReceipt", [tx_hash], provider)
        return result if result is not None else None

    def get_logs(self, params: dict[str, Any], provider: str = "primary") -> list[dict[str, Any]]:
        result = self.call("eth_getLogs", [params], provider)
        if not isinstance(result, list):
            raise RpcError("eth_getLogs returned a non-list")
        return result

    # --- simulation ------------------------------------------------------

    def simulate(
        self,
        *,
        from_address: str,
        to: str,
        data: str,
        value: int = 0,
        block: str = "latest",
        provider: str = "primary",
    ) -> str:
        """eth_call simulation of the exact broadcast payload. Returns raw output.

        A revert raises RpcError from the node; the caller treats that as a
        hard failure of the gate, never as permission to relax constraints.
        """
        payload = {
            "from": from_address,
            "to": to_checksum_address(to),
            "data": data,
            "value": hex(value),
        }
        return str(self.call("eth_call", [payload, self._pinned(block)], provider))

    def wait_receipt(self, tx_hash: str, timeout_s: float | None = None) -> dict[str, Any]:
        deadline = time.monotonic() + (
            timeout_s if timeout_s is not None else self.config.receipt_timeout_ms / 1000
        )
        pending = {
            self._workers.submit(self.get_transaction_receipt, tx_hash, provider): provider
            for provider in ("primary", "secondary")
        }
        try:
            while pending and time.monotonic() < deadline:
                completed, _ = wait(
                    pending,
                    timeout=max(0, deadline - time.monotonic()),
                    return_when=FIRST_COMPLETED,
                )
                retry = []
                for future in completed:
                    provider = pending.pop(future)
                    try:
                        receipt = future.result()
                        if receipt is not None:
                            return receipt
                    except Exception:  # noqa: BLE001
                        pass
                    retry.append(provider)
                if retry:
                    time.sleep(min(0.25, max(0, deadline - time.monotonic())))
                    for provider in retry:
                        if time.monotonic() < deadline:
                            pending[
                                self._workers.submit(
                                    self.get_transaction_receipt, tx_hash, provider
                                )
                            ] = provider
        finally:
            for future in pending:
                future.cancel()
        raise TimeoutError(f"receipt timeout for {tx_hash}")
