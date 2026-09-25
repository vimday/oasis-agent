"""Operator CLI: turn confirmed broadcast receipts into the next native declaration.

Run from the repository root as ``python -m scripts.run_lp_receipts``.

Every named transaction must already appear in the operator's broadcast journal, so a
declaration can only ever describe a transaction this system signed and sent. Each one is
then read from two independent providers at its own receipt's block, its whole native
cost is reconstructed from retained raw bytes, and every open broadcast among them is
closed from that same observed receipt. Nothing here signs, broadcasts or holds a key,
and ``execution_eligible`` stays false.

The output is the ``--native`` declaration file the reconciliation observer consumes:
one entry per manifest account, with the summed gas of the named transactions and no
non-gas movement. An account that also sent native value keeps an unexplained residual
and its next frame is refused, which is the fail-closed outcome this tool cannot improve.

Exit codes: 0 = declarations written, 2 = a receipt was refused or unreadable,
1 = invalid configuration.
"""

import argparse
from collections.abc import Mapping, Sequence
from pathlib import Path

from oasis_agent.evidence.exact_capture import RecordedSession
from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.execution.account_capture import SessionFactory, one_shot_session
from oasis_agent.execution.lp_broadcast import (
    LpBroadcastJournal,
    broadcast_policy_from_json,
    broadcast_policy_hash,
)
from oasis_agent.execution.lp_receipts import (
    ReceiptObservation,
    gas_declarations,
    observe_receipt,
)
from oasis_agent.models.common import canonical_json, normalize_hash32
from oasis_agent.risk.portfolio_manifest import ChainInventory, PortfolioManifest
from oasis_agent.rpc.budget import BudgetPolicy
from scripts.run_account_risk_reconcile import (
    _bind_endpoints,
    _budget_policies,
    _environment,
    _rpc_map,
)

DECLARATION_SCHEMA = "oasis.lp-receipt-declarations.v1"


def _requested(values: list[str], manifest: PortfolioManifest) -> dict[str, ChainInventory]:
    """Each named transaction must belong to a chain this manifest declares."""
    entries = {str(entry.chain_id): entry for entry in manifest.chains}
    requested: dict[str, ChainInventory] = {}
    for value in values:
        chain, separator, transaction = value.partition(":")
        entry = entries.get(chain.strip())
        if not separator or entry is None:
            raise ValueError("each transaction must name a declared chain and hash")
        digest = normalize_hash32(transaction.strip())
        if digest in requested:
            raise ValueError("one request per transaction required")
        requested[digest] = entry
    if not requested:
        raise ValueError("at least one broadcast transaction is required")
    return requested


def _sessions(
    *, urls: Mapping[str, str], policies: tuple[BudgetPolicy, ...], budget: Path
) -> SessionFactory:
    """One fresh one-shot session per declared provider, sharing one budget ledger."""

    def build(source: str) -> RecordedSession:
        return one_shot_session(source, urls=urls, policies=policies, budget=budget)

    return build


def _summary(
    receipts: Sequence[ReceiptObservation], request_hashes: Mapping[str, str]
) -> list[dict[str, object]]:
    return [
        {
            "chain_id": receipt.chain_id,
            "transaction_hash": receipt.transaction_hash,
            "request_hash": request_hashes[receipt.transaction_hash],
            "block_number": receipt.block_number,
            "block_hash": receipt.block_hash,
            "payer": receipt.payer,
            "status": receipt.status,
            "total_native": receipt.total_native,
            "evidence": [reference.content_hash for reference in receipt.evidence],
        }
        for receipt in receipts
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument("--transaction", action="append", default=[], metavar="CHAIN:HASH")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--rpc", action="append", default=[], metavar="CHAIN:PRIMARY:SECONDARY")
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--interval-ms", type=int, default=3000)
    parser.add_argument("--max-requests", type=int, default=256)
    args = parser.parse_args()
    opened: LpBroadcastJournal | None = None
    try:
        manifest = PortfolioManifest.model_validate_json(args.manifest.read_bytes())
        policy = broadcast_policy_from_json(args.policy.read_bytes())
        endpoints = _bind_endpoints(manifest, _rpc_map(args.rpc))
        if not 3000 <= args.interval_ms <= 60000:
            raise ValueError("minimum request interval is three seconds")
        if not args.journal.is_file():
            raise ValueError("an existing broadcast journal is required")
        names = {name for pair in endpoints.values() for name in pair}
        urls = _environment(args.env_file, names)
        requested = _requested(args.transaction, manifest)
        opened = LpBroadcastJournal(args.journal, policy=policy)
        journal = opened
        for digest in requested:
            if digest not in journal.seen_transactions():
                raise ValueError("a named transaction is not in the broadcast journal")
        policies = _budget_policies(
            names, max_requests=args.max_requests, interval_ms=args.interval_ms
        )
        args.output.mkdir(parents=True, exist_ok=False)
        store = EvidenceStore(args.output / "evidence.db")
    except Exception:  # noqa: BLE001 - never print endpoint values
        if opened is not None:
            opened.close()
        parser.exit(1, "Invalid receipt configuration; no signing is available.\n")

    receipts: list[ReceiptObservation] = []
    request_hashes: dict[str, str] = {}
    resolved: list[str] = []
    sessions = _sessions(urls=urls, policies=policies, budget=args.output / "budget.db")
    try:
        for digest, entry in requested.items():
            receipt = observe_receipt(store, entry, transaction_hash=digest, sessions=sessions)
            receipts.append(receipt)
            request_hashes[digest] = journal.request_hash(digest)
            if digest in journal.open_transactions():
                journal.resolve(
                    digest, receipt_status=receipt.status, block_number=receipt.block_number
                )
                resolved.append(digest)
        declarations = gas_declarations(manifest, receipts)
    except Exception:  # noqa: BLE001 - RPC errors may echo endpoint URLs
        store.close()
        journal.close()
        (args.output / "report.json").write_bytes(
            canonical_json({"status": "receipt_failed_redacted", "execution_eligible": False})
            + b"\n"
        )
        parser.exit(2, "Receipt observation failed; no declaration was written.\n")
    store.close()
    journal.close()
    (args.output / "declarations.json").write_bytes(canonical_json(declarations) + b"\n")
    report = {
        "schema": DECLARATION_SCHEMA,
        "manifest_digest": manifest.digest,
        "policy_hash": broadcast_policy_hash(policy),
        "receipts": _summary(receipts, request_hashes),
        "resolved": sorted(resolved),
        "declarations": declarations,
        "execution_eligible": False,
        "unresolved": [
            "block_hash_is_a_provider_claim_not_a_finalized_header",
            "non_gas_native_movement_is_never_derived_from_a_receipt",
            "declarations_are_never_written_to_the_risk_journal",
            "an_admitted_commitment_is_settled_or_released_only_by_reconciliation",
        ],
    }
    (args.output / "report.json").write_bytes(canonical_json(report) + b"\n")
    print(canonical_json({"receipts": len(receipts), "execution_eligible": False}).decode())


if __name__ == "__main__":
    main()
