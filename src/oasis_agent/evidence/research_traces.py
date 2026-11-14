"""Validate the two call trace formats used in historical Ethereum research."""

from dataclasses import dataclass
from typing import TypedDict

from ..arbitrage.cycle_plan import address
from ..arbitrage.cycle_protocols import data_bytes
from .public_receipts import ObservedReceipt, quantity
from .rpc_validation import (
    strict_integer,
    strict_json_object,
    strict_list,
    strict_object,
    strict_string,
)

MAX_FRAMES = 4096
MAX_DEPTH = 64
CALL_TYPES = {"CALL", "STATICCALL", "DELEGATECALL"}
TracePath = tuple[int, ...]


@dataclass(frozen=True, slots=True)
class Frame:
    kind: str
    sender: str
    recipient: str
    data: bytes
    value: int
    error: str | None
    output: bytes | None


def frame(
    kind: object,
    sender: object,
    recipient: object,
    data: object,
    value: object,
    error: object,
    output: object,
) -> Frame:
    opcode = strict_string(kind, "trace kind")
    if opcode not in CALL_TYPES:
        raise ValueError("unsupported trace opcode")
    amount = quantity(value)
    if kind == "STATICCALL" and amount:
        raise ValueError("static frame with value")
    if error is not None and not strict_string(error, "trace error"):
        raise ValueError("empty error")
    if output is None and error is None:
        raise ValueError("missing successful trace output")
    return Frame(
        opcode,
        address(sender),
        address(recipient),
        data_bytes(data),
        amount,
        strict_string(error, "trace error") if error is not None else None,
        data_bytes(output) if output is not None else None,
    )


def flat_calls(value: object) -> dict[TracePath, Frame]:
    """Decode the complete Blockscout raw trace and validate claimed child slots."""
    rows = strict_list(value, "raw trace")
    if not 1 <= len(rows) <= MAX_FRAMES:
        raise ValueError("flat trace frame budget")
    frames: dict[TracePath, Frame] = {}
    counts = {}
    for item in rows:
        row = strict_object(item, "flat frame")
        if row.get("type") != "call" or ("error" in row and row["error"] is None):
            raise ValueError("invalid or unsupported flat frame")
        path = tuple(
            strict_integer(part, "trace index")
            for part in strict_list(row.get("traceAddress"), "trace path")
        )
        count = strict_integer(row.get("subtraces"), "subtraces")
        if (
            path in frames
            or len(path) > MAX_DEPTH
            or any(not 0 <= index < MAX_FRAMES for index in path)
            or not 0 <= count < MAX_FRAMES
        ):
            raise ValueError("invalid flat trace index")
        action = strict_object(row.get("action"), "trace action")
        result = (
            {}
            if row.get("result") is None and row.get("error") is not None
            else strict_object(row.get("result"), "trace result")
        )
        frames[path] = frame(
            strict_string(action.get("callType"), "trace call type").upper(),
            action.get("from"),
            action.get("to"),
            action.get("input"),
            action.get("value", "0x0") if action.get("callType") != "call" else action.get("value"),
            row.get("error"),
            result.get("output"),
        )
        counts[path] = count
    if (
        () not in frames
        or sum(counts.values()) != len(frames) - 1
        or any(
            path and (path[:-1] not in counts or path[-1] >= counts[path[:-1]]) for path in frames
        )
    ):
        raise ValueError("incomplete flat trace tree")
    verify_linkage(frames)
    return frames


def nested_calls(value: object) -> dict[TracePath, Frame]:
    """Decode Geth callTracer frames while retaining each failed ancestor."""
    frames: dict[TracePath, Frame] = {}
    pending: list[tuple[TracePath, object]] = [((), value)]
    while pending:
        path, item = pending.pop()
        if len(frames) + len(pending) >= MAX_FRAMES or len(path) > MAX_DEPTH:
            raise ValueError("nested trace frame budget")
        row = strict_object(item, "nested frame")
        children = strict_list(row.get("calls", []), "nested children")
        if len(frames) + len(pending) + len(children) >= MAX_FRAMES:
            raise ValueError("nested trace child budget")
        if "error" in row and row["error"] is None:
            raise ValueError("null trace error")
        # Geth omits the output key for empty returns and for failures with no revert data.
        frames[path] = frame(
            row.get("type"),
            row.get("from"),
            row.get("to"),
            row.get("input"),
            row.get("value", "0x0") if row.get("type") != "CALL" else row.get("value"),
            row.get("error"),
            row.get("output", "0x"),
        )
        pending.extend(((*path, index), child) for index, child in enumerate(children))
    verify_linkage(frames)
    return frames


def verify_linkage(frames: dict[TracePath, Frame]) -> None:
    for path, call in frames.items():
        if not path:
            if call.kind != "CALL":
                raise ValueError("trace root is not CALL")
            continue
        parent = frames[path[:-1]]
        caller = parent.sender if parent.kind == "DELEGATECALL" else parent.recipient
        if call.sender != caller:
            raise ValueError("trace caller context changed")


def first_failure(frames: dict[TracePath, Frame], path: TracePath) -> TracePath | None:
    return next(
        (path[:depth] for depth in range(len(path) + 1) if frames[path[:depth]].error), None
    )


class NativeMovement(TypedDict):
    path: TracePath
    first_failed_path: TracePath | None
    native_delta_raw: int


class TraceCorrection(TypedDict):
    frames: int
    correction: int
    movements: list[NativeMovement]


def trace_correction(
    body: bytes, receipt: ObservedReceipt, account: str, indexed: int
) -> TraceCorrection:
    frames = flat_calls(strict_json_object(b'{"frames":' + body + b"}")["frames"])
    root = frames[()]
    if (root.sender, root.recipient, root.error is None) != (
        receipt.sender,
        receipt.recipient,
        bool(receipt.status),
    ) or account in (root.sender, root.recipient):
        raise ValueError("trace root does not match retained receipt")
    movements: list[NativeMovement] = []
    for path, frame in sorted(frames.items()):
        if (
            not path
            or frame.kind != "CALL"
            or not frame.value
            or account not in (frame.sender, frame.recipient)
        ):
            continue
        native = ((frame.recipient == account) - (frame.sender == account)) * frame.value
        movements.append(
            {
                "path": path,
                "first_failed_path": first_failure(frames, path),
                "native_delta_raw": native,
            }
        )
    observed = sum(m["native_delta_raw"] for m in movements if m["first_failed_path"] is None)
    return {"frames": len(frames), "correction": observed - indexed, "movements": movements}
