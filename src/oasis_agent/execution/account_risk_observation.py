"""Independent retained-proof observations into the existing global loss ledger.

No network, keys, reservation release, execution-state mutation or authorization.
The opening proof must describe the ledger's actual baseline, never a new NAV
origin chosen by the caller. Complete gas is mandatory at every closing account.
"""

from typing import Literal

from pydantic import Field

from ..evidence.store import EvidenceStore
from ..launch.models import StrictModel
from ..models.common import canonical_json, sha256_hex
from ..risk.account_risk import GlobalRiskJournal, PortfolioFrame, RiskState
from ..risk.portfolio_manifest import PortfolioManifest
from .account_risk_reader import AccountObservation, EvidenceReplayAccountRiskReader


class RiskObservationRequest(StrictModel):
    schema_version: Literal["oasis.risk-observation.v1"] = Field(alias="schema")
    manifest: PortfolioManifest
    opening: dict[str, AccountObservation]
    closing: dict[str, AccountObservation]


def _same_inventory(left: PortfolioFrame, right: PortfolioFrame) -> bool:
    # Flows describe the preceding interval, not the boundary inventory. The
    # opening reconstruction has zero flows; the stored boundary can have flows
    # from the prior accepted interval. Every other field must match exactly.
    excluded = {"movements", "non_trading_flow_usd_micros"}
    return {a.key: a.model_dump(exclude=excluded) for a in left.accounts} == {
        a.key: a.model_dump(exclude=excluded) for a in right.accounts
    }


def observe_account_risk(
    store: EvidenceStore,
    journal: GlobalRiskJournal,
    request: RiskObservationRequest,
    *,
    now: int,
) -> tuple[RiskState, str]:
    """Replay full raw inventory/cash/gas before the single atomic ledger update.

    Existing gross losses, latches, commitments and settlement history remain
    authoritative. A retry replays raw proofs again, checks freshness and adds
    no event when the exact closing frame is already current. This does not
    authenticate finality, external cash purpose, LP exit or an economic edge.
    """
    if journal.policy != request.manifest.policy:
        raise ValueError("observation manifest differs from journal policy")
    old = journal.current_state()
    if old.frame is None:
        raise ValueError("observation requires an established risk baseline")
    if any(
        o.native_gas_evidence is None or o.native_gas_units or o.non_trading_native_units
        for o in request.closing.values()
    ):
        raise ValueError("complete account gas required; scalar overrides forbidden")
    opening = EvidenceReplayAccountRiskReader(store, request.manifest, request.opening).reconcile()
    closing = EvidenceReplayAccountRiskReader(
        store, request.manifest, request.closing, previous=opening
    ).reconcile()
    if closing != old.frame and not _same_inventory(opening, old.frame):
        raise ValueError("raw opening differs from persistent risk inventory")
    return journal.observe(
        closing,
        expected=old,
        now=now,
        evidence_hash=sha256_hex(canonical_json(request.model_dump(mode="json", by_alias=True))),
    )
