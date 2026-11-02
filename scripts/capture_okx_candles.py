"""Capture bounded OKX DEX historical candles as retained, hash-addressed research input.

Read-only: one authenticated GET per page to `/api/v6/dex/market/historical-candles`.
Raw page bytes are written verbatim with their sha256; `candles.jsonl` holds strictly
parsed, confirmed, strictly descending-then-sorted rows. This is a provider claim for
backtest/volatility research, not chain evidence or a settlement price. Never signs.

Run as `python -m scripts.capture_okx_candles --out reports/evidence/<dir> ...`.
"""

import argparse
import hashlib
import json
import time
import urllib.parse
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from oasis_agent.evidence.rpc_validation import strict_json_object
from oasis_agent.live.okx_price import (
    CREDENTIAL_NAMES,
    HOST,
    NATIVE,
    OKX_NATIVE,
    OkxError,
    https_request,
    signed_headers,
)
from scripts.capture_token_research import credentials

CANDLE_PATH = "/api/v6/dex/market/historical-candles"
BARS = {"1m": 60, "5m": 300, "15m": 900, "1H": 3600, "4H": 14400, "1D": 86400}
PAGE_LIMIT = 299
MAX_PAGES = 200
MAX_PAGE_BYTES = 500_000

Fetch = Callable[[str], bytes]


def parse_page(raw: bytes, bar_ms: int) -> list[dict[str, Any]]:
    """Strict rows: 8 string fields, positive finite OHLC, aligned, descending, unique."""
    payload = strict_json_object(raw)
    data = payload.get("data")
    if payload.get("code") != "0" or not isinstance(data, list):
        raise OkxError("candle response refused")
    rows: list[dict[str, Any]] = []
    for item in data:
        if (
            not isinstance(item, list)
            or len(item) != 8
            or not all(isinstance(v, str) for v in item)
            or not item[0].isdigit()
            or item[7] not in ("0", "1")
        ):
            raise OkxError("candle row refused")
        try:
            o, h, low, c, vol, vol_usd = (Decimal(v) for v in item[1:7])
        except InvalidOperation:
            raise OkxError("candle value not decimal") from None
        values = (o, h, low, c, vol, vol_usd)
        if (
            not all(v.is_finite() for v in values)
            or min(o, h, low, c) <= 0
            or min(vol, vol_usd) < 0
        ):
            raise OkxError("candle value out of range")
        if not (low <= min(o, c) and max(o, c) <= h):
            raise OkxError("candle OHLC inconsistent")
        ts = int(item[0])
        if ts % bar_ms:
            raise OkxError("candle not aligned to bar")
        if rows and ts >= rows[-1]["ts"]:
            raise OkxError("candles not strictly descending")
        rows.append(
            {
                "ts": ts,
                "open": item[1],
                "high": item[2],
                "low": item[3],
                "close": item[4],
                "volume": item[5],
                "volume_usd": item[6],
                "confirmed": item[7] == "1",
            }
        )
    return rows


def capture(
    out: Path,
    *,
    chain_id: int,
    token: str,
    bar: str,
    start_ms: int,
    end_ms: int,
    fetch: Fetch,
    now: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """Page backwards from `end_ms` to `start_ms`; keep confirmed rows in [start, end)."""
    if bar not in BARS:
        raise ValueError("unsupported bar")
    bar_ms = BARS[bar] * 1000
    if not 0 <= start_ms < end_ms or start_ms % bar_ms or end_ms % bar_ms:
        raise ValueError("window must be bar-aligned and non-empty")
    if (end_ms - start_ms) // bar_ms > PAGE_LIMIT * MAX_PAGES:
        raise ValueError("window exceeds page budget")
    token = token.lower()
    query_token = OKX_NATIVE if token == NATIVE else token
    out.mkdir(parents=True, exist_ok=False)
    pages, rows = [], {}
    cursor = end_ms
    for index in range(MAX_PAGES):
        params = {
            "chainIndex": str(chain_id),
            "tokenContractAddress": query_token,
            "bar": bar,
            "limit": str(PAGE_LIMIT),
            "after": str(cursor),
        }
        path = CANDLE_PATH + "?" + urllib.parse.urlencode(params)
        retrieved_at = int(now())
        raw = fetch(path)
        digest = hashlib.sha256(raw).hexdigest()
        (out / f"page-{index:03d}.json").write_bytes(raw)
        page = parse_page(raw, bar_ms)
        pages.append(
            {"index": index, "path": path, "sha256": digest, "rows": len(page), "at": retrieved_at}
        )
        if page and page[0]["ts"] >= cursor:
            raise OkxError("page ignored its cursor")
        for r in page:
            if start_ms <= r["ts"] < end_ms and r["confirmed"]:
                if r["ts"] in rows and rows[r["ts"]] != r:
                    raise OkxError("overlapping pages disagree")
                rows[r["ts"]] = r
        if not page or page[-1]["ts"] <= start_ms:
            break
        cursor = page[-1]["ts"]
    else:
        raise OkxError("page budget exhausted before window start")
    ordered = [rows[ts] for ts in sorted(rows)]
    missing = [
        ts for ts in range(start_ms, end_ms, bar_ms) if ts not in rows
    ]  # provider gaps stay explicit; nothing is interpolated
    lines = "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in ordered)
    (out / "candles.jsonl").write_text(lines)
    manifest = {
        "schema": "okx-dex-candles/v1",
        "source": HOST + CANDLE_PATH,
        "claim": "provider aggregate, not chain evidence",
        "chain_id": chain_id,
        "token": token,
        "bar": bar,
        "start_ms": start_ms,
        "end_ms": end_ms,
        "rows": len(ordered),
        "missing_bars": len(missing),
        "first_missing_ms": missing[:20],
        "candles_sha256": hashlib.sha256(lines.encode()).hexdigest(),
        "pages": pages,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n")
    return manifest


def okx_fetch(creds: dict[str, str]) -> Fetch:
    def fetch(path: str) -> bytes:
        headers = signed_headers(creds, "GET", path, "", datetime.now(UTC))
        try:
            return https_request("GET", HOST + path, headers, None, MAX_PAGE_BYTES)
        except Exception:  # noqa: BLE001 - transport errors may echo request headers
            raise OkxError("candle request failed (details redacted)") from None

    return fetch


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--chain-id", type=int, default=8453)
    parser.add_argument("--token", default=NATIVE)
    parser.add_argument("--bar", choices=sorted(BARS), default="1H")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    args = parser.parse_args()
    if not 1 <= args.days <= 365:
        raise SystemExit("--days must be 1-365")
    bar_ms = BARS[args.bar] * 1000
    end_ms = int(time.time() * 1000) // bar_ms * bar_ms
    start_ms = end_ms - args.days * 86_400_000
    creds = credentials(args.env_file, CREDENTIAL_NAMES)
    try:
        manifest = capture(
            args.out,
            chain_id=args.chain_id,
            token=args.token,
            bar=args.bar,
            start_ms=start_ms,
            end_ms=end_ms,
            fetch=okx_fetch(creds),
        )
    except OkxError as exc:
        raise SystemExit(f"capture refused: {exc}") from None
    summary = {k: manifest[k] for k in ("rows", "missing_bars", "candles_sha256", "bar")}
    print(json.dumps(summary | {"pages": len(manifest["pages"])}))


if __name__ == "__main__":
    main()
