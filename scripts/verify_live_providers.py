"""Bounded, retained Base trace/cost acceptance; no key, signing or broadcasting.

One new acceptance run uses a maximum of 12 calls per source, including a single
transport retry for ordinary idempotent reads. Trace itself is never retried.
An existing output directory is terminal; it cannot be reset through this CLI.
"""

import argparse
import json
import ssl
from contextlib import closing
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit

from oasis_agent.evidence.exact_capture import wire_envelope
from oasis_agent.evidence.rpc_capture import BoundedExchangeRecorder
from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.live.provider_acceptance import collect_quorum, replay
from oasis_agent.models.common import BlockRef, canonical_json, normalize_hash32, sha256_hex
from oasis_agent.rpc.budget import BudgetedPoster, BudgetPolicy, RpcBudget
from oasis_agent.rpc.transport import JsonRpcTransport, _UrlLibPoster
from scripts.run_account_risk_reconcile import _environment

POLICIES = (BudgetPolicy("drpc", 12, 3000), BudgetPolicy("alchemy", 12, 3000))
BUDGET_PATH = (
    Path(__file__).resolve().parents[1] / "reports/evidence/live-provider-acceptance-budget.sqlite"
)


def failure_category(error: OSError) -> str:
    if isinstance(error, HTTPError):
        return f"http_{error.code}"
    reason = error.reason if isinstance(error, URLError) else error
    if isinstance(reason, ssl.SSLError):
        return "tls_failure"
    if isinstance(reason, TimeoutError):
        return "timeout"
    return "transport_failure"


class AuditedPoster:
    def __init__(self, http, failures):
        self.http, self.failures = http, failures

    def post(self, payload):
        try:
            return self.http.post(payload)
        except OSError as error:
            self.failures.append(
                {
                    "method": json.loads(payload)["method"],
                    "request_hash": sha256_hex(payload),
                    "category": failure_category(error),
                }
            )
            raise


def verify(
    root: Path, block: BlockRef, transaction: str, urls: dict[str, str], *, budget_path: Path
):
    transaction = normalize_hash32(transaction)
    if block.chain_id != 8453 or block.timestamp is None:
        raise ValueError("timestamped Base block required")
    if root.exists():
        raise ValueError("existing acceptance is terminal; refusing reset")
    parsed = urlsplit(urls.get("alchemy", ""))
    if (
        set(urls) != {"drpc", "alchemy"}
        or urls["drpc"] != "https://base.drpc.org/"
        or parsed.scheme != "https"
        or parsed.hostname != "base-mainnet.g.alchemy.com"
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("declared independent provider identities required")
    budget_path.parent.mkdir(parents=True, exist_ok=True)
    with RpcBudget(budget_path, POLICIES) as budget:
        used = budget.counts()
        if any(p.max_requests - used[p.source] < 9 for p in POLICIES):
            raise ValueError("insufficient retained budget for complete capture")
    root.mkdir(parents=True)
    scope = {
        "schema": "oasis.live-provider-scope.v1",
        "block": asdict(block),
        "transaction": transaction,
        "endpoints": {s: sha256_hex(u.encode()) for s, u in urls.items()},
        "policies": [asdict(p) for p in POLICIES],
        "read_retries": 1,
        "trace_retries": 0,
        "prior_paid_request_reserve": 4,
        "paid_cu_reserve_per_request": 1000,
        "public_usd_per_million_cu": "0.525",
        "combined_public_rate_max_usd": "0.0084",
        "owner_acceptance_usd_ceiling": "0.10",
        "invoice_verified": False,
        "execution_eligible": False,
    }
    with (root / "scope.json").open("xb") as handle:
        handle.write(canonical_json(scope))
    failures = {s: [] for s in urls}
    captures = []
    for source in ("drpc", "alchemy"):
        recorder = BoundedExchangeRecorder(max_exchanges=12, max_bytes=10_000_000)
        poster = BudgetedPoster(
            AuditedPoster(_UrlLibPoster(urls[source], 15), failures[source]),
            budget_path,
            POLICIES,
            source,
        )
        captures.append(
            (
                JsonRpcTransport(
                    "https://unused.invalid",
                    http=poster,
                    retries=1,
                    backoff_seconds=3,
                    total_deadline_seconds=40,
                    recorder=recorder,
                ),
                recorder,
                source,
            )
        )
    report = {
        "schema": "oasis.live-provider-report.v1",
        "scope_hash": sha256_hex(canonical_json(scope)),
        "retrieved_at": datetime.now(UTC).isoformat(),
        "passed": False,
        "execution_eligible": False,
    }
    try:
        with closing(EvidenceStore(root / "evidence.db")) as store:
            refs = collect_quorum(store, captures[0], captures[1], block, transaction)
            first, second = (replay(store, ref) for ref in refs)
            if first != second:
                raise ValueError("replay disagreement")
            report.update(passed=True, result=first, evidence=[asdict(r) for r in refs])
    except Exception:  # noqa: BLE001 - fixed text; provider exceptions can contain credentials
        report["refusal"] = "provider capture or exact replay refused; no execution authorization"
    report["transport_failures"] = failures
    report["captures"] = [
        {
            "source": s,
            "request": wire_envelope(r.exchanges, response=False),
            "response": wire_envelope(r.exchanges, response=True),
        }
        for _, r, s in captures
    ]
    with RpcBudget(budget_path, POLICIES) as budget:
        report["request_counts"] = budget.counts()
    report["evidence_hash"] = sha256_hex(canonical_json(report))
    with (root / "report.json").open("x") as output:
        json.dump(report, output, indent=2)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scope", type=Path, required=True, help="JSON with block and transaction")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    args = parser.parse_args()
    raw = json.loads(args.scope.read_text())
    if set(raw) != {"block", "transaction"}:
        raise ValueError("scope requires exactly block and transaction")
    values = _environment(args.env_file, {"BASE_RPC_PRIMARY_ALT"})
    report = verify(
        args.out,
        BlockRef(**raw["block"]),
        raw["transaction"],
        {"drpc": "https://base.drpc.org/", "alchemy": values["BASE_RPC_PRIMARY_ALT"]},
        budget_path=BUDGET_PATH,
    )
    print(
        json.dumps(
            {
                k: report[k]
                for k in ("passed", "request_counts", "transport_failures", "evidence_hash")
            }
        )
    )
    raise SystemExit(0 if report["passed"] else 3)


if __name__ == "__main__":
    main()
