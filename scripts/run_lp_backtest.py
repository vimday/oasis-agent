#!/usr/bin/env python3
"""Run an evidence-bound LP factorial backtest from a locked JSON input."""

import argparse
import json
from dataclasses import asdict, fields
from enum import Enum
from pathlib import Path
from typing import Any

from oasis_agent.farming.backtest import (
    ExperimentLock,
    LpReplayObservation,
    WalkForwardSpec,
    backtest_strategy,
)
from oasis_agent.models.common import BlockRef, EvidenceRef, canonical_json, sha256_hex


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _strict(data: dict[str, Any], cls: type[Any]) -> None:
    expected = {item.name for item in fields(cls) if item.init}
    if set(data) != expected:
        raise ValueError(
            f"{cls.__name__} fields mismatch: missing={sorted(expected - set(data))}, "
            f"unknown={sorted(set(data) - expected)}"
        )


def _block(data: dict[str, Any]) -> BlockRef:
    _strict(data, BlockRef)
    return BlockRef(**data)


def _reference(data: dict[str, Any]) -> EvidenceRef:
    _strict(data, EvidenceRef)
    decoded = dict(data)
    decoded["block"] = _block(data["block"]) if data["block"] is not None else None
    return EvidenceRef(**decoded)


def _observation(data: dict[str, Any]) -> LpReplayObservation:
    _strict(data, LpReplayObservation)
    decoded = dict(data)
    decoded["block"] = _block(data["block"])
    decoded["evidence"] = tuple(_reference(item) for item in data["evidence"])
    return LpReplayObservation(**decoded)


def _experiment(data: dict[str, Any]) -> ExperimentLock:
    _strict(data, ExperimentLock)
    decoded = dict(data)
    decoded["evidence"] = tuple(_reference(item) for item in data["evidence"])
    return ExperimentLock(**decoded)


def _jsonable(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): _jsonable(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(child) for child in value]
    if isinstance(value, float):
        raise TypeError("binary float cannot enter an LP research result")
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output must be a new file")
    document = json.loads(args.input.read_text(), object_pairs_hook=_unique_object)
    if not isinstance(document, dict) or set(document) != {
        "schema",
        "experiment",
        "walk_forward",
        "observations",
    }:
        raise ValueError("backtest document has an invalid envelope")
    if document["schema"] != "oasis.lp-backtest-input.v1":
        raise ValueError("unsupported backtest input schema")
    walk_data = document["walk_forward"]
    _strict(walk_data, WalkForwardSpec)
    result = backtest_strategy(
        tuple(_observation(item) for item in document["observations"]),
        _experiment(document["experiment"]),
        WalkForwardSpec(**walk_data),
    )
    payload = canonical_json(
        {
            "schema": "oasis.lp-backtest-result.v1",
            "input_sha256": sha256_hex(args.input.read_bytes()),
            "result": _jsonable(asdict(result)),
        }
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(payload + b"\n")
    print(f"LP backtest accepted={result.accepted}; evidence={args.output}")


if __name__ == "__main__":
    main()
