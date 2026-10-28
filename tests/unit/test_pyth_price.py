"""Historical wire compatibility and hostile mutations; no network or live claims."""

import base64
import hashlib
import json
import struct
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from oasis_agent.evidence.pyth_price import FEEDS, decode_pyth_account

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/pyth-solana-single-source-historical.json"


def observation(symbol="ETH"):
    rows = json.loads(FIXTURE.read_bytes())["responses"]
    for row in rows:
        body = base64.b64decode(row["body_base64"], validate=True)
        assert hashlib.sha256(body).hexdigest() == row["body_sha256"]
    account_row, block_row = rows
    result = json.loads(base64.b64decode(account_row["body_base64"]))["result"]
    block = json.loads(base64.b64decode(block_row["body_base64"]))["result"]
    assert account_row["request"]["params"][1]["commitment"] == "finalized"
    assert block_row["request"]["params"][0] == result["context"]["slot"]
    index = ("ETH", "USDC").index(symbol)
    account = result["value"][index]
    return base64.b64decode(account["data"][0], validate=True), {
        "symbol": symbol,
        "account_address": account_row["request"]["params"][0][index],
        "reported_owner": account["owner"],
        "executable": account["executable"],
        "context_slot": result["context"]["slot"],
        "context_block_time": block["blockTime"],
        "observed_at_ms": account_row["retrieved_at_ms"],
    }


@pytest.mark.parametrize(
    "symbol,price,confidence,published,posted",
    [
        ("ETH", 234517299888, 187137750, 1793125672, 462171939),
        ("USDC", 99981780, 14904, 1793125700, 462172062),
    ],
)
def test_retained_historical_accounts(symbol, price, confidence, published, posted):
    raw, context = observation(symbol)
    fields = decode_pyth_account(raw, **context)
    assert fields.price_integer == price
    assert fields.confidence_integer == confidence
    assert fields.publish_time == published
    assert fields.posted_slot == posted
    assert fields.context_slot == 462172138
    assert fields.account_sha256 == hashlib.sha256(raw).hexdigest()
    assert fields.oracle_validated is False and fields.execution_eligible is False
    with pytest.raises(FrozenInstanceError):
        fields.price_integer = 1


@pytest.mark.parametrize("symbol", ["ETH", "USDC"])
@pytest.mark.parametrize("age_ms,accepted", [(0, True), (1_800_000, True), (1_800_001, False)])
def test_exact_owner_freshness_boundary(symbol, age_ms, accepted):
    raw, context = observation(symbol)
    published = struct.unpack_from("<q", raw, 93)[0]
    # Boundary fixtures are synthetic, distinct from the original retained context.
    context["context_block_time"] = published
    context["observed_at_ms"] = published * 1000 + age_ms
    if accepted:
        assert decode_pyth_account(raw, **context).oracle_validated is False
    else:
        with pytest.raises(ValueError, match="stale or future"):
            decode_pyth_account(raw, **context)


@pytest.mark.parametrize(
    "key,value",
    [
        ("symbol", "BTC"),
        ("account_address", FEEDS["USDC"][0]),
        ("reported_owner", "rec2HHDDnjLfj4kE7VyEtFA1HPGQLK33259532cRyHp"),
        ("executable", True),
        ("executable", 0),
        ("context_slot", 462171938),
        ("context_slot", True),
        ("context_block_time", 1793125671),
        ("context_block_time", 1793125719),
        ("observed_at_ms", 1793125671999),
        ("observed_at_ms", 1793125718941.0),
        ("observed_at_ms", 0),
        ("observed_at_ms", 2**63),
    ],
)
def test_identity_and_clock_mutations(key, value):
    raw, context = observation()
    context[key] = value
    with pytest.raises(ValueError):
        decode_pyth_account(raw, **context)


@pytest.mark.parametrize(
    "offset,replacement",
    [
        (0, b"\x00"),
        (40, b"\x00"),  # Partial moves subsequent fields by a byte; never parse as Full.
        (40, b"\x02"),
        (41, b"\x00"),
        (73, struct.pack("<q", 0)),
        (73, struct.pack("<q", -1)),
        (81, struct.pack("<Q", 234517299888)),
        (81, struct.pack("<Q", 2**64 - 1)),
        (89, struct.pack("<i", -6)),
        (93, struct.pack("<q", 1793125719)),
        (101, struct.pack("<q", 0)),
        (101, struct.pack("<q", 1793125673)),
        (125, struct.pack("<Q", 0)),
        (125, struct.pack("<Q", 462172139)),
        (133, b"\x01"),
    ],
)
def test_account_field_mutations(offset, replacement):
    raw, context = observation()
    changed = raw[:offset] + replacement + raw[offset + len(replacement) :]
    with pytest.raises(ValueError):
        decode_pyth_account(changed, **context)


@pytest.mark.parametrize("length", [0, 8, 40, 73, 132, 133, 135])
def test_unsupported_lengths(length):
    raw, context = observation()
    changed = (raw + b"\0")[:length]
    with pytest.raises(ValueError, match="layout"):
        decode_pyth_account(changed, **context)


def test_old_capture_cannot_be_reused_at_a_later_clock():
    raw, context = observation()
    context["observed_at_ms"] += 3_600_000
    with pytest.raises(ValueError, match="stale or future"):
        decode_pyth_account(raw, **context)
