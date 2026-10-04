"""Acquire one historical transaction cost without a broadcast journal or wallet.

Reuse one campaign directory on retry: configuration, endpoint hashes and request
budget are frozen. Raw partial sessions are retained on refusal. A cost observation
does not establish LP participation, valuation, finality or net returns.
"""

import argparse
import fcntl
import json
import os
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import Field, TypeAdapter, model_validator

from oasis_agent.evidence.exact_capture import wire_envelope
from oasis_agent.evidence.rpc_capture import BoundedExchangeRecorder
from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.execution.lp_receipts import _capture, _probe_header
from oasis_agent.farming.transaction_costs import (
    FAMILIES,
    persist_transaction_cost,
    replay_transaction_cost,
)
from oasis_agent.launch.models import Address, Positive, StrictModel
from oasis_agent.models.chain import BlockSnapshot
from oasis_agent.models.common import EvidenceRef, canonical_json, normalize_address, sha256_hex
from oasis_agent.rpc.budget import BudgetedPoster, BudgetPolicy, RpcBudget
from oasis_agent.rpc.transport import HttpPoster, JsonRpcTransport
from scripts.capture_lp_position import ProviderSource
from scripts.run_v4_route_probe import CurlPoster


class Request(StrictModel):
    schema_version: Literal["oasis.historical-receipt-input.v1"] = Field(alias="schema")
    chain_id: Positive
    transaction_hash: Annotated[str, Field(pattern=r"^0x[0-9a-fA-F]{64}$")]
    payer: Address
    sources: tuple[ProviderSource, ProviderSource]
    max_requests_per_provider: Annotated[int, Field(ge=8, le=1000)]
    min_interval_ms: Annotated[int, Field(ge=0, le=60_000)]

    @model_validator(mode="after")
    def bounded(self):
        if self.chain_id not in FAMILIES or int(self.payer, 16) == 0:
            raise ValueError("supported chain and nonzero payer required")
        if len({s.alias for s in self.sources}) != 2 or len({s.env for s in self.sources}) != 2:
            raise ValueError("two distinct providers required")
        return self


def _write(path, value):
    temporary = path.with_suffix(".tmp")
    with temporary.open("wb") as f:
        f.write(canonical_json(value) + b"\n")
        f.flush()
        os.fsync(f.fileno())
    temporary.replace(path)


def _report(header, captures, references):
    return {
        "schema": "oasis.historical-receipt-cost.v1",
        "header": asdict(header),
        "cost": asdict(captures[0].reconstruct()),
        "evidence": [asdict(ref) for ref in references],
        "position_allocation": "unresolved",
        "valuation": "unresolved",
        "execution_eligible": False,
    }


def capture(request: Request, posters: dict[str, HttpPoster], output: Path, endpoint_hashes):
    aliases = tuple(s.alias for s in request.sources)
    if set(posters) != set(aliases) or set(endpoint_hashes) != set(aliases):
        raise ValueError("declared sources required")
    if len(set(endpoint_hashes.values())) != 2:
        raise ValueError("distinct endpoint bindings required")
    output.mkdir(parents=True, exist_ok=True)
    with (output / "owner.lock").open("a+b") as owner:
        fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        binding = {
            "request": request.model_dump(mode="json", by_alias=True),
            "endpoint_hashes": endpoint_hashes,
        }
        manifest = output / "manifest.json"
        if manifest.exists():
            if json.loads(manifest.read_bytes()) != binding:
                raise ValueError("historical campaign binding changed")
            if not all((output / name).is_file() for name in ("budget.db", "evidence.db")):
                raise ValueError("historical campaign lost budget or evidence")
        elif any(p.name != "owner.lock" for p in output.iterdir()):
            raise ValueError("unbound campaign directory is not empty")
        policies = tuple(
            BudgetPolicy(a, request.max_requests_per_provider, request.min_interval_ms)
            for a in aliases
        )
        with RpcBudget(output / "budget.db", policies) as budget:
            counts = budget.counts()
        store = EvidenceStore(output / "evidence.db")
        sessions = []
        try:
            if not manifest.exists():
                _write(manifest, binding)
            report_path = output / "report.json"
            if report_path.exists():
                raw = json.loads(report_path.read_bytes())
                header = TypeAdapter(BlockSnapshot).validate_python(raw["header"])
                refs = TypeAdapter(tuple[EvidenceRef, EvidenceRef]).validate_python(raw["evidence"])
                captures = tuple(
                    replay_transaction_cost(
                        store, ref, header=header, transaction_hash=request.transaction_hash
                    )
                    for ref in refs
                )
                if (
                    header.ref.chain_id != request.chain_id
                    or tuple(ref.source for ref in refs) != aliases
                    or any(
                        c.reconstruct().payer != normalize_address(request.payer) for c in captures
                    )
                    or captures[0].reconstruct() != captures[1].reconstruct()
                    or raw != _report(header, captures, refs)
                ):
                    raise ValueError("retained historical report differs from its proofs")
                return raw
            minimum = (8, 6) if FAMILIES[request.chain_id] == "op" else (6, 4)
            if any(
                counts[a] + n > request.max_requests_per_provider
                for a, n in zip(aliases, minimum, strict=True)
            ):
                _write(
                    output / "status.json",
                    {
                        "stage": "halted",
                        "reason": "insufficient_remaining_budget",
                        "request_counts": counts,
                        "execution_eligible": False,
                    },
                )
                raise ValueError("insufficient remaining budget")

            def session(source):
                recorder = BoundedExchangeRecorder()
                rpc = JsonRpcTransport(
                    "https://historical.invalid",
                    retries=0,
                    recorder=recorder,
                    http=BudgetedPoster(posters[source], output / "budget.db", policies, source),
                )
                pair = (rpc, recorder, source)
                sessions.append(pair)
                return pair

            try:
                header = _probe_header(
                    session(aliases[0]),
                    chain_id=request.chain_id,
                    transaction_hash=request.transaction_hash,
                )
                captures = tuple(
                    _capture(session(a), header, request.transaction_hash) for a in aliases
                )
                costs = tuple(c.reconstruct() for c in captures)
                if costs[0] != costs[1] or costs[0].payer != normalize_address(request.payer):
                    raise ValueError("historical cost or payer disagreement")
                refs = tuple(persist_transaction_cost(store, c) for c in captures)
                report = _report(header, captures, refs)
                _write(report_path, report)
                state = {"stage": "completed", "execution_eligible": False}
            except BaseException:
                state = {
                    "stage": "halted",
                    "reason": "historical_capture_refused",
                    "execution_eligible": False,
                }
                raise
            finally:
                for _, recorder, source in sessions:
                    if recorder.exchanges:
                        store.put(
                            "historical_receipt_wire",
                            canonical_json({"execution_eligible": False}),
                            None,
                            source,
                            request=wire_envelope(recorder.exchanges, response=False),
                            raw_response=wire_envelope(recorder.exchanges, response=True),
                        )
                with RpcBudget(output / "budget.db", policies) as budget:
                    state["request_counts"] = budget.counts()
                _write(output / "status.json", state)
            return report
        finally:
            store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        request = Request.from_json(args.request.read_bytes())
        urls = {s.alias: os.environ[s.env] for s in request.sources}
        if any(
            urlsplit(u).scheme != "https"
            or not urlsplit(u).hostname
            or urlsplit(u).username
            or urlsplit(u).password
            or urlsplit(u).fragment
            for u in urls.values()
        ):
            raise ValueError("explicit HTTPS endpoints required")
        capture(
            request,
            {a: CurlPoster(u) for a, u in urls.items()},
            args.output,
            {a: sha256_hex(u.encode()) for a, u in urls.items()},
        )
    except Exception:  # noqa: BLE001 - hostile RPC errors must remain redacted
        parser.exit(3, "Historical receipt refused; inspect retained campaign evidence.\n")
    print("Historical receipt cost retained; position attribution and valuation unresolved.")


if __name__ == "__main__":
    main()
