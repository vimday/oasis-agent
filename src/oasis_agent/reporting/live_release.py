"""Current direct-loop release boundary, shared by CLI, execution and read-only UI.

This describes missing implemented controls, not operator permission. No file,
flag, environment variable, dashboard approval or passing unit test can turn a
missing implementation into an authorization. Remove an item only alongside its
end-to-end implementation and validation.
"""

from pydantic import BaseModel


class ReleaseBlocker(BaseModel):
    code: str
    title: str
    required: str
    implementation_status: str = "implemented_pending_integration_validation"


class AcceptancePermit(BaseModel):
    """One owner-authorized zero-principal transaction that produces the first real receipt.

    The release blockers all require a real dedicated-account receipt, which no closed
    release can produce. This permit admits exactly one action kind at exactly one account
    nonce. Once any transaction with that nonce lands, the permit can never match again.
    """

    kind: str
    account_nonce: int
    authorized_by: str


class LiveRelease(BaseModel):
    schema_version: str = "oasis.live-release.v1"
    new_actions_allowed: bool = False
    owner_canary_authorized: bool = True
    capital_limit_usd: int = 500
    daily_loss_limit_usd: int = 200
    cumulative_loss_limit_usd: int = 500
    acceptance_permit: AcceptancePermit | None = None
    release_evidence: str | None = None
    blockers: list[ReleaseBlocker]


# The nonce-3 acceptance permit (owner 2026-10-28) was consumed by approve_usdc
# 0xe0363189ebb4e995fa3bedd58e228da7d6bfdc8c10b6f98c3cba3c2b2c5dc4fd, included at Base
# block 53250445 with status 1. The deployed authority settled it read-only from two
# providers into live_approval_cash evidence with 336424665230 wei of gas equal to the
# receipt. That receipt is the dedicated-account evidence the former blockers required.
ACCEPTANCE_PERMIT: AcceptancePermit | None = None

RELEASE_EVIDENCE = (
    "owner 2026-10-31 opened the USD 500 canary; acceptance receipt "
    "0xe0363189ebb4e995fa3bedd58e228da7d6bfdc8c10b6f98c3cba3c2b2c5dc4fd at Base block "
    "53250445 reconciled from two providers by the deployed authority"
)


# Owner decision 2026-10-31. Setting this False restores the closed release below; the
# closed path stays tested so a later stop needs no new code.
RELEASE_OPEN = True

CLOSED_BLOCKER = ReleaseBlocker(
    code="release_closed",
    title="实盘发布已关闭",
    required="项目所有者重新放行前不允许新交易。",
    implementation_status="closed_by_owner",
)


def live_release() -> LiveRelease:
    # Capital, loss and action limits remain enforced by LiveConfig and the risk state;
    # opening the release removes no per-action check.
    if not RELEASE_OPEN:
        return LiveRelease(acceptance_permit=ACCEPTANCE_PERMIT, blockers=[CLOSED_BLOCKER])
    return LiveRelease(
        new_actions_allowed=True,
        acceptance_permit=ACCEPTANCE_PERMIT,
        release_evidence=RELEASE_EVIDENCE,
        blockers=[],
    )


def require_live_release() -> None:
    report = live_release()
    if not report.new_actions_allowed or report.blockers:
        raise ValueError("live release blocked: " + ", ".join(b.code for b in report.blockers))


def acceptance_allows(kind: object, nonce: object) -> bool:
    """True only for the permitted action kind at the permitted account nonce."""
    permit = ACCEPTANCE_PERMIT
    return (
        permit is not None
        and kind == permit.kind
        and type(nonce) is int
        and nonce == permit.account_nonce
    )


def require_live_action(kind: object, nonce: object) -> None:
    """Gate one signing decision: the acceptance permit or a complete release."""
    if not acceptance_allows(kind, nonce):
        require_live_release()


def require_live_start() -> None:
    """Gate service startup; per-action checks still apply to every signature."""
    if ACCEPTANCE_PERMIT is None:
        require_live_release()
