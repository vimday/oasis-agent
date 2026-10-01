"""Operator-side broadcast gate for an already-signed LP transaction.

The signer sidecar stops at an assembled, signed raw transaction. This module is
the only place in the repository that may put one on chain, and it holds no key
and cannot produce or modify a signature. It decodes the exact EIP-1559 (type 2)
envelope, requires it to agree field for field with the sidecar request that
produced it, applies the operator's own chain, target, fee and value allowlists,
and only then submits the raw bytes to one endpoint whose chain has been pinned.

A denial is the default outcome for every missing, mismatched, or unstated
condition. Every attempt is journaled append-only and hash-linked, and the
journal records hashes and reasons only: no raw transaction, signature, or key
ever appears in it. A broadcast whose result is unknown is journaled as
unresolved and is never retried automatically, because a transaction that may
have landed must not be replayed blindly; it is closed by an explicit receipt
observation instead.
"""

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import Any, Protocol, Self, runtime_checkable

from eth_account import Account
from eth_hash.auto import keccak
from rlp import decode as rlp_decode  # type: ignore[import-untyped]

from ..evidence.rpc_validation import rpc_quantity
from ..models.common import (
    canonical_json,
    normalize_address,
    normalize_hash32,
    sha256_hex,
)
from ..rpc.transport import HttpPoster, JsonRpcTransport
from .lp_signer_policy import calldata_hash
from .lp_signer_sidecar import (
    SECP256K1_ORDER,
    LpSidecarDecision,
    LpSidecarRequest,
    sidecar_request_hash,
)

BROADCAST_SCHEMA = "oasis.lp-broadcast.v1"
BROADCAST_POLICY_SCHEMA = "oasis.lp-broadcast-policy.v1"
EIP1559_TYPE = 0x02
MAX_RAW_TRANSACTION_BYTES = 128 * 1024
_UINT64 = 2**64
_UINT256 = 2**256
_OUTCOMES = frozenset({"denied", "broadcast", "unresolved", "resolved"})


@dataclass(frozen=True, slots=True)
class BroadcastPolicy:
    """The operator's own allowlists and caps, independent of the signer policy.

    ``chain_ids`` and ``targets`` are allowlists rather than single values so one
    operator authority can cover every chain and contract it has reviewed; an
    empty allowlist is refused at construction, which makes "deny everything" a
    configuration error instead of a silent default.
    """

    chain_ids: frozenset[int]
    account: str
    targets: frozenset[str]
    max_value_wei: int
    max_gas_limit: int
    max_fee_per_gas: int
    max_total_gas_cost_wei: int

    def __post_init__(self) -> None:
        if not isinstance(self.chain_ids, frozenset) or not self.chain_ids:
            raise ValueError("broadcast policy requires a non-empty chain allowlist")
        for value in self.chain_ids:
            if type(value) is not int or not 0 < value < _UINT256:
                raise ValueError("broadcast policy chain IDs must be bounded integers")
        if not isinstance(self.targets, frozenset) or not self.targets:
            raise ValueError("broadcast policy requires a non-empty target allowlist")
        object.__setattr__(self, "account", normalize_address(self.account))
        object.__setattr__(
            self, "targets", frozenset(normalize_address(target) for target in self.targets)
        )
        for name in ("max_value_wei", "max_gas_limit", "max_fee_per_gas", "max_total_gas_cost_wei"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        for name in ("max_gas_limit", "max_fee_per_gas", "max_total_gas_cost_wei"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be a positive cap")


def broadcast_policy_payload(policy: BroadcastPolicy) -> dict[str, Any]:
    """Canonical, order-independent JSON for one broadcast policy."""
    if not isinstance(policy, BroadcastPolicy):
        raise TypeError("typed broadcast policy required")
    return {
        "schema": BROADCAST_POLICY_SCHEMA,
        "chain_ids": sorted(policy.chain_ids),
        "account": policy.account,
        "targets": sorted(policy.targets),
        "max_value_wei": policy.max_value_wei,
        "max_gas_limit": policy.max_gas_limit,
        "max_fee_per_gas": policy.max_fee_per_gas,
        "max_total_gas_cost_wei": policy.max_total_gas_cost_wei,
    }


def broadcast_policy_hash(policy: BroadcastPolicy) -> str:
    return sha256_hex(canonical_json(broadcast_policy_payload(policy)))


def broadcast_policy_to_json(policy: BroadcastPolicy) -> bytes:
    return canonical_json(broadcast_policy_payload(policy))


def broadcast_policy_from_json(payload: bytes) -> BroadcastPolicy:
    data = _expect_fields(
        json.loads(payload, object_pairs_hook=_unique_object),
        {
            "schema",
            "chain_ids",
            "account",
            "targets",
            "max_value_wei",
            "max_gas_limit",
            "max_fee_per_gas",
            "max_total_gas_cost_wei",
        },
        "broadcast policy",
    )
    if data["schema"] != BROADCAST_POLICY_SCHEMA:
        raise ValueError("broadcast policy schema is unsupported")
    for name in ("chain_ids", "targets"):
        if not isinstance(data[name], list):
            raise ValueError(f"broadcast policy {name} must be a list")
    return BroadcastPolicy(
        chain_ids=frozenset(data["chain_ids"]),
        account=data["account"],
        targets=frozenset(data["targets"]),
        max_value_wei=data["max_value_wei"],
        max_gas_limit=data["max_gas_limit"],
        max_fee_per_gas=data["max_fee_per_gas"],
        max_total_gas_cost_wei=data["max_total_gas_cost_wei"],
    )


@dataclass(frozen=True, slots=True)
class SignedLpTransaction:
    """The decoded, recoverable contents of one signed type-2 transaction."""

    chain_id: int
    nonce: int
    max_priority_fee_per_gas: int
    max_fee_per_gas: int
    gas_limit: int
    target: str
    value: int
    calldata: bytes
    signer: str
    transaction_hash: str
    calldata_hash: str
    raw: bytes = field(repr=False)

    def __post_init__(self) -> None:
        for name, bound in (
            ("chain_id", _UINT256),
            ("nonce", _UINT64),
            ("max_priority_fee_per_gas", _UINT256),
            ("max_fee_per_gas", _UINT256),
            ("gas_limit", _UINT64),
            ("value", _UINT256),
        ):
            value = getattr(self, name)
            if type(value) is not int or not 0 <= value < bound:
                raise ValueError(f"decoded transaction {name} is outside the accepted range")
        if self.gas_limit <= 0:
            raise ValueError("decoded transaction gas limit must be positive")
        if self.chain_id == 0:
            raise ValueError("decoded transaction chain id must be nonzero")
        if not isinstance(self.calldata, bytes) or not isinstance(self.raw, bytes):
            raise TypeError("decoded transaction calldata and raw bytes are required")
        object.__setattr__(self, "target", normalize_address(self.target))
        object.__setattr__(self, "signer", normalize_address(self.signer))
        object.__setattr__(self, "transaction_hash", normalize_hash32(self.transaction_hash))
        object.__setattr__(self, "calldata_hash", normalize_hash32(self.calldata_hash))
        if self.calldata_hash != calldata_hash(self.calldata):
            raise ValueError("decoded transaction calldata hash does not match its calldata")
        if self.transaction_hash != "0x" + keccak(self.raw).hex():
            raise ValueError("decoded transaction hash does not match its raw bytes")


def _as_int(value: object, name: str, bound: int) -> int:
    if type(value) is int:
        number = value
    elif isinstance(value, bytes):
        number = int.from_bytes(value, "big")
    else:
        raise ValueError(f"signed transaction {name} is not an integer")
    if not 0 <= number < bound:
        raise ValueError(f"signed transaction {name} is outside the accepted range")
    return number


def _as_bytes(value: object, name: str) -> bytes:
    if not isinstance(value, bytes):
        raise ValueError(f"signed transaction {name} is not a byte string")
    return value


def decode_signed_transaction(raw: bytes) -> SignedLpTransaction:
    """Decode and verify one signed type-2 transaction, or refuse it.

    Legacy, access-list, and blob envelopes are refused rather than interpreted,
    because a gate that accepts several encodings cannot claim what it checked.
    """
    if not isinstance(raw, bytes):
        raise TypeError("raw transaction must be bytes")
    if not raw or len(raw) > MAX_RAW_TRANSACTION_BYTES:
        raise ValueError("raw transaction length is outside the accepted bounds")
    if raw[0] != EIP1559_TYPE:
        raise ValueError("only EIP-1559 (type 2) transactions may be broadcast")
    try:
        fields = rlp_decode(raw[1:])
    except Exception as exc:
        raise ValueError("signed transaction envelope is not decodable RLP") from exc
    if not isinstance(fields, list) or len(fields) != 12:
        raise ValueError("signed transaction envelope is not a complete type-2 list")
    (
        chain_id,
        nonce,
        max_priority_fee_per_gas,
        max_fee_per_gas,
        gas_limit,
        target,
        value,
        calldata,
        access_list,
        y_parity,
        r,
        s,
    ) = fields
    if access_list != []:
        raise ValueError("signed transaction with an access list is refused")
    target_bytes = _as_bytes(target, "target")
    if len(target_bytes) != 20:
        raise ValueError("signed transaction target must be exactly one address")
    calldata_bytes = _as_bytes(calldata, "calldata")
    decoded_chain = _as_int(chain_id, "chain ID", _UINT256)
    if decoded_chain == 0:
        raise ValueError("signed transaction chain ID must be nonzero")
    decoded_gas = _as_int(gas_limit, "gas limit", _UINT64)
    if decoded_gas <= 0:
        raise ValueError("signed transaction gas limit must be positive")
    signature_r = _as_int(r, "r", SECP256K1_ORDER)
    signature_s = _as_int(s, "s", SECP256K1_ORDER)
    if not signature_r or not signature_s:
        raise ValueError("signed transaction signature is outside the secp256k1 range")
    if signature_s > SECP256K1_ORDER // 2:
        raise ValueError("high-s signatures are rejected (EIP-2)")
    _as_int(y_parity, "y parity", 2)
    try:
        signer = normalize_address(Account.recover_transaction(raw))
    except Exception as exc:
        raise ValueError("signed transaction cannot be recovered") from exc
    return SignedLpTransaction(
        chain_id=decoded_chain,
        nonce=_as_int(nonce, "nonce", _UINT64),
        max_priority_fee_per_gas=_as_int(max_priority_fee_per_gas, "priority fee", _UINT256),
        max_fee_per_gas=_as_int(max_fee_per_gas, "max fee per gas", _UINT256),
        gas_limit=decoded_gas,
        target="0x" + target_bytes.hex(),
        value=_as_int(value, "value", _UINT256),
        calldata=calldata_bytes,
        signer=signer,
        transaction_hash="0x" + keccak(raw).hex(),
        calldata_hash=calldata_hash(calldata_bytes),
        raw=raw,
    )


@dataclass(frozen=True, slots=True)
class BroadcastExpectation:
    """What the signer sidecar actually signed, restated for the operator gate.

    The expectation exists so the gate never has to trust the raw bytes it was
    handed: every field of the signed transaction must equal the corresponding
    field of the request the sidecar evaluated.
    """

    request_hash: str
    transaction_hash: str
    chain_id: int
    account: str
    nonce: int
    target: str
    value: int
    gas_limit: int
    max_fee_per_gas: int
    max_priority_fee_per_gas: int
    calldata_hash: str

    def __post_init__(self) -> None:
        for name in ("request_hash", "transaction_hash", "calldata_hash"):
            object.__setattr__(self, name, normalize_hash32(getattr(self, name), name))
        object.__setattr__(self, "account", normalize_address(self.account))
        object.__setattr__(self, "target", normalize_address(self.target))
        for name, bound in (
            ("chain_id", _UINT256),
            ("nonce", _UINT64),
            ("value", _UINT256),
            ("gas_limit", _UINT64),
            ("max_fee_per_gas", _UINT256),
            ("max_priority_fee_per_gas", _UINT256),
        ):
            value = getattr(self, name)
            if type(value) is not int or not 0 <= value < bound:
                raise ValueError(f"broadcast expectation {name} is outside the accepted range")


def raw_bytes_from_decision(decision: LpSidecarDecision) -> bytes:
    """The exact signed bytes a sidecar decision reports, hash-checked."""
    if not isinstance(decision, LpSidecarDecision):
        raise TypeError("typed sidecar decision required")
    if decision.outcome != "signed" or decision.raw_transaction_hex is None:
        raise ValueError("only a signed sidecar decision carries a transaction")
    encoded = decision.raw_transaction_hex
    digits = encoded.removeprefix("0x")
    if not digits or len(digits) % 2:
        raise ValueError("sidecar decision did not report canonical raw bytes")
    try:
        raw = bytes.fromhex(digits)
    except ValueError as exc:
        raise ValueError("sidecar decision did not report canonical raw bytes") from exc
    if "0x" + keccak(raw).hex() != decision.transaction_hash:
        raise ValueError("sidecar decision raw transaction does not match its hash")
    return raw


def expectation_from_sidecar(
    request: LpSidecarRequest, decision: LpSidecarDecision
) -> BroadcastExpectation:
    """Bind one signed decision to the exact request that produced it."""
    if not isinstance(request, LpSidecarRequest):
        raise TypeError("typed sidecar request required")
    raw_bytes_from_decision(decision)
    if decision.request_hash != sidecar_request_hash(request):
        raise ValueError("sidecar decision does not belong to this request")
    # ``raw_bytes_from_decision`` already refused a decision without a
    # transaction, so this narrowing is a documented invariant.
    assert decision.transaction_hash is not None
    signing = request.signing
    return BroadcastExpectation(
        request_hash=decision.request_hash,
        transaction_hash=decision.transaction_hash,
        chain_id=signing.intent.chain_id,
        account=signing.account,
        nonce=signing.account_nonce,
        target=signing.target,
        value=signing.value,
        gas_limit=signing.gas_limit,
        max_fee_per_gas=request.max_fee_per_gas,
        max_priority_fee_per_gas=request.max_priority_fee_per_gas,
        calldata_hash=calldata_hash(signing.calldata),
    )


@dataclass(frozen=True, slots=True)
class BroadcastAuthorization:
    """Pure admission result: nothing is sent and nothing is journaled here."""

    authorized: bool
    reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.authorized and self.reasons:
            raise ValueError("an authorized broadcast cannot carry reasons")
        if not self.authorized and not self.reasons:
            raise ValueError("a refused broadcast requires explicit reasons")


def authorize_broadcast(
    policy: BroadcastPolicy,
    expectation: BroadcastExpectation,
    transaction: SignedLpTransaction,
) -> BroadcastAuthorization:
    """Decide whether this exact transaction may be sent, with no I/O at all."""
    if not isinstance(policy, BroadcastPolicy):
        raise TypeError("typed broadcast policy required")
    if not isinstance(expectation, BroadcastExpectation):
        raise TypeError("typed broadcast expectation required")
    if not isinstance(transaction, SignedLpTransaction):
        raise TypeError("decoded signed transaction required")
    reasons: list[str] = []
    if expectation.chain_id not in policy.chain_ids:
        reasons.append("chain is not in the operator allowlist")
    if transaction.chain_id != expectation.chain_id:
        reasons.append("signed transaction chain differs from the sidecar request")
    if transaction.signer != policy.account:
        reasons.append("signed transaction signer is not the operator account")
    if transaction.signer != expectation.account:
        reasons.append("signed transaction signer differs from the sidecar request account")
    if transaction.nonce != expectation.nonce:
        reasons.append("signed transaction nonce differs from the sidecar request")
    if transaction.target != expectation.target:
        reasons.append("signed transaction target differs from the sidecar request")
    if transaction.value != expectation.value:
        reasons.append("signed transaction value differs from the sidecar request")
    if transaction.gas_limit != expectation.gas_limit:
        reasons.append("signed transaction gas limit differs from the sidecar request")
    if transaction.max_fee_per_gas != expectation.max_fee_per_gas:
        reasons.append("signed transaction max fee differs from the sidecar request")
    if transaction.max_priority_fee_per_gas != expectation.max_priority_fee_per_gas:
        reasons.append("signed transaction priority fee differs from the sidecar request")
    if transaction.calldata_hash != expectation.calldata_hash:
        reasons.append("signed transaction calldata differs from the sidecar request")
    if transaction.transaction_hash != expectation.transaction_hash:
        reasons.append("raw transaction is not the one the sidecar produced")
    if transaction.target not in policy.targets:
        reasons.append("target is not in the operator allowlist")
    if transaction.value > policy.max_value_wei:
        reasons.append("value exceeds the operator cap")
    if transaction.gas_limit > policy.max_gas_limit:
        reasons.append("gas limit exceeds the operator cap")
    if transaction.max_fee_per_gas > policy.max_fee_per_gas:
        reasons.append("max fee per gas exceeds the operator cap")
    elif transaction.max_fee_per_gas * transaction.gas_limit > policy.max_total_gas_cost_wei:
        reasons.append("worst-case gas cost exceeds the operator cap")
    return BroadcastAuthorization(not reasons, tuple(reasons))


@dataclass(frozen=True, slots=True)
class BroadcastDecision:
    outcome: str
    reasons: tuple[str, ...]
    request_hash: str
    transaction_hash: str
    signer: str | None = None

    def __post_init__(self) -> None:
        if self.outcome not in {"denied", "broadcast", "unresolved"}:
            raise ValueError("broadcast outcome must be denied, broadcast, or unresolved")
        object.__setattr__(
            self, "request_hash", normalize_hash32(self.request_hash, "request hash")
        )
        object.__setattr__(
            self, "transaction_hash", normalize_hash32(self.transaction_hash, "transaction hash")
        )
        if self.signer is not None:
            object.__setattr__(self, "signer", normalize_address(self.signer))
        if self.outcome == "denied":
            if not self.reasons:
                raise ValueError("a denied broadcast requires explicit reasons")
        elif self.outcome == "broadcast":
            if self.reasons or self.signer is None:
                raise ValueError("a broadcast outcome requires a signer and no reasons")
        elif not self.reasons or self.signer is None:
            raise ValueError("an unresolved broadcast requires a signer and reasons")


@runtime_checkable
class BroadcastRpc(Protocol):
    """Narrow endpoint interface: one chain read and one raw submission."""

    def chain_id(self) -> int: ...

    def send_raw_transaction(self, raw_hex: str) -> str: ...


class HttpBroadcastRpc:
    """Single-shot HTTP endpoint; it never retries and never signs anything."""

    def __init__(self, url: str, *, http: HttpPoster | None = None, timeout: float = 20.0) -> None:
        self._transport = JsonRpcTransport(url, http=http, timeout=timeout, retries=0)

    def chain_id(self) -> int:
        return rpc_quantity(self._transport.request("eth_chainId", []))

    def send_raw_transaction(self, raw_hex: str) -> str:
        return str(self._transport.request("eth_sendRawTransaction", [raw_hex]))


class _AlreadyRecorded(ValueError):
    """Another writer already durably claimed this transaction."""


class LpBroadcastJournal:
    """Append-only, hash-linked audit trail for operator broadcast attempts.

    The journal binds one policy fingerprint and records only request hashes,
    transaction hashes, the recovered signer, outcomes, and reasons. Raw
    transactions, signatures, and keys never appear in it, and a resolution must
    name a transaction this journal already broadcast.
    """

    def __init__(self, path: Path, *, policy: BroadcastPolicy) -> None:
        self.path = path
        self.policy_hash = broadcast_policy_hash(policy)
        self.binding = canonical_json(
            {
                "schema": BROADCAST_SCHEMA,
                "policy_hash": self.policy_hash,
                "policy": broadcast_policy_payload(policy),
            }
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
                self.db.execute("INSERT OR IGNORE INTO policy VALUES (1, ?)", (self.binding,))
                if self.db.execute("SELECT payload FROM policy WHERE id=1").fetchone() != (
                    self.binding,
                ):
                    raise ValueError("broadcast journal policy cannot change")
            self._read()
        except Exception:
            self.db.close()
            raise

    def _read(self) -> tuple[set[str], set[str]]:
        """Revalidate the whole chain; return (open, resolved) transaction hashes."""
        if self.db.execute("SELECT payload FROM policy WHERE id=1").fetchone() != (self.binding,):
            raise ValueError("broadcast journal policy corrupt")
        previous = "0" * 64
        sequence = 0
        open_hashes: set[str] = set()
        resolved_hashes: set[str] = set()
        for seq, payload, digest in self.db.execute(
            "SELECT seq, payload, digest FROM events ORDER BY seq"
        ):
            data = json.loads(payload)
            _validate_record(data, previous=previous)
            if (
                seq != sequence + 1
                or data["seq"] != seq
                or hashlib.sha256(payload).hexdigest() != digest
                or data["policy_hash"] != self.policy_hash
            ):
                raise ValueError("broadcast audit chain corrupt")
            transaction_hash = data["transaction_hash"]
            if data["outcome"] == "resolved":
                if transaction_hash not in open_hashes:
                    raise ValueError("broadcast resolution does not name a broadcast")
                open_hashes.discard(transaction_hash)
                resolved_hashes.add(transaction_hash)
            elif data["outcome"] in {"broadcast", "unresolved"}:
                if transaction_hash in resolved_hashes:
                    raise ValueError("broadcast audit chain corrupt")
                open_hashes.add(transaction_hash)
            previous = digest
            sequence = seq
        return open_hashes, resolved_hashes

    def open_transactions(self) -> tuple[str, ...]:
        """Submitted or unresolved transactions whose receipt was never observed.

        A transaction in this set may already be on chain, so it must not be
        re-sent; it is closed by an explicit receipt observation instead.
        """
        open_hashes, _ = self._read()
        return tuple(sorted(open_hashes))

    def seen_transactions(self) -> frozenset[str]:
        """Every transaction this journal already broadcast or left unresolved."""
        open_hashes, resolved_hashes = self._read()
        return frozenset(open_hashes | resolved_hashes)

    def append(
        self,
        *,
        request_hash: str,
        transaction_hash: str,
        outcome: str,
        reasons: tuple[str, ...],
        signer: str | None = None,
        receipt_status: int | None = None,
        block_number: int | None = None,
        require_unseen: bool = False,
    ) -> None:
        """Append one hash-linked audit record inside a single writer transaction."""
        data = {
            "schema": BROADCAST_SCHEMA,
            "seq": 0,
            "previous": "",
            "policy_hash": self.policy_hash,
            "request_hash": request_hash,
            "transaction_hash": transaction_hash,
            "signer": signer,
            "outcome": outcome,
            "reasons": list(reasons),
            "receipt_status": receipt_status,
            "block_number": block_number,
        }
        _validate_record(data, previous=None)
        try:
            self.db.execute("BEGIN IMMEDIATE")
            open_hashes, resolved_hashes = self._read()
            if require_unseen and transaction_hash in open_hashes | resolved_hashes:
                raise _AlreadyRecorded("transaction already claimed")
            if outcome == "resolved" and transaction_hash not in open_hashes:
                raise ValueError("broadcast resolution does not name a broadcast")
            if outcome in {"broadcast", "unresolved"} and transaction_hash in resolved_hashes:
                raise ValueError("a resolved transaction cannot be broadcast again")
            row = self.db.execute("SELECT seq FROM events ORDER BY seq DESC LIMIT 1").fetchone()
            sequence = row[0] if row is not None else 0
            tail = self.db.execute("SELECT digest FROM events ORDER BY seq DESC LIMIT 1").fetchone()
            data["seq"] = sequence + 1
            data["previous"] = tail[0] if tail is not None else "0" * 64
            payload = canonical_json(data)
            self.db.execute(
                "INSERT INTO events (seq, payload, digest) VALUES (?, ?, ?)",
                (sequence + 1, payload, hashlib.sha256(payload).hexdigest()),
            )
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

    def resolve(self, transaction_hash: str, *, receipt_status: int, block_number: int) -> None:
        """Close one open broadcast with an observed receipt; exactly once."""
        if type(receipt_status) is not int or receipt_status not in (0, 1):
            raise ValueError("receipt status must be 0 or 1")
        if type(block_number) is not int or block_number < 0:
            raise ValueError("resolution block number must be a nonnegative integer")
        self.append(
            request_hash=self.request_hash(transaction_hash),
            transaction_hash=normalize_hash32(transaction_hash, "transaction hash"),
            outcome="resolved",
            reasons=(),
            receipt_status=receipt_status,
            block_number=block_number,
        )

    def request_hash(self, transaction_hash: str) -> str:
        """The admitted request a journaled transaction belongs to, resolved or not.

        Exposed so a later observation of the same transaction — a receipt, for
        instance — can be reported against the request that produced it, without
        reopening or trusting anything the journal recorded about the outcome.
        """
        requested = normalize_hash32(transaction_hash, "transaction hash")
        self._read()
        for (payload,) in self.db.execute("SELECT payload FROM events ORDER BY seq"):
            data = json.loads(payload)
            if data["transaction_hash"] == requested and data["outcome"] in {
                "broadcast",
                "unresolved",
            }:
                return str(data["request_hash"])
        raise ValueError("broadcast resolution does not name a broadcast")

    def close(self) -> None:
        self.db.close()


def _validate_record(data: dict[str, Any], *, previous: str | None) -> None:
    """One place defines what a valid audit record is, for readers and writers."""
    expected = {
        "schema",
        "seq",
        "previous",
        "policy_hash",
        "request_hash",
        "transaction_hash",
        "signer",
        "outcome",
        "reasons",
        "receipt_status",
        "block_number",
    }
    if set(data) != expected or data["schema"] != BROADCAST_SCHEMA:
        raise ValueError("broadcast audit schema corrupt")
    outcome = data["outcome"]
    if outcome not in _OUTCOMES:
        raise ValueError("broadcast audit outcome corrupt")
    if previous is not None and data["previous"] != previous:
        raise ValueError("broadcast audit chain corrupt")
    try:
        normalize_hash32(data["request_hash"], "request hash")
        normalize_hash32(data["transaction_hash"], "transaction hash")
    except (TypeError, ValueError) as exc:
        raise ValueError("broadcast audit hash corrupt") from exc
    reasons = data["reasons"]
    if not isinstance(reasons, list) or any(not isinstance(item, str) for item in reasons):
        raise ValueError("broadcast audit reasons corrupt")
    if reasons != list(dict.fromkeys(reasons)):
        raise ValueError("broadcast audit reasons corrupt")
    receipt_status = data["receipt_status"]
    block_number = data["block_number"]
    if data["signer"] is not None:
        try:
            normalize_address(data["signer"])
        except (TypeError, ValueError) as exc:
            raise ValueError("broadcast audit signer corrupt") from exc
    if outcome == "denied":
        if not reasons:
            raise ValueError("broadcast audit denial corrupt")
        if receipt_status is not None or block_number is not None:
            raise ValueError("broadcast audit denial corrupt")
    elif outcome == "broadcast":
        if reasons or data["signer"] is None:
            raise ValueError("broadcast audit record corrupt")
        if receipt_status is not None or block_number is not None:
            raise ValueError("broadcast audit record corrupt")
    elif outcome == "unresolved":
        if not reasons or data["signer"] is None:
            raise ValueError("broadcast audit record corrupt")
        if receipt_status is not None or block_number is not None:
            raise ValueError("broadcast audit record corrupt")
    else:
        if reasons or data["signer"] is not None:
            raise ValueError("broadcast audit resolution corrupt")
        if receipt_status not in (0, 1):
            raise ValueError("broadcast audit resolution corrupt")
        if type(block_number) is not int or block_number < 0:
            raise ValueError("broadcast audit resolution corrupt")


class LpBroadcaster:
    """Decode, authorize, submit once, and journal every outcome.

    The broadcaster is the last gate before the chain. It refuses to send when
    any declared binding is missing, when the endpoint is on another chain, or
    when the submission's result is not the exact transaction it authorized. An
    unknown result is journaled as unresolved and never retried by this module.
    """

    def __init__(self, policy: BroadcastPolicy, journal_path: Path, *, rpc: BroadcastRpc) -> None:
        if not isinstance(policy, BroadcastPolicy):
            raise TypeError("typed broadcast policy required")
        if not isinstance(journal_path, Path):
            raise TypeError("journal path required")
        if not isinstance(rpc, BroadcastRpc):
            raise TypeError("broadcast endpoint must implement the RPC protocol")
        self.policy = policy
        self.rpc = rpc
        self.journal = LpBroadcastJournal(journal_path, policy=policy)

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.journal.close()

    def broadcast(self, raw: bytes, *, expectation: BroadcastExpectation) -> BroadcastDecision:
        if not isinstance(expectation, BroadcastExpectation):
            raise TypeError("typed broadcast expectation required")
        if not isinstance(raw, bytes):
            raise TypeError("raw signed transaction must be bytes")
        observed = "0x" + keccak(raw).hex()
        try:
            transaction = decode_signed_transaction(raw)
        except (TypeError, ValueError) as exc:
            return self._deny(expectation, observed, f"raw transaction is refused: {exc}")
        if observed != expectation.transaction_hash:
            return self._deny(
                expectation, observed, "raw transaction is not the one the sidecar produced"
            )
        if observed in self.journal.seen_transactions():
            return self._deny(
                expectation,
                observed,
                "this transaction is already recorded in the broadcast journal",
                signer=transaction.signer,
            )
        authorization = authorize_broadcast(self.policy, expectation, transaction)
        if not authorization.authorized:
            return self._deny(
                expectation, observed, *authorization.reasons, signer=transaction.signer
            )
        try:
            endpoint_chain = self.rpc.chain_id()
        except Exception:  # noqa: BLE001 - an unreadable endpoint must refuse, never send
            return self._deny(
                expectation,
                observed,
                "broadcast endpoint chain is unreadable",
                signer=transaction.signer,
            )
        if endpoint_chain != expectation.chain_id:
            return self._deny(
                expectation,
                observed,
                "broadcast endpoint is on a different chain than the signed transaction",
                signer=transaction.signer,
            )
        # Commit before the irreversible network call. A crash at any later point
        # leaves an unknown outcome that blocks resend, including after restart.
        # BEGIN IMMEDIATE makes this claim atomic across competing processes.
        try:
            self.journal.append(
                request_hash=expectation.request_hash,
                transaction_hash=observed,
                outcome="unresolved",
                reasons=("submission reserved; outcome is unknown",),
                signer=transaction.signer,
                require_unseen=True,
            )
        except _AlreadyRecorded:
            return self._deny(
                expectation,
                observed,
                "this transaction is already recorded in the broadcast journal",
                signer=transaction.signer,
            )
        try:
            returned = self.rpc.send_raw_transaction("0x" + raw.hex())
        except Exception:  # noqa: BLE001 - the outcome is unknown; never retry a submission
            return self._unresolved(
                expectation, observed, transaction, "broadcast outcome is unknown"
            )
        try:
            submitted_hash = normalize_hash32(returned, "transaction hash")
        except (TypeError, ValueError):
            return self._unresolved(
                expectation,
                observed,
                transaction,
                "broadcast returned a malformed transaction hash",
            )
        if submitted_hash != observed:
            return self._unresolved(
                expectation,
                observed,
                transaction,
                "broadcast returned a different transaction hash",
            )
        self.journal.append(
            request_hash=expectation.request_hash,
            transaction_hash=observed,
            outcome="broadcast",
            reasons=(),
            signer=transaction.signer,
        )
        return BroadcastDecision(
            "broadcast", (), expectation.request_hash, observed, transaction.signer
        )

    def _deny(
        self,
        expectation: BroadcastExpectation,
        observed: str,
        *reasons: str,
        signer: str | None = None,
    ) -> BroadcastDecision:
        decision = BroadcastDecision(
            "denied", tuple(reasons), expectation.request_hash, observed, signer
        )
        self.journal.append(
            request_hash=expectation.request_hash,
            transaction_hash=observed,
            outcome="denied",
            reasons=decision.reasons,
            signer=signer,
        )
        return decision

    def _unresolved(
        self,
        expectation: BroadcastExpectation,
        observed: str,
        transaction: SignedLpTransaction,
        reason: str,
    ) -> BroadcastDecision:
        decision = BroadcastDecision(
            "unresolved", (reason,), expectation.request_hash, observed, transaction.signer
        )
        self.journal.append(
            request_hash=expectation.request_hash,
            transaction_hash=observed,
            outcome="unresolved",
            reasons=decision.reasons,
            signer=transaction.signer,
        )
        return decision


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _expect_fields(value: object, expected: set[str], name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError(f"{name} has invalid fields")
    return value


__all__ = [
    "BroadcastAuthorization",
    "BroadcastDecision",
    "BroadcastExpectation",
    "BroadcastPolicy",
    "BroadcastRpc",
    "HttpBroadcastRpc",
    "LpBroadcastJournal",
    "LpBroadcaster",
    "SignedLpTransaction",
    "authorize_broadcast",
    "broadcast_policy_from_json",
    "broadcast_policy_hash",
    "broadcast_policy_payload",
    "broadcast_policy_to_json",
    "decode_signed_transaction",
    "expectation_from_sidecar",
    "raw_bytes_from_decision",
]
