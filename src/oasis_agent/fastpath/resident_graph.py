"""Block-bound native pool graph for read-only shadow scans."""

from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from re import fullmatch
from threading import RLock
from types import TracebackType
from typing import Any, Self

from .bridge import FastpathBridge, FastpathCallError, require_int
from .pool_graph import (
    CycleCandidate,
    CycleScanReport,
    CycleSizingResidual,
    PoolNode,
    _parse_scan_report,
)

_ABI_VERSION = 1
_ABI_CAPABILITIES = {
    "graph_session_v1",
    "atomic_successor_updates",
    "batched_anchor_scan",
    "shadow_only",
}
_ZERO_HASH = "0x" + "0" * 64


@dataclass(frozen=True, slots=True)
class GraphBlock:
    chain_id: int
    block_number: int
    block_hash: str
    parent_hash: str

    def __post_init__(self) -> None:
        chain = require_int(self.chain_id, "chain_id", non_negative=True)
        height = require_int(self.block_number, "block_number", non_negative=True)
        if not 0 < chain < 2**64 or height >= 2**64:
            raise ValueError("graph block chain or height is outside its domain")
        for name, value in (("block_hash", self.block_hash), ("parent_hash", self.parent_hash)):
            if not isinstance(value, str) or fullmatch(r"0x[0-9a-fA-F]{64}", value) is None:
                raise ValueError(f"{name} must contain exactly 32 hex bytes")
            object.__setattr__(self, name, value.lower())
        if self.block_hash in (_ZERO_HASH, self.parent_hash) or (
            (self.parent_hash == _ZERO_HASH) != (height == 0)
        ):
            raise ValueError("invalid graph block hash or genesis parent")

    def to_wire(self) -> dict[str, object]:
        return {
            "chain_id": self.chain_id,
            "block_number": self.block_number,
            "block_hash": self.block_hash,
            "parent_hash": self.parent_hash,
        }


@dataclass(frozen=True, slots=True)
class AnchorScan:
    """Scan one anchor using its own integer input, profit and cost units."""

    base_token: str
    min_amount_in: int
    max_amount_in: int
    gas_cost_in_base: int = 0
    l1_fee_in_base: int = 0
    min_net_profit: int = 1
    per_hop_gas_cost_in_base: int = 0
    v2_hop_gas_cost_in_base: int | None = None
    cl_hop_gas_cost_in_base: int | None = None
    deduplicate_cycles: bool = False
    max_hops: int = 3

    def to_wire(self) -> dict[str, object]:
        if (
            not isinstance(self.base_token, str)
            or not self.base_token.isascii()
            or not self.base_token.strip()
        ):
            raise ValueError("base_token must be a non-empty ASCII identifier")
        max_hops = require_int(self.max_hops, "max_hops", non_negative=True)
        if not 2 <= max_hops <= 4:
            raise ValueError("max_hops must be between 2 and 4")
        if type(self.deduplicate_cycles) is not bool:
            raise TypeError("deduplicate_cycles must be a boolean")
        low = require_int(self.min_amount_in, "min_amount_in")
        high = require_int(self.max_amount_in, "max_amount_in")
        if not 0 < low <= high:
            raise ValueError("invalid anchor input bounds")

        wire: dict[str, object] = {
            "base_token": self.base_token.strip().lower(),
            "max_hops": max_hops,
            "deduplicate_cycles": self.deduplicate_cycles,
        }
        for name, value in (
            ("min_amount_in", low),
            ("max_amount_in", high),
            ("gas_cost_in_base", self.gas_cost_in_base),
            ("l1_fee_in_base", self.l1_fee_in_base),
            ("min_net_profit", self.min_net_profit),
            ("per_hop_gas_cost_in_base", self.per_hop_gas_cost_in_base),
            ("v2_hop_gas_cost_in_base", self.v2_hop_gas_cost_in_base),
            ("cl_hop_gas_cost_in_base", self.cl_hop_gas_cost_in_base),
        ):
            if value is not None:
                amount = require_int(value, name, non_negative=True)
                if amount >= 2**256:
                    raise ValueError(f"{name} exceeds uint256")
                wire[name] = str(amount)
        return wire


def _validate_binding(
    response: Mapping[str, Any], session_id: int, block: GraphBlock, count: int
) -> None:
    native_block = response.get("block")
    if (
        type(response.get("session_id")) is not int
        or response["session_id"] != session_id
        or response.get("valid") is not True
        or type(response.get("pool_count")) is not int
        or response["pool_count"] != count
        or not isinstance(native_block, dict)
        or type(native_block.get("chain_id")) is not int
        or type(native_block.get("block_number")) is not int
        or native_block != block.to_wire()
    ):
        raise FastpathCallError("native resident graph returned a different state binding")


class ResidentPoolGraph:
    """Own a native session. Every returned scan is checked against the requested block."""

    def __init__(
        self, block: GraphBlock, pools: Sequence[PoolNode], *, bridge: FastpathBridge | None = None
    ) -> None:
        self._bridge = FastpathBridge() if bridge is None else bridge
        self._lock = RLock()
        negotiated = self._bridge.eval_json({"cmd": "graph_session_capabilities"})
        capabilities = negotiated.get("capabilities")
        build = negotiated.get("build_id")
        maximum = negotiated.get("max_anchors")
        abi = negotiated.get("abi_version")
        if (
            negotiated.get("service") != "oasis-core"
            or type(abi) is not int
            or abi != _ABI_VERSION
            or not isinstance(capabilities, list)
            or not all(isinstance(capability, str) for capability in capabilities)
            or not _ABI_CAPABILITIES.issubset(capabilities)
            or not isinstance(build, str)
            or fullmatch(r"[0-9a-f]{64}", build) is None
            or type(maximum) is not int
            or maximum <= 0
        ):
            raise FastpathCallError("native resident graph ABI or capabilities are incompatible")
        self._max_anchors: int = maximum
        self._build_id = build
        self._v4_bitmap_steps = "v4_bitmap_steps" in capabilities
        self._chain_id = block.chain_id
        response = self._bridge.eval_json(
            {
                "cmd": "graph_session_open",
                "block": block.to_wire(),
                "pools": self._wire_pools(pools),
            }
        )
        session_id = response.get("session_id")
        if type(session_id) is not int or not 0 < session_id < 2**64:
            raise FastpathCallError("native resident graph session ID is invalid")
        try:
            _validate_binding(response, session_id, block, len(pools))
        except FastpathCallError:
            with suppress(FastpathCallError):
                self._bridge.eval_json({"cmd": "graph_session_close", "session_id": session_id})
            raise
        self._session: int | None = session_id
        self._block: GraphBlock | None = block
        self._pool_count = len(pools)

    @property
    def build_id(self) -> str:
        return self._build_id

    def _wire_pools(self, pools: Sequence[PoolNode]) -> list[dict[str, object]]:
        if not self._v4_bitmap_steps and any(pool.v4_tick_spacing is not None for pool in pools):
            raise FastpathCallError("native resident graph lacks V4 bitmap-step support")
        return [pool.to_wire() for pool in pools]

    def _session_id(self) -> int:
        if self._session is None:
            raise FastpathCallError("native resident graph is closed")
        return self._session

    def _require_block(self) -> None:
        if self._block is None:
            raise FastpathCallError("native resident graph requires a full replacement")

    def _update(
        self, command: str, block: GraphBlock, pools: Sequence[PoolNode], count: int
    ) -> None:
        session = self._session_id()
        self._block = None
        response = self._bridge.eval_json(
            {
                "cmd": command,
                "session_id": session,
                "block": block.to_wire(),
                "pools": self._wire_pools(pools),
            }
        )
        _validate_binding(response, session, block, count)
        self._pool_count = count
        self._block = block

    def advance(self, block: GraphBlock, changed_pools: Sequence[PoolNode]) -> None:
        """Update changed pools at the direct child. An empty update still advances the pin."""
        with self._lock:
            self._session_id()
            self._require_block()
            self._update("graph_session_advance", block, changed_pools, self._pool_count)

    def replace(self, block: GraphBlock, pools: Sequence[PoolNode]) -> None:
        """Resume from an independently verified complete snapshot on the same chain."""
        if block.chain_id != self._chain_id:
            raise ValueError("replacement must use the resident graph's chain")
        with self._lock:
            self._update("graph_session_replace", block, pools, len(pools))

    def scan(self, block: GraphBlock, anchors: Sequence[AnchorScan]) -> tuple[CycleScanReport, ...]:
        if not 1 <= len(anchors) <= self._max_anchors:
            raise ValueError("resident anchor batch size is outside native limits")
        wires = [anchor.to_wire() for anchor in anchors]
        tokens = [wire["base_token"] for wire in wires]
        if len(set(tokens)) != len(tokens):
            raise ValueError("resident anchor tokens must be distinct")
        with self._lock:
            session = self._session_id()
            self._require_block()
            self._block = None
            response = self._bridge.eval_json(
                {
                    "cmd": "graph_session_scan",
                    "session_id": session,
                    "block": block.to_wire(),
                    "anchors": wires,
                }
            )
            _validate_binding(response, session, block, self._pool_count)
            rows = response.get("reports")
            if (
                response.get("base_tokens") != tokens
                or not isinstance(rows, list)
                or len(rows) != len(wires)
            ):
                raise FastpathCallError("native resident graph returned an incomplete anchor batch")
            reports = tuple(
                self._parse_anchor_report(row, block.chain_id, token)
                for row, token in zip(rows, tokens, strict=True)
            )
            self._block = block
            return reports

    @staticmethod
    def _parse_anchor_report(row: object, chain_id: int, token: object) -> CycleScanReport:
        if not isinstance(row, dict):
            raise FastpathCallError("native resident graph anchor result must be an object")
        report = _parse_scan_report(row)
        if report.sizing_residuals is None:
            raise FastpathCallError("native resident graph omitted sizing coverage")
        rows: tuple[CycleCandidate | CycleSizingResidual, ...] = (
            *report.candidates,
            *report.sizing_residuals,
        )
        if (
            report.cycles_evaluated < 0
            or report.elapsed_ns < 0
            or any(result.chain_id != chain_id or result.base_token != token for result in rows)
        ):
            raise FastpathCallError("native resident graph returned invalid anchor results")
        return report

    def close(self) -> None:
        with self._lock:
            if self._session is None:
                return
            session = self._session
            self._block = None
            result = self._bridge.eval_json({"cmd": "graph_session_close", "session_id": session})
            if (
                type(result.get("session_id")) is not int
                or result["session_id"] != session
                or result.get("closed") is not True
            ):
                raise FastpathCallError("native resident graph did not acknowledge close")
            self._session = None

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
            exc.add_note(f"closing resident graph also failed: {close_error}")
