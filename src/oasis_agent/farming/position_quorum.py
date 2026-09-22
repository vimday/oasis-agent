"""Two captured position views must agree after independent raw-RPC reconstruction."""

import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime

from ..evidence.rpc_capture import BoundedExchangeRecorder
from ..evidence.store import EvidenceStore
from ..models.common import EvidenceRef
from ..rpc.client import RpcClient
from ..rpc.transport import JsonRpcTransport
from .position_capture import (
    PositionSnapshotCapture,
    collect_position_snapshot,
    persist_position_snapshot_evidence,
)


@dataclass(frozen=True, slots=True)
class PositionQuorum:
    left: PositionSnapshotCapture
    right: PositionSnapshotCapture
    retrieved_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        for capture in (self.left, self.right):
            if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", capture.source):
                raise ValueError("public source alias required")
            capture.validate()
        if (
            self.left.source == self.right.source
            or self.left.header != self.right.header
            or self.left.state_view != self.right.state_view
            or self.left.snapshot != self.right.snapshot
        ):
            raise ValueError("two position providers disagree or share an alias")

    def retain(self, store: EvidenceStore) -> tuple[EvidenceRef, EvidenceRef]:
        self.validate()
        return (
            persist_position_snapshot_evidence(store, self.left, retrieved_at=self.retrieved_at),
            persist_position_snapshot_evidence(store, self.right, retrieved_at=self.retrieved_at),
        )


def collect_position_quorum(
    primary: tuple[JsonRpcTransport, BoundedExchangeRecorder, str],
    secondary: tuple[JsonRpcTransport, BoundedExchangeRecorder, str],
    *,
    chain_id: int,
    position_manager: str,
    state_view: str,
    token_id: int,
    block_number: int,
) -> PositionQuorum:
    """Caller supplies fresh independent sessions with wired recorders and finite timeouts."""
    if type(chain_id) is not int or not 0 < chain_id < 2**256:
        raise ValueError("explicit bounded chain required")
    if primary[0] is secondary[0] or primary[1] is secondary[1] or primary[2] == secondary[2]:
        raise ValueError("independent capture sessions required")

    def capture(
        session: tuple[JsonRpcTransport, BoundedExchangeRecorder, str],
    ) -> PositionSnapshotCapture:
        transport, recorder, source = session
        rpc = RpcClient(transport, expected_chain_id=chain_id)
        header, snapshot = collect_position_snapshot(
            rpc,
            position_manager=position_manager,
            state_view=state_view,
            token_id=token_id,
            block_number=block_number,
        )
        return PositionSnapshotCapture(source, header, state_view, snapshot, recorder.exchanges)

    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="lp-position") as workers:
        left = workers.submit(capture, primary)
        right = workers.submit(capture, secondary)
        return PositionQuorum(left.result(), right.result())
