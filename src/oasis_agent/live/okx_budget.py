"""Explicit, retained zero-cost OKX price admission; never creates a quota.

The operator supplies pricing/authorization evidence digests. This validates the
declared scope, not the provider's remaining monthly allowance. No payment or
paid fallback is supported, and the original ledger must already exist.
"""

import os
import stat
from dataclasses import asdict, dataclass
from pathlib import Path

from ..evidence.rpc_validation import strict_digest, strict_json_object
from ..models.common import canonical_json, normalize_address, sha256_hex
from ..rpc.budget import BudgetPolicy, RpcBudget

PRICE_ENDPOINT = "https://web3.okx.com/api/v6/dex/market/price"


@dataclass(frozen=True, slots=True)
class OkxPriceReadGrant:
    schema: str
    endpoint: str
    chain_id: int
    currencies: tuple[str, str]
    api_key_hash: str
    valid_from_ms: int
    valid_until_ms: int
    max_requests: int
    min_interval_ms: int
    billing_mode: str
    free_requests_remaining: int
    pricing_evidence_hash: str
    authorization_evidence_hash: str

    def __post_init__(self) -> None:
        if (
            self.schema != "oasis.okx-price-free-read-grant.v1"
            or self.endpoint != PRICE_ENDPOINT
            or self.billing_mode != "free_quota_only"
        ):
            raise ValueError("explicit free-only OKX price grant required; unknown pricing refused")
        for field in ("api_key_hash", "pricing_evidence_hash", "authorization_evidence_hash"):
            digest = strict_digest(getattr(self, field), field)
            if digest == "0" * 64:
                raise ValueError("non-placeholder evidence digest required")
        if type(self.chain_id) is not int or not 0 < self.chain_id < 2**64:
            raise ValueError("explicit EVM chain required")
        if (
            not isinstance(self.currencies, tuple)
            or len(self.currencies) != 2
            or len(set(self.currencies)) != 2
            or any(normalize_address(c) != c for c in self.currencies)
        ):
            raise ValueError("two canonical pool currencies required")
        if any(
            type(v) is not int
            for v in (
                self.valid_from_ms,
                self.valid_until_ms,
                self.max_requests,
                self.min_interval_ms,
                self.free_requests_remaining,
            )
        ):
            raise ValueError("integer grant bounds required")
        if (
            not 0 <= self.valid_from_ms < self.valid_until_ms
            or self.valid_until_ms - self.valid_from_ms > 86_400_000
            or not 1 <= self.max_requests <= 4096
            or self.free_requests_remaining < self.max_requests
            or not 100 <= self.min_interval_ms <= 60_000
        ):
            raise ValueError("bounded time, request quota and documented free allowance required")

    @classmethod
    def load(cls, path: Path) -> "OkxPriceReadGrant":
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ValueError("regular OKX grant file required")
            with os.fdopen(descriptor, "rb", closefd=False) as handle:
                raw = handle.read(16_385)
        finally:
            os.close(descriptor)
        if len(raw) > 16_384:
            raise ValueError("OKX grant too large")
        values = strict_json_object(raw)
        if set(values) != set(cls.__dataclass_fields__) or not isinstance(
            values["currencies"], list
        ):
            raise ValueError("complete exact OKX grant fields required")
        return cls(**(values | {"currencies": tuple(values["currencies"])}))  # type: ignore[arg-type]

    def policies(self) -> tuple[BudgetPolicy, ...]:
        # Bind *all* grant terms, including identity, pricing and expiry, into the
        # durable policy. Changing a grant cannot reset or expand this ledger.
        source = sha256_hex(canonical_json(asdict(self)))
        return (BudgetPolicy(source, self.max_requests, self.min_interval_ms),)

    def require_scope(
        self, api_key: str, chain_id: int, currencies: tuple[str, ...], now_ms: int
    ) -> None:
        if (
            sha256_hex(api_key.encode()) != self.api_key_hash
            or type(chain_id) is not int
            or chain_id != self.chain_id
            or not currencies
            or len(currencies) > 2
            or len(set(currencies)) != len(currencies)
            or not set(currencies).issubset(self.currencies)
            or type(now_ms) is not int
            or not self.valid_from_ms <= now_ms < self.valid_until_ms
        ):
            raise ValueError("OKX request is outside retained grant scope")


class OkxPriceReadBudget:
    def __init__(self, path: Path, grant: OkxPriceReadGrant) -> None:
        if path.absolute() != path.resolve() or not path.is_file():
            raise ValueError("original regular OKX budget ledger required")
        self.path, self.grant = path, grant
        with RpcBudget(path, grant.policies(), require_existing=True):
            pass

    def require_available(
        self, api_key: str, chain_id: int, currencies: tuple[str, ...], now_ms: int
    ) -> None:
        self.grant.require_scope(api_key, chain_id, currencies, now_ms)
        policies = self.grant.policies()
        with RpcBudget(self.path, policies, require_existing=True) as budget:
            if budget.counts()[policies[0].source] >= self.grant.max_requests:
                raise ValueError("OKX request budget exhausted")

    def reserve(
        self, api_key: str, chain_id: int, currencies: tuple[str, ...], now_ms: int
    ) -> None:
        self.grant.require_scope(api_key, chain_id, currencies, now_ms)
        policies = self.grant.policies()
        with RpcBudget(self.path, policies, require_existing=True) as budget:
            if budget.reserve(policies[0].source, now_ms=now_ms):
                raise ValueError("OKX pacing refused; no wait or retry")
