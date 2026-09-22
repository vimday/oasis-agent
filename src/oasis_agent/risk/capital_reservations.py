"""One USD reservation ceiling shared by all chains and accounts in a canary.

Reservation is bookkeeping, not risk or signing approval. The trusted caller must
value the worst-case commitment including gas and independently verify settlement.
"""

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from ..models.common import canonical_json, normalize_address, sha256_hex


@dataclass(frozen=True, slots=True)
class CapitalReservation:
    intent_id: str
    chain_id: int
    account: str
    usd_micros: int
    state: str
    execution_eligible: bool = field(default=False, init=False)


class CapitalReservations:
    def __init__(self, path: Path, *, total_usd_micros: int, chains: tuple[int, ...]) -> None:
        if type(total_usd_micros) is not int or not 0 < total_usd_micros < 2**63:
            raise ValueError("positive global integer USD limit required")
        if (
            not chains
            or any(type(c) is not int or c <= 0 for c in chains)
            or len(set(chains)) != len(chains)
        ):
            raise ValueError("unique explicit chains required")
        self.limit = total_usd_micros
        self.chains = tuple(sorted(chains))
        self._policy = canonical_json({"limit": self.limit, "chains": self.chains})
        self._db = sqlite3.connect(path, timeout=5, isolation_level=None)
        self._db.execute("PRAGMA synchronous=FULL")
        try:
            self._db.execute("BEGIN IMMEDIATE")
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS reservation_policy "
                "(id INTEGER PRIMARY KEY, payload BLOB NOT NULL)"
            )
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS reservations "
                "(intent TEXT PRIMARY KEY, chain INTEGER NOT NULL, account TEXT NOT NULL, "
                "amount INTEGER NOT NULL, state TEXT NOT NULL, proof TEXT, digest TEXT NOT NULL)"
            )
            row = self._db.execute("SELECT payload FROM reservation_policy WHERE id=1").fetchone()
            if row is None:
                if self._db.execute("SELECT count(*) FROM reservations").fetchone()[0]:
                    raise ValueError("reservation policy missing")
                self._db.execute("INSERT INTO reservation_policy VALUES (1, ?)", (self._policy,))
            self._validate()
            self._db.commit()
        except BaseException:
            self._db.rollback()
            self._db.close()
            raise

    def _validate(self) -> None:
        row = self._db.execute("SELECT payload FROM reservation_policy WHERE id=1").fetchone()
        if row is None or bytes(row[0]) != self._policy:
            raise ValueError("global reservation policy changed")
        for intent, chain, account, amount, state, proof, digest in self._db.execute(
            "SELECT * FROM reservations"
        ):
            if sha256_hex(canonical_json([intent, chain, account, amount, state, proof])) != digest:
                raise ValueError("reservation integrity failure")
            self._check(intent, chain, account, amount)
            if state not in {"reserved", "submitted", "settled"}:
                raise ValueError("invalid reservation state")
            if (state == "settled") != (proof is not None):
                raise ValueError("settlement proof/state mismatch")
            if proof is not None:
                self._proof(proof)
        if self._used() > self.limit:
            raise ValueError("stored commitments exceed global cap")

    def _check(self, intent: str, chain: int, account: str, amount: int) -> None:
        if (
            not isinstance(intent, str)
            or not 1 <= len(intent) <= 128
            or not all(c.isascii() and (c.isalnum() or c in "_-:") for c in intent)
        ):
            raise ValueError("bounded public intent identifier required")
        if type(chain) is not int or chain not in self.chains:
            raise ValueError("chain not allowed")
        if account != normalize_address(account) or int(account, 16) == 0:
            raise ValueError("canonical nonzero account required")
        if type(amount) is not int or not 0 < amount <= self.limit:
            raise ValueError("commitment exceeds global cap or is invalid")

    @staticmethod
    def _proof(value: str) -> None:
        if (
            not isinstance(value, str)
            or len(value) != 64
            or any(c not in "0123456789abcdef" for c in value)
        ):
            raise ValueError("canonical reconciliation digest required")

    def _used(self) -> int:
        return int(
            self._db.execute(
                "SELECT COALESCE(SUM(amount), 0) FROM reservations WHERE state != 'settled'"
            ).fetchone()[0]
        )

    def reserve(
        self, intent: str, *, chain: int, account: str, usd_micros: int
    ) -> CapitalReservation:
        self._check(intent, chain, account, usd_micros)
        self._db.execute("BEGIN IMMEDIATE")
        try:
            self._validate()
            row = self._db.execute(
                "SELECT chain, account, amount, state FROM reservations WHERE intent=?", (intent,)
            ).fetchone()
            if row is not None:
                if tuple(row[:3]) != (chain, account, usd_micros):
                    raise ValueError("intent replay changed commitment")
                state = row[3]
            else:
                if self._used() + usd_micros > self.limit:
                    raise ValueError("cross-chain total capital limit exceeded")
                self._db.execute(
                    "INSERT INTO reservations VALUES (?, ?, ?, ?, 'reserved', NULL, ?)",
                    (
                        intent,
                        chain,
                        account,
                        usd_micros,
                        sha256_hex(
                            canonical_json([intent, chain, account, usd_micros, "reserved", None])
                        ),
                    ),
                )
                state = "reserved"
            self._db.commit()
            return CapitalReservation(intent, chain, account, usd_micros, state)
        except BaseException:
            self._db.rollback()
            raise

    def mark_submitted(self, intent: str) -> None:
        self._transition(intent, None)

    def settle(self, intent: str, *, reconciliation_digest: str) -> None:
        """Caller attests independently verified final settlement, never receipt timeout."""
        self._proof(reconciliation_digest)
        self._transition(intent, reconciliation_digest)

    def _transition(self, intent: str, proof: str | None) -> None:
        self._db.execute("BEGIN IMMEDIATE")
        try:
            self._validate()
            row = self._db.execute(
                "SELECT state, proof FROM reservations WHERE intent=?", (intent,)
            ).fetchone()
            if row is None:
                raise ValueError("unknown capital reservation")
            if proof is None:
                if row[0] == "settled":
                    raise ValueError("settled intent cannot be resubmitted")
                self._db.execute(
                    "UPDATE reservations SET state='submitted' WHERE intent=?", (intent,)
                )
            else:
                if row[0] == "settled" and row[1] != proof:
                    raise ValueError("settlement replay changed proof")
                self._db.execute(
                    "UPDATE reservations SET state='settled', proof=? WHERE intent=?",
                    (proof, intent),
                )
            updated = self._db.execute(
                "SELECT intent, chain, account, amount, state, proof FROM reservations "
                "WHERE intent=?",
                (intent,),
            ).fetchone()
            self._db.execute(
                "UPDATE reservations SET digest=? WHERE intent=?",
                (sha256_hex(canonical_json(list(updated))), intent),
            )
            self._db.commit()
        except BaseException:
            self._db.rollback()
            raise

    def used_usd_micros(self) -> int:
        self._db.execute("BEGIN")
        try:
            self._validate()
            return self._used()
        finally:
            self._db.rollback()

    def close(self) -> None:
        self._db.close()
