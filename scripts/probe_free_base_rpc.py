"""Bounded free-endpoint capability probe; no private URLs, keys, retries or polling.

Each invocation uses at most four requests per selected provider. The default
persistent ledger allows eight requests total per provider across restarts.
"""

import argparse
import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from oasis_agent.errors import RpcError
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
# Separate providers and frozen ledger: never modify the earlier public budget.
DISCOVERY = {
    "quicknode-demo": "https://docs-demo.base-mainnet.quiknode.pro/",
    "onfinality": "https://base.api.onfinality.io/public",
}
DISCOVERY_POLICIES = tuple(BudgetPolicy(name, 4, 3000) for name in DISCOVERY)
DISCOVERY_BUDGET = (
    Path(__file__).resolve().parents[1] / "reports/evidence/free-base-rpc-discovery-budget.sqlite"
)


def probe(source: str, tx: str, budget: Path) -> dict[str, object]:
    endpoints = PUBLIC | DISCOVERY
    if source not in endpoints:
        raise ValueError("only explicitly listed free endpoints are accepted")
    tx = normalize_hash32(tx)
    policies = DISCOVERY_POLICIES if source in DISCOVERY else POLICIES
    if source in DISCOVERY:
        with RpcBudget(budget, policies) as ledger:
            if ledger.counts()[source] != 0:
                raise ValueError("discovery attempt already charged; refusing another probe")
    recorder = BoundedExchangeRecorder(max_exchanges=4, max_bytes=5_000_000)
    transport = JsonRpcTransport(
        "https://unused.invalid",
        http=BudgetedPoster(_UrlLibPoster(endpoints[source], 10), budget, policies, source),
        retries=0,
        recorder=recorder,
    )
    checks: dict[str, object] = {"chain": False, "receipt": False, "block": False, "trace": False}
    try:
        checks["chain"] = transport.request("eth_chainId", []) == "0x2105"
        if not checks["chain"]:
            raise ValueError("chain mismatch")
        receipt = transport.request("eth_getTransactionReceipt", [tx])
        if not isinstance(receipt, dict) or receipt.get("transactionHash") != tx:
            raise ValueError("receipt unavailable")
        checks["receipt"] = True
        header = transport.request("eth_getBlockByNumber", [receipt["blockNumber"], False])
        if not isinstance(header, dict) or header.get("hash") != receipt.get("blockHash"):
            raise ValueError("receipt block unavailable")
        checks["block"] = header
        trace = transport.request(
            "debug_traceTransaction",
            [tx, {"tracer": "callTracer", "timeout": "5s", "tracerConfig": {"onlyTopCall": False}}],
        )
        checks["trace"] = isinstance(trace, dict) and trace.get("type") == "CALL"
    except (ValueError, OSError, RpcError, KeyError):
        # Retain wire evidence when available, without echoing provider error text.
        checks["refused_or_unavailable"] = True
    report = {
        "schema": "oasis.free-base-rpc-probe.v1",
        "source": source,
        "endpoint": endpoints[source],
        "retrieved_at": datetime.now(UTC).isoformat(),
        "transaction_hash": tx,
        "checks": checks,
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
    parser.add_argument("--budget", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.provider in DISCOVERY:
        if args.budget is not None:
            parser.error("new-provider discovery uses its fixed persistent budget")
        args.budget = DISCOVERY_BUDGET
    elif args.budget is None:
        args.budget = Path("reports/evidence/free-base-rpc-budget.sqlite")
    if args.out.exists():
        parser.error("output exists; refusing a duplicate probe")
    args.budget.parent.mkdir(parents=True, exist_ok=True)
    report = probe(args.provider, args.transaction, args.budget)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("x") as handle:
        json.dump(report, handle, indent=2)
    print(
        json.dumps(
            {"source": args.provider, "checks": report["checks"], "execution_eligible": False}
        )
    )


if __name__ == "__main__":
    main()
