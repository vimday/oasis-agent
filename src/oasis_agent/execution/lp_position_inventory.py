"""Block-pinned LP position inventory as two-provider agreed evidence.

The LP shadow decision needs ``inventory0`` and ``inventory1`` at the market
window's own decision block, and ``LpFeatureWindow`` refuses any value that is not
a content-hash-bound ``AgreedInteger``. The market window deliberately leaves
"reconciled_inventory" to a separate verifier, so a hand-written integer can never
satisfy it. This module closes that gap for a declared v4 position: it replays two
retained provider captures of the same NFT at the decision block, requires them to
agree, and derives the position's token amounts from the decision block's own
agreed pool price.

The amounts are chain facts, not estimates: principal follows from the position's
liquidity and tick range at that price, and the position's reported uncollected
fees are added, exactly as ``farming.inventory.position_inventory`` defines them.
There is no target weight, valuation currency, reference price or economic
assumption here, and nothing in this module signs, reserves capital or broadcasts.
"""

from dataclasses import asdict

from ..evidence.store import EvidenceRecord, EvidenceStore
from ..farming.inventory import PositionInventory, position_inventory
from ..farming.position_capture import PositionSnapshotCapture, replay_position_snapshot_evidence
from ..farming.position_quorum import PositionQuorum
from ..farming.positions import PositionSnapshot
from ..models.common import EvidenceRef
from ..models.position_scope import LpPositionScope
from ..shadow.features import AgreedInteger, agreed_value_payload
from ..shadow.market_window import MarketWindow

DERIVED_INVENTORY_KIND = "derived_position_integer"
INVENTORY_SCHEMA = "oasis.derived-position-inventory.v1"
INVENTORY_NAMES = ("inventory0", "inventory1")


def _decision_price(window: MarketWindow) -> AgreedInteger:
    """The one agreed pool price this window pinned at its own decision block."""
    prices = tuple(item for item in window.prices if item.block == window.decision_block)
    if len(prices) != 1 or prices[0].name != "sqrt_price_x96":
        raise ValueError("the window has no single decision-block pool price")
    return prices[0]


def _decision_inventory(
    window: MarketWindow,
    snapshots: tuple[PositionSnapshot, PositionSnapshot],
    *,
    scope: LpPositionScope,
) -> PositionInventory:
    """Bind the position identity to the window and the declared scope, then size it."""
    block = window.decision_block
    snapshot = snapshots[0]
    if snapshots[1] != snapshot:
        raise ValueError("position providers disagree on the position")
    if snapshot.block != block:
        raise ValueError("the position captures are not the window's decision block")
    if snapshot.pool_id != window.pool_id:
        raise ValueError("the position is not the market window's pool")
    if (
        snapshot.pool_id != scope.pool_id
        or snapshot.position_manager != scope.position_manager
        or snapshot.token_id != scope.token_id
        or snapshot.nft_owner != scope.owner
        or block.chain_id != scope.chain_id
    ):
        raise ValueError("the position does not match the declared scope")
    return position_inventory(snapshot, _decision_price(window).value)


def _retain_agreed(
    store: EvidenceStore,
    *,
    name: str,
    value: int,
    window: MarketWindow,
    snapshot: PositionSnapshot,
    parents: tuple[dict[str, object], ...],
    records: tuple[EvidenceRecord, ...],
) -> AgreedInteger:
    """Persist one agreed value per provider, borrowing each provider's own bytes.

    The request and response bytes are the position capture's wire envelope: it is
    the capture that read this NFT's state at the decision block. The
    interpretation says so rather than implying a separate read produced the value.
    """
    block = window.decision_block
    interpretation = {
        "schema": INVENTORY_SCHEMA,
        "execution_eligible": False,
        "bytes_kind": "position_snapshot_wire_envelope_that_pinned_this_value",
        "pool_id": window.pool_id,
        "name": name,
        "value": value,
        "block": asdict(block),
        "position_manager": snapshot.position_manager,
        "token_id": snapshot.token_id,
        "owner": snapshot.nft_owner,
        "parents": [dict(parent) for parent in parents],
    }
    payload = agreed_value_payload(name, value, block)
    references = tuple(
        store.put(
            DERIVED_INVENTORY_KIND,
            payload,
            block,
            record.reference.source,
            request=record.request_payload,
            raw_response=record.raw_response_payload,
            interpretation=interpretation,
            retrieved_at=record.reference.retrieved_at,
        )
        for record in records
    )
    return AgreedInteger(name, value, block, references)


def agreed_position_inventory(
    store: EvidenceStore,
    window: MarketWindow,
    position_references: tuple[EvidenceRef, EvidenceRef],
    *,
    scope: LpPositionScope,
) -> tuple[AgreedInteger, AgreedInteger]:
    """Retain and return the decision-block inventory of one declared position.

    Both captures, the window's own pool price and the declared scope are checked
    before the first write, so a refused input leaves no partially retained value
    behind. The two returned values carry the window's own two provider sources, as
    the cycle assembler requires, and are not authorization for anything.
    """
    if not isinstance(store, EvidenceStore):
        raise TypeError("an evidence store is required")
    if not isinstance(window, MarketWindow):
        raise TypeError("a verified market window is required")
    if not isinstance(scope, LpPositionScope):
        raise TypeError("typed LP position scope required")
    if (
        not isinstance(position_references, tuple)
        or len(position_references) != 2
        or position_references[0].source == position_references[1].source
    ):
        raise ValueError("two distinct position providers are required")
    captures: tuple[PositionSnapshotCapture, PositionSnapshotCapture] = (
        replay_position_snapshot_evidence(store, position_references[0]),
        replay_position_snapshot_evidence(store, position_references[1]),
    )
    quorum = PositionQuorum(captures[0], captures[1])
    if (
        quorum.left.header.ref != window.decision_block
        or quorum.right.header.ref != window.decision_block
    ):
        raise ValueError("the position captures are not the window's decision block")
    inventory = _decision_inventory(
        window, (quorum.left.snapshot, quorum.right.snapshot), scope=scope
    )
    price = _decision_price(window)
    window_sources = frozenset(reference.source for reference in price.evidence)
    if frozenset(capture.source for capture in captures) != window_sources:
        raise ValueError("position providers must be the window's own two providers")
    records = tuple(store.get_record(reference) for reference in position_references)
    if any(record.request_payload is None for record in records):
        raise ValueError("a position capture retained no wire request for reuse")
    parents = tuple(
        asdict(reference) for reference in sorted(position_references, key=lambda item: item.source)
    )
    values = (inventory.total0, inventory.total1)
    agreed = tuple(
        _retain_agreed(
            store,
            name=name,
            value=value,
            window=window,
            snapshot=quorum.left.snapshot,
            parents=parents,
            records=records,
        )
        for name, value in zip(INVENTORY_NAMES, values, strict=True)
    )
    return agreed[0], agreed[1]


__all__ = [
    "DERIVED_INVENTORY_KIND",
    "INVENTORY_NAMES",
    "INVENTORY_SCHEMA",
    "agreed_position_inventory",
]
