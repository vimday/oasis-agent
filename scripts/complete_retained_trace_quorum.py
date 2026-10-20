"""Complete one retained QuickNode/Alchemy trace comparison without repeating free reads.

Uses only the unspent Alchemy allowance in the existing acceptance ledger. Four
calls maximum, no retry, no key, and no signing or production authorization.
"""

import argparse
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from oasis_agent.evidence.exact_capture import decode_header, successful_results, wire_envelope
from oasis_agent.evidence.rpc_capture import BoundedExchangeRecorder
from oasis_agent.evidence.rpc_validation import decode_exchange, restore_exchanges, rpc_quantity
from oasis_agent.farming.transaction_costs import (
    TransactionCostCapture,
    transaction_cost_head,
    transaction_cost_tail,
)
from oasis_agent.live.provider_acceptance import extra_requests, replay_trace_capture
from oasis_agent.models.common import canonical_json, sha256_hex
from oasis_agent.rpc.budget import BudgetedPoster, RpcBudget
from oasis_agent.rpc.transport import JsonRpcTransport, _UrlLibPoster
from scripts.run_account_risk_reconcile import _environment
from scripts.verify_live_providers import BUDGET_PATH, POLICIES, AuditedPoster


def checked_report(path):
    raw = json.loads(path.read_bytes())
    claimed = raw.pop("evidence_hash")
    if sha256_hex(canonical_json(raw)) != claimed:
        raise ValueError("retained report digest mismatch")
    return raw, claimed


def retained_inputs(peer_path, body_path):
    body, body_hash = checked_report(body_path)
    capture = next(x for x in body["captures"] if x["source"] == "drpc")
    x = restore_exchanges(canonical_json(capture["request"]), canonical_json(capture["response"]))
    if len(x) != 7:
        raise ValueError("exact retained cost and transaction capture required")
    header = decode_header(decode_exchange(x[1])[1]["result"], chain_id=8453)
    tx = decode_exchange(x[6])[1]["result"]
    cost = TransactionCostCapture(header, tx["hash"], "drpc", x[:6]).reconstruct()
    receipt = decode_exchange(x[2])[1]["result"]
    successful_results(
        x,
        transaction_cost_head(header.ref, tx["hash"])
        + transaction_cost_tail(header.ref, gas=rpc_quantity(receipt["gasUsed"]))
        + [extra_requests(header.ref, tx["hash"])[0]],
    )
    if cost.payer != tx["from"] or cost.status != 1:
        raise ValueError("retained payer/status mismatch")
    peer, peer_hash = checked_report(peer_path)
    if (
        peer["source"] != "quicknode-demo"
        or peer["endpoint"] != "https://docs-demo.base-mainnet.quiknode.pro/"
    ):
        raise ValueError("independent retained QuickNode source required")
    wire = restore_exchanges(canonical_json(peer["request"]), canonical_json(peer["response"]))
    summary = replay_trace_capture(wire, header.ref, tx)
    return header.ref, tx, summary, peer_hash, body_hash


def complete(root, peer_path, body_path, url, *, budget_path=BUDGET_PATH):
    if root.exists():
        raise ValueError("completion output already exists")
    block, tx, expected, peer_hash, body_hash = retained_inputs(peer_path, body_path)
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "base-mainnet.g.alchemy.com"
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("declared Alchemy endpoint required")
    if not budget_path.is_file():
        raise ValueError("existing acceptance budget required; no fresh ledger")
    with RpcBudget(budget_path, POLICIES) as budget:
        before = budget.counts()
        if before["alchemy"] != 0:
            raise ValueError("paid completion already attempted; refusing another")
    scope = {
        "schema": "oasis.retained-trace-completion.v1",
        "transaction": tx["hash"],
        "peer_report_hash": peer_hash,
        "body_report_hash": body_hash,
        "endpoint_hash": sha256_hex(url.encode()),
        "budget_counts_before": before,
        "max_paid_calls": 4,
        "retries": 0,
        "prior_paid_calls": 4,
        "combined_public_rate_allowance_usd": "0.0042",
        "owner_ceiling_usd": "0.10",
        "invoice_verified": False,
        "execution_eligible": False,
    }
    # Permanent exclusive claim closes the check/send race across output paths.
    # Never remove it on failure: an interrupted attempt is not a fresh allowance.
    claim = budget_path.with_name(budget_path.name + ".retained-trace-claim.json")
    try:
        with claim.open("x") as handle:
            handle.write(canonical_json(scope).decode())
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        raise ValueError("paid completion already claimed; refusing another") from None
    root.mkdir(parents=True)
    (root / "scope.json").write_bytes(canonical_json(scope))
    recorder = BoundedExchangeRecorder(max_exchanges=4, max_bytes=10_000_000)
    failures = []
    rpc = JsonRpcTransport(
        "https://unused.invalid",
        retries=0,
        recorder=recorder,
        http=BudgetedPoster(
            AuditedPoster(_UrlLibPoster(url, 15), failures), budget_path, POLICIES, "alchemy"
        ),
    )
    report = {
        "schema": "oasis.retained-trace-completion-result.v1",
        "passed": False,
        "scope_hash": sha256_hex(canonical_json(scope)),
        "retrieved_at": datetime.now(UTC).isoformat(),
        "execution_eligible": False,
    }
    try:
        for method, params in [
            ("eth_chainId", []),
            ("eth_getTransactionReceipt", [tx["hash"]]),
            ("eth_getBlockByNumber", [hex(block.number), False]),
            extra_requests(block, tx["hash"])[1],
        ]:
            rpc.request(method, params)
        observed = replay_trace_capture(recorder.exchanges, block, tx)
        if observed != expected:
            raise ValueError("independent trace/receipt mismatch")
        report.update(passed=True, result=observed)
    except Exception:  # noqa: BLE001 - fixed redacted message only
        report["refusal"] = "independent retained trace comparison refused"
    report.update(
        request=wire_envelope(recorder.exchanges, response=False),
        response=wire_envelope(recorder.exchanges, response=True),
        transport_failures=failures,
    )
    with RpcBudget(budget_path, POLICIES) as budget:
        report["request_counts"] = budget.counts()
    report["evidence_hash"] = sha256_hex(canonical_json(report))
    (root / "report.json").write_text(json.dumps(report, indent=2))
    return report


def replay_completion(root, peer_path, body_path):
    """Rebuild both trace legs offline; no credentials or budget are opened."""
    block, tx, expected, peer_hash, body_hash = retained_inputs(peer_path, body_path)
    report, digest = checked_report(root / "report.json")
    scope = json.loads((root / "scope.json").read_bytes())
    if (
        report.get("schema") != "oasis.retained-trace-completion-result.v1"
        or scope.get("schema") != "oasis.retained-trace-completion.v1"
        or report.get("scope_hash") != sha256_hex(canonical_json(scope))
        or scope.get("peer_report_hash") != peer_hash
        or scope.get("body_report_hash") != body_hash
        or scope.get("transaction") != tx["hash"]
        or scope.get("max_paid_calls") != 4
        or scope.get("retries") != 0
        or report.get("passed") is not True
        or report.get("execution_eligible") is not False
        or scope.get("execution_eligible") is not False
        or report.get("transport_failures") != []
    ):
        raise ValueError("retained completion scope mismatch")
    exchanges = restore_exchanges(
        canonical_json(report["request"]), canonical_json(report["response"])
    )
    observed = replay_trace_capture(exchanges, block, tx)
    if observed != expected or report.get("result") != observed:
        raise ValueError("retained completion trace/receipt mismatch")
    before, after = scope["budget_counts_before"], report["request_counts"]
    if before["alchemy"] != 0 or after != {**before, "alchemy": 4}:
        raise ValueError("retained completion budget mismatch")
    return {**report, "evidence_hash": digest}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--peer", type=Path, required=True)
    parser.add_argument("--body", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--replay", action="store_true", help="offline replay; no RPC or credentials"
    )
    args = parser.parse_args()
    if args.replay:
        r = replay_completion(args.out, args.peer, args.body)
        print(json.dumps({k: r[k] for k in ("passed", "result", "evidence_hash")}))
        return
    # Replay before reading any selected-only provider credential.
    retained_inputs(args.peer, args.body)
    url = _environment(Path(".env"), {"BASE_RPC_PRIMARY_ALT"})["BASE_RPC_PRIMARY_ALT"]
    r = complete(args.out, args.peer, args.body, url)
    print(json.dumps({k: r[k] for k in ("passed", "request_counts", "evidence_hash")}))
    raise SystemExit(0 if r["passed"] else 3)


if __name__ == "__main__":
    main()
