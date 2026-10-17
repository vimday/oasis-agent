"""Direct live reads share the retained-acquisition quota, without automatic retries."""

from pathlib import Path
from typing import Any

from ..errors import RpcError
from ..rpc.budget import BudgetedPoster, RpcBudget
from ..rpc.transport import JsonRpcTransport, _UrlLibPoster
from .chain import ChainError, Rpc
from .rpc_profile import LiveRpcProfile


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
    ) -> None:
        super().__init__(url, retries=1)
        policies = profile.policies(("live_primary", "live_secondary"))
        if source not in {p.source for p in policies}:
            raise ValueError("unknown live RPC source")
        path = directory / "live.receipt-budget.db"
        with RpcBudget(path, policies):
            pass
        self.reads = JsonRpcTransport(
            "https://unused.invalid",
            http=BudgetedPoster(_UrlLibPoster(url, 10), path, policies, source),
            retries=0,
        )

    def call(self, method: str, params: list[Any]) -> Any:
        try:
            return self.reads.request(method, params)
        except (RpcError, ValueError, OSError):
            raise ChainError("budgeted live RPC read refused; details redacted") from None
