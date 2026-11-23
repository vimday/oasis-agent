"""Compare resident scans with stateless reports from verified full shadow snapshots."""

from collections.abc import Sequence
from dataclasses import dataclass, replace
from threading import Lock
from time import perf_counter_ns
from types import TracebackType
from typing import Literal, Self

from .bridge import FastpathBridge, FastpathCallError, FastpathUnavailableError
from .incremental_scanner import _pool_pricing_signature
from .pool_graph import CycleScanReport, PoolKind, PoolNode
from .resident_graph import AnchorScan, GraphBlock, ResidentPoolGraph

type _Transition = Literal["opened", "advanced", "repeated", "replaced_gap", "replaced_topology"]


class ResidentShadowError(FastpathCallError):
    """A resident comparison failed and the comparator has stopped."""


@dataclass(frozen=True, slots=True)
class ResidentShadowResult:
    block: GraphBlock
    transition: _Transition
    pool_count: int
    submitted_pool_rows: int
    delta_ns: int
    apply_ns: int
    scan_ns: int
    native_calls: int
    build_id: str


def _pool_rows(block: GraphBlock, pools: Sequence[PoolNode]) -> dict[str, PoolNode]:
    rows: dict[str, PoolNode] = {}
    if not pools:
        raise ResidentShadowError("resident shadow requires a complete nonempty pool snapshot")
    timestamp = pools[0].timestamp_ms
    for pool in pools:
        if (
            pool.chain_id != block.chain_id
            or pool.block_number != block.block_number
            or pool.timestamp_ms != timestamp
        ):
            raise ResidentShadowError(
                "resident shadow pool observations do not share the block pin"
            )
        key = pool.pool_id.strip().lower()
        if not key or key in rows:
            raise ResidentShadowError("resident shadow has an empty or duplicate pool ID")
        rows[key] = pool
    return rows


def _topology(pool: PoolNode) -> tuple[PoolKind, str, str, int, int]:
    return (
        pool.kind,
        pool.token0.strip().lower(),
        pool.token1.strip().lower(),
        pool.decimals0,
        pool.decimals1,
    )


def _plan(
    prior: GraphBlock | None,
    prior_rows: dict[str, PoolNode],
    block: GraphBlock,
    rows: dict[str, PoolNode],
) -> tuple[_Transition, tuple[PoolNode, ...]]:
    if prior is None:
        return "opened", tuple(rows.values())
    if block.chain_id != prior.chain_id or block.block_number < prior.block_number:
        raise ResidentShadowError("resident shadow chain changed or the block height regressed")
    if block.block_number == prior.block_number:
        if block != prior:
            raise ResidentShadowError(
                "resident shadow block identity conflicted at the same height"
            )
        if rows != prior_rows:
            raise ResidentShadowError("resident shadow repeated block has different pool state")
        return "repeated", ()
    if block.block_hash == prior.block_hash:
        raise ResidentShadowError("resident shadow reused a block hash at a different height")
    if block.block_number > prior.block_number + 1:
        if block.parent_hash == prior.block_hash:
            raise ResidentShadowError("resident shadow block gap has an inconsistent parent")
        return "replaced_gap", tuple(rows.values())
    if block.parent_hash != prior.block_hash:
        raise ResidentShadowError("resident shadow direct successor has a different parent")
    if prior_rows.keys() != rows.keys() or any(
        _topology(prior_rows[key]) != _topology(pool) for key, pool in rows.items()
    ):
        return "replaced_topology", tuple(rows.values())
    changed = tuple(
        pool
        for key, pool in rows.items()
        if _pool_pricing_signature(prior_rows[key]) != _pool_pricing_signature(pool)
    )
    return "advanced", changed


class ResidentShadowComparator:
    """Maintain one comparison session, with a latched stop after a failed check.

    The caller supplies independently verified full pool snapshots and baseline
    reports for each anchor in input order. Block binding here checks continuity
    and row metadata. It carries no proof of the source or data completeness.
    """

    def __init__(self, *, bridge: FastpathBridge | None = None) -> None:
        self._bridge = bridge
        self._graph: ResidentPoolGraph | None = None
        self._block: GraphBlock | None = None
        self._rows: dict[str, PoolNode] = {}
        self._stopped = False
        self._lock = Lock()

    def check(
        self,
        block: GraphBlock,
        pools: Sequence[PoolNode],
        anchors: Sequence[AnchorScan],
        baseline_reports: Sequence[CycleScanReport],
    ) -> ResidentShadowResult:
        """Check every model result exactly, ignoring only elapsed timing."""
        with self._lock:
            if self._stopped:
                raise ResidentShadowError("resident shadow comparison has stopped")
            # A failure or interruption at any stage blocks reuse of the native pin.
            self._stopped = True
            try:
                result = self._check(block, pools, tuple(anchors), tuple(baseline_reports))
            except ResidentShadowError:
                raise
            except (
                FastpathCallError,
                FastpathUnavailableError,
                KeyError,
                TypeError,
                ValueError,
            ) as exc:
                raise ResidentShadowError(f"resident shadow comparison failed: {exc}") from exc
            self._stopped = False
            return result

    def _check(
        self,
        block: GraphBlock,
        pools: Sequence[PoolNode],
        anchors: tuple[AnchorScan, ...],
        baseline: tuple[CycleScanReport, ...],
    ) -> ResidentShadowResult:
        if not anchors or len(baseline) != len(anchors):
            raise ResidentShadowError("resident shadow baseline does not cover the anchor batch")
        if any(report.sizing_residuals is None for report in baseline):
            raise ResidentShadowError("resident shadow baseline has unknown sizing coverage")

        started = perf_counter_ns()
        rows = _pool_rows(block, pools)
        transition, payload = _plan(self._block, self._rows, block, rows)
        delta_ns = perf_counter_ns() - started
        graph = self._graph
        started = perf_counter_ns()
        if graph is None:
            graph = ResidentPoolGraph(block, payload, bridge=self._bridge)
            self._graph = graph
            native_calls = 2  # Capability negotiation and open.
        elif transition == "advanced":
            graph.advance(block, payload)
            native_calls = 1
        elif transition == "repeated":
            native_calls = 0
        else:
            graph.replace(block, payload)
            native_calls = 1
        apply_ns = perf_counter_ns() - started

        started = perf_counter_ns()
        reports = graph.scan(block, anchors)
        scan_ns = perf_counter_ns() - started
        for anchor, actual, expected in zip(anchors, reports, baseline, strict=True):
            if replace(actual, elapsed_ns=0) != replace(expected, elapsed_ns=0):
                raise ResidentShadowError(
                    f"resident shadow differs from stateless baseline for {anchor.base_token}"
                )

        self._block = block
        self._rows = rows
        return ResidentShadowResult(
            block,
            transition,
            len(rows),
            len(payload),
            delta_ns,
            apply_ns,
            scan_ns,
            native_calls + 1,
            graph.build_id,
        )

    def close(self) -> None:
        with self._lock:
            self._stopped = True
            if self._graph is not None:
                self._graph.close()
                self._graph = None
            self._rows.clear()
            self._block = None

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        try:
            self.close()
        except FastpathCallError as close_error:
            if exc is None:
                raise
            exc.add_note(f"closing resident shadow also failed: {close_error}")
