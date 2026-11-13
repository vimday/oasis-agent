"""Synthetic Metric API capture; all balances and prices are invented test data."""

import sys
from pathlib import Path

from oasis_agent.arbitrage.metric import MetricPlan, requests
from oasis_agent.models.common import canonical_json, sha256_hex

P0, P1 = "0x" + "11" * 20, "0x" + "22" * 20
T0, T1 = "0x" + "aa" * 20, "0x" + "bb" * 20


def plan_data():
    return {
        "schema": "oasis-metric-plan-v1",
        "scenario": "synthetic",
        "chain_id": 1,
        "tokens": [
            {"address": T0, "symbol": "SYN18", "decimals": 18},
            {"address": T1, "symbol": "SYN6", "decimals": 6},
        ],
        "pools": [P0, P1],
        "start_token": 0,
        "sizes_raw": [10**18, 2 * 10**18],
        "max_age_seconds": 15,
        "max_skew_seconds": 2,
        "max_capture_seconds": 30,
        "capital_raw": 10**20,
        "commitments_raw": 0,
        "reserve_raw": 0,
        "costs": [
            {"kind": k, "raw": 0}
            for k in ("gas", "builder", "financing", "failure_buffer", "opportunity")
        ],
    }


def level(index, price, amount_in, amount_out):
    return {
        "binIdx": index,
        "price": str(price),
        "cumulativeInputVolume": str(amount_in),
        "cumulativeVolume": str(amount_out),
        "priceImpactE6": "0",
    }


def book(pool, bid=2000, ask=2001):
    return {
        "poolAddress": pool,
        "priceProviderStatus": "healthy",
        "serverTs": 100,
        "bidAdj": str(bid * 2**64),
        "askAdj": str(ask * 2**64),
        "totalToken0Available": str(10**20),
        "totalToken1Available": str(10**12),
        "depth": {
            "bids": [level(0, bid * 2**64, 10**19, bid * 10**7)],
            "asks": [level(0, ask * 2**64, ask * 10**7, 10**19)],
        },
    }


def inputs():
    metadata = {
        "data": [
            {
                "poolAddress": p,
                "contractVersion": "v1",
                "token0": T0,
                "token1": T1,
                "token0Decimals": 18,
                "token1Decimals": 6,
                "pauseLevel": 0,
            }
            for p in (P0, P1)
        ]
    }
    batch = {
        "results": [
            {"poolAddress": P0, "quote": book(P0, 2100, 2101)},
            {"poolAddress": P1, "quote": book(P1, 2000, 2001)},
        ]
    }
    return metadata, batch


def main() -> None:
    directory = Path(sys.argv[1])
    directory.mkdir()
    raw = canonical_json(plan_data())
    (directory / "plan.json").write_bytes(raw)
    plan = MetricPlan.model_validate_json(raw)
    exchanges = []
    for index, (spec, payload) in enumerate(zip(requests(plan), inputs(), strict=True)):
        request, response = canonical_json(spec), canonical_json(payload)
        (directory / f"{index}.request.json").write_bytes(request)
        (directory / f"{index}.response.json").write_bytes(response)
        exchanges.append(
            {
                "request_sha256": sha256_hex(request),
                "response_sha256": sha256_hex(response),
                "started": 101,
                "received": 101,
                "http_status": 200,
                "error": None,
            }
        )
    (directory / "capture.json").write_bytes(
        canonical_json(
            {
                "schema": "oasis-metric-capture-v1",
                "plan_sha256": sha256_hex(raw),
                "started": 101,
                "status": "complete",
                "exchanges": exchanges,
            }
        )
    )


if __name__ == "__main__":
    main()
