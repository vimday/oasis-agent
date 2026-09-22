"""Durable LP account/nonce coordination, with no signing or broadcast capability.

Each journal binds one account, chain and policy version. BEGIN IMMEDIATE and
unique nonce reservations serialize competing processes. Evidence hashes are
references for the independent verifier, not assertions that finality or
simulation has been proven. External anchoring is needed to detect truncation
or replacement of the whole database.
"""

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from types import TracebackType
from typing import Self

from ..errors import ExecutionBlockedError
from ..models.common import (
    BlockRef,
    EvidenceRef,
    canonical_json,
    normalize_address,
    normalize_hash32,
    sha256_hex,
)
from .lp_state_machine import (
    VALID_TRANSITIONS,
    LpIntent,
    LpIntentMode,
    LpState,
    RuntimePin,
    RuntimeRole,
    TickRange,
)


@dataclass(frozen=True, slots=True)
class DurableLpRecord:
    intent_hash: str
    nonce: int
    state: LpState
    transaction_hash: str | None
    recovery_transaction_hash: str | None
    position_token_id: int
    digest: str


@dataclass(frozen=True, slots=True)
class HaltRecoveryProof:
    """Independent post-state proof for a completed, exposure-reducing unwind."""

    chain_id: int
    account: str
    position_token_id: int
    recovery_transaction_hash: str
    finalized_block: BlockRef
    position_liquidity_after: int
    receipt_success: bool
    token_balances_reconciled: bool
    approvals_revoked: bool
    evidence: tuple[EvidenceRef, ...]

    def __post_init__(self) -> None:
        if type(self.chain_id) is not int or self.chain_id <= 0:
            raise ValueError("positive recovery chain required")
        object.__setattr__(self, "account", normalize_address(self.account))
        object.__setattr__(
            self,
            "recovery_transaction_hash",
            normalize_hash32(self.recovery_transaction_hash),
        )
        if type(self.position_token_id) is not int or self.position_token_id <= 0:
            raise ValueError("positive recovery position token id required")
        if type(self.position_liquidity_after) is not int or self.position_liquidity_after < 0:
            raise ValueError("recovery liquidity must be a nonnegative integer")
        for name in ("receipt_success", "token_balances_reconciled", "approvals_revoked"):
            if type(getattr(self, name)) is not bool:
                raise TypeError(f"{name} must be a bool")
        if self.finalized_block.chain_id != self.chain_id:
            raise ValueError("recovery finality block belongs to another chain")
        if not self.evidence:
            raise ValueError("recovery requires retained evidence")
        expected_content_hash = recovery_evidence_hash(
            chain_id=self.chain_id,
            account=self.account,
            position_token_id=self.position_token_id,
            recovery_transaction_hash=self.recovery_transaction_hash,
            finalized_block=self.finalized_block,
            position_liquidity_after=self.position_liquidity_after,
            receipt_success=self.receipt_success,
            token_balances_reconciled=self.token_balances_reconciled,
            approvals_revoked=self.approvals_revoked,
        )
        sources: set[str] = set()
        interpretations: set[tuple[str, str]] = set()
        for reference in self.evidence:
            if (
                reference.kind != "recovery-reconciliation"
                or reference.block is None
                or reference.block.chain_id != self.chain_id
                or reference.block.number > self.finalized_block.number
                or reference.content_hash != expected_content_hash
                or not reference.request_hash
                or not reference.raw_response_hash
                or not reference.interpretation_hash
            ):
                raise ValueError("recovery evidence is incomplete or outside the finality window")
            sources.add(reference.source)
            interpretations.add((reference.content_hash, reference.interpretation_hash))
        if len(sources) < 2 or len(interpretations) != 1:
            raise ValueError("recovery requires two distinct providers with one interpretation")

    @property
    def proof_hash(self) -> str:
        return "0x" + hashlib.sha256(canonical_json(asdict(self))).hexdigest()

    @property
    def closes_exposure(self) -> bool:
        return (
            self.position_liquidity_after == 0
            and self.receipt_success
            and self.token_balances_reconciled
            and self.approvals_revoked
        )


def recovery_evidence_hash(
    *,
    chain_id: int,
    account: str,
    position_token_id: int,
    recovery_transaction_hash: str,
    finalized_block: BlockRef,
    position_liquidity_after: int,
    receipt_success: bool,
    token_balances_reconciled: bool,
    approvals_revoked: bool,
) -> str:
    """Hash the exact recovery facts independent providers must reproduce."""
    return sha256_hex(
        canonical_json(
            {
                "schema": "oasis.lp-halt-recovery.v1",
                "chain_id": chain_id,
                "account": normalize_address(account),
                "position_token_id": position_token_id,
                "recovery_transaction_hash": normalize_hash32(recovery_transaction_hash),
                "finalized_block": asdict(finalized_block),
                "position_liquidity_after": position_liquidity_after,
                "receipt_success": receipt_success,
                "token_balances_reconciled": token_balances_reconciled,
                "approvals_revoked": approvals_revoked,
            }
        )
    )


class LpExecutionJournal:
    def __init__(self, path: Path, *, chain_id: int, account: str, config_hash: str):
        if type(chain_id) is not int or not 0 < chain_id < 1 << 256:
            raise ValueError("positive uint256 chain required")
        self.chain_id = chain_id
        self.account = normalize_address(account)
        if int(self.account, 16) == 0:
            raise ValueError("nonzero account required")
        self.config_hash = normalize_hash32(config_hash)
        self.policy = canonical_json(
            {"chain_id": chain_id, "account": self.account, "config_hash": self.config_hash}
        )
        self.db = sqlite3.connect(path, timeout=10)
        try:
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS policy "
                "(id INTEGER PRIMARY KEY CHECK(id=1), payload BLOB NOT NULL)"
            )
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS events "
                "(seq INTEGER PRIMARY KEY, payload BLOB NOT NULL, digest TEXT NOT NULL)"
            )
            with self.db:
                self.db.execute("INSERT OR IGNORE INTO policy VALUES (1, ?)", (self.policy,))
                if self.db.execute("SELECT payload FROM policy WHERE id=1").fetchone() != (
                    self.policy,
                ):
                    raise ValueError("execution journal policy cannot change")
            self._read()
        except Exception:
            self.db.close()
            raise

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.db.close()

    def _read(self) -> tuple[dict[str, DurableLpRecord], str, int]:
        if self.db.execute("SELECT payload FROM policy WHERE id=1").fetchone() != (self.policy,):
            raise ValueError("execution journal policy corrupt")
        previous = "0" * 64
        sequence = 0
        records: dict[str, DurableLpRecord] = {}
        nonces: dict[int, str] = {}
        for seq, payload, digest in self.db.execute(
            "SELECT seq, payload, digest FROM events ORDER BY seq"
        ):
            data = json.loads(payload)
            if (
                seq != sequence + 1
                or data["seq"] != seq
                or data["previous"] != previous
                or hashlib.sha256(payload).hexdigest() != digest
                or data["policy_hash"] != hashlib.sha256(self.policy).hexdigest()
            ):
                raise ValueError("execution audit chain corrupt")
            v1_fields = {
                "schema",
                "seq",
                "previous",
                "policy_hash",
                "intent_hash",
                "nonce",
                "state",
                "transaction_hash",
                "evidence_hash",
                "intent",
            }
            v2_fields = v1_fields | {"recovery"}
            schema = data.get("schema")
            if (
                not (
                    (schema == "oasis.lp-execution-journal.v1" and set(data) == v1_fields)
                    or (
                        schema
                        in {
                            "oasis.lp-execution-journal.v2",
                            "oasis.lp-execution-journal.v3",
                        }
                        and set(data) == v2_fields
                    )
                )
                or canonical_json(data) != payload
                or type(data["seq"]) is not int
            ):
                raise ValueError("execution audit schema corrupt")
            recovery_raw = data.get("recovery")
            normalize_hash32(data["evidence_hash"])
            identity = data["intent_hash"]
            if (
                not isinstance(identity, str)
                or len(identity) != 64
                or normalize_hash32("0x" + identity)[2:] != identity
            ):
                raise ValueError("execution intent identity corrupt")
            tx_hash = data["transaction_hash"]
            if tx_hash is not None and normalize_hash32(tx_hash) != tx_hash:
                raise ValueError("execution transaction identity corrupt")
            nonce = data["nonce"]
            if type(nonce) is not int or not 0 <= nonce < 1 << 256:
                raise ValueError("execution audit nonce corrupt")
            state = LpState(data["state"])
            old = records.get(identity)
            if old is None:
                if state is not LpState.RESERVED or nonce in nonces:
                    raise ValueError("execution reservation corrupt")
                raw_intent = data["intent"]
                if not isinstance(raw_intent, dict):
                    raise ValueError("execution reservation intent missing")
                try:
                    decoded = dict(raw_intent)
                    decoded["mode"] = LpIntentMode(decoded["mode"])
                    decoded["old_range"] = TickRange(**decoded["old_range"])
                    decoded["new_range"] = TickRange(**decoded["new_range"])
                    decoded["runtime_pins"] = tuple(
                        RuntimePin(RuntimeRole(pin["role"]), pin["address"], pin["code_hash"])
                        for pin in decoded["runtime_pins"]
                    )
                    intent = LpIntent(**decoded)
                except (TypeError, KeyError) as exc:
                    raise ValueError("execution reservation intent corrupt") from exc
                if (
                    intent.intent_hash != identity
                    or intent.nonce != nonce
                    or intent.chain_id != self.chain_id
                    or intent.config_hash != self.config_hash
                    or data["evidence_hash"] != intent.state_hash
                    or tx_hash is not None
                    or any(
                        r.state not in {LpState.CLOSED, LpState.EXPIRED} for r in records.values()
                    )
                    or (nonces and nonce <= max(nonces))
                ):
                    raise ValueError("execution reservation semantics corrupt")
                nonces[nonce] = identity
                position_token_id = intent.position_token_id
                recovery_transaction_hash = None
            else:
                recovery: HaltRecoveryProof | None = None
                if recovery_raw is not None:
                    try:
                        decoded_recovery = dict(recovery_raw)
                        decoded_recovery["finalized_block"] = BlockRef(
                            **decoded_recovery["finalized_block"]
                        )
                        decoded_recovery["evidence"] = tuple(
                            EvidenceRef(
                                **{
                                    **item,
                                    "block": BlockRef(**item["block"]),
                                }
                            )
                            for item in decoded_recovery["evidence"]
                        )
                        recovery = HaltRecoveryProof(**decoded_recovery)
                    except (TypeError, KeyError) as exc:
                        raise ValueError("execution recovery proof corrupt") from exc
                invalid_recovery = old.state is LpState.HALTED and not (
                    state is LpState.CLOSED
                    and recovery is not None
                    and recovery.chain_id == self.chain_id
                    and recovery.account == self.account
                    and recovery.position_token_id == old.position_token_id
                    and recovery.closes_exposure
                    and data["evidence_hash"] == recovery.proof_hash
                    and tx_hash == old.transaction_hash
                )
                if (
                    old.nonce != nonce
                    or data["intent"] is not None
                    or invalid_recovery
                    or (old.state is not LpState.HALTED and recovery is not None)
                    or (state is LpState.EXPIRED and old.transaction_hash is not None)
                    or (
                        old.transaction_hash is None
                        and tx_hash is not None
                        and state is not LpState.SIGNED
                    )
                    or (state in {LpState.SIGNED, LpState.SUBMITTED} and tx_hash is None)
                    or state not in VALID_TRANSITIONS[old.state]
                    or (
                        old.transaction_hash is not None
                        and data["transaction_hash"] != old.transaction_hash
                    )
                ):
                    raise ValueError("execution transition corrupt")
                position_token_id = old.position_token_id
                recovery_transaction_hash = (
                    recovery.recovery_transaction_hash
                    if recovery is not None
                    else old.recovery_transaction_hash
                )
            records[identity] = DurableLpRecord(
                identity,
                nonce,
                state,
                data["transaction_hash"],
                recovery_transaction_hash,
                position_token_id,
                digest,
            )
            sequence, previous = seq, digest
        return records, previous, sequence

    def get(self, intent_hash: str) -> DurableLpRecord:
        return self._read()[0][intent_hash]

    def _append(
        self,
        *,
        intent_hash: str,
        nonce: int,
        target: LpState,
        transaction_hash: str | None,
        evidence_hash: str,
        previous: str,
        sequence: int,
        intent: LpIntent | None = None,
        recovery: HaltRecoveryProof | None = None,
        position_token_id: int = 0,
    ) -> DurableLpRecord:
        payload = canonical_json(
            {
                "schema": "oasis.lp-execution-journal.v3",
                "seq": sequence + 1,
                "previous": previous,
                "policy_hash": hashlib.sha256(self.policy).hexdigest(),
                "intent_hash": intent_hash,
                "nonce": nonce,
                "state": target.value,
                "transaction_hash": transaction_hash,
                "evidence_hash": evidence_hash,
                "intent": asdict(intent) if intent is not None else None,
                "recovery": asdict(recovery) if recovery is not None else None,
            }
        )
        digest = hashlib.sha256(payload).hexdigest()
        self.db.execute("INSERT INTO events VALUES (?, ?, ?)", (sequence + 1, payload, digest))
        return DurableLpRecord(
            intent_hash,
            nonce,
            target,
            transaction_hash,
            recovery.recovery_transaction_hash if recovery is not None else None,
            intent.position_token_id if intent is not None else position_token_id,
            digest,
        )

    def reserve(self, intent: LpIntent) -> DurableLpRecord:
        if intent.chain_id != self.chain_id or intent.config_hash != self.config_hash:
            raise ExecutionBlockedError("intent does not match journal policy")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            records, previous, sequence = self._read()
            if intent.intent_hash in records:
                result = records[intent.intent_hash]
            else:
                if any(record.nonce == intent.nonce for record in records.values()):
                    raise ExecutionBlockedError("account nonce already reserved")
                if any(
                    record.state not in {LpState.CLOSED, LpState.EXPIRED}
                    for record in records.values()
                ):
                    raise ExecutionBlockedError("account has an unresolved LP action")
                if records and intent.nonce <= max(record.nonce for record in records.values()):
                    raise ExecutionBlockedError("account nonce cannot regress")
                result = self._append(
                    intent_hash=intent.intent_hash,
                    nonce=intent.nonce,
                    target=LpState.RESERVED,
                    transaction_hash=None,
                    evidence_hash=intent.state_hash,
                    previous=previous,
                    sequence=sequence,
                    intent=intent,
                )
            self.db.commit()
            return result
        except Exception:
            self.db.rollback()
            raise

    def recover_halted(
        self,
        intent_hash: str,
        *,
        expected_digest: str,
        proof: HaltRecoveryProof,
    ) -> DurableLpRecord:
        """Close a halted action only after an independently evidenced full unwind."""
        if not isinstance(proof, HaltRecoveryProof):
            raise TypeError("typed halt recovery proof required")
        self.db.execute("BEGIN IMMEDIATE")
        try:
            records, previous, sequence = self._read()
            record = records[intent_hash]
            if record.digest != expected_digest:
                raise ExecutionBlockedError("stale execution recovery")
            if record.state is not LpState.HALTED:
                raise ExecutionBlockedError("only a halted LP action may use recovery")
            if (
                proof.chain_id != self.chain_id
                or proof.account != self.account
                or proof.position_token_id != record.position_token_id
                or not proof.closes_exposure
            ):
                raise ExecutionBlockedError("recovery proof does not establish a full safe unwind")
            result = self._append(
                intent_hash=intent_hash,
                nonce=record.nonce,
                target=LpState.CLOSED,
                transaction_hash=record.transaction_hash,
                evidence_hash=proof.proof_hash,
                previous=previous,
                sequence=sequence,
                recovery=proof,
                position_token_id=record.position_token_id,
            )
            self.db.commit()
            return result
        except Exception:
            self.db.rollback()
            raise

    def advance(
        self,
        intent_hash: str,
        *,
        expected_digest: str,
        target: LpState,
        evidence_hash: str,
        transaction_hash: str | None = None,
    ) -> DurableLpRecord:
        evidence_hash = normalize_hash32(evidence_hash)
        if not isinstance(target, LpState):
            raise TypeError("typed LP target state required")
        if transaction_hash is not None:
            transaction_hash = normalize_hash32(transaction_hash)
        self.db.execute("BEGIN IMMEDIATE")
        try:
            records, previous, sequence = self._read()
            record = records[intent_hash]
            if record.digest != expected_digest:
                raise ExecutionBlockedError("stale execution transition")
            if target not in VALID_TRANSITIONS[record.state] or record.state is LpState.HALTED:
                raise ExecutionBlockedError("invalid durable LP transition; halt requires recovery")
            if target is LpState.EXPIRED and record.transaction_hash is not None:
                raise ExecutionBlockedError(
                    "signed/submitted action must be reconciled, not expired"
                )
            if transaction_hash is not None and target not in {LpState.SIGNED, LpState.SUBMITTED}:
                raise ValueError("transaction identity may only be introduced at signing")
            if record.transaction_hash is not None and transaction_hash not in (
                None,
                record.transaction_hash,
            ):
                raise ValueError("transaction identity cannot change")
            tx_hash = transaction_hash or record.transaction_hash
            if target in {LpState.SIGNED, LpState.SUBMITTED} and tx_hash is None:
                raise ValueError("signed transaction hash required")
            result = self._append(
                intent_hash=intent_hash,
                nonce=record.nonce,
                target=target,
                transaction_hash=tx_hash,
                evidence_hash=evidence_hash,
                previous=previous,
                sequence=sequence,
                position_token_id=record.position_token_id,
            )
            self.db.commit()
            return result
        except Exception:
            self.db.rollback()
            raise


__all__ = [
    "DurableLpRecord",
    "HaltRecoveryProof",
    "LpExecutionJournal",
    "recovery_evidence_hash",
]
