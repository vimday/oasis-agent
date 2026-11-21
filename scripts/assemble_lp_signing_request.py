"""Assemble one signer request from retained proofs; never sign and never broadcast.

The frozen signer gate refuses every request unless a pinned simulation, a fresh
execution preflight and exactly one risk decision are all retained evidence whose
bytes re-derive to the hashes it checks. Nothing produced that bundle, so a real
intent could never reach a signer. This CLI produces it: it re-captures the
manifest's declared two-provider account proofs at the intent's own state block,
prices the intent's worst case into a retained risk decision, collects a fresh
two-provider preflight head, assembles the three into one request and reports the
gate's own verdict.

The gas envelope is never a free parameter. It is exactly the fee caps the sidecar
config declares, and its gas limit is the one the plan's own intent simulated, so
the retained decision cannot describe a cheaper transaction than the one a signer
would be handed. Every reference must already exist in the evidence store, and the
plan, the preflight and the decision must all describe the same intent, chain,
account and state block.

A denied bundle writes no request file at all: an unauthorized request never
becomes a path a signer could be pointed at. This CLI holds no key, cannot sign,
cannot broadcast and cannot reserve capital, and `execution_eligible` stays false.
"""

import argparse
from pathlib import Path
from typing import NoReturn

from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.execution.account_capture import capture_observation
from oasis_agent.execution.account_risk_reader import EvidenceReplayAccountRiskReader
from oasis_agent.execution.lp_preflight import (
    collect_lp_preflight_quorum,
    persist_lp_preflight_evidence,
)
from oasis_agent.execution.lp_risk_decision import decide_lp_risk, persist_lp_risk_decision
from oasis_agent.execution.lp_signer_sidecar import (
    sidecar_limits_from_json,
    sidecar_request_hash,
    sidecar_request_to_json,
)
from oasis_agent.execution.lp_signing_request import (
    assemble_lp_signing_request,
    parse_prepared_simulation,
)
from oasis_agent.models.common import canonical_json
from oasis_agent.risk.account_risk import RiskState
from oasis_agent.risk.portfolio_manifest import PortfolioManifest
from oasis_agent.rpc.budget import BudgetPolicy
from scripts.run_account_risk_reconcile import (
    _account,
    _bind_blocks,
    _bind_endpoints,
    _bind_state,
    _budget_policies,
    _environment,
    _native_declarations,
    _pairs,
    _prior_state,
    _rpc_map,
    _session_factory,
    _sessions,
)

type Declared = tuple[
    PortfolioManifest,
    dict[int, int],
    dict[str, str],
    dict[str, tuple[int, int]],
    RiskState,
    tuple[BudgetPolicy, ...],
]


def _configuration(args: argparse.Namespace) -> Declared:
    """Read the declared policy, providers and blocks without touching a network."""
    manifest = PortfolioManifest.model_validate_json(args.manifest.read_bytes())
    if len(manifest.chains) != 1:
        # One request reconciles one block, so a multi-chain manifest cannot produce one.
        raise ValueError("one signing request reconciles exactly one manifest chain")
    blocks = _bind_blocks(
        manifest, {int(key): int(value) for key, value in _pairs(args.block, "block").items()}
    )
    endpoints = _bind_endpoints(manifest, _rpc_map(args.rpc))
    if not 3000 <= args.interval_ms <= 60000:
        raise ValueError("minimum request interval is three seconds")
    names = {name for pair in endpoints.values() for name in pair}
    urls = _environment(args.env_file, names)
    state = _bind_state(manifest, _prior_state(args.state))
    if state.frame is None and args.native is not None:
        raise ValueError("native declarations require a previous state")
    native = _native_declarations(args.native, manifest)
    policies = _budget_policies(names, max_requests=args.max_requests, interval_ms=args.interval_ms)
    return manifest, blocks, urls, native, state, policies


def _deny(parser: argparse.ArgumentParser, payload: dict[str, object]) -> NoReturn:
    """Report a refusal without writing anything a signer could consume."""
    parser.exit(3, canonical_json({**payload, "execution_eligible": False}).decode() + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--budget", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--rpc", action="append", default=[], metavar="CHAIN:PRIMARY:SECONDARY")
    parser.add_argument(
        "--block", action="append", default=[], metavar="CHAIN=NUMBER", help="fresh preflight head"
    )
    parser.add_argument(
        "--native", type=Path, help="declared native gas/transfer units per account"
    )
    parser.add_argument("--state", type=Path, help="previous risk state for a continuous frame")
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--interval-ms", type=int, default=3000)
    parser.add_argument("--max-requests", type=int, default=256)
    args = parser.parse_args()

    try:
        manifest, blocks, urls, native, state, policies = _configuration(args)
        policy, max_fee_per_gas, max_priority_fee_per_gas = sidecar_limits_from_json(
            args.config.read_bytes()
        )
        prepared = parse_prepared_simulation(args.plan.read_bytes())
        entry = manifest.chains[0]
    except OSError, TypeError, ValueError, KeyError:
        parser.exit(1, "Invalid assembly configuration; no signing is available.\n")

    intent = prepared.plan.intent
    if entry.chain_id != intent.chain_id or blocks[entry.chain_id] <= intent.state_block:
        parser.exit(1, "The preflight head must be a later block on the intent's own chain.\n")

    store = EvidenceStore(args.evidence)
    sessions = _session_factory(urls=urls, policies=policies, budget=args.budget)
    try:
        observation = capture_observation(
            store,
            entry,
            block=intent.state_block,
            previous=_account(state.frame, entry.key),
            native_gas_units=native[entry.key][0],
            non_trading_native_units=native[entry.key][1],
            sessions=sessions,
        )
    except Exception:  # noqa: BLE001 - RPC errors may echo endpoint URLs
        store.close()
        parser.exit(2, "Account capture failed; retained proofs are incomplete.\n")

    try:
        reader = EvidenceReplayAccountRiskReader(
            store, manifest, {entry.key: observation}, previous=state.frame
        )
        decided = decide_lp_risk(
            reader,
            intent=intent,
            account=entry.account,
            calldata=prepared.plan.calldata,
            simulation_block=prepared.block,
            simulation_evidence=prepared.evidence,
            gas_limit=intent.max_gas_units,
            max_fee_per_gas=max_fee_per_gas,
            max_priority_fee_per_gas=max_priority_fee_per_gas,
        )
        decision_reference = persist_lp_risk_decision(store, decided)
        quorum = collect_lp_preflight_quorum(
            *_sessions(
                sources=(entry.primary_source, entry.secondary_source),
                urls=urls,
                policies=policies,
                budget=args.budget,
            ),
            policy=policy,
            block_number=blocks[entry.chain_id],
        )
        preflight = persist_lp_preflight_evidence(store, quorum)
        request, verdict = assemble_lp_signing_request(
            store,
            policy=policy,
            manifest=manifest,
            prepared=prepared,
            preflight=preflight,
            decision_reference=decision_reference,
            max_fee_per_gas=max_fee_per_gas,
            max_priority_fee_per_gas=max_priority_fee_per_gas,
        )
    except Exception as error:  # noqa: BLE001 - our own bounded messages only
        store.close()
        _deny(parser, {"outcome": "refused", "reason": str(error)})

    store.close()
    if not verdict.authorized:
        _deny(
            parser,
            {
                "outcome": "denied",
                "risk_reasons": list(decided.reasons),
                "reasons": list(verdict.reasons),
            },
        )
    try:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        with args.out.open("xb") as output:
            output.write(sidecar_request_to_json(request) + b"\n")
    except OSError:
        parser.exit(2, "The request could not be written; nothing is offered to a signer.\n")
    print(
        canonical_json(
            {
                "outcome": "requested",
                "request_hash": sidecar_request_hash(request),
                "authorized": True,
                "execution_eligible": False,
            }
        ).decode()
    )


if __name__ == "__main__":
    main()
