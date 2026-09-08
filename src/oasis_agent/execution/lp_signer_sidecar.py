"""Isolated LP signing sidecar with default no-key denial (WP7).

The sidecar independently rechecks the frozen static policy
(``authorize_lp_signing``), binds chain/account/config, applies its own fee
caps, and only then asks an explicitly configured external key provider for a
signature. With no provider, or a provider whose signature does not recover to
the policy account, the sidecar denies: it never holds key material and never
falls back to a local signing path. Every request, decision, and signature
hash is journaled append-only and hash-linked. No raw signature, key, or
mnemonic is ever journaled; only hashes are.

The EIP-1559 digest is constructed here from the exact envelope fields and is
the only value handed to the provider, so the provider cannot sign any other
payload ("no blind signing"). The assembled signed transaction is returned to
the operator/broadcaster, never broadcast by this module.
"""

import hashlib
import json
import sqlite3
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from types import TracebackType
from typing import Any, Protocol, Self, runtime_checkable

from eth_account import Account
from eth_hash.auto import keccak
from rlp import encode as rlp_encode  # type: ignore[import-untyped]

from ..models.common import (
    BlockRef,
    EvidenceRef,
    canonical_json,
    normalize_address,
    normalize_hash32,
    sha256_hex,
)
from .lp_signer_policy import (
    LpSignerPolicy,
    LpSigningRequest,
    authorize_lp_signing,
    calldata_hash,
)
from .lp_state_machine import LpIntent, LpIntentMode, TickRange

SECP256K1_ORDER = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
SIDE_CAR_SCHEMA = "oasis.lp-signer-sidecar.v1"
SIDE_CAR_REQUEST_SCHEMA = "oasis.lp-signer-request.v1"


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _expect_keys(value: object, expected: set[str], name: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError(f"{name} has invalid fields")
    return value


class StaleAccountNonce(Exception):
    """The journal already records the requested account nonce."""

    def __init__(self, nonce: int) -> None:
        super().__init__(f"account nonce {nonce} is stale or already signed")
        self.nonce = nonce


class UnresolvedAccountNonce(Exception):
    """A prior signing attempt may still consume an account nonce."""


@runtime_checkable
class SigningKeyProvider(Protocol):
    """External key authority. Implementations wrap an HSM, remote signing
    service, or vault; the sidecar never stores or receives key material.

    The provider receives only the exact EIP-1559 digest the sidecar built and
    must return the secp256k1 (r, s, y_parity) signature over it.
    """

    def sign(self, digest: bytes) -> tuple[int, int, int]:
        """Return (r, s, y_parity) for the 32-byte digest."""
        ...


class NullSigningKeyProvider:
    """Fail-closed provider used when no external key authority is wired."""

    def sign(self, digest: bytes) -> tuple[int, int, int]:
        raise RuntimeError("no signing key provider is configured")


@dataclass(frozen=True, slots=True)
class LpSidecarConfig:
    policy: LpSignerPolicy
    max_fee_per_gas: int
    max_priority_fee_per_gas: int
    journal_path: Path
    provider: SigningKeyProvider | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.policy, LpSignerPolicy):
            raise TypeError("typed signer policy required")
        for name in ("max_fee_per_gas", "max_priority_fee_per_gas"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.max_priority_fee_per_gas > self.max_fee_per_gas:
            raise ValueError("priority fee cannot exceed the max fee")
        if not isinstance(self.journal_path, Path):
            raise TypeError("journal path required")
        if self.provider is not None and not isinstance(self.provider, SigningKeyProvider):
            raise TypeError("key provider must implement the signing protocol")


@dataclass(frozen=True, slots=True)
class LpSidecarRequest:
    signing: LpSigningRequest
    max_fee_per_gas: int
    max_priority_fee_per_gas: int
    access_list: tuple[Any, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.signing, LpSigningRequest):
            raise TypeError("typed signing request required")
        for name in ("max_fee_per_gas", "max_priority_fee_per_gas"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.max_priority_fee_per_gas > self.max_fee_per_gas:
            raise ValueError("priority fee cannot exceed the max fee")
        if self.access_list != ():
            raise ValueError("sidecar forbids access lists")


@dataclass(frozen=True, slots=True)
class LpSidecarDecision:
    outcome: str
    reasons: tuple[str, ...]
    request_hash: str
    digest: str | None = None
    signature_hash: str | None = None
    transaction_hash: str | None = None
    raw_transaction_hex: str | None = None

    def __post_init__(self) -> None:
        if self.outcome not in {"denied", "signed"}:
            raise ValueError("sidecar outcome must be denied or signed")
        if self.outcome == "signed":
            if self.reasons or self.digest is None or self.signature_hash is None:
                raise ValueError("a signed outcome requires no reasons and full hashes")
            if self.transaction_hash is None or not self.raw_transaction_hex:
                raise ValueError("a signed outcome requires the assembled transaction")
        else:
            if not self.reasons:
                raise ValueError("a denied outcome requires explicit reasons")


def _address_bytes(address: str) -> bytes:
    return bytes.fromhex(normalize_address(address)[2:])


def _eip1559_fields(request: LpSidecarRequest) -> list[object]:
    """Canonical pre-signature EIP-1559 envelope fields (type 2)."""
    signing = request.signing
    intent = signing.intent
    return [
        intent.chain_id,
        signing.account_nonce,
        request.max_priority_fee_per_gas,
        request.max_fee_per_gas,
        signing.gas_limit,
        _address_bytes(signing.target),
        signing.value,
        signing.calldata,
        [],
    ]


def transaction_digest(request: LpSidecarRequest) -> bytes:
    """keccak256 of the type-2 envelope; the only value a provider may sign."""
    fields = _eip1559_fields(request)
    return keccak(b"\x02" + rlp_encode(fields))


def signed_transaction_bytes(request: LpSidecarRequest, *, r: int, s: int, y_parity: int) -> bytes:
    """Assemble the raw signed type-2 transaction from a provider signature."""
    for name, value, bound in (("r", r, SECP256K1_ORDER), ("s", s, SECP256K1_ORDER)):
        if type(value) is not int or not 1 <= value < bound:
            raise ValueError(f"{name} is outside the secp256k1 range")
    if s > SECP256K1_ORDER // 2:
        raise ValueError("high-s signatures are rejected (EIP-2)")
    if y_parity not in (0, 1):
        raise ValueError("y_parity must be 0 or 1")
    fields = [*_eip1559_fields(request), y_parity, r, s]
    return b"\x02" + bytes(rlp_encode(fields))


def recover_signer(raw: bytes) -> str:
    """Address recovered from a signed type-2 transaction (no key involved)."""
    return str(Account.recover_transaction(raw))


def sidecar_request_hash(request: LpSidecarRequest) -> str:
    identity = {
        "schema": SIDE_CAR_REQUEST_SCHEMA,
        "signing": {
            "intent": asdict(request.signing.intent),
            "account": request.signing.account,
            "target": request.signing.target,
            "calldata_hash": calldata_hash(request.signing.calldata),
            "value": request.signing.value,
            "account_nonce": request.signing.account_nonce,
            "executor_last_nonce": request.signing.executor_last_nonce,
            "gas_limit": request.signing.gas_limit,
            "current_block": asdict(request.signing.current_block),
            "simulation_block": asdict(request.signing.simulation_block),
            "simulation_succeeded": request.signing.simulation_succeeded,
            "simulation_calldata_hash": request.signing.simulation_calldata_hash,
            "observed_code_hashes": list(request.signing.observed_code_hashes),
            "risk_approved": request.signing.risk_approved,
            "risk_decision_hash": request.signing.risk_decision_hash,
            "evidence": [asdict(item) for item in request.signing.evidence],
        },
        "max_fee_per_gas": request.max_fee_per_gas,
        "max_priority_fee_per_gas": request.max_priority_fee_per_gas,
        "access_list": [],
    }
    return "0x" + keccak(canonical_json(identity)).hex()


class LpSignerSidecarJournal:
    """Append-only, hash-linked audit trail for sidecar requests and outcomes.

    The journal binds one policy fingerprint, chain, account, config hash, and
    fee-cap set. It records only request/decision/signature hashes and
    rejection reasons; raw signatures, keys, and mnemonics never appear.
    """

    def __init__(self, path: Path, *, config: LpSidecarConfig) -> None:
        self.path = path
        policy = config.policy
        self.policy_hash = sha256_hex(canonical_json(asdict(policy)))
        self.binding = canonical_json(
            {
                "schema": SIDE_CAR_SCHEMA,
                "chain_id": policy.chain_id,
                "account": policy.account,
                "config_hash": policy.config_hash,
                "policy_hash": self.policy_hash,
                "max_fee_per_gas": config.max_fee_per_gas,
                "max_priority_fee_per_gas": config.max_priority_fee_per_gas,
            }
        )
        # All journal access is serialized by the sidecar instance lock, so the
        # connection is deliberately shared across threads (check_same_thread
        # off). Never touch ``db`` outside ``handle``/``close``.
        self.db = sqlite3.connect(path, timeout=10, check_same_thread=False)
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
            self.db.execute(
                "CREATE TABLE IF NOT EXISTS nonce_reservations ("
                "account_nonce INTEGER PRIMARY KEY, request_hash TEXT UNIQUE NOT NULL, "
                "status TEXT NOT NULL CHECK(status IN ('reserved','signed','failed')))"
            )
            with self.db:
                self.db.execute("INSERT OR IGNORE INTO policy VALUES (1, ?)", (self.binding,))
                if self.db.execute("SELECT payload FROM policy WHERE id=1").fetchone() != (
                    self.binding,
                ):
                    raise ValueError("signer sidecar journal policy cannot change")
            self._max_signed_nonce = self._read()
        except Exception:
            self.db.close()
            raise

    def _read(self) -> int:
        if self.db.execute("SELECT payload FROM policy WHERE id=1").fetchone() != (self.binding,):
            raise ValueError("signer sidecar journal policy corrupt")
        previous = "0" * 64
        sequence = 0
        max_nonce = -1
        for seq, payload, digest in self.db.execute(
            "SELECT seq, payload, digest FROM events ORDER BY seq"
        ):
            data = json.loads(payload)
            if (
                seq != sequence + 1
                or data["seq"] != seq
                or data["previous"] != previous
                or hashlib.sha256(payload).hexdigest() != digest
                or data["policy_hash"] != self.policy_hash
            ):
                raise ValueError("signer sidecar audit chain corrupt")
            schema = data.get("schema")
            expected = {
                "schema",
                "seq",
                "previous",
                "policy_hash",
                "request_hash",
                "outcome",
                "reasons",
                "digest",
                "signature_hash",
                "transaction_hash",
                "account_nonce",
            }
            if schema != SIDE_CAR_SCHEMA or set(data) != expected:
                raise ValueError("signer sidecar audit schema corrupt")
            if canonical_json(data) != payload or data["outcome"] not in {
                "denied",
                "signed",
            }:
                raise ValueError("signer sidecar audit record corrupt")
            normalize_hash32(data["request_hash"])
            if data["outcome"] == "signed":
                for name in ("digest", "signature_hash", "transaction_hash"):
                    normalize_hash32(data[name])
                nonce = data["account_nonce"]
                if type(nonce) is not int or nonce < 0:
                    raise ValueError("signer sidecar nonce corrupt")
                max_nonce = max(max_nonce, nonce)
            previous = digest
            sequence = seq
        return max_nonce

    @property
    def max_signed_nonce(self) -> int:
        return self._max_signed_nonce

    def append(
        self,
        *,
        request_hash: str,
        outcome: str,
        reasons: tuple[str, ...],
        digest: str | None,
        signature_hash: str | None,
        transaction_hash: str | None,
        account_nonce: int,
        reservation_request_hash: str | None = None,
        reservation_status: str | None = None,
    ) -> None:
        """Append one hash-linked audit record.

        Event sequencing and an optional reservation finalization happen in one
        ``BEGIN IMMEDIATE`` transaction. The nonce itself must be reserved before
        an external provider is called.
        """
        if (reservation_request_hash is None) != (reservation_status is None):
            raise ValueError("reservation request and status must be supplied together")
        if reservation_status not in {None, "signed", "failed"}:
            raise ValueError("reservation final status must be signed or failed")
        try:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute("SELECT seq FROM events ORDER BY seq DESC LIMIT 1").fetchone()
            sequence = row[0] if row is not None else 0
            previous = self._tail_digest()
            payload = canonical_json(
                {
                    "schema": SIDE_CAR_SCHEMA,
                    "seq": sequence + 1,
                    "previous": previous,
                    "policy_hash": self.policy_hash,
                    "request_hash": request_hash,
                    "outcome": outcome,
                    "reasons": list(reasons),
                    "digest": digest,
                    "signature_hash": signature_hash,
                    "transaction_hash": transaction_hash,
                    "account_nonce": account_nonce,
                }
            )
            self.db.execute(
                "INSERT INTO events (seq, payload, digest) VALUES (?, ?, ?)",
                (sequence + 1, payload, hashlib.sha256(payload).hexdigest()),
            )
            if reservation_request_hash is not None:
                changed = self.db.execute(
                    "UPDATE nonce_reservations SET status=? "
                    "WHERE account_nonce=? AND request_hash=? AND status='reserved'",
                    (reservation_status, account_nonce, reservation_request_hash),
                ).rowcount
                if changed != 1:
                    raise ValueError("signer nonce reservation is missing or corrupt")
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise
        if outcome == "signed":
            self._max_signed_nonce = max(self._max_signed_nonce, account_nonce)

    def reserve_nonce(self, account_nonce: int, request_hash: str) -> None:
        """Reserve before signing so another process cannot obtain a signature."""
        normalize_hash32(request_hash)
        if type(account_nonce) is not int or account_nonce < 0:
            raise ValueError("account nonce must be a nonnegative integer")
        try:
            self.db.execute("BEGIN IMMEDIATE")
            unresolved = self.db.execute(
                "SELECT account_nonce FROM nonce_reservations "
                "WHERE status IN ('reserved','failed') LIMIT 1"
            ).fetchone()
            if unresolved is not None:
                raise UnresolvedAccountNonce(
                    f"account nonce {unresolved[0]} has an unresolved signing attempt"
                )
            row = self.db.execute("SELECT MAX(account_nonce) FROM nonce_reservations").fetchone()
            maximum = -1 if row is None or row[0] is None else row[0]
            if account_nonce <= maximum:
                raise StaleAccountNonce(account_nonce)
            self.db.execute(
                "INSERT INTO nonce_reservations VALUES (?, ?, 'reserved')",
                (account_nonce, request_hash),
            )
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

    def _tail_digest(self) -> str:
        row = self.db.execute("SELECT digest FROM events ORDER BY seq DESC LIMIT 1").fetchone()
        return row[0] if row is not None else "0" * 64

    def close(self) -> None:
        self.db.close()


class LpSignerSidecar:
    """Independent signing authority: static policy, fee caps, journal, provider.

    ``handle`` fails closed at every boundary: static policy denial, fee-cap
    violation, missing provider, provider failure, signature that does not
    recover to the policy account, or a stale/replayed account nonce all deny
    without producing a transaction.

    Concurrency: ``handle`` is serialized by a per-instance lock (check, sign,
    and journal append are one critical section), and the journal re-checks the
    account nonce inside a single ``BEGIN IMMEDIATE`` transaction so that even
    accidental multi-process use cannot sign the same nonce twice. Deployment
    topology must remain a single sidecar instance per policy account.
    """

    def __init__(self, config: LpSidecarConfig) -> None:
        if not isinstance(config, LpSidecarConfig):
            raise TypeError("typed sidecar config required")
        self.config = config
        self.journal = LpSignerSidecarJournal(config.journal_path, config=config)
        self._lock = threading.Lock()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.journal.close()

    def handle(self, request: LpSidecarRequest) -> LpSidecarDecision:
        with self._lock:
            return self._handle_locked(request)

    def _handle_locked(self, request: LpSidecarRequest) -> LpSidecarDecision:
        reasons: list[str] = []
        request_hash = sidecar_request_hash(request)
        signing = request.signing
        policy = self.config.policy
        static = authorize_lp_signing(policy, signing)
        reasons.extend(static.reasons)
        if request.max_fee_per_gas > self.config.max_fee_per_gas:
            reasons.append("max fee per gas exceeds the sidecar cap")
        if request.max_priority_fee_per_gas > self.config.max_priority_fee_per_gas:
            reasons.append("priority fee per gas exceeds the sidecar cap")
        if signing.account_nonce <= self.journal.max_signed_nonce:
            reasons.append("account nonce is stale or already signed")
        provider = self.config.provider
        if provider is None:
            reasons.append("no signing key provider is configured")

        if reasons:
            decision = LpSidecarDecision("denied", tuple(dict.fromkeys(reasons)), request_hash)
            self.journal.append(
                request_hash=request_hash,
                outcome="denied",
                reasons=decision.reasons,
                digest=None,
                signature_hash=None,
                transaction_hash=None,
                account_nonce=signing.account_nonce,
            )
            return decision

        # A missing provider always appends a reason above and returns, so the
        # narrowed assertion below is a documented invariant, not a fallback path.
        assert provider is not None

        try:
            self.journal.reserve_nonce(signing.account_nonce, request_hash)
        except (StaleAccountNonce, UnresolvedAccountNonce):
            decision = LpSidecarDecision(
                "denied",
                ("account nonce is stale or has an unresolved signing attempt",),
                request_hash,
            )
            self.journal.append(
                request_hash=request_hash,
                outcome="denied",
                reasons=decision.reasons,
                digest=None,
                signature_hash=None,
                transaction_hash=None,
                account_nonce=signing.account_nonce,
            )
            return decision

        digest = transaction_digest(request)
        try:
            r, s, y_parity = provider.sign(digest)
        except Exception:  # noqa: BLE001 - the provider is an external authority boundary;
            # any failure must deny with an audit record rather than propagate or retry
            decision = LpSidecarDecision(
                "denied",
                ("signing provider failed",),
                request_hash,
            )
            self.journal.append(
                request_hash=request_hash,
                outcome="denied",
                reasons=decision.reasons,
                digest="0x" + digest.hex(),
                signature_hash=None,
                transaction_hash=None,
                account_nonce=signing.account_nonce,
                reservation_request_hash=request_hash,
                reservation_status="failed",
            )
            return decision

        try:
            raw = signed_transaction_bytes(request, r=r, s=s, y_parity=y_parity)
        except ValueError as exc:
            decision = LpSidecarDecision(
                "denied",
                (f"provider signature invalid: {exc}",),
                request_hash,
            )
            self.journal.append(
                request_hash=request_hash,
                outcome="denied",
                reasons=decision.reasons,
                digest="0x" + digest.hex(),
                signature_hash=None,
                transaction_hash=None,
                account_nonce=signing.account_nonce,
                reservation_request_hash=request_hash,
                reservation_status="failed",
            )
            return decision

        try:
            recovered = normalize_address(recover_signer(raw))
        except Exception:  # noqa: BLE001 - a malformed provider signature must
            # deny with an audit record instead of propagating or retrying
            decision = LpSidecarDecision(
                "denied",
                ("provider signature cannot be recovered",),
                request_hash,
            )
            self.journal.append(
                request_hash=request_hash,
                outcome="denied",
                reasons=decision.reasons,
                digest="0x" + digest.hex(),
                signature_hash=None,
                transaction_hash=None,
                account_nonce=signing.account_nonce,
                reservation_request_hash=request_hash,
                reservation_status="failed",
            )
            return decision

        if recovered != policy.account:
            decision = LpSidecarDecision(
                "denied",
                ("provider signature does not recover to the policy account",),
                request_hash,
            )
            self.journal.append(
                request_hash=request_hash,
                outcome="denied",
                reasons=decision.reasons,
                digest="0x" + digest.hex(),
                signature_hash=None,
                transaction_hash=None,
                account_nonce=signing.account_nonce,
                reservation_request_hash=request_hash,
                reservation_status="failed",
            )
            return decision

        signature_hash = "0x" + sha256_hex(canonical_json({"r": r, "s": s, "y_parity": y_parity}))
        transaction_hash = "0x" + keccak(raw).hex()
        decision = LpSidecarDecision(
            "signed",
            (),
            request_hash,
            digest="0x" + digest.hex(),
            signature_hash=signature_hash,
            transaction_hash=transaction_hash,
            raw_transaction_hex=raw.hex(),
        )
        self.journal.append(
            request_hash=request_hash,
            outcome="signed",
            reasons=(),
            digest=decision.digest,
            signature_hash=signature_hash,
            transaction_hash=transaction_hash,
            account_nonce=signing.account_nonce,
            reservation_request_hash=request_hash,
            reservation_status="signed",
        )
        return decision


# --- JSON round-trips for the operator CLI (no secret material) ---


def sidecar_config_to_json(config: LpSidecarConfig) -> bytes:
    return canonical_json(
        {
            "policy": asdict(config.policy),
            "max_fee_per_gas": config.max_fee_per_gas,
            "max_priority_fee_per_gas": config.max_priority_fee_per_gas,
        }
    )


def sidecar_config_from_json(
    payload: bytes, *, journal_path: Path, provider: SigningKeyProvider | None = None
) -> LpSidecarConfig:
    data = _expect_keys(
        json.loads(payload, object_pairs_hook=_unique_object),
        {"policy", "max_fee_per_gas", "max_priority_fee_per_gas"},
        "sidecar config",
    )
    policy_data = _expect_keys(
        data["policy"],
        {
            "chain_id",
            "account",
            "executor",
            "pool_id",
            "config_hash",
            "code_hashes",
            "max_liquidity",
            "max_amount0",
            "max_amount1",
            "max_gas_units",
            "max_fee_pips",
            "max_state_age_blocks",
            "max_deadline_seconds",
        },
        "sidecar policy",
    )
    policy = LpSignerPolicy(**policy_data)
    return LpSidecarConfig(
        policy=policy,
        max_fee_per_gas=data["max_fee_per_gas"],
        max_priority_fee_per_gas=data["max_priority_fee_per_gas"],
        journal_path=journal_path,
        provider=provider,
    )


def _intent_to_json(intent: LpIntent) -> dict[str, Any]:
    data = asdict(intent)
    data["mode"] = intent.mode.value
    data["code_hashes"] = list(intent.code_hashes)
    return data


def _intent_from_json(data: dict[str, Any]) -> LpIntent:
    raw = dict(data)
    raw["mode"] = LpIntentMode(raw["mode"])
    raw["code_hashes"] = tuple(raw["code_hashes"])
    raw["old_range"] = TickRange(**raw["old_range"])
    raw["new_range"] = TickRange(**raw["new_range"])
    return LpIntent(**raw)


def sidecar_request_to_json(request: LpSidecarRequest) -> bytes:
    signing = request.signing
    return canonical_json(
        {
            "signing": {
                "intent": _intent_to_json(signing.intent),
                "account": signing.account,
                "target": signing.target,
                "calldata": "0x" + signing.calldata.hex(),
                "value": signing.value,
                "account_nonce": signing.account_nonce,
                "executor_last_nonce": signing.executor_last_nonce,
                "gas_limit": signing.gas_limit,
                "current_block": asdict(signing.current_block),
                "simulation_block": asdict(signing.simulation_block),
                "simulation_succeeded": signing.simulation_succeeded,
                "simulation_calldata_hash": signing.simulation_calldata_hash,
                "observed_code_hashes": list(signing.observed_code_hashes),
                "risk_approved": signing.risk_approved,
                "risk_decision_hash": signing.risk_decision_hash,
                "evidence": [asdict(item) for item in signing.evidence],
            },
            "max_fee_per_gas": request.max_fee_per_gas,
            "max_priority_fee_per_gas": request.max_priority_fee_per_gas,
            "access_list": [],
        }
    )


def sidecar_request_from_json(payload: bytes) -> LpSidecarRequest:
    data = _expect_keys(
        json.loads(payload, object_pairs_hook=_unique_object),
        {"signing", "max_fee_per_gas", "max_priority_fee_per_gas", "access_list"},
        "sidecar request",
    )
    signing = _expect_keys(
        data["signing"],
        {
            "intent",
            "account",
            "target",
            "calldata",
            "value",
            "account_nonce",
            "executor_last_nonce",
            "gas_limit",
            "current_block",
            "simulation_block",
            "simulation_succeeded",
            "simulation_calldata_hash",
            "observed_code_hashes",
            "risk_approved",
            "risk_decision_hash",
            "evidence",
        },
        "signing request",
    )
    intent_data = _expect_keys(
        signing["intent"],
        {
            "intent_id",
            "chain_id",
            "pool_id",
            "position_token_id",
            "mode",
            "target",
            "calldata_hash",
            "old_range",
            "new_range",
            "liquidity",
            "amount0_min",
            "amount1_min",
            "amount0_max",
            "amount1_max",
            "max_swap_input",
            "max_fee_pips",
            "max_gas_units",
            "nonce",
            "state_block",
            "state_hash",
            "deadline_timestamp",
            "config_hash",
            "code_hashes",
        },
        "LP intent",
    )
    intent = _intent_from_json(intent_data)
    evidence = tuple(
        EvidenceRef(
            **{
                **item,
                "block": BlockRef(**item["block"]),
            }
        )
        for item in signing["evidence"]
    )
    request = LpSigningRequest(
        intent=intent,
        account=signing["account"],
        target=signing["target"],
        calldata=bytes.fromhex(signing["calldata"][2:]),
        value=signing["value"],
        account_nonce=signing["account_nonce"],
        executor_last_nonce=signing["executor_last_nonce"],
        gas_limit=signing["gas_limit"],
        current_block=BlockRef(**signing["current_block"]),
        simulation_block=BlockRef(**signing["simulation_block"]),
        simulation_succeeded=signing["simulation_succeeded"],
        simulation_calldata_hash=signing["simulation_calldata_hash"],
        observed_code_hashes=tuple(signing["observed_code_hashes"]),
        risk_approved=signing["risk_approved"],
        risk_decision_hash=signing["risk_decision_hash"],
        evidence=evidence,
    )
    return LpSidecarRequest(
        signing=request,
        max_fee_per_gas=data["max_fee_per_gas"],
        max_priority_fee_per_gas=data["max_priority_fee_per_gas"],
        access_list=tuple(data["access_list"]),
    )


__all__ = [
    "LpSidecarConfig",
    "LpSidecarDecision",
    "LpSidecarRequest",
    "LpSignerSidecar",
    "LpSignerSidecarJournal",
    "NullSigningKeyProvider",
    "SigningKeyProvider",
    "recover_signer",
    "sidecar_config_from_json",
    "sidecar_config_to_json",
    "sidecar_request_from_json",
    "sidecar_request_hash",
    "sidecar_request_to_json",
    "signed_transaction_bytes",
    "transaction_digest",
]
