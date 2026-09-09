"""Dual-provider JSON-RPC client with fail-closed agreement checks.

Two independent Base endpoints are required for every decision input: chain
id, block, wallet nonce/balance, token code hash, quotes and simulations.
Disagreement raises instead of returning a permissive default.
"""

import json
import ssl
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

import certifi
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
    return to_checksum_address(address)[2:].rjust(64, "0")


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


class JsonRpc:
    def __init__(self, primary_url: str, secondary_url: str, config: ExecutionConfig):
        if not primary_url or not secondary_url:
            raise ValueError("both provider URLs are required")
        self.primary_url = primary_url
        self.secondary_url = secondary_url
        self.config = config
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=certifi.where()))
        )

    def _post(self, url: str, method: str, params: list[Any]) -> Any:
        last: Exception | None = None
        for attempt in range(self.config.rpc_retries):
            try:
                payload = json.dumps(
                    {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
                ).encode()
                req = urllib.request.Request(
                    url,
                    data=payload,
                    headers={"Content-Type": "application/json", "User-Agent": UA},
                    method="POST",
                )
                with self._opener.open(req, timeout=self.config.rpc_timeout_ms / 1000) as resp:
                    data = json.loads(resp.read())
                if data.get("error"):
                    # A node-level rejection (revert, bad params, nonce error)
                    # is a definitive answer; retrying cannot change it and
                    # only delays the next cycle.
                    raise RpcError(str(data["error"]))
                return data["result"]
            except RpcError:
                raise
            except Exception as exc:  # noqa: BLE001
                last = exc
                time.sleep(0.3 * (attempt + 1))
        raise RpcError(f"{method} failed on {_redact_url(url)}: {last}") from last

    def call(self, method: str, params: list[Any], provider: str = "primary") -> Any:
        url = self.primary_url if provider == "primary" else self.secondary_url
        return self._post(url, method, params)

    def dual(self, method: str, params: list[Any]) -> DualValue:
        return DualValue(
            self._post(self.primary_url, method, params),
            self._post(self.secondary_url, method, params),
        )

    # --- typed helpers -------------------------------------------------

    def chain_id(self) -> int:
        return int(self.dual("eth_chainId", []).require_agreement("chainId"), 16)

    def block_number(self, provider: str = "primary") -> int:
        return int(self.call("eth_blockNumber", [], provider), 16)

    def dual_block_number(self) -> tuple[int, int]:
        p = self.block_number("primary")
        s = self.block_number("secondary")
        if abs(p - s) > 1:
            raise ProviderDisagreement(f"block divergence {p} != {s}")
        return p, s

    def gas_price(self) -> int:
        return int(self.dual("eth_gasPrice", []).require_agreement("gasPrice"), 16)

    def get_balance(self, address: str, block: str = "latest", provider: str = "primary") -> int:
        return int(self.call("eth_getBalance", [address, block], provider), 16)

    def get_transaction_count(
        self, address: str, block: str = "latest", provider: str = "primary"
    ) -> int:
        return int(self.call("eth_getTransactionCount", [address, block], provider), 16)

    def get_code(self, address: str, block: str = "latest", provider: str = "primary") -> str:
        return str(self.call("eth_getCode", [address, block], provider))

    def eth_call(self, to: str, data: str, block: str = "latest", provider: str = "primary") -> str:
        return str(self.call("eth_call", [{"to": to, "data": data}, block], provider))

    def dual_eth_call(self, to: str, data: str, block: str = "latest") -> DualValue:
        return self.dual("eth_call", [{"to": to, "data": data}, block])

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
        return self.eth_call(payload["to"], data, block, provider)

    def wait_receipt(self, tx_hash: str, timeout_s: float | None = None) -> dict[str, Any]:
        deadline = time.monotonic() + (
            timeout_s if timeout_s is not None else self.config.receipt_timeout_ms / 1000
        )
        while time.monotonic() < deadline:
            for provider in ("primary", "secondary"):
                try:
                    receipt = self.get_transaction_receipt(tx_hash, provider)
                    if receipt is not None:
                        return receipt
                except Exception:  # noqa: BLE001
                    continue
            time.sleep(1.5)
        raise TimeoutError(f"receipt timeout for {tx_hash}")
