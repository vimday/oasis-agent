"""Run the Base ETH/USDC v4 LP loop in shadow (paper) or live mode.

Shadow never signs. New live actions remain blocked until the implemented
release controls are complete. `--readiness` lists those controls without RPC,
state writes or key access. `--reconcile-only` replays an existing pending
transaction using two providers and never constructs a signer.

Touch `data/live/STOP` to stop after the current tick. A loss-cap breach, a
runtime/USDC implementation change, repeated reverts or an unresolved
submission latch `halted` in the state file; clear it with `--clear-halt`
only after reviewing the cause.
"""

import argparse
import fcntl
import json
import os
import time
from contextlib import ExitStack, closing
from dataclasses import asdict
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory

from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.execution.account_capture import one_shot_session
from oasis_agent.live.authority_socket import TransactionSocketClient
from oasis_agent.live.budget_rpc import BudgetedRpc
from oasis_agent.live.chain import Rpc
from oasis_agent.live.config import LiveConfig
from oasis_agent.live.precheck import check_live
from oasis_agent.live.receipts import RetainedReceiptCostReader
from oasis_agent.live.recovery import adopt_authority_pending, require_recovery_wallets
from oasis_agent.live.rpc_profile import LiveRpcProfile
from oasis_agent.live.runner import LiveLoop
from oasis_agent.live.state import StateStore
from oasis_agent.models.common import canonical_json, sha256_hex
from oasis_agent.reporting.live_release import live_release
from oasis_agent.rpc.budget import RpcBudget

ROOT = Path(__file__).resolve().parents[1]


def receipt_cost_reader(
    config: LiveConfig,
    store: EvidenceStore,
    state_dir: Path,
    primary_url: str,
    secondary_url: str,
    *,
    profile: LiveRpcProfile | None = None,
    require_existing_budget: bool = False,
) -> RetainedReceiptCostReader:
    """Share live endpoints with bounded, raw-recording cost acquisition sessions."""
    if primary_url.rstrip("/") == secondary_url.rstrip("/"):
        raise ValueError("independent receipt providers require distinct endpoints")
    sources = ("live_primary", "live_secondary")
    urls = dict(zip(sources, (primary_url, secondary_url), strict=True))
    policies = (profile or LiveRpcProfile()).policies(sources)
    budget = state_dir / "live.receipt-budget.db"
    # Validate the durable cap before a loop can sign. Restarts never reset it.
    with RpcBudget(budget, policies, require_existing=require_existing_budget):
        pass
    return RetainedReceiptCostReader(
        store,
        chain_id=config.chain_id,
        account=config.account,
        native_decimals=config.decimals0,
        sources=sources,
        sessions=partial(
            one_shot_session,
            urls=urls,
            policies=policies,
            budget=budget,
            require_existing_budget=require_existing_budget,
        ),
    )


def rpc_url(name: str) -> str:
    # Strategy must never read a mixed credential file that may contain wallet
    # keys. Operators/Compose pass only the explicitly required RPC variables.
    value = os.environ.get(name)
    if value is None or not value.strip():
        raise SystemExit("Required RPC environment variable is missing or empty.")
    return value


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--mode", choices=["shadow", "live"], required=True)
    parser.add_argument("--config", type=Path, default=ROOT / "configs/live.base-eth-usdc.json")
    parser.add_argument("--state-dir", type=Path, default=ROOT / "data/live")
    parser.add_argument("--rpc-env", default="BASE_RPC_PRIMARY_ALT")
    parser.add_argument("--signer-socket", type=Path)
    parser.add_argument(
        "--rpc-profile", type=Path, help="explicit RPC pacing; existing budgets stay frozen"
    )
    parser.add_argument("--hours", type=float, help="stop after this many hours")
    parser.add_argument("--ticks", type=int, help="stop after this many ticks")
    parser.add_argument("--clear-halt", action="store_true")
    parser.add_argument("--report", action="store_true")
    parser.add_argument(
        "--check",
        action="store_true",
        help="two-provider check; spends existing durable RPC quota, never signs or saves state",
    )
    parser.add_argument(
        "--acknowledge-flow",
        action="store_true",
        help="treat the next wallet balance change as an external deposit/withdrawal (not P&L)",
    )
    parser.add_argument(
        "--drop-pending",
        action="store_true",
        help="forget a pending submission proven absent from chain (no receipt, nonce unused)",
    )
    parser.add_argument("--readiness", action="store_true", help="offline release blockers; no RPC")
    parser.add_argument(
        "--reconcile-only",
        action="store_true",
        help="reconcile pending once; optional public authority status only",
    )
    args = parser.parse_args()
    if (args.readiness or args.reconcile_only) and (
        args.mode != "live"
        or sum((args.readiness, args.reconcile_only)) != 1
        or any(
            (
                args.clear_halt,
                args.drop_pending,
                args.acknowledge_flow,
                args.report,
                args.check,
                args.signer_socket is not None and not args.reconcile_only,
                args.hours is not None,
                args.ticks is not None,
            )
        )
    ):
        parser.error("readiness/reconcile-only requires live mode without action flags")
    if args.readiness:
        print(live_release().model_dump_json(indent=2))
        raise SystemExit(3)
    # A release failure precedes RPC resolution, lock/state creation and signer construction.
    if args.mode == "live" and not any(
        (
            args.check,
            args.report,
            args.clear_halt,
            args.drop_pending,
            args.acknowledge_flow,
            args.reconcile_only,
        )
    ):
        print(live_release().model_dump_json(indent=2))
        raise SystemExit(3)

    config = LiveConfig.load(args.config)
    profile = LiveRpcProfile.load(args.rpc_profile)
    if args.check:
        if args.mode != "live" or any(
            (
                args.clear_halt,
                args.drop_pending,
                args.acknowledge_flow,
                args.report,
                args.signer_socket is not None,
                args.hours is not None,
                args.ticks is not None,
            )
        ):
            parser.error("--check requires --mode live and cannot be combined with action flags")
        try:
            primary_url, secondary_url = rpc_url(args.rpc_env), rpc_url(config.secondary_rpc_env)
            if primary_url.rstrip("/") == secondary_url.rstrip("/"):
                raise ValueError("duplicate provider")
            with TemporaryDirectory(prefix="oasis-live-check-") as temp:
                directory = Path(temp)
                with closing(EvidenceStore(directory / "evidence.db")) as evidence:
                    reader = receipt_cost_reader(
                        config,
                        evidence,
                        args.state_dir,
                        primary_url,
                        secondary_url,
                        profile=profile,
                        require_existing_budget=True,
                    )
                    report = check_live(
                        config,
                        StateStore(args.state_dir / "live.state.json"),
                        BudgetedRpc(
                            primary_url,
                            args.state_dir,
                            "live_primary",
                            profile,
                            require_existing_budget=True,
                        ),
                        BudgetedRpc(
                            secondary_url,
                            args.state_dir,
                            "live_secondary",
                            profile,
                            require_existing_budget=True,
                        ),
                        stop_file=args.state_dir / "STOP",
                        receipt_costs=reader,
                    )
                    # The diagnostic is ephemeral; do not advertise discarded refs as retained.
                    if "observation" in report:
                        report["observation"]["nav_evidence_ephemeral"] = True
                    report["rpc_budget_persistent"] = True

        except (Exception, SystemExit):  # noqa: BLE001 - redact endpoint/configuration failures
            report = {
                "schema": "oasis.live-operational-precheck.v1",
                "operational_checks_passed": False,
                "execution_eligible": False,
                "signer_contacted": False,
                "blockers": ["precheck_inputs_unresolved"],
            }
        print(json.dumps(report, indent=1))
        if not report["operational_checks_passed"]:
            raise SystemExit(3)
        return
    if (
        args.reconcile_only
        and not StateStore(args.state_dir / "live.state.json").journal_path.is_file()
    ):
        parser.exit(3, "Recovery requires an existing strategy journal.\n")
    args.state_dir.mkdir(parents=True, exist_ok=True)
    # One process per state file: two loops would race on the nonce and the state.
    lock = (args.state_dir / f"{args.mode}.lock").open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit(f"another {args.mode} loop holds {lock.name}") from None
    store = StateStore(args.state_dir / f"{args.mode}.state.json")
    if args.mode == "live" and not args.report:
        store.bind(sha256_hex(canonical_json(asdict(config))), require_existing=args.reconcile_only)
    if args.reconcile_only:
        state = store.load("live")
        if state.pending is None and state.signing_attempt is None:
            print("no pending transaction")
            return
        try:
            # Refuse a lost/fresh quota before any RPC, evidence creation or status adoption.
            with RpcBudget(
                args.state_dir / "live.receipt-budget.db",
                profile.policies(("live_primary", "live_secondary")),
                require_existing=True,
            ):
                pass
        except Exception:  # noqa: BLE001 - fixed diagnostic, no provider or path details
            parser.exit(3, "Recovery RPC budget unresolved; state preserved.\n")
        try:
            require_recovery_wallets(args.state_dir / "live.receipts.db", state, config)
        except Exception:  # noqa: BLE001 - no raw evidence or local path in diagnostics
            parser.exit(3, "Recovery wallet evidence unresolved; state preserved.\n")
        with closing(EvidenceStore(args.state_dir / "live.receipts.db")) as evidence:
            primary_url, secondary_url = rpc_url(args.rpc_env), rpc_url(config.secondary_rpc_env)
            loop = LiveLoop.recovery(
                config,
                BudgetedRpc(
                    primary_url,
                    args.state_dir,
                    "live_primary",
                    profile,
                    require_existing_budget=True,
                ),
                store,
                "live",
            )
            loop.secondary = BudgetedRpc(
                secondary_url,
                args.state_dir,
                "live_secondary",
                profile,
                require_existing_budget=True,
            )
            loop.receipt_costs = receipt_cost_reader(
                config,
                evidence,
                args.state_dir,
                primary_url,
                secondary_url,
                profile=profile,
                require_existing_budget=True,
            )
            try:
                if loop.state.signing_attempt is not None:
                    if args.signer_socket is None:
                        raise ValueError("unresolved signing attempt requires authority status")
                    public = TransactionSocketClient(
                        args.signer_socket, config.account
                    ).pending_status(loop.state.signing_attempt["nonce"])
                    adopt_authority_pending(loop.state, config, public)
                    store.save(loop.state)
                result = loop._resolve_pending() if loop.state.pending else "no pending transaction"
            except Exception:  # noqa: BLE001 - providers can expose credentials
                print("pending reconciliation unresolved; state preserved")
                raise SystemExit(3) from None
            store.save(loop.state)
            print(result)
            if loop.state.pending:
                raise SystemExit(3)
        return
    if args.drop_pending:
        rpc = (
            BudgetedRpc(rpc_url(args.rpc_env), args.state_dir, "live_primary", profile)
            if args.mode == "live"
            else Rpc(rpc_url(args.rpc_env))
        )
        loop = LiveLoop.recovery(config, rpc, store, args.mode)
        print(loop.drop_pending())
        store.save(loop.state)
        return
    if args.acknowledge_flow:
        state = store.load(args.mode)
        state.acknowledge_flow = True
        if state.halted and state.halted.startswith("unexplained wallet change"):
            state.halted = None
        state.record("flow_acknowledged")
        store.save(state)
        print("next wallet balance change will be booked as an external flow")
    if args.clear_halt:
        state = store.load(args.mode)
        print(f"clearing halt: {state.halted}")
        state.halted = None
        state.consecutive_reverts = 0
        store.save(state)
    if args.report or args.clear_halt or args.acknowledge_flow:
        state = store.load(args.mode)
        print(
            json.dumps(
                {
                    "halted": state.halted,
                    "position": state.position and asdict(state.position),
                    "pending": state.pending and asdict(state.pending),
                    "initial_nav_usd": state.initial_nav_usd,
                    "external_flow_usd": state.external_flow_usd,
                    "daily_loss_usd": state.daily_loss_usd,
                    "last_nav_usd": state.last_nav_usd,
                    "gas_spent_eth": state.gas_spent_wei / 1e18,
                    "legacy_gas_cost_incomplete": state.legacy_gas_cost_incomplete,
                    "recent": state.history[-15:],
                },
                indent=1,
                default=str,
            )
        )
        return
    signer = None
    if args.mode == "live":
        if args.signer_socket is None:
            raise SystemExit("--signer-socket is required in live mode")
        signer = TransactionSocketClient(args.signer_socket.expanduser().absolute(), config.account)
    elif args.signer_socket is not None:
        raise SystemExit("shadow mode never signs; drop --signer-socket")
    log_path = args.state_dir / f"{args.mode}.log"

    def log(line: str) -> None:
        stamped = f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {line}"
        print(stamped, flush=True)
        with log_path.open("a") as handle:
            handle.write(stamped + "\n")

    with ExitStack() as resources:
        primary_url = rpc_url(args.rpc_env)
        primary: Rpc = Rpc(primary_url)
        secondary = None
        costs = None
        if args.mode == "live":
            secondary_url = rpc_url(config.secondary_rpc_env)
            evidence = EvidenceStore(args.state_dir / "live.receipts.db")
            resources.callback(evidence.close)
            costs = receipt_cost_reader(
                config,
                evidence,
                args.state_dir,
                primary_url,
                secondary_url,
                profile=profile,
            )
            primary = BudgetedRpc(primary_url, args.state_dir, "live_primary", profile)
            secondary = BudgetedRpc(secondary_url, args.state_dir, "live_secondary", profile)
        loop = LiveLoop(
            config,
            primary,
            store,
            mode=args.mode,
            signer=signer,
            stop_file=args.state_dir / "STOP",
            log=log,
            secondary=secondary,
            receipt_costs=costs,
        )
        until = time.time() + args.hours * 3600 if args.hours else None
        loop.run(max_ticks=args.ticks, until=until)
        print(json.dumps(loop.report(), indent=1, default=str))


if __name__ == "__main__":
    main()
