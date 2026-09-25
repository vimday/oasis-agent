"""WP7 operator CLI for the isolated LP signer sidecar.

This CLI holds no key material. With no `--signer-socket` it runs the static
policy and fee caps and journals a denial recording that no key authority is
wired. With a socket it asks a *separate* signing authority
(`scripts/run_lp_signer_daemon.py`) for the exact EIP-1559 digest only, then
verifies the signature by recovering the signer address; the authority can be an
owner-only local process, an HSM, or a remote service.

With `--risk-manifest` it also wires the independent admission boundary: a
cross-chain loss/capital ledger (`GlobalRiskJournal`) fronted by a reader that
re-captures the declared two-provider proofs at the signing request's own block
before any key authority is called. Both capabilities are required for a
signature: without either, the sidecar denies and no signature exists.

The reader's bounds are literal. One manifest chain only, because one signing
request carries one block. Any declared position must already be owned and
declared at the reconciled block, so the reference deployment mints the first
canary position out of band, declares it, and only then opens the admission
ledger; starting the ledger afterwards is what keeps its day and cumulative loss
history from beginning mid-life. Native gas spent by an earlier signed
transaction must be declared explicitly (`--risk-native`), because an undeclared
native change stays unresolved and blocks the next frame.

Producing a signature is never an execution permit, and this CLI never broadcasts.
"""

import argparse
import json
from pathlib import Path

from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.execution.account_risk_guard import AccountRiskGuard
from oasis_agent.execution.live_account_reader import CaptureAccountRiskReader, FrameProvider
from oasis_agent.execution.lp_signer_sidecar import (
    SIDE_CAR_DECISION_SCHEMA,
    LpSignerSidecar,
    sidecar_config_from_json,
    sidecar_request_from_json,
)
from oasis_agent.execution.socket_key_provider import SocketKeyProvider
from oasis_agent.models.common import canonical_json
from oasis_agent.risk.account_risk import GlobalRiskJournal, PortfolioFrame
from oasis_agent.risk.portfolio_manifest import PortfolioManifest
from scripts.run_account_risk_reconcile import (
    _bind_endpoints,
    _budget_policies,
    _environment,
    _native_declarations,
    _rpc_map,
    _session_factory,
)

_RISK_OPTIONS = (
    "risk_journal",
    "risk_store",
    "risk_budget",
    "risk_rpc",
    "risk_native",
)


def _key_authority(args: argparse.Namespace) -> SocketKeyProvider | None:
    if args.signer_socket is None and args.signer_account is None:
        return None
    if args.signer_socket is None or args.signer_account is None:
        raise ValueError("a signer socket needs its account")
    return SocketKeyProvider(
        args.signer_socket, args.signer_account, timeout_ms=args.signer_timeout_ms
    )


def _risk_guard(
    args: argparse.Namespace, store: EvidenceStore | None
) -> tuple[AccountRiskGuard, GlobalRiskJournal] | None:
    """Build the independent admission boundary, or nothing when it is not asked for."""
    if args.risk_manifest is None:
        if any(getattr(args, name) for name in _RISK_OPTIONS):
            raise ValueError("admission options require a risk manifest")
        return None
    if store is None:
        raise ValueError("a risk manifest needs its retained evidence store")
    for name in ("risk_journal", "risk_budget"):
        if getattr(args, name) is None:
            raise ValueError("a risk manifest needs its journal and budget ledger")
    manifest = PortfolioManifest.model_validate_json(args.risk_manifest.read_bytes())
    endpoints = _bind_endpoints(manifest, _rpc_map(args.risk_rpc))
    if not 3000 <= args.risk_interval_ms <= 60000:
        raise ValueError("minimum request interval is three seconds")
    names = {name for pair in endpoints.values() for name in pair}
    urls = _environment(args.risk_env_file, names)
    policies = _budget_policies(
        names, max_requests=args.risk_max_requests, interval_ms=args.risk_interval_ms
    )
    journal = GlobalRiskJournal(args.risk_journal, manifest.policy)
    try:
        reader = CaptureAccountRiskReader(
            store,
            manifest,
            sessions=_session_factory(urls=urls, policies=policies, budget=args.risk_budget),
            previous=_frame_provider(journal),
            native=_native_declarations(args.risk_native, manifest),
        )
    # A refused reader would otherwise leave an opened ledger behind.
    except Exception:
        journal.close()
        raise
    return AccountRiskGuard(journal, reader), journal


def _frame_provider(journal: GlobalRiskJournal) -> FrameProvider:
    """The ledger's own last committed frame: one lineage, never an unrelated snapshot."""

    def current() -> PortfolioFrame | None:
        return journal.current_state().frame

    return current


def _add_risk_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--risk-manifest", type=Path, default=None)
    parser.add_argument("--risk-journal", type=Path, default=None)
    parser.add_argument("--risk-store", type=Path, default=None)
    parser.add_argument("--risk-budget", type=Path, default=None)
    parser.add_argument(
        "--risk-rpc", action="append", default=[], metavar="CHAIN:PRIMARY:SECONDARY"
    )
    parser.add_argument("--risk-env-file", type=Path, default=Path(".env"))
    parser.add_argument("--risk-native", type=Path, default=None)
    parser.add_argument("--risk-interval-ms", type=int, default=3000)
    parser.add_argument("--risk-max-requests", type=int, default=256)


def _write_decision(path: Path, decision) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        canonical_json(
            {
                "schema": SIDE_CAR_DECISION_SCHEMA,
                "outcome": decision.outcome,
                "reasons": list(decision.reasons),
                "request_hash": decision.request_hash,
                "digest": decision.digest,
                "signature_hash": decision.signature_hash,
                "transaction_hash": decision.transaction_hash,
                "raw_transaction_hex": decision.raw_transaction_hex,
            }
        )
        + b"\n"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--journal", type=Path, default=None)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--signer-socket", type=Path, default=None)
    parser.add_argument("--signer-account", default=None)
    parser.add_argument("--signer-timeout-ms", type=int, default=3000)
    _add_risk_arguments(parser)
    args = parser.parse_args()

    journal = args.journal or args.out.with_suffix(".db")
    store: EvidenceStore | None = None
    risk: tuple[AccountRiskGuard, GlobalRiskJournal] | None = None
    try:
        config_bytes = args.config.read_bytes()
        request_bytes = args.request.read_bytes()
        if args.risk_store is not None:
            store = EvidenceStore(args.risk_store)
        risk = _risk_guard(args, store)
        config = sidecar_config_from_json(
            config_bytes,
            journal_path=journal,
            provider=_key_authority(args),
            risk_guard=None if risk is None else risk[0],
        )
        request = sidecar_request_from_json(request_bytes)
    except (OSError, TypeError, ValueError) as error:
        if risk is not None:
            risk[1].close()
        if store is not None:
            store.close()
        parser.exit(1, f"Invalid sidecar configuration or request: {error}\n")
    sidecar = LpSignerSidecar(config)
    try:
        decision = sidecar.handle(request)
    finally:
        sidecar.journal.close()
        if risk is not None:
            risk[1].close()
        if store is not None:
            store.close()
    _write_decision(args.out, decision)
    print(json.dumps({"outcome": decision.outcome, "reasons": list(decision.reasons)}))


if __name__ == "__main__":
    main()
