"""Admission-time reconciliation at the signing request's own block.

The signer's guard must reserve capital against a frame observed at the very block
the transaction will bind to, not against evidence captured at some earlier or
unrelated height. This reader rebuilds that frame on demand: it acquires the
manifest's declared two-provider proofs at the request's block, retains them,
replays them through the evidence-bound reader, and prices the worst-case
commitment. It holds no key, never signs and never broadcasts.

Bounds, and each one fails closed:

- the manifest must declare exactly one chain. One signing request carries exactly
  one block, so a multi-chain frame could not be pinned to it and is refused rather
  than assembled from blocks of different heights;
- the captured frame must land on the request's own block number *and* hash, which
  the evidence-bound reader already enforces, so a reorg, a stale tip or a
  provider disagreement refuses instead of signing against a different state;
- the prior baseline is the caller's own previous frame, so the account is
  reconciled along one lineage rather than against an unrelated snapshot;
- native movement is an explicit operator declaration. An undeclared outflow is
  left unresolved by the accounting layer and blocks the frame; it is never
  inferred from the balance change.

Any capture, replay, valuation or pricing failure raises. The guard denies.
"""

from collections.abc import Callable, Mapping

from ..evidence.store import EvidenceStore
from ..risk.account_risk import AccountFrame, PortfolioFrame
from ..risk.portfolio_manifest import PortfolioManifest
from .account_capture import SessionPairFactory, capture_observation
from .account_risk_reader import EvidenceReplayAccountRiskReader
from .lp_signer_sidecar import LpSidecarRequest

type FrameProvider = Callable[[], PortfolioFrame | None]
"""The admission boundary's own last committed frame, or None before inception."""


def _account(frame: PortfolioFrame, key: str) -> AccountFrame:
    for account in frame.accounts:
        if account.key == key:
            return account
    raise ValueError("previous frame does not cover the signing account")


class CaptureAccountRiskReader:
    """Captures the signing account at the request's block, then replays it."""

    def __init__(
        self,
        store: EvidenceStore,
        manifest: PortfolioManifest,
        *,
        sessions: SessionPairFactory,
        previous: FrameProvider,
        native: Mapping[str, tuple[int, int]] | None = None,
    ) -> None:
        if len(manifest.chains) != 1:
            raise ValueError("one pinned signing block can only reconcile a single-chain manifest")
        declared = dict(native or {})
        if set(declared) - set(manifest.keys):
            raise ValueError("native declarations must name manifest accounts")
        for units in declared.values():
            if (
                not isinstance(units, tuple)
                or len(units) != 2
                or any(type(unit) is not int or unit < 0 for unit in units)
            ):
                raise ValueError("native declarations must be nonnegative unit pairs")
        self.store = store
        self.manifest = manifest
        self.sessions = sessions
        self.previous = previous
        self.native = declared

    def reconcile_and_price(self, request: LpSidecarRequest) -> tuple[PortfolioFrame, int]:
        """Rebuild the frame at the request block and price its worst-case commitment."""
        signing = request.signing
        key = f"{signing.intent.chain_id}:{signing.account}"
        entry = self.manifest.chain(key)
        baseline = self.previous()
        prior = None if baseline is None else _account(baseline, key)
        gas_units, transfer_units = self.native.get(key, (0, 0))
        observation = capture_observation(
            self.store,
            entry,
            block=signing.current_block.number,
            previous=prior,
            native_gas_units=gas_units,
            non_trading_native_units=transfer_units,
            sessions=self.sessions,
        )
        reader = EvidenceReplayAccountRiskReader(
            self.store, self.manifest, {entry.key: observation}, previous=baseline
        )
        return reader.reconcile_and_price(request)


__all__ = ["CaptureAccountRiskReader", "FrameProvider"]
