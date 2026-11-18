"""Capture or replay Equation News (`eqn.news`) items into immutable catalyst bundles.

Default mode is offline ``replay``; ``public-live`` makes at most one bounded
keyless HTTPS GET to ``https://eqn.news/api/v1/news``. Outputs are intelligence
captures only (``execution_eligible: false``) and never grant trading authority.
"""

import argparse
import os
import sys
import time
from collections import Counter
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any

from oasis_agent.intelligence.catalyst_scorer import CatalystSignal, classify_catalyst
from oasis_agent.intelligence.eqn_catalog import (
    EQN_CHANNEL_KEYS,
    MAX_EQN_PAYLOAD_BYTES,
    EqnBatchCapture,
    parse_news_payload,
)
from oasis_agent.intelligence.eqn_client import (
    EQN_REST_NEWS_URL,
    EqnClient,
    EqnFetchError,
)
from oasis_agent.models.common import canonical_json, sha256_hex

CAPTURE_SCHEMA_VERSION = "oasis.eqn-capture.v1"
CATALYSTS_SCHEMA_VERSION = "oasis.eqn-catalysts.v1"
MANIFEST_SCHEMA_VERSION = "oasis.eqn-manifest.v1"


def _write_new(path: Path, payload: bytes) -> None:
    if path.is_symlink():
        raise ValueError(f"refusing symlink output path: {path}")
    with path.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def _read_bounded(path: Path, limit: int = MAX_EQN_PAYLOAD_BYTES) -> bytes:
    if path.is_symlink():
        raise ValueError(f"refusing symlink input path: {path}")
    if not path.is_file():
        raise ValueError(f"input file not found: {path}")
    raw = path.read_bytes()
    if len(raw) > limit:
        raise ValueError("input payload exceeds bounded byte limit")
    return raw


def _serialize_batch(batch: EqnBatchCapture, *, mode: str, limit: int) -> bytes:
    payload: dict[str, Any] = {
        "schema_version": CAPTURE_SCHEMA_VERSION,
        "mode": mode,
        "capture_id": batch.capture_id,
        "channel": batch.channel,
        "limit": limit,
        "request_url": batch.request_url,
        "raw_sha256": batch.raw_sha256,
        "published_ts_ms": batch.published_ts_ms,
        "received_ts_ms": batch.received_ts_ms,
        "clock_skew_ms": batch.clock_skew_ms,
        "next_cursor": batch.next_cursor,
        "items": [asdict(item) for item in batch.items],
        "execution_eligible": False,
    }
    return bytes(canonical_json(payload)) + b"\n"


def _serialize_catalysts(
    signals: tuple[CatalystSignal, ...],
    *,
    capture_sha256: str,
    raw_input_sha256: str,
) -> bytes:
    counts = dict(sorted(Counter(sig.kind.value for sig in signals).items()))
    risk_freeze_count = sum(1 for sig in signals if sig.risk_freeze)
    payload: dict[str, Any] = {
        "schema_version": CATALYSTS_SCHEMA_VERSION,
        "execution_eligible": False,
        "capture_sha256": capture_sha256,
        "raw_input_sha256": raw_input_sha256,
        "signal_count": len(signals),
        "risk_freeze_count": risk_freeze_count,
        "counts_by_kind": counts,
        "signals": [
            {
                **asdict(sig),
                "kind": sig.kind.value,
                "direction": sig.direction.value,
            }
            for sig in signals
        ],
    }
    return bytes(canonical_json(payload)) + b"\n"


def run_capture(
    *,
    mode: str,
    channel: str,
    limit: int,
    out_dir: Path,
    input_path: Path | None = None,
    received_ts_ms: int | None = None,
    client: EqnClient | None = None,
) -> dict[str, Any]:
    """Run offline replay or bounded public-live capture and write immutable outputs."""
    if mode not in {"replay", "public-live"}:
        raise ValueError(f"unsupported mode: {mode!r}")
    if channel != "all" and channel not in EQN_CHANNEL_KEYS:
        raise ValueError(f"unsupported channel: {channel!r}")
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("limit must be an integer between 1 and 100")

    obs_ms = int(time.time() * 1000) if received_ts_ms is None else received_ts_ms
    if type(obs_ms) is not int or obs_ms <= 0:
        raise ValueError("received_ts_ms must be a positive integer")

    if out_dir.is_symlink():
        raise ValueError("refusing symlink output directory")
    out_dir.mkdir(parents=True, exist_ok=True)
    for filename in ("capture.json", "catalysts.json", "manifest.json"):
        if (out_dir / filename).exists():
            raise FileExistsError(f"refusing to overwrite existing {filename}")

    if mode == "replay":
        if input_path is None:
            raise ValueError("--input is required when --mode replay")
        raw_bytes = _read_bounded(input_path)
        batch = parse_news_payload(
            raw_bytes,
            channel=channel,
            request_url=EQN_REST_NEWS_URL,
            received_ts_ms=obs_ms,
        )
    else:
        if input_path is not None:
            raise ValueError("--input cannot be combined with --mode public-live")
        if client is not None:
            batch = client.fetch_news(channel=channel, limit=limit, received_ts_ms=obs_ms)
        else:
            with EqnClient(max_requests=1, timeout_s=10.0) as live_client:
                batch = live_client.fetch_news(channel=channel, limit=limit, received_ts_ms=obs_ms)

    signals = tuple(classify_catalyst(item) for item in batch.items)
    capture_bytes = _serialize_batch(batch, mode=mode, limit=limit)
    capture_sha256 = sha256_hex(capture_bytes)
    catalysts_bytes = _serialize_catalysts(
        signals,
        capture_sha256=capture_sha256,
        raw_input_sha256=batch.raw_sha256,
    )
    catalysts_sha256 = sha256_hex(catalysts_bytes)
    risk_freeze_count = sum(1 for sig in signals if sig.risk_freeze)

    manifest: dict[str, Any] = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "mode": mode,
        "channel": channel,
        "limit": limit,
        "execution_eligible": False,
        "raw_input_sha256": batch.raw_sha256,
        "capture_sha256": capture_sha256,
        "catalysts_sha256": catalysts_sha256,
        "item_count": len(batch.items),
        "signal_count": len(signals),
        "risk_freeze_count": risk_freeze_count,
        "published_ts_ms": batch.published_ts_ms,
        "received_ts_ms": batch.received_ts_ms,
        "clock_skew_ms": batch.clock_skew_ms,
    }
    manifest_bytes = canonical_json(manifest) + b"\n"

    _write_new(out_dir / "capture.json", capture_bytes)
    _write_new(out_dir / "catalysts.json", catalysts_bytes)
    _write_new(out_dir / "manifest.json", manifest_bytes)
    return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mode",
        choices=("replay", "public-live"),
        default="replay",
        help="Offline replay from --input JSON or single bounded public-live read.",
    )
    parser.add_argument(
        "--channel",
        default="all",
        help="EQN channel key or 'all'.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Item limit between 1 and 100.",
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=None,
        help="Input JSON payload path required for --mode replay.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        required=True,
        help="Output directory for immutable capture.json, catalysts.json, and manifest.json.",
    )
    parser.add_argument(
        "--received-ts-ms",
        type=int,
        default=None,
        help="Optional explicit millisecond observation timestamp for deterministic replay.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        manifest = run_capture(
            mode=args.mode,
            channel=args.channel,
            limit=args.limit,
            out_dir=args.out,
            input_path=args.input,
            received_ts_ms=args.received_ts_ms,
        )
    except (OSError, ValueError, TypeError, EqnFetchError) as exc:
        print(f"EQN catalyst capture refused: {exc}", file=sys.stderr)
        return 2
    print(
        f"eqn-capture mode={manifest['mode']} channel={manifest['channel']} "
        f"items={manifest['item_count']} freezes={manifest['risk_freeze_count']} "
        "execution_eligible=false"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
