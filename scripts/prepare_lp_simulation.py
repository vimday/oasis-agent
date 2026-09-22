"""Prepare a bounded LP simulation report from two RPCs; never sign or broadcast."""

import argparse
import json
import os
import sqlite3
from dataclasses import asdict
from pathlib import Path

from oasis_agent.errors import OasisError
from oasis_agent.evidence.rpc_capture import BoundedExchangeRecorder
from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.execution.lp_preparation import LpPlanBounds, build_lp_simulation_plan
from oasis_agent.execution.lp_signer_policy import LpSignerPolicy
from oasis_agent.execution.lp_simulation import simulate_lp_plan
from oasis_agent.execution.lp_state_machine import RuntimePin, RuntimeRole
from oasis_agent.farming.position_quorum import collect_position_quorum
from oasis_agent.models.common import canonical_json
from oasis_agent.rpc.transport import JsonRpcTransport
from oasis_agent.shadow.journal import ShadowJournal
from oasis_agent.shadow.service import LpCycle, _unique_object


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("config", "cycle", "journal", "evidence", "report"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument(
        "--now", type=int, required=True, help="explicit preparation UTC epoch seconds"
    )
    parser.add_argument(
        "--simulate", action="store_true", help="run two hash-pinned eth_call simulations"
    )
    args = parser.parse_args(argv)
    store = None
    journal = None
    try:
        config = json.loads(args.config.read_bytes(), object_pairs_hook=_unique_object)
        expected = {
            "policy",
            "bounds",
            "position_manager",
            "state_view",
            "token_id",
            "block_number",
            "shadow_max_age_seconds",
        }
        if not isinstance(config, dict) or set(config) != expected:
            raise ValueError("invalid preparation configuration")
        config["policy"]["runtime_pins"] = tuple(
            RuntimePin(RuntimeRole(pin["role"]), pin["address"], pin["code_hash"])
            for pin in config["policy"]["runtime_pins"]
        )
        policy = LpSignerPolicy(**config["policy"])
        bounds = LpPlanBounds(**config["bounds"])
        cycle = LpCycle.from_json(args.cycle.read_bytes())
        if args.report.exists():
            raise ValueError("report already exists")
        urls = (
            os.environ.get("OASIS_LP_RPC_PRIMARY", ""),
            os.environ.get("OASIS_LP_RPC_SECONDARY", ""),
        )
        if not all(urls) or urls[0] == urls[1]:
            raise ValueError("distinct explicit RPC endpoints required")
        recorders = (BoundedExchangeRecorder(), BoundedExchangeRecorder())
        sessions = tuple(
            (JsonRpcTransport(url, timeout=10, retries=0, recorder=recorder), recorder, source)
            for url, recorder, source in zip(urls, recorders, ("primary", "secondary"), strict=True)
        )
        journal = ShadowJournal(
            args.journal,
            chain_id=policy.chain_id,
            max_age_seconds=config["shadow_max_age_seconds"],
            read_only=True,
        )
        positions = collect_position_quorum(
            sessions[0],
            sessions[1],
            chain_id=policy.chain_id,
            position_manager=config["position_manager"],
            state_view=config["state_view"],
            token_id=config["token_id"],
            block_number=config["block_number"],
        )
        store = EvidenceStore(args.evidence)
        plan = build_lp_simulation_plan(
            store, journal, cycle, positions, policy, bounds, now=args.now
        )
        report_payload = plan.payload
        if args.simulate:
            simulation_recorders = (BoundedExchangeRecorder(), BoundedExchangeRecorder())
            simulation_sessions = tuple(
                (JsonRpcTransport(url, timeout=10, retries=0, recorder=recorder), recorder, source)
                for url, recorder, source in zip(
                    urls, simulation_recorders, ("primary", "secondary"), strict=True
                )
            )
            simulation = simulate_lp_plan(
                store, plan, simulation_sessions[0], simulation_sessions[1]
            )
            report = json.loads(plan.payload)
            report["simulation"] = {
                "plan_hash": simulation.plan_hash,
                "evidence": [asdict(ref) for ref in simulation.evidence],
                "execution_eligible": False,
            }
            report["unresolved"].remove("pinned_simulation")
            report_payload = canonical_json(report)
        with args.report.open("xb") as output:
            output.write(report_payload + b"\n")
        print(
            "LP pinned simulation retained; execution_eligible=false"
            if args.simulate
            else "LP simulation plan retained; execution_eligible=false"
        )
        return 0
    except (ValueError, TypeError, KeyError, OSError, sqlite3.Error, OasisError):
        parser.exit(1, "LP preparation failed; details redacted; no execution authorized.\n")
    finally:
        if journal is not None:
            journal.close()
        if store is not None:
            store.close()


if __name__ == "__main__":
    raise SystemExit(main())
