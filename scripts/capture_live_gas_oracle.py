"""Plan or explicitly acquire one bounded Base gas-oracle discovery quorum.

Default mode is offline. Capture requires a separate exact-plan zero-cost grant,
the original stopped service volumes and their existing budgets. No key, journal
migration, configuration application, service start, signature or broadcast exists
here. Successful discovery proposes observed fingerprints; it never approves them.
"""

import argparse
import json
import os
import time
from contextlib import closing, contextmanager
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlsplit

from oasis_agent.evidence.exact_capture import decode_header, wire_envelope
from oasis_agent.evidence.rpc_validation import restore_exchanges, rpc_quantity, strict_json_object
from oasis_agent.evidence.store import EvidenceStore, ReadOnlyEvidenceStore
from oasis_agent.live.config import LiveConfig
from oasis_agent.live.gas_oracle import (
    LEGACY_PIN_NAMES,
    ORACLE,
    implementation_address,
    runtime_hash,
)
from oasis_agent.live.gas_oracle_evidence import (
    candidate_configuration,
    discovery_requests,
    retain_discovery_quorum,
)
from oasis_agent.live.rpc_profile import LiveRpcProfile
from oasis_agent.models.common import (
    BlockRef,
    canonical_json,
    evidence_ref_from_json,
    sha256_hex,
)
from oasis_agent.rpc.budget import RpcBudget
from scripts import capture_live_recovery as recovery
from scripts.review_live_gas_oracle import _read

MAX_REQUESTS = 13
PUBLICATIONS = ("gas-wire.json", "candidate.json")


@contextmanager
def prepared(strategy, authority, config, profile):
    """Keep original recovery locks and checkpoints; derive a distinct grant."""
    if set(config.expected_pins) != LEGACY_PIN_NAMES:
        raise ValueError("discovery requires the original five-pin configuration")
    with recovery.prepared(strategy, authority, config, profile) as base:
        original, _, _, budgets, verify = base
        plan = deepcopy(original)
        plan.pop("plan_hash")
        plan.update(
            schema="oasis.live-gas-oracle-capture-plan.v1",
            purpose="gas_oracle_discovery_only",
            original_recovery_plan_hash=original["plan_hash"],
            request_limits={
                "strategy": dict(zip(recovery.SOURCES, (7, 6), strict=True)),
                "authority": dict.fromkeys(recovery.SOURCES, 0),
            },
            total_requests=MAX_REQUESTS,
            methods_per_provider_per_service={
                "strategy": {
                    "per_provider": {
                        "eth_chainId": 1,
                        "eth_getBlockByNumber": 2,
                        "eth_getCode": 2,
                        "eth_getStorageAt": 1,
                    },
                    "extra_primary_head_lookup": 1,
                },
                "authority": {},
            },
            oracle=ORACLE,
            implementation_target="derived_only_from_each_source_pinned_storage_reply",
            runtime_approved=False,
            configuration_applied=False,
        )
        plan["plan_hash"] = sha256_hex(canonical_json(plan))
        yield plan, budgets, verify


def _wire_scope(wire):
    """Re-derive both complete raw captures before retaining a proposal."""
    if set(wire) != {"block", "providers"}:
        raise ValueError("exact discovery wire scope required")
    block = BlockRef(**wire["block"])
    providers = wire["providers"]
    if (
        not isinstance(providers, list)
        or len(providers) != 2
        or any(
            not isinstance(p, dict) or set(p) != {"source", "request", "response"}
            for p in providers
        )
        or tuple(p["source"] for p in providers) != recovery.SOURCES
    ):
        raise ValueError("declared exact provider pair required")
    pair = tuple(
        restore_exchanges(canonical_json(p["request"]), canonical_json(p["response"]))
        for p in providers
    )
    return block, providers, pair


def _candidate(wire, config, evidence, *, now):
    block, _, pair = _wire_scope(wire)
    refs = retain_discovery_quorum(evidence, block, pair, sources=recovery.SOURCES)
    return candidate_configuration(evidence, refs, block, config, sources=recovery.SOURCES, now=now)


def capture(
    strategy,
    authority,
    config,
    profile,
    out,
    approved_hash,
    scope_path,
    *,
    clock=time.time,
    monotonic=time.monotonic,
    sleep=None,
    transport=recovery.BoundedPoster,
):
    """Requires recovery.supervise at every network-capable CLI boundary."""
    if any(out.absolute().is_relative_to(root.absolute()) for root in (strategy, authority)):
        raise ValueError("discovery output must be outside original service volumes")
    out.mkdir(mode=0o700, parents=False, exist_ok=False)
    report = {
        "schema": "oasis.live-gas-oracle-capture.v1",
        "execution_eligible": False,
        "runtime_approved": False,
        "configuration_applied": False,
        "capture_passed": False,
        "exit_code": 3,
        "request_counts": None,
    }
    control = None
    try:
        with prepared(strategy, authority, config, profile) as (plan, budgets, verify):
            report["plan"] = plan
            scope = recovery._scope(scope_path, approved_hash, plan, clock())
            report["cost_scope_hash"] = sha256_hex(canonical_json(scope))
            report["cost_scope_status"] = "operator_declaration_not_provider_billing_verification"
            urls = {
                source: os.environ.get(env, "")
                for source, env in zip(recovery.SOURCES, recovery.RPC_ENV, strict=True)
            }
            if len({urlsplit(url).hostname for url in urls.values()}) != 2 or any(
                urlsplit(url).scheme != "https"
                or not urlsplit(url).hostname
                or urlsplit(url).username is not None
                or urlsplit(url).password is not None
                or urlsplit(url).fragment
                or sha256_hex(url.encode()) != scope["endpoint_hashes"][source]
                for source, url in urls.items()
            ):
                raise ValueError("declared independent query endpoints unresolved")
            (out / "plan.json").write_bytes(canonical_json(plan))
            control = recovery.CaptureControl(
                plan, budgets, profile, out, clock=clock, monotonic=monotonic, sleep=sleep
            )
            control.deadline = min(control.deadline, scope["valid_until_ms"] / 1000)
            head_rpc, _, _ = control.session("strategy", recovery.SOURCES[0], urls, transport)
            head_header = decode_header(
                head_rpc.request("eth_getBlockByNumber", ["latest", False]),
                chain_id=config.chain_id,
            )
            head = head_header.ref
            if head.timestamp is None or not 0 <= clock() - head.timestamp <= 120:
                raise ValueError("fresh pinned head required")
            control.deadline = min(control.deadline, head.timestamp + 120)
            providers, observed = [], []
            for source in recovery.SOURCES:
                verify()
                rpc, recorder, _ = control.session("strategy", source, urls, transport)
                # Only these four templates precede discovery of the implementation.
                prefix = discovery_requests(head, ORACLE)[:4]
                values = []
                for index, (method, params) in enumerate(prefix):
                    value = rpc.request(method, params)
                    if index == 0 and rpc_quantity(value) != config.chain_id:
                        raise ValueError("discovery chain differs")
                    if index == 1 and decode_header(value, chain_id=config.chain_id) != head_header:
                        raise ValueError("discovery header differs")
                    if index == 2:
                        runtime_hash(value)
                    if observed and (
                        (index == 2 and runtime_hash(value) != runtime_hash(observed[0][2]))
                        or (index == 3 and value != observed[0][3])
                    ):
                        raise ValueError("independent oracle code or storage disagrees")
                    values.append(value)
                implementation = implementation_address(values[3])
                tail = discovery_requests(head, implementation)[4:]
                implementation_code = rpc.request(*tail[0])
                runtime_hash(implementation_code)
                if observed and runtime_hash(implementation_code) != runtime_hash(observed[0][4]):
                    raise ValueError("independent implementation code disagrees")
                closing_header = decode_header(rpc.request(*tail[1]), chain_id=config.chain_id)
                if closing_header != head_header:
                    raise ValueError("discovery header changed")
                observed.append([*values, implementation_code])
                providers.append(
                    {
                        "source": source,
                        "request": wire_envelope(recorder.exchanges, response=False),
                        "response": wire_envelope(recorder.exchanges, response=True),
                    }
                )
            wire = {"block": asdict(head), "providers": providers}
            # Only this new attempt's evidence DB is written; production evidence
            # stores remain under the original recovery boundary's read-only path.
            with closing(EvidenceStore(out / "evidence.db")) as evidence:
                candidate = _candidate(wire, config, evidence, now=int(clock()))
            verify()
            control.remaining()
            if control.counts != plan["request_limits"]:
                raise ValueError("exact thirteen-read scope required")
            for role in recovery.ROLES:
                with RpcBudget(
                    budgets[role], profile.policies(recovery.SOURCES), read_only=True
                ) as q:
                    if q.counts() != {
                        source: plan["services"][role]["budget_counts"][source]
                        + control.counts[role][source]
                        for source in recovery.SOURCES
                    }:
                        raise ValueError("original budget changed outside this attempt")
            raw = canonical_json(wire)
            (out / "candidate-gas-wire.json").write_bytes(raw)
            (out / "candidate-proposal.json").write_bytes(canonical_json(candidate))
            report.update(
                capture_passed=True,
                exit_code=0,
                status="observed_candidate_requires_review",
                wire_hash=sha256_hex(raw),
                candidate_hash=sha256_hex(canonical_json(candidate)),
            )
    except Exception:  # noqa: BLE001 - never echo endpoints or provider bodies
        if control is not None:
            control.cancelled.set()
        report["refusal"] = "discovery scope, transport, freshness or quorum unresolved; no retry"
    finally:
        if control is not None:
            report["request_counts"] = control.counts
            control.wire.close()
        report["evidence_hash"] = sha256_hex(canonical_json(report))
        (out / "report.json").write_bytes(canonical_json(report))
    return report


def publications(out, worker, config, *, approved_hash=None):
    """The parent replays raw bytes after the bounded worker has exited zero."""
    summary = dict(worker)
    digest = summary.pop("evidence_hash")
    raw = _read(out / "candidate-gas-wire.json", 8_000_000)
    proposal_raw = _read(out / "candidate-proposal.json", 65_536)
    plan = dict(worker["plan"])
    plan_hash = plan.pop("plan_hash")
    if (
        sha256_hex(canonical_json(summary)) != digest
        or sha256_hex(canonical_json(plan)) != plan_hash
        or (approved_hash is not None and approved_hash != plan_hash)
        or plan["schema"] != "oasis.live-gas-oracle-capture-plan.v1"
        or plan["config_hash"] != sha256_hex(canonical_json(asdict(config)))
        or plan["total_requests"] != MAX_REQUESTS
        or plan["max_seconds"] != recovery.MAX_SECONDS
        or plan["retries"] != 0
        or plan["transport_profile"] != recovery.TRANSPORT_PROFILE
        or plan["request_limits"]
        != {
            "strategy": dict(zip(recovery.SOURCES, (7, 6), strict=True)),
            "authority": dict.fromkeys(recovery.SOURCES, 0),
        }
        or len(raw) > 8_000_000
        or sha256_hex(raw) != worker["wire_hash"]
        or sha256_hex(proposal_raw) != worker["candidate_hash"]
        or worker["runtime_approved"] is not False
        or worker["execution_eligible"] is not False
        or worker["configuration_applied"] is not False
        or worker["request_counts"] != worker["plan"]["request_limits"]
    ):
        raise ValueError("complete bounded discovery report required")
    wire, proposal = strict_json_object(raw), strict_json_object(proposal_raw)
    block, providers, _ = _wire_scope(wire)
    refs = tuple(evidence_ref_from_json(ref) for ref in proposal["evidence"])
    with closing(ReadOnlyEvidenceStore(out / "evidence.db")) as evidence:
        expected = candidate_configuration(
            evidence, refs, block, config, sources=recovery.SOURCES, now=proposal["reviewed_at"]
        )
        if canonical_json(expected) != proposal_raw:
            raise ValueError("candidate differs from retained raw proof")
        for ref, provider in zip(refs, providers, strict=True):
            record = evidence.get_record(ref)
            if (
                ref.source != provider["source"]
                or record.request_payload != canonical_json(provider["request"])
                or record.raw_response_payload != canonical_json(provider["response"])
            ):
                raise ValueError("discovery wire differs from retained proof")
        candidate_configuration(
            evidence, refs, block, config, sources=recovery.SOURCES, now=int(time.time())
        )
    return {"gas-wire.json": raw, "candidate.json": proposal_raw}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--strategy-dir", type=Path, required=True)
    parser.add_argument("--authority-dir", type=Path, required=True)
    parser.add_argument(
        "--config", type=Path, default=recovery.ROOT / "configs/live.base-eth-usdc.json"
    )
    parser.add_argument(
        "--rpc-profile", type=Path, default=recovery.ROOT / "configs/live.rpc.canary.json"
    )
    parser.add_argument("--capture", action="store_true")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--approved-plan-hash")
    parser.add_argument("--zero-cost-scope", type=Path)
    args = parser.parse_args()
    supplied = (args.out, args.approved_plan_hash, args.zero_cost_scope)
    if (args.capture and any(v is None for v in supplied)) or (
        not args.capture and any(v is not None for v in supplied)
    ):
        parser.error(
            "capture requires exact plan, zero-cost scope and new output; plan accepts none"
        )
    try:
        config, profile = LiveConfig.load(args.config), LiveRpcProfile.load(args.rpc_profile)
        if args.capture:
            report = recovery.supervise(
                lambda: capture(
                    args.strategy_dir,
                    args.authority_dir,
                    config,
                    profile,
                    args.out,
                    args.approved_plan_hash,
                    args.zero_cost_scope,
                ),
                args.out,
                publisher=lambda out, worker: publications(
                    out, worker, config, approved_hash=args.approved_plan_hash
                ),
                publication_names=PUBLICATIONS,
            )
        else:
            with prepared(args.strategy_dir, args.authority_dir, config, profile) as scope:
                report = scope[0]
        print(json.dumps(report, indent=2))
        raise SystemExit(report.get("exit_code", 0))
    except Exception:  # noqa: BLE001 - fixed redacted refusal
        parser.exit(3, "Gas discovery unresolved; no configuration or execution authorized.\n")


if __name__ == "__main__":
    main()
