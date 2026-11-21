"""Resume the fixed LP diagnostic campaign with separate <=100-block wire reads.

This command cannot create or enlarge a campaign budget. It replays the completed
market archive and copies each provider's original header proof without changing
its identity. Partial sessions are retained; an accepted provider proof is reused.
No wallet, signing or broadcast capability is imported.
"""

import argparse
import fcntl
import json
import os
import subprocess
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import TypeAdapter

from oasis_agent.errors import RpcError
from oasis_agent.evidence.exact_capture import wire_envelope
from oasis_agent.evidence.rpc_capture import BoundedExchangeRecorder
from oasis_agent.evidence.rpc_validation import strict_json_object
from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.farming.segmented_logs import (
    SegmentedLogPlan,
    collect_segmented_logs,
    minimum_requests,
    replay_segmented_logs,
)
from oasis_agent.models.common import EvidenceRef, canonical_json, sha256_hex
from oasis_agent.rpc.budget import BudgetedPoster, BudgetPolicy, RpcBudget
from oasis_agent.rpc.collector import require_provider_agreement
from oasis_agent.rpc.transport import JsonRpcTransport
from oasis_agent.shadow.market_archive import ArchiveConfig, MarketChunk, validate_archive
from scripts.capture_lp_market_archive import _write


class RetainedErrorCurlPoster:
    """Keep valid RPC errors returned with HTTP 4xx, without exposing URLs/stderr."""

    def __init__(self, url):
        self.url = url
        self.last_http = None

    def post(self, payload):
        self.last_http = None
        try:
            p = subprocess.run(
                [
                    "/usr/bin/curl",
                    "--http1.1",
                    "--silent",
                    "--show-error",
                    "--max-time",
                    "20",
                    "--max-filesize",
                    "1048576",
                    "--write-out",
                    "\n%{http_code}",
                    self.url,
                    "-H",
                    "Content-Type: application/json",
                    "--data-binary",
                    "@-",
                ],
                input=payload,
                capture_output=True,
                timeout=25,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise OSError("RPC process deadline exceeded") from None
        self.last_http = {"curl_exit": p.returncode, "http_status": 0}
        if p.returncode != 0:
            raise OSError("RPC curl transport failed")
        body, status = p.stdout.rsplit(b"\n", 1)
        self.last_http["http_status"] = int(status)
        decoded = strict_json_object(body)
        if not 200 <= int(status) < 300 and "error" not in decoded:
            raise OSError("RPC HTTP refusal without a JSON-RPC error")
        return body


def _copy(store, original, ref):
    record = original.get_record(ref)
    copied = store.put(
        ref.kind,
        record.payload,
        ref.block,
        ref.source,
        request=record.request_payload,
        raw_response=record.raw_response_payload,
        interpretation=record.interpretation_payload,
        retrieved_at=ref.retrieved_at,
        schema_version=ref.schema_version,
    )
    if copied != ref:
        raise ValueError("header evidence identity changed during copy")


def _report(store, refs):
    datasets = tuple(replay_segmented_logs(store, ref) for ref in refs)
    agreement = require_provider_agreement(datasets)
    return {
        "schema": "oasis.lp-action-discovery-segmented.v1",
        "agreement": asdict(agreement),
        "evidence": [asdict(ref) for ref in refs],
        "logs": [asdict(log) for log in datasets[0].logs],
        "event_count": len(datasets[0].logs),
        "log_range_complete": True,
        "cash_window_complete": False,
        "execution_eligible": False,
        "unresolved": [
            "NFT_identity_and_ownership",
            "owner_payments",
            "all_costs_and_valuation",
            "out_of_sample_net_returns",
            "production_admission",
        ],
    }


def resume(campaign: Path, archive: Path, posters, endpoint_hashes, *, sources=("public", "paid")):
    # Existing directories/ledgers are mandatory: this is not a fresh budget CLI.
    if not all((campaign / n).is_file() for n in ("manifest.json", "budget.db", "evidence.db")):
        raise ValueError("existing diagnostic campaign and original ledger required")
    if sources not in (("public",), ("paid",), ("public", "paid")):
        raise ValueError("one declared provider or the original ordered pair required")
    with (campaign / "owner.lock").open("a+b") as owner, (archive / "owner.lock").open("rb") as ao:
        fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(ao, fcntl.LOCK_SH | fcntl.LOCK_NB)
        return _resume_locked(campaign, archive, posters, endpoint_hashes, sources)


def _resume_locked(campaign, archive, posters, endpoint_hashes, sources):
    manifest = strict_json_object((campaign / "manifest.json").read_bytes())
    original_manifest = (archive / "manifest.json").read_bytes()
    binding = strict_json_object(original_manifest)
    config = ArchiveConfig.model_validate_json(canonical_json(binding["config"]))
    if (
        manifest.get("schema") != "oasis.lp-action-discovery-campaign.v1"
        or manifest["archive_manifest_sha256"] != sha256_hex(original_manifest)
        or manifest["archive_complete_checkpoint_sha256"]
        != sha256_hex((archive.parent / "complete-checkpoint-sha256.json").read_bytes())
        or endpoint_hashes != manifest["endpoint_hashes"]
        or endpoint_hashes != binding["endpoint_hashes"]
        or set(posters) != {"public", "paid"}
        or len(set(endpoint_hashes.values())) != 2
        or (
            manifest["chain_id"],
            manifest["start_block"],
            manifest["end_block"],
            manifest["address"],
        )
        != (
            config.observer.chain_id,
            config.start_block,
            config.end_block,
            config.observer.pool_manager,
        )
        or manifest["topics"]
        != [
            "0xf208f4912782fd25c7f114ca3723a2d5dd6f3bcc3ac8db5af63baa85f711d5ec",
            config.observer.pool_id,
        ]
    ):
        raise ValueError("diagnostic/archive/endpoint scope differs")
    pinned_files = strict_json_object(
        (archive.parent / "complete-checkpoint-sha256.json").read_bytes()
    )
    hashes_by_path = {Path(name).resolve(): digest for name, digest in pinned_files.items()}
    for path in (
        archive / "manifest.json",
        archive / "evidence.db",
        *sorted(archive.glob("chunk-*.json")),
    ):
        if hashes_by_path.get(path.resolve()) != sha256_hex(path.read_bytes()):
            raise ValueError("archive input differs from the frozen complete checkpoint")
    policies = tuple(
        BudgetPolicy(a, manifest["max_requests_per_provider"], manifest["min_interval_ms"])
        for a in ("public", "paid")
    )
    with RpcBudget(campaign / "budget.db", policies) as budget:
        counts = budget.counts()
    original = EvidenceStore(archive / "evidence.db")
    store = EvidenceStore(campaign / "evidence.db")
    completed = False
    try:
        chunks = tuple(
            TypeAdapter(MarketChunk).validate_json(p.read_bytes())
            for p in sorted(archive.glob("chunk-*.json"))
        )
        validate_archive(original, chunks, config)
        if len(chunks) != len(config.ranges):
            raise ValueError("completed archive required")
        plans = {}
        for source in ("public", "paid"):
            refs = tuple(next(r for r in c.swaps if r.source == source) for c in chunks)
            for ref in refs:
                _copy(store, original, ref)
            plans[source] = SegmentedLogPlan(
                refs, manifest["address"], tuple(manifest["topics"]), 100
            )
        refs = []
        for source, plan in plans.items():
            path = campaign / f"segmented-{source}.json"
            if path.exists():
                ref = TypeAdapter(EvidenceRef).validate_json(path.read_bytes())
                record = strict_json_object(store.get(ref))
                if ref.source != source or record["plan"] != json.loads(
                    canonical_json(asdict(plan))
                ):
                    raise ValueError("retained segmented plan changed")
                replay_segmented_logs(store, ref)
                refs.append(ref)
                continue
            if source not in sources:
                continue
            minimum = minimum_requests(store, plan, source)
            if counts[source] + minimum > manifest["max_requests_per_provider"]:
                raise ValueError("original remaining budget cannot finish this provider")
            recorder = BoundedExchangeRecorder()
            rpc = JsonRpcTransport(
                "https://segmented.invalid",
                retries=0,
                recorder=recorder,
                http=BudgetedPoster(posters[source], campaign / "budget.db", policies, source),
            )
            try:
                ref = collect_segmented_logs(store, plan, (rpc, recorder, source))
                _write(path, asdict(ref))
                refs.append(ref)
            finally:
                if recorder.exchanges:
                    store.put(
                        "lp_action_segmented_attempt",
                        canonical_json({"execution_eligible": False}),
                        None,
                        source,
                        request=wire_envelope(recorder.exchanges, response=False),
                        raw_response=wire_envelope(recorder.exchanges, response=True),
                    )
        if len(refs) != 2:
            return {
                "stage": "partial",
                "evidence": [asdict(ref) for ref in refs],
                "event_counts": {
                    ref.source: len(replay_segmented_logs(store, ref).logs) for ref in refs
                },
                "log_range_complete": False,
                "execution_eligible": False,
            }
        report = _report(store, tuple(refs))
        out = campaign / "segmented-report.json"
        if out.exists() and canonical_json(json.loads(out.read_bytes())) != canonical_json(report):
            raise ValueError("retained segmented report differs from its proofs")
        _write(out, report)
        completed = True
        return report
    finally:
        with RpcBudget(campaign / "budget.db", policies) as budget:
            final_counts = budget.counts()
        _write(
            campaign / "segmented-status.json",
            {
                "stage": "completed" if completed else "partial_or_refused",
                "request_counts": final_counts,
                "remaining_requests": {
                    s: manifest["max_requests_per_provider"] - n for s, n in final_counts.items()
                },
                "log_range_complete": completed,
                "execution_eligible": False,
            },
        )
        _write(
            campaign / "segmented-http-status.json",
            {s: getattr(p, "last_http", None) for s, p in posters.items()},
        )
        original.close()
        store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--campaign", type=Path, required=True)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--source", choices=("public", "paid", "all"), default="all")
    args = parser.parse_args()
    urls = {"public": os.environ["BASE_RPC_PRIMARY"], "paid": os.environ["BASE_RPC_SECONDARY"]}
    for url in urls.values():
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.fragment
        ):
            raise ValueError("HTTPS endpoints without userinfo or fragment required")
    posters = {source: RetainedErrorCurlPoster(url) for source, url in urls.items()}
    hashes = {source: sha256_hex(url.encode()) for source, url in urls.items()}
    try:
        sources = ("public", "paid") if args.source == "all" else (args.source,)
        report = resume(args.campaign, args.archive, posters, hashes, sources=sources)
        print(
            canonical_json(
                {
                    "event_count": report.get("event_count"),
                    "event_counts": report.get("event_counts"),
                    "log_range_complete": report["log_range_complete"],
                    "execution_eligible": False,
                }
            ).decode()
        )
    except ValueError, TypeError, OSError, KeyError, RpcError:
        print(canonical_json({"stage": "refused", "execution_eligible": False}).decode())
        raise SystemExit(3) from None


if __name__ == "__main__":
    main()
