"""Independent account-reader boundary for signer admission, never a strategy input."""

import time
from collections.abc import Callable
from typing import Protocol

from ..risk.account_risk import Commitment, GlobalRiskJournal, PortfolioFrame
from .lp_signer_sidecar import LpSidecarRequest, sidecar_request_hash


class AccountRiskReader(Protocol):
    def reconcile_and_price(self, request: LpSidecarRequest) -> tuple[PortfolioFrame, int]:
        """Replay full inventory/raw proofs; price maximum incremental principal plus gas.

        Must reject unknown inventory, unsettled operations, incomplete accounting,
        ambiguous cash flows and invalid prices. No implementation is loaded by default.
        """
        ...


def _request_digest(request: LpSidecarRequest) -> str:
    """Bare SHA-256 request identity, the form the ledger binds commitments to."""
    return sidecar_request_hash(request)[2:]


class AccountRiskGuard:
    def __init__(
        self,
        journal: GlobalRiskJournal,
        reader: AccountRiskReader,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.journal = journal
        self.reader = reader
        self.clock = clock

    @property
    def policy_digest(self) -> str:
        return self.journal.policy.digest

    def reserve(self, request: LpSidecarRequest) -> str:
        frame, amount = self.reader.reconcile_and_price(request)
        signing = request.signing
        key = f"{signing.intent.chain_id}:{signing.account}"
        account = next((a for a in frame.accounts if a.key == key), None)
        if account is None or (
            account.block_number != signing.current_block.number
            or account.block_hash != signing.current_block.block_hash
        ):
            raise ValueError("independent account block differs from signing request")
        return self.journal.admit(
            frame,
            Commitment(request_hash=_request_digest(request), account_key=key, usd_micros=amount),
            now=int(self.clock()),
        )

    def release(self, request: LpSidecarRequest, *, reason: str) -> None:
        self.journal.release(_request_digest(request), reason=reason)
