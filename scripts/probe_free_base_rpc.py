"""Bounded free-endpoint capability probe; no private URLs, keys, retries or polling.

Each invocation uses at most four requests per selected provider. The original
frozen ledgers must already exist; neither output paths nor CLI options renew quota.
"""

import argparse
import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from oasis_agent.evidence.exact_capture import wire_envelope
from oasis_agent.evidence.rpc_capture import BoundedExchangeRecorder
from oasis_agent.models.common import canonical_json, normalize_hash32, sha256_hex
from oasis_agent.rpc.budget import BudgetedPoster, BudgetPolicy, RpcBudget
from oasis_agent.rpc.transport import JsonRpcTransport, _UrlLibPoster

# Explicit no-key public endpoints, from each provider's published documentation.
PUBLIC = {
    "1rpc": "https://public.1rpc.io/base",
    "publicnode": "https://base-rpc.publicnode.com",
}
POLICIES = tuple(BudgetPolicy(name, 8, 3000) for name in PUBLIC)
ROOT = Path(__file__).resolve().parents[1]
BUDGET = ROOT / "reports/evidence/free-base-rpc-budget.sqlite"
# Separate providers and frozen ledger: never modify the earlier public budget.
DISCOVERY = {
    "quicknode-demo": "https://docs-demo.base-mainnet.quiknode.pro/",
    "onfinality": "https://base.api.onfinality.io/public",
}
DISCOVERY_POLICIES = tuple(BudgetPolicy(name, 4, 3000) for name in DISCOVERY)
DISCOVERY_BUDGET = ROOT / "reports/evidence/free-base-rpc-discovery-budget.sqlite"


def budget_counts(path: Path, policies: tuple[BudgetPolicy, ...]) -> dict[str, int]:
    if path.absolute() != path.resolve() or not path.is_file():
        raise ValueError("original regular budget required")
    with RpcBudget(path, policies, require_existing=True) as ledger:
        return ledger.counts()


def probe(source: str, tx: str) -> dict[str, object]:
    endpoints = PUBLIC | DISCOVERY
    if source not in endpoints:
        raise ValueError("only explicitly listed free endpoints are accepted")
    tx = normalize_hash32(tx)
    policies = DISCOVERY_POLICIES if source in DISCOVERY else POLICIES
    budget = DISCOVERY_BUDGET if source in DISCOVERY else BUDGET
    recorder = BoundedExchangeRecorder(max_exchanges=4, max_bytes=5_000_000)
    checks: dict[str, object] = {"chain": False, "receipt": False, "block": False, "trace": False}
    counts_before = None
    failure_stage = "budget"
    try:
        counts_before = budget_counts(budget, policies)
        if source in DISCOVERY and counts_before[source] != 0:
            raise ValueError("discovery attempt already charged; refusing another probe")
        limit = next(p.max_requests for p in policies if p.source == source)
        if counts_before[source] + 4 > limit:
            raise ValueError("existing quota cannot complete one probe")
        transport = JsonRpcTransport(
            "https://unused.invalid",
            http=BudgetedPoster(
                _UrlLibPoster(endpoints[source], 10),
                budget,
                policies,
                source,
                require_existing=True,
            ),
            retries=0,
            recorder=recorder,
        )
        failure_stage = "chain"
        checks["chain"] = transport.request("eth_chainId", []) == "0x2105"
        if not checks["chain"]:
            raise ValueError("chain mismatch")
        failure_stage = "receipt"
        receipt = transport.request("eth_getTransactionReceipt", [tx])
        if not isinstance(receipt, dict) or receipt.get("transactionHash") != tx:
            raise ValueError("receipt unavailable")
        checks["receipt"] = True
        failure_stage = "block"
        header = transport.request("eth_getBlockByNumber", [receipt["blockNumber"], False])
        if not isinstance(header, dict) or header.get("hash") != receipt.get("blockHash"):
            raise ValueError("receipt block unavailable")
        checks["block"] = header
        failure_stage = "trace"
        trace = transport.request(
            "debug_traceTransaction",
            [tx, {"tracer": "callTracer", "timeout": "5s", "tracerConfig": {"onlyTopCall": False}}],
        )
        checks["trace"] = isinstance(trace, dict) and trace.get("type") == "CALL"
        if not checks["trace"]:
            raise ValueError("call trace unavailable")
        failure_stage = "none"
    except Exception:  # noqa: BLE001 - diagnostic errors must not expose provider text
        # Retain wire evidence when available, without echoing provider error text.
        checks["refused_or_unavailable"] = True
    counts_after = None
    try:
        counts_after = budget_counts(budget, policies)
    except Exception:  # noqa: BLE001 - never recreate a missing or corrupt ledger
        failure_stage = "budget_finalization"
        checks["refused_or_unavailable"] = True
    report = {
        "schema": "oasis.free-base-rpc-probe.v2",
        "source": source,
        "endpoint": endpoints[source],
        "retrieved_at": datetime.now(UTC).isoformat(),
        "transaction_hash": tx,
        "checks": checks,
        "capability_verified": failure_stage == "none",
        "failure_stage": failure_stage,
        "request_counts_before": counts_before,
        "request_counts": counts_after,
        "budget_policy": [asdict(p) for p in policies],
        "execution_eligible": False,
        "request": wire_envelope(recorder.exchanges, response=False),
        "response": wire_envelope(recorder.exchanges, response=True),
        "scope": "capability diagnostic, not independent cash or production acceptance",
    }
    report["evidence_hash"] = sha256_hex(canonical_json(report))
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=tuple(PUBLIC | DISCOVERY), required=True)
    parser.add_argument("--transaction", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.out.exists() or args.out.is_symlink():
        parser.error("output exists; refusing a duplicate probe")
    report = probe(args.provider, args.transaction)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("x") as handle:
        json.dump(report, handle, indent=2)
    print(
        json.dumps({key: report[key] for key in ("source", "capability_verified", "failure_stage")})
    )
    raise SystemExit(0 if report["capability_verified"] else 3)


if __name__ == "__main__":
    main()
