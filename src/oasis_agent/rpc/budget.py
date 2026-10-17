"""Durable, shared request budgets for read-only observation campaigns.

Reservations are spent before network I/O and never refunded, including failures.
The caller must reuse the same ledger across cycles/restarts; it must not silently
create a replacement ledger when a quota or policy lock rejects a request.
"""

import json
import re
import sqlite3
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from types import TracebackType
from typing import Self

from ..models.common import canonical_json, sha256_hex
from .transport import HttpPoster


@dataclass(frozen=True, slots=True)
class BudgetPolicy:
    source: str
    max_requests: int
    min_interval_ms: int

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", self.source):
            raise ValueError("safe RPC budget source alias required")
        if type(self.max_requests) is not int or not 1 <= self.max_requests <= 1_000_000:
            raise ValueError("RPC request cap must be 1..1000000")
        if type(self.min_interval_ms) is not int or not 0 <= self.min_interval_ms <= 60_000:
            raise ValueError("RPC interval must be 0..60000 integer milliseconds")


class RpcBudget:
    def __init__(self, path: Path, policies: tuple[BudgetPolicy, ...]):
        if not policies or len({item.source for item in policies}) != len(policies):
            raise ValueError("distinct RPC budget policies required")
        self.policies = {item.source: item for item in policies}
        payload = canonical_json(
            [asdict(item) for item in sorted(policies, key=lambda p: p.source)]
        )
        self.policy_hash = sha256_hex(payload)
        self.db = sqlite3.connect(path, isolation_level=None, timeout=10)
        try:
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.execute("BEGIN IMMEDIATE")
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS rpc_budget_policy "
                "(id INTEGER PRIMARY KEY CHECK(id=1), payload BLOB NOT NULL)"
            )
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS rpc_usage "
                "(seq INTEGER PRIMARY KEY, payload BLOB NOT NULL, digest TEXT NOT NULL)"
            )
            self.db.execute("INSERT OR IGNORE INTO rpc_budget_policy VALUES(1,?)", (payload,))
            if self.db.execute("SELECT payload FROM rpc_budget_policy WHERE id=1").fetchone() != (
                payload,
            ):
                raise ValueError("RPC budget policy changed; refusing reset or increased cap")
            self._read()
            self.db.commit()
        except Exception:
            self.db.rollback()
            self.db.close()
            raise

    def _read(self) -> tuple[dict[str, int], dict[str, int], int, str]:
        counts = dict.fromkeys(self.policies, 0)
        last: dict[str, int] = {}
        previous = "0" * 64
        sequence = 0
        for seq, raw, digest in self.db.execute(
            "SELECT seq,payload,digest FROM rpc_usage ORDER BY seq"
        ):
            item = json.loads(raw)
            if not isinstance(item, dict) or set(item) != {
                "seq",
                "source",
                "at_ms",
                "previous",
                "policy",
            }:
                raise ValueError("RPC usage record malformed")
            source, at = item["source"], item["at_ms"]
            if (
                seq != sequence + 1
                or item["seq"] != seq
                or canonical_json(item) != raw
                or sha256_hex(raw) != digest
                or item["previous"] != previous
                or item["policy"] != self.policy_hash
                or source not in self.policies
                or type(at) is not int
                or at < 0
            ):
                raise ValueError("RPC usage audit chain invalid")
            policy = self.policies[source]
            if source in last and at - last[source] < policy.min_interval_ms:
                raise ValueError("RPC usage clock or interval invalid")
            counts[source] += 1
            if counts[source] > policy.max_requests:
                raise ValueError("RPC usage exceeds frozen quota")
            last[source] = at
            sequence, previous = seq, digest
        return counts, last, sequence, previous

    def reserve(self, source: str, *, now_ms: int) -> int:
        """Return wait milliseconds, or spend one request and return zero.

        A positive result reserves nothing. Recheck after waiting; another worker
        may have spent the next slot. No caller may send without a zero result.
        """
        if type(now_ms) is not int or now_ms < 0 or source not in self.policies:
            raise ValueError("valid source and integer wall clock required")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            counts, last, seq, previous = self._read()
            policy = self.policies[source]
            if counts[source] >= policy.max_requests:
                raise ValueError("RPC request budget exhausted")
            if source in last and now_ms < last[source]:
                raise ValueError("RPC budget clock regressed")
            wait = max(0, last[source] + policy.min_interval_ms - now_ms) if source in last else 0
            if not wait:
                raw = canonical_json(
                    {
                        "seq": seq + 1,
                        "source": source,
                        "at_ms": now_ms,
                        "previous": previous,
                        "policy": self.policy_hash,
                    }
                )
                self.db.execute(
                    "INSERT INTO rpc_usage VALUES(?,?,?)", (seq + 1, raw, sha256_hex(raw))
                )
            self.db.commit()
            return wait
        except Exception:
            self.db.rollback()
            raise

    def counts(self) -> dict[str, int]:
        return self._read()[0]

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.db.close()


class BudgetedPoster:
    """Observation-only HTTP wrapper; request quota covers underlying failures too."""

    def __init__(
        self,
        http: HttpPoster,
        path: Path,
        policies: tuple[BudgetPolicy, ...],
        source: str,
        *,
        clock: Callable[[], int] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.http, self.path, self.policies, self.source = http, path, policies, source
        self.clock = clock or (lambda: time.time_ns() // 1_000_000)
        self.sleep = sleep

    def post(self, payload: bytes) -> bytes:
        request = json.loads(payload)
        if not isinstance(request, dict) or request.get("method") not in {
            "eth_chainId",
            "eth_blockNumber",
            "eth_getBlockByNumber",
            "eth_getCode",
            "eth_getBalance",
            "eth_getTransactionCount",
            "eth_getStorageAt",
            "debug_traceTransaction",
            "eth_getLogs",
            "eth_getTransactionReceipt",
            "eth_call",
            "eth_estimateGas",
        }:
            raise ValueError("budgeted observation method not allowed")
        if request["method"] == "debug_traceTransaction":
            params = request.get("params")
            if (
                not isinstance(params, list)
                or len(params) != 2
                or not isinstance(params[0], str)
                or re.fullmatch(r"0x[0-9a-f]{64}", params[0]) is None
                or params[1]
                != {"tracer": "callTracer", "timeout": "5s", "tracerConfig": {"onlyTopCall": False}}
            ):
                raise ValueError("only bounded complete callTracer observation is allowed")
        while True:
            with RpcBudget(self.path, self.policies) as budget:
                wait = budget.reserve(self.source, now_ms=self.clock())
            if not wait:
                return self.http.post(payload)
            self.sleep(wait / 1000)
