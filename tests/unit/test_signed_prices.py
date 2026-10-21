"""Real upstream signatures establish compatibility; mutations must fail closed."""

import base64
import copy
import json
import os
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest
from eth_hash.auto import keccak
from eth_keys.constants import SECPK1_N
from eth_keys.datatypes import PrivateKey

from oasis_agent.live import signed_prices as prices
from oasis_agent.models.common import canonical_json, sha256_hex

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/redstone-primary-prod-historical.json"
TIMESTAMP = 1719856900000


def packages():
    return json.loads(FIXTURE.read_bytes())


def verify(data, **kwargs):
    return prices.verify_prices(json.dumps(data).encode(), observed_at_ms=TIMESTAMP, **kwargs)


def test_official_production_signatures_without_mocked_trust():
    result = prices.verify_prices(FIXTURE.read_bytes(), observed_at_ms=TIMESTAMP)
    assert result["prices"]["ETH"]["median"] == 348695203485
    assert result["prices"]["USDC"]["median"] == 99988697
    for feed in prices.FEEDS:
        assert {p["signer"] for p in result["prices"][feed]["witnesses"]} == prices.SIGNERS
        assert result["prices"][feed]["decimals"] == 8
    assert result["execution_eligible"] is False
    digest = result.pop("evidence_hash")
    assert digest == sha256_hex(canonical_json(result))
    assert result["raw_hash"] == sha256_hex(FIXTURE.read_bytes())


@pytest.mark.parametrize(
    "offset,accepted", [(0, True), (1800000, True), (1800001, False), (-1, False)]
)
def test_freshness_boundary(offset, accepted):
    if accepted:
        prices.verify_prices(FIXTURE.read_bytes(), observed_at_ms=TIMESTAMP + offset)
    else:
        with pytest.raises(ValueError, match="stale or future"):
            prices.verify_prices(FIXTURE.read_bytes(), observed_at_ms=TIMESTAMP + offset)


@pytest.mark.parametrize("count", [3, 4, 5])
def test_three_to_five_unique_signers_and_floor_even_median(count):
    data = {f: rows[:count] for f, rows in packages().items()}
    result = verify(data)
    expected_eth = {3: 348695203485, 4: 348687101742, 5: 348695203485}
    assert result["prices"]["ETH"]["median"] == expected_eth[count]


@pytest.mark.parametrize("count", [0, 1, 2, 6])
def test_invalid_quorum_size(count):
    data = packages()
    data["USDC"] = (data["USDC"] * 2)[:count]
    with pytest.raises(ValueError, match="three to five"):
        verify(data)


def test_duplicate_signatures_do_not_create_quorum():
    data = packages()
    data["USDC"] = [data["USDC"][0]] * 3
    with pytest.raises(ValueError, match="duplicate signer"):
        verify(data)


def test_authentic_but_disagreeing_quotes_are_refused():
    with pytest.raises(ValueError, match="spread exceeded"):
        verify(packages(), max_spread_bps=1)


@pytest.mark.parametrize(
    "field,value",
    [
        ("timestampMilliseconds", TIMESTAMP + 1),
        ("timestampMilliseconds", True),
        ("timestampMilliseconds", 2**48),
        ("dataPackageId", "BTC"),
        ("dataServiceId", "untrusted"),
        ("signerAddress", "0x" + "00" * 20),
        ("signature", "invalid"),
        ("unknown", "ignored?"),
    ],
)
def test_tampered_package(field, value):
    data = packages()
    data["ETH"][0][field] = value
    data["ETH"][0]["isSignatureValid"] = True
    with pytest.raises(ValueError):
        verify(data)


@pytest.mark.parametrize(
    "field,value",
    [
        ("value", 3487),
        ("value", "3486.563328645609"),
        ("value", True),
        ("value", 0),
        ("value", -1),
        ("value", 10**13),
        ("dataFeedId", "USDC"),
        ("decimals", 6),
        ("decimals", True),
        ("valueByteSize", 31),
        ("unknown", 1),
    ],
)
def test_tampered_signed_point(field, value):
    data = packages()
    data["ETH"][0]["dataPoints"][0][field] = value
    with pytest.raises(ValueError):
        verify(data)


def test_unsigned_metadata_cannot_change_value_or_signer():
    data = packages()
    data["ETH"][0]["isSignatureValid"] = False
    data["ETH"][0]["dataPoints"][0]["metadata"] = {"value": 999, "signerAddress": "forged"}
    assert verify(data)["prices"] == verify(packages())["prices"]


def test_equivalent_raw_base64_value_authenticates_same_bytes():
    data = json.loads(FIXTURE.read_bytes(), parse_float=Decimal)
    package = data["ETH"][0]
    verified = prices.verify_package(package, "ETH")
    package["dataPoints"][0]["value"] = base64.b64encode(
        verified.answer.to_bytes(32, "big")
    ).decode()
    assert prices.verify_package(package, "ETH") == verified


def test_high_s_malleability_refused():
    data = packages()
    package = data["ETH"][0]
    sig = base64.b64decode(package["signature"])
    s = int.from_bytes(sig[32:64], "big")
    package["signature"] = base64.b64encode(
        sig[:32] + (SECPK1_N - s).to_bytes(32, "big") + bytes([55 - sig[64]])
    ).decode()
    with pytest.raises(ValueError, match="low-s"):
        verify(data)


@pytest.mark.parametrize(
    "kwargs",
    [{"max_age_ms": 1800001}, {"max_age_ms": True}, {"max_spread_bps": 41}, {"max_spread_bps": 0}],
)
def test_policy_cannot_be_widened(kwargs):
    with pytest.raises(ValueError, match="bounded observation"):
        verify(packages(), **kwargs)


@pytest.mark.parametrize(
    "raw",
    [
        b'{"ETH":[],"ETH":[],"USDC":[]}',
        b'{"ETH":NaN}',
        b'{"ETH":Infinity}',
        b"[]",
        b"{}",
        b"x" * 1000001,
    ],
)
def test_malformed_or_unbounded_json(raw):
    with pytest.raises(ValueError):
        prices.verify_prices(raw, observed_at_ms=TIMESTAMP)


def test_cross_feed_signed_timestamp_must_agree(monkeypatch):
    # Ephemeral test keys never enter a production policy. Sign bytes independently
    # of the implementation serializer; each individual package must authenticate.
    keys = [PrivateKey(os.urandom(32)) for _ in range(3)]
    monkeypatch.setattr(
        prices, "SIGNERS", frozenset(k.public_key.to_checksum_address().lower() for k in keys)
    )
    data = {}
    for feed, timestamp in [("ETH", TIMESTAMP), ("USDC", TIMESTAMP - 1)]:
        data[feed] = []
        for key in keys:
            payload = bytes.fromhex(
                feed.encode().hex().ljust(64, "0")
                + f"{100000000:064x}"
                + f"{timestamp:012x}"
                + "00000020"
                + "000001"
            )
            sig = key.sign_msg_hash(keccak(payload))
            package = {
                "dataPackageId": feed,
                "timestampMilliseconds": timestamp,
                "dataPoints": [{"dataFeedId": feed, "value": 1}],
                "signature": base64.b64encode(
                    sig.r.to_bytes(32, "big") + sig.s.to_bytes(32, "big") + bytes([sig.v + 27])
                ).decode(),
            }
            assert prices.verify_package(package, feed).timestamp_ms == timestamp
            data[feed].append(package)
    with pytest.raises(ValueError, match="one signed timestamp"):
        verify(data)


def run_cli(source, output, historical=True):
    command = [
        sys.executable,
        "-m",
        "scripts.verify_signed_prices",
        "--input",
        str(source),
        "--out",
        str(output),
    ]
    if historical:
        command += ["--at-ms", str(TIMESTAMP)]
    return subprocess.run(command, capture_output=True, text=True, timeout=15, check=False)


def test_cli_preserves_evidence_and_labels_historical_clock(tmp_path):
    output = tmp_path / "result.json"
    assert run_cli(FIXTURE, output).returncode == 0
    raw = output.read_bytes()
    report = json.loads(raw)
    assert report["clock_source"] == "operator_historical"
    assert report["network_calls"] == 0
    assert report["execution_eligible"] is False
    assert run_cli(FIXTURE, output).returncode != 0
    assert output.read_bytes() == raw


def test_current_clock_refuses_historical_fixture(tmp_path):
    output = tmp_path / "current.json"
    assert run_cli(FIXTURE, output, historical=False).returncode == 3
    report = json.loads(output.read_bytes())
    assert report["clock_source"] == "host"
    assert report["passed"] is False
    assert "result" not in report


@pytest.mark.parametrize("kind", ["symlink", "fifo", "malformed", "large"])
def test_cli_refuses_unsafe_inputs_without_echoing_content(tmp_path, kind):
    source, output = tmp_path / "input", tmp_path / "output.json"
    if kind == "symlink":
        source.symlink_to(FIXTURE)
    elif kind == "fifo":
        os.mkfifo(source)
    elif kind == "large":
        source.write_bytes(b"x" * 1000001)
    else:
        source.write_text("private-input-marker")
    completed = run_cli(source, output)
    assert completed.returncode == 3
    assert "private-input-marker" not in completed.stdout + completed.stderr + output.read_text()
    assert json.loads(output.read_bytes())["passed"] is False


def test_one_feed_cannot_stand_in_for_both():
    data = packages()
    data["USDC"] = copy.deepcopy(data["ETH"])
    with pytest.raises(ValueError):
        verify(data)
