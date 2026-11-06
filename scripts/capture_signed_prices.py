"""Capture one authenticated RedStone price candidate for Base NAV review."""

import argparse
import asyncio
import json
import os
import re
import time
from pathlib import Path

import httpx

from oasis_agent.live.signed_prices import FEEDS, MAX_RAW_BYTES, SERVICE, verify_prices
from oasis_agent.live.valuation import require_usdc_nav_band
from oasis_agent.models.common import canonical_json, sha256_hex

GATEWAY = "https://oracle-gateway.a.redstone.finance"
URL = f"{GATEWAY}/v2/data-packages/latest-by-data-feeds/{SERVICE}"
KEY_ENV = "REDSTONE_API_KEY"


class GatewayRefusal(ValueError):
    """Fixed status code for a response refused before package verification."""

    def __init__(self, status: int):
        self.status = status
        super().__init__("gateway HTTP status refused")


async def download(client: httpx.AsyncClient, credential: str) -> bytes:
    async with client.stream(
        "GET",
        URL,
        params={"dataFeedIds": ",".join(FEEDS)},
        headers={
            "X-API-Key": credential,
            "Accept": "application/json",
            "Accept-Encoding": "identity",
        },
    ) as response:
        if response.status_code != 200:
            raise GatewayRefusal(response.status_code)
        if response.headers.get("content-encoding", "identity").lower() != "identity":
            raise ValueError("uncompressed gateway response required")
        raw = bytearray()
        async for chunk in response.aiter_raw(chunk_size=16_384):
            if len(raw) + len(chunk) > MAX_RAW_BYTES:
                raise ValueError("gateway response exceeds byte bound")
            raw.extend(chunk)
    return bytes(raw)


async def fetch(credential: str) -> bytes:
    async with (
        asyncio.timeout(15),
        httpx.AsyncClient(
            follow_redirects=False,
            trust_env=False,
            timeout=5,
        ) as client,
    ):
        return await download(client, credential)


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True, help="New private evidence directory")
    args = parser.parse_args()
    try:
        args.out.mkdir(mode=0o700)
    except OSError:
        parser.error("new output directory under an existing parent required")

    report = {
        "schema": "oasis.redstone-price-acquisition.v1",
        "request": {"method": "GET", "url": URL, "feed_ids": list(FEEDS)},
        "service": SERVICE,
        "attempts": 0,
        "passed": False,
        "execution_eligible": False,
    }
    stage = "credential"
    try:
        credential = os.environ.get(KEY_ENV, "")
        if re.fullmatch(r"[A-Za-z0-9._~+/=-]{12,4096}", credential) is None:
            raise ValueError("gateway credential missing or malformed")
        stage = "gateway"
        report["started_at_ms"] = now_ms()
        report["attempts"] = 1
        raw = asyncio.run(fetch(credential))
        observed = now_ms()
        report.update(response_bytes=len(raw), observed_at_ms=observed, raw_sha256=sha256_hex(raw))
        stage = "verification"
        if not 0 <= observed - report["started_at_ms"] <= 15_000:
            raise ValueError("gateway observation deadline elapsed")
        result = verify_prices(raw, observed_at_ms=observed)
        require_usdc_nav_band(result["prices"]["USDC"]["median"])
        # Check both the exact response and decoded JSON strings before retaining
        # the body. A gateway may reflect a credential in unsigned metadata.
        if credential.encode() in raw or credential in json.dumps(
            json.loads(raw), ensure_ascii=False
        ):
            raise ValueError("gateway response reflects credential")
        stage = "publication"
        finished = now_ms()
        if not observed <= finished <= result["timestamp_ms"] + result["max_age_ms"]:
            raise ValueError("signed prices expired before publication")
        with (args.out / "prices.json").open("xb") as handle:
            handle.write(raw)
        report.update(passed=True, finished_at_ms=finished, result=result)
    except GatewayRefusal as exc:
        report.update(refusal_stage=stage, http_status=exc.status)
    except Exception:  # noqa: BLE001 - redact provider response and credential
        report["refusal_stage"] = stage

    report["evidence_hash"] = sha256_hex(canonical_json(report))
    with (args.out / "report.json").open("xb") as handle:
        handle.write(canonical_json(report) + b"\n")
    print(json.dumps({k: report[k] for k in ("passed", "attempts", "evidence_hash")}))
    return 0 if report["passed"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
