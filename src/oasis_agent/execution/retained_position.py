"""Evidence-bound valuation of one declared v4 LP position.

A reader may count a position only when its USD value is reconstructed from
retained proof: two independent position snapshots that agree, the pool's slot0
window covering the position's own block, and one complete USD feed round per
pool currency. Every input is replayed from the evidence store and checked
against the declared identity, so a swapped pool, token, state view, provider or
block refuses instead of revaluing the position.

The module holds no key and exposes no signing or broadcast interface, and it
claims no account NAV on its own: binding a valuation into the policy inventory
is the manifest's and the reader's job.
"""

from dataclasses import dataclass

from ..evidence.store import EvidenceStore
from ..farming.position_capture import replay_position_snapshot_evidence
from ..farming.position_quorum import PositionQuorum
from ..farming.position_valuation import CurrencyPrice, PositionValuation, value_position
from ..models.common import EvidenceRef, normalize_address, normalize_hash32
from ..shadow.pool_state_capture import Slot0WindowQuorum, replay_slot0_window_evidence


@dataclass(frozen=True, slots=True)
class RetainedPosition:
    """The declared identity that retained position proof must satisfy.

    `position_manager` is compared as an address only: neither capture pins its
    runtime hash, so this module must not pretend that it did. The state view
    and pool manager runtime hashes are checked against the slot0 window, which
    does pin them.
    """

    chain_id: int
    position_manager: str
    state_view: str
    pool_id: str
    token_id: int
    state_view_runtime_keccak: str
    pool_manager_runtime_keccak: str

    def __post_init__(self) -> None:
        if type(self.chain_id) is not int or not 0 < self.chain_id < 2**256:
            raise ValueError("retained position requires an explicit bounded chain")
        if type(self.token_id) is not int or not 0 < self.token_id < 2**256:
            raise ValueError("retained position requires a bounded token id")
        object.__setattr__(self, "position_manager", normalize_address(self.position_manager))
        object.__setattr__(self, "state_view", normalize_address(self.state_view))
        object.__setattr__(self, "pool_id", normalize_hash32(self.pool_id))
        object.__setattr__(
            self, "state_view_runtime_keccak", normalize_hash32(self.state_view_runtime_keccak)
        )
        object.__setattr__(
            self,
            "pool_manager_runtime_keccak",
            normalize_hash32(self.pool_manager_runtime_keccak),
        )


def _require_declared_pair(
    pair: tuple[EvidenceRef, ...],
    providers: frozenset[str],
    kind: str,
) -> tuple[EvidenceRef, EvidenceRef]:
    """Only two distinct proofs, from exactly the declared providers, may support a value."""
    if len(pair) != 2 or pair[0] == pair[1]:
        raise ValueError(f"{kind} valuation requires two distinct retained proofs")
    if {reference.kind for reference in pair} != {kind}:
        raise ValueError(f"retained proof kind must be {kind}")
    if {reference.source for reference in pair} != providers:
        raise ValueError(f"{kind} valuation requires exactly the declared providers")
    return pair[0], pair[1]


def value_retained_position(
    store: EvidenceStore,
    *,
    retained: RetainedPosition,
    snapshots: tuple[EvidenceRef, EvidenceRef],
    pool_state: tuple[EvidenceRef, EvidenceRef],
    currency0: CurrencyPrice,
    currency1: CurrencyPrice,
    providers: frozenset[str],
    owner: str | None = None,
) -> PositionValuation:
    """Value one declared position from retained proof, or refuse.

    Both providers must have seen the same position and the same pool state, the
    pool state must cover the position's own block, and every declared identity
    must match what the proofs actually say. Nothing is inferred from absence.

    When `owner` is given the snapshot's own `ownerOf` answer must name it, so a
    caller can bind ownership and value in one replay instead of trusting that a
    separate capture agreed.
    """
    if not isinstance(retained, RetainedPosition):
        raise TypeError("retained position identity required")
    if len(providers) != 2 or any(not isinstance(name, str) or not name for name in providers):
        raise ValueError("exactly two declared providers required")
    snapshot_refs = _require_declared_pair(tuple(snapshots), providers, "position_snapshot")
    pool_refs = _require_declared_pair(tuple(pool_state), providers, "slot0_window")
    quorum = PositionQuorum(
        replay_position_snapshot_evidence(store, snapshot_refs[0]),
        replay_position_snapshot_evidence(store, snapshot_refs[1]),
    )
    snapshot = quorum.left.snapshot
    if snapshot.block.chain_id != retained.chain_id:
        raise ValueError("retained position is not on the declared chain")
    if normalize_address(snapshot.position_manager) != retained.position_manager:
        raise ValueError("retained position manager differs from the declaration")
    if snapshot.token_id != retained.token_id:
        raise ValueError("retained position token id differs from the declaration")
    if owner is not None and snapshot.nft_owner != normalize_address(owner):
        raise ValueError("retained position is not owned by the expected account")
    if snapshot.pool_id != retained.pool_id:
        raise ValueError("retained position pool differs from the declaration")
    if quorum.left.state_view != retained.state_view:
        raise ValueError("retained position state view differs from the declaration")
    window = Slot0WindowQuorum(
        replay_slot0_window_evidence(store, pool_refs[0]),
        replay_slot0_window_evidence(store, pool_refs[1]),
    )
    left = window.left
    if left.pool_id != retained.pool_id:
        raise ValueError("retained pool state window is a different pool")
    if left.state_view != retained.state_view:
        raise ValueError("retained pool state window uses a different state view")
    if left.state_view_runtime_keccak != retained.state_view_runtime_keccak:
        raise ValueError("state view runtime hash differs from the declaration")
    if left.pool_manager_runtime_keccak != retained.pool_manager_runtime_keccak:
        raise ValueError("pool manager runtime hash differs from the declaration")
    index = next(
        (position for position, header in enumerate(left.headers) if header.ref == snapshot.block),
        None,
    )
    if index is None:
        raise ValueError("pool state window does not cover the position block")
    return value_position(snapshot, left.states[index], currency0=currency0, currency1=currency1)


__all__ = ["RetainedPosition", "value_retained_position"]
