"""Read-only bounded approval assessment; no approve/revoke transactions."""

from typing import Literal

from .models import Address, StrictModel, UInt


class Approval(StrictModel):
    owner: Address
    spender: Address
    amount: UInt
    expiration: UInt | None
    revoked: bool
    mechanism: Literal["erc20", "permit2"]


def approval_status(
    approval: Approval | None,
    *,
    now: int,
    required: int,
    maximum: int,
    owner: str,
    spender: str,
) -> str:
    from_values = (now, required, maximum)
    if any(type(v) is not int or v < 0 for v in from_values) or not 0 < required <= maximum:
        raise ValueError("valid time and bounded token quantities required")
    if approval is None:
        return "unresolved"
    if approval.owner != owner or approval.spender != spender:
        return "wrong_binding"
    if approval.revoked or approval.amount == 0:
        return "revoked"
    # Plain ERC-20 allowance has no enforceable expiry.
    if approval.mechanism != "permit2" or approval.expiration is None:
        return "expiry_not_enforced"
    if approval.expiration <= now:
        return "expired"
    if not required <= approval.amount <= maximum:
        return "amount_out_of_bounds"
    return "ready_observed"
