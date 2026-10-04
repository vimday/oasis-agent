"""Capture or offline-replay one frozen outer-router block and all its receipts."""

import argparse
import fcntl
import json
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal, Self
from urllib.parse import urlsplit

from pydantic import Field, model_validator

from oasis_agent.evidence.rpc_capture import BoundedExchangeRecorder
from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.farming.router_activity import (
    RouterActivityCapture,
    RouterActivityPlan,
    collect_router_activity,
    replay_router_activity,
    retain_router_activity,
    router_transactions,
)
from oasis_agent.launch.models import StrictModel
from oasis_agent.models.common import EvidenceRef, canonical_json, sha256_hex
from oasis_agent.rpc.budget import BudgetedPoster, BudgetPolicy, RpcBudget
from oasis_agent.rpc.transport import JsonRpcTransport
from scripts.capture_quote_grid import Source
from scripts.run_v4_route_probe import CurlPoster


class Scope(StrictModel):
    schema_version: Literal["oasis.router-activity-scope.v1"] = Field(alias="schema")
    purpose: Literal["complete_outer_router_receipts_at_quote_block"]
    output_directory: Annotated[str, Field(min_length=1, max_length=4096)]
    plan: RouterActivityPlan
    sources: tuple[Source, Source]
    max_requests_per_source: Annotated[int, Field(strict=True, ge=1, le=1000)]
    min_interval_ms: Annotated[int, Field(strict=True, ge=3000, le=60000)]
    automatic_retries: Annotated[int, Field(strict=True, ge=0, le=0)]

    @model_validator(mode="after")
    def bindings(self) -> Self:
        if (
            tuple(s.alias for s in self.sources) != self.plan.providers
            or len({s.env for s in self.sources}) != 2
            or len({s.endpoint for s in self.sources}) != 2
            or self.max_requests_per_source < 5 + self.plan.max_router_transactions
        ):
            raise ValueError("source or sufficient budget binding differs")
        # The independent historical purpose is confined to public HTTPS endpoints.
        for source in self.sources:
            parsed = urlsplit(source.endpoint)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.fragment
                or parsed.query
            ):
                raise ValueError("public HTTPS endpoint required")
        return self


class References(StrictModel):
    schema_version: Literal["oasis.router-activity-input.v1"] = Field(alias="schema")
    scope_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    evidence: tuple[EvidenceRef, EvidenceRef]


def _write(path: Path, body: object) -> None:
    with path.open("xb") as output:
        output.write(canonical_json(body) + b"\n")
        output.flush()
        os.fsync(output.fileno())


class ScopedPoster:
    def __init__(self, source: Source, scope: Scope, config: Path, raw: bytes, root: Path):
        self.source, self.scope, self.config, self.raw, self.root = source, scope, config, raw, root
        self.http = CurlPoster(source.endpoint)
        self.hashes: set[str] = set()

    def post(self, payload: bytes) -> bytes:
        if self.config.read_bytes() != self.raw:
            raise ValueError("router scope changed before HTTP")
        req = json.loads(payload)
        expected = [
            *self.scope.plan.opening(),
            ("eth_getBlockByNumber", [hex(self.scope.plan.header.ref.number), False]),
            *(("eth_getTransactionReceipt", [h]) for h in self.hashes),
        ]
        if not any(
            req["method"] == m and canonical_json(req["params"]) == canonical_json(p)
            for m, p in expected
        ):
            raise ValueError("router request outside frozen scope")
        item: dict[str, object] = {
            "source": self.source.alias,
            "requested_at": datetime.now(UTC).isoformat(),
            "request": payload.hex(),
        }
        try:
            response = self.http.post(payload)
            item.update(response=response.hex(), transport_status="response")
            if req["method"] == "eth_getBlockByNumber" and req["params"][-1] is True:
                parsed = json.loads(response)
                if "result" in parsed:
                    self.hashes = {
                        str(tx["hash"])
                        for tx in router_transactions(self.scope.plan, parsed["result"])
                    }
            return response
        except OSError:
            item.update(response=None, transport_status="transport_failure_redacted")
            raise
        finally:
            item["retrieved_at"] = datetime.now(UTC).isoformat()
            with (self.root / f"{self.source.alias}-attempts.jsonl").open("ab") as log:
                log.write(canonical_json(item) + b"\n")
                log.flush()
                os.fsync(log.fileno())


def replay(config: Path, root: Path, output: Path) -> None:
    raw = config.read_bytes()
    scope = Scope.from_json(raw)
    digest = sha256_hex(raw)
    if root.resolve() != Path(scope.output_directory).resolve():
        raise ValueError("router directory differs from scope")
    if json.loads((root / "scope-sha256.json").read_bytes()) != {"scope_sha256": digest}:
        raise ValueError("retained router scope differs")
    refs = References.from_json((root / "input.json").read_bytes())
    if (
        refs.scope_sha256 != digest
        or tuple(r.source for r in refs.evidence) != scope.plan.providers
    ):
        raise ValueError("router references differ from scope")
    store = EvidenceStore(root / "evidence.db")
    try:
        left, right = (replay_router_activity(store, ref, scope.plan) for ref in refs.evidence)
        if left != right:
            raise ValueError("router receipt providers disagree")
        _write(
            output,
            {
                "scope_sha256": digest,
                "evidence": [asdict(r) for r in refs.evidence],
                **left,
                "coverage": (
                    "all outer calls to pinned router in one block; not internal aggregator routes"
                ),
                "owner_cash_attribution": "unresolved",
                "full_action_cost": None,
                "oos_accepted": False,
                "canary_accepted": False,
            },
        )
    finally:
        store.close()


def capture(config: Path, root: Path) -> None:
    raw = config.read_bytes()
    scope = Scope.from_json(raw)
    if root.resolve() != Path(scope.output_directory).resolve():
        raise ValueError("router directory differs from scope")
    if any(os.environ.get(s.env) != s.endpoint for s in scope.sources):
        raise ValueError("RPC environment differs from frozen endpoints")
    root.mkdir(parents=True, exist_ok=True)
    with (root / "owner.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if any((root / n).exists() for n in ("scope-sha256.json", "budget.db", "evidence.db")):
            raise ValueError("existing router attempt needs investigation; no implicit restart")
        _write(root / "scope-sha256.json", {"scope_sha256": sha256_hex(raw)})
        policies = tuple(
            BudgetPolicy(s.alias, scope.max_requests_per_source, scope.min_interval_ms)
            for s in scope.sources
        )
        with RpcBudget(root / "budget.db", policies):
            pass

        def one(source: Source) -> RouterActivityCapture:
            recorder = BoundedExchangeRecorder(
                max_exchanges=5 + scope.plan.max_router_transactions, max_bytes=16_000_000
            )
            poster = BudgetedPoster(
                ScopedPoster(source, scope, config, raw, root),
                root / "budget.db",
                policies,
                source.alias,
            )
            transport = JsonRpcTransport(source.endpoint, http=poster, retries=0, recorder=recorder)
            return collect_router_activity((transport, recorder, source.alias), scope.plan)

        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(one, source) for source in scope.sources]
                left, right = (f.result() for f in futures)
            if left.facts() != right.facts():
                raise ValueError("router receipt providers disagree")
            store = EvidenceStore(root / "evidence.db")
            try:
                refs = tuple(retain_router_activity(store, c) for c in (left, right))
            finally:
                store.close()
            _write(
                root / "input.json",
                {
                    "schema": "oasis.router-activity-input.v1",
                    "scope_sha256": sha256_hex(raw),
                    "evidence": [asdict(r) for r in refs],
                },
            )
            replay(config, root, root / "report.json")
        except Exception as error:  # noqa: BLE001 - raw failures retained; fixed public error
            _write(
                root / "failure.json",
                {
                    "scope_sha256": sha256_hex(raw),
                    "reason_type": type(error).__name__,
                    "reason": "router capture refused",
                    "target_admitted": False,
                    "execution_eligible": False,
                },
            )
            raise ValueError("router capture refused; investigate retained attempts") from None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--replay-output", type=Path)
    args = parser.parse_args()
    try:
        if args.replay_output is None:
            capture(args.config, args.root)
        else:
            replay(args.config, args.root, args.replay_output)
    except Exception:  # noqa: BLE001 - no endpoint/input in public error
        parser.exit(3, "Router activity refused; existing budget and failed samples retained.\n")
    print("Historical router receipt observation retained; admission and execution remain false.")


if __name__ == "__main__":
    main()
