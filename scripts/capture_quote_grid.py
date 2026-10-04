"""Capture one frozen historical quote/spot grid with durable public-RPC budgets.

Failures retain exact attempts and consume quota. A finished or refused scope
cannot silently restart, replace its ledger, or acquire a different block.
"""

import argparse
import fcntl
import json
import os
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from oasis_agent.evidence.exact_capture import decode_header
from oasis_agent.evidence.rpc_capture import BoundedExchangeRecorder
from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.farming.quote_grid_capture import (
    QuoteCapturePlan,
    QuoteSourceCapture,
    collect_quote_source,
    retain_quote_grid,
)
from oasis_agent.farming.quote_grid_replay import replay_quote_grid
from oasis_agent.launch.models import StrictModel
from oasis_agent.models.chain import BlockSnapshot
from oasis_agent.models.common import canonical_json, sha256_hex
from oasis_agent.rpc.budget import BudgetedPoster, BudgetPolicy, RpcBudget
from oasis_agent.rpc.transport import JsonRpcTransport
from scripts.run_v4_route_probe import CurlPoster


class Source(StrictModel):
    alias: Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")]
    env: Annotated[str, Field(pattern=r"^[A-Z_][A-Z0-9_]{0,127}$")]
    endpoint: Annotated[str, Field(strict=True, min_length=1, max_length=4096)]


class Scope(StrictModel):
    schema_version: Literal["oasis.hash-quote-capture.v1"] = Field(alias="schema")
    purpose: Literal["new_block_hash_bound_quote_spot_benchmark"]
    output_directory: Annotated[str, Field(strict=True, min_length=1, max_length=4096)]
    plan: QuoteCapturePlan
    sources: tuple[Source, Source]
    max_requests_per_source: Annotated[int, Field(strict=True, ge=1, le=1000)]
    min_interval_ms: Annotated[int, Field(strict=True, ge=3000, le=60000)]
    automatic_retries: Annotated[int, Field(strict=True, ge=0, le=0)]

    @model_validator(mode="after")
    def bindings(self) -> Self:
        if (
            tuple(s.alias for s in self.sources) != self.plan.providers
            or len({s.env for s in self.sources}) != 2
        ):
            raise ValueError("scope sources differ from plan")
        if self.max_requests_per_source < self.plan.successful_request_count:
            raise ValueError("budget cannot finish one successful source capture")
        if len({s.endpoint for s in self.sources}) != 2:
            raise ValueError("scope requires two distinct endpoints")
        return self


class ScopedPoster:
    """Only declared methods/targets/data; exact attempts retained, no URLs/errors."""

    def __init__(self, url: str, source: str, scope: Scope, raw: bytes, config: Path, root: Path):
        self.http = CurlPoster(url)
        self.source = source
        self.scope = scope
        self.raw = raw
        self.config = config
        self.root = root
        self.header: BlockSnapshot | None = None

    def post(self, payload: bytes) -> bytes:
        if self.config.read_bytes() != self.raw:
            raise ValueError("capture scope changed before network I/O")
        request = json.loads(payload)
        method, params = request["method"], request["params"]
        plan = self.scope.plan
        expected: list[tuple[str, list[object]]]
        if method == "eth_chainId":
            expected = [("eth_chainId", [])]
        elif method == "eth_getBlockByNumber":
            expected = [("eth_getBlockByNumber", [hex(plan.block_number), False])]
        else:
            if self.header is None:
                raise ValueError("state call before pinned header")
            expected = plan.expected(self.header)[2:-1]
        if not any(
            method == m and canonical_json(params) == canonical_json(p) for m, p in expected
        ):
            raise ValueError("network request outside frozen scope")
        item: dict[str, object] = {
            "source": self.source,
            "retrieved_at": datetime.now(UTC).isoformat(),
            "request": payload.hex(),
        }
        try:
            response = self.http.post(payload)
            item.update(response=response.hex(), transport_status="response")
            if method == "eth_getBlockByNumber" and self.header is None:
                decoded = json.loads(response)
                if "result" in decoded and decoded["result"] is not None:
                    header = decode_header(decoded["result"], chain_id=plan.chain_id)
                    if header.ref.number != plan.block_number:
                        raise ValueError("wrong scope header")
                    self.header = header
            return response
        except OSError:
            item.update(response=None, transport_status="transport_failure_redacted")
            raise
        finally:
            with (self.root / f"{self.source}-attempts.jsonl").open("ab") as stream:
                stream.write(canonical_json(item) + b"\n")
                stream.flush()
                os.fsync(stream.fileno())


def capture(config: Path, root: Path, urls: dict[str, str]) -> None:
    raw = config.read_bytes()
    scope = Scope.from_json(raw)
    if root.resolve() != Path(scope.output_directory).resolve():
        raise ValueError("capture directory differs from frozen scope")
    if set(urls) != set(scope.plan.providers) or len(set(urls.values())) != 2:
        raise ValueError("two distinct declared endpoints required")
    if urls != {s.alias: s.endpoint for s in scope.sources}:
        raise ValueError("endpoints differ from frozen scope")
    for url in urls.values():
        parsed = urllib.parse.urlsplit(url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.fragment
        ):
            raise ValueError("HTTPS endpoint required")
    root.mkdir(parents=True, exist_ok=True)
    with (root / "owner.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        marker = root / "scope-sha256.json"
        digest = canonical_json({"scope_sha256": sha256_hex(raw)}) + b"\n"
        if marker.exists():
            if marker.read_bytes() != digest or not (root / "budget.db").is_file():
                raise ValueError("scope or persistent ledger replaced")
            raise ValueError("existing attempt needs investigation; no implicit restart")
        if (root / "budget.db").exists() or (root / "evidence.db").exists():
            raise ValueError("unbound existing capture artifacts refuse")
        with marker.open("xb") as output:
            output.write(digest)
        policies = tuple(
            BudgetPolicy(s.alias, scope.max_requests_per_source, scope.min_interval_ms)
            for s in scope.sources
        )
        with RpcBudget(root / "budget.db", policies):
            pass

        def one(source: Source) -> QuoteSourceCapture:
            url = urls[source.alias]
            poster = BudgetedPoster(
                ScopedPoster(url, source.alias, scope, raw, config, root),
                root / "budget.db",
                policies,
                source.alias,
            )
            recorder = BoundedExchangeRecorder(
                max_exchanges=scope.plan.successful_request_count, max_bytes=4_000_000
            )
            transport = JsonRpcTransport(url, http=poster, retries=0, recorder=recorder)
            return collect_quote_source((transport, recorder, source.alias), scope.plan)

        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(one, s) for s in scope.sources]
                captured = tuple(f.result() for f in futures)
            store = EvidenceStore(root / "evidence.db")
            try:
                request = retain_quote_grid(store, (captured[0], captured[1]))
                result = replay_quote_grid(store, store, request)
            finally:
                store.close()
            for name, body in [
                ("input.json", {"schema": "oasis.quote-grid-input.v1", "request": asdict(request)}),
                (
                    "report.json",
                    {
                        "schema": "oasis.quote-grid-report.v1",
                        "scope_sha256": sha256_hex(raw),
                        **asdict(result),
                        "capacity_role": (
                            "sampled direct quotes in currency1 base units; no settled exit or USD"
                        ),
                        "price_role": "fee-inclusive average shortfall; historical snapshot only",
                    },
                ),
            ]:
                with (root / name).open("xb") as output:
                    output.write(canonical_json(body) + b"\n")
        except Exception as error:  # noqa: BLE001 - fixed refusal, attempts kept; no raw errors
            with (root / "failure.json").open("xb") as output:
                output.write(
                    canonical_json(
                        {
                            "scope_sha256": sha256_hex(raw),
                            "reason_type": type(error).__name__,
                            "reason": "hash quote capture refused; raw attempts retained",
                            "target_admitted": False,
                            "execution_eligible": False,
                        }
                    )
                    + b"\n"
                )
            raise ValueError("hash quote capture refused; investigate retained attempts") from None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    try:
        scope = Scope.from_json(args.config.read_bytes())
        urls = {s.alias: os.environ.get(s.env, "") for s in scope.sources}
        capture(args.config, args.root, urls)
    except Exception:  # noqa: BLE001 - never echo input, endpoint or raw provider errors
        parser.exit(
            3, "Hash quote capture refused; retained budget and failures must be investigated.\n"
        )
    print("Historical hash-bound grid retained; target admission and execution remain false.")


if __name__ == "__main__":
    main()
