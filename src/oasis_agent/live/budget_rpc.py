"""Direct live reads share the retained-acquisition quota, without automatic retries."""

import sqlite3
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError

from ..errors import RpcError
from ..rpc.budget import BUDGETED_READ_METHODS, BudgetedPoster, RpcBudget
from ..rpc.transport import HttpPoster, JsonRpcTransport, _UrlLibPoster
from .chain import ChainError, Rpc
from .rpc_profile import LiveRpcProfile


class _TransportRefusal(RpcError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _DiagnosticPoster:
    def __init__(self, http: HttpPoster) -> None:
        self.http = http

    def post(self, payload: bytes) -> bytes:
        try:
            return self.http.post(payload)
        except HTTPError as error:
            code = error.code
            reason = f"http_{code}" if type(code) is int and 100 <= code <= 599 else "http_error"
            raise _TransportRefusal(reason) from None
        except URLError as error:
            reason = "timeout" if isinstance(error.reason, TimeoutError) else "transport"
            raise _TransportRefusal(reason) from None
        except TimeoutError:
            raise _TransportRefusal("timeout") from None
        except OSError:
            raise _TransportRefusal("transport") from None


class BudgetedRpc(Rpc):
    """Budget every read, including head/nonce/receipt polling.

    Broadcasting retains Rpc.send_raw's separate exactly-once path. It is not a
    read and is never retried or sent through the observation transport.
    """

    def __init__(
        self,
        url: str,
        directory: Path,
        source: str,
        profile: LiveRpcProfile,
        *,
        require_existing_budget: bool = False,
    ) -> None:
        super().__init__(url, retries=1)
        policies = profile.policies(("live_primary", "live_secondary"))
        if source not in {p.source for p in policies}:
            raise ValueError("unknown live RPC source")
        self.source = source
        path = directory / "live.receipt-budget.db"
        with RpcBudget(path, policies, require_existing=require_existing_budget):
            pass
        self.reads = JsonRpcTransport(
            "https://unused.invalid",
            http=BudgetedPoster(
                _DiagnosticPoster(_UrlLibPoster(url, 10)),
                path,
                policies,
                source,
                require_existing=require_existing_budget,
            ),
            retries=0,
        )

    def call(self, method: str, params: list[Any]) -> Any:
        try:
            return self.reads.request(method, params)
        except (RpcError, ValueError, OSError, sqlite3.Error) as error:
            safe_method = (
                method
                if type(method) is str and method in BUDGETED_READ_METHODS
                else "unknown_method"
            )
            reason = error.reason if isinstance(error, _TransportRefusal) else "request_or_response"
            raise ChainError(
                f"budgeted live RPC read refused: {self.source} {safe_method} {reason}; "
                "details redacted"
            ) from None
