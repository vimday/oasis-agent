"""Public transaction commitments retained before broadcast; no signature or raw bytes."""

from dataclasses import asdict, dataclass
from typing import Any

from ..execution.lp_broadcast import decode_signed_transaction
from ..models.common import canonical_json, normalize_address, normalize_hash32, sha256_hex


@dataclass(frozen=True, slots=True)
class SubmittedTransaction:
    transaction_hash: str
    chain_id: int
    account: str
    nonce: int
    target: str
    value: int
    calldata_hash: str
    gas_limit: int
    max_fee_per_gas: int
    max_priority_fee_per_gas: int

    def __post_init__(self) -> None:
        for name in ("transaction_hash", "calldata_hash"):
            object.__setattr__(self, name, normalize_hash32(getattr(self, name)))
        for name in ("account", "target"):
            object.__setattr__(self, name, normalize_address(getattr(self, name)))
        for name in (
            "chain_id",
            "nonce",
            "value",
            "gas_limit",
            "max_fee_per_gas",
            "max_priority_fee_per_gas",
        ):
            value = getattr(self, name)
            limit = 2**64 if name in {"nonce", "gas_limit"} else 2**256
            if type(value) is not int or not 0 <= value < limit:
                raise ValueError("transaction commitment integer out of range")
        if (
            not self.chain_id
            or not self.gas_limit
            or self.max_priority_fee_per_gas > self.max_fee_per_gas
        ):
            raise ValueError("transaction commitment chain/gas/fees invalid")

    @classmethod
    def from_signed(cls, raw: bytes) -> SubmittedTransaction:
        """Recover the signer and derive every field from the actual signed envelope."""
        tx = decode_signed_transaction(raw)
        return cls(
            tx.transaction_hash,
            tx.chain_id,
            tx.signer,
            tx.nonce,
            tx.target,
            tx.value,
            tx.calldata_hash,
            tx.gas_limit,
            tx.max_fee_per_gas,
            tx.max_priority_fee_per_gas,
        )


def pending_plan_hash(
    config: dict[str, Any],
    kind: str,
    intent: dict[str, Any],
    tx: SubmittedTransaction,
    position_before: dict[str, Any] | None = None,
    wallet_evidence: tuple[dict[str, Any], ...] = (),
) -> str:
    """Detect recovery under a different config, local intent or signed commitment.

    This is an integrity binding inside the existing local state trust boundary,
    not independent signing authority or authentication of a tampered state file.
    """
    return sha256_hex(
        canonical_json(
            {
                "schema": "oasis.live-pending-plan.v2",
                "config": config,
                "kind": kind,
                "intent": intent,
                "transaction": asdict(tx),
                "position_before": position_before,
                "wallet_evidence": wallet_evidence,
            }
        )
    )
