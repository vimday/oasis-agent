"""Import complete original public log captures into verified, durable filter coverage."""

import argparse
import sqlite3
import sys
from dataclasses import asdict
from pathlib import Path

from oasis_agent.arbitrage.cycle_evidence import read_bounded
from oasis_agent.evidence.chain_store import CanonicalChainStore
from oasis_agent.evidence.ingestion import persist_and_verify_block_range
from oasis_agent.evidence.public_ranges import RangeContext
from oasis_agent.evidence.public_rpc_plan import PublicRangePlan
from oasis_agent.evidence.rpc_validation import strict_json_object
from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.models.common import sha256_hex
from oasis_agent.rpc.transport import RpcExchange

from .capture_public_rpc import MAX_BODY, load_plan, replay, validate_context
from .research_metric import publish


def import_range(directory: Path, store: EvidenceStore):
    result = replay(directory)
    if result["status"] != "complete" or result["plan_kind"] != "prospective_log_range":
        raise ValueError("complete_public_range_required")
    plan, raw = load_plan(directory / "plan.json")
    if not isinstance(plan, PublicRangePlan) or sha256_hex(raw) != result["plan_sha256"]:
        raise ValueError("capture_changed_during_import")
    manifest_raw = read_bounded(directory / "capture.json", 256 * 1024)
    if sha256_hex(manifest_raw) != result["capture_sha256"]:
        raise ValueError("capture_changed_during_import")
    rows = strict_json_object(manifest_raw)["exchanges"]
    context = None
    exchanges = []
    records = result["exports"]["records"]
    for index, (method, _) in enumerate(plan.requests()):
        request = read_bounded(directory / f"{index}.request.json", 32 * 1024)
        response = read_bounded(directory / f"{index}.response.json", MAX_BODY)
        if (
            sha256_hex(request) != rows[index]["request_sha256"]
            or sha256_hex(response) != rows[index]["response_sha256"]
        ):
            raise ValueError("capture_changed_during_import")
        context = validate_context(plan, index, records[index]["response"]["result"], context)
        exchanges.append(RpcExchange(method, index + 1, request, response, 1))
    if not isinstance(context, RangeContext):
        raise ValueError("range_context_missing")
    return persist_and_verify_block_range(
        store,
        context.dataset(plan),
        tuple(exchanges),
        research_single_provider=True,
        retrieved_at=records[-1]["fetched_utc"],
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("capture", "store", "chain-store", "output"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--stream", required=True)
    args = parser.parse_args()
    try:
        if not args.stream or len(args.stream) > 128:
            raise ValueError("invalid_stream")
        store = EvidenceStore(args.store)
        try:
            verified = import_range(args.capture, store)
        finally:
            store.close()
        chain = CanonicalChainStore(args.chain_store)
        try:
            dataset = verified.dataset
            with chain.atomic():
                applied = chain.apply(args.stream, dataset, evidence=verified.evidence)
                coverage = chain.coverage(
                    args.stream, dataset.chain_id, dataset.start_block, dataset.end_block
                )
                report = {
                    "schema": "oasis-public-range-import-v1",
                    "evidence": asdict(verified.evidence),
                    "coverage": coverage,
                    "canonical_application": asdict(applied),
                    "log_count": len(dataset.logs),
                    "source_trust": verified.trust.value,
                    "execution_eligible": False,
                }
        finally:
            chain.close()
        publish(args.output, report)
    except (ValueError, TypeError, OSError, sqlite3.Error):
        print("Range import refused; no usable shadow output", file=sys.stderr)
        return 2
    print(f"Imported verified filter coverage; {len(dataset.logs)} logs; observation only")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
