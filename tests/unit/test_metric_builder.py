"""Synthetic storage claims, source-specific decoding, time and caller boundaries."""

import copy
import json

import pytest

from oasis_agent.arbitrage import metric_builder as m
from oasis_agent.models.common import canonical_json, sha256_hex
from scripts import research_titan_state as cli
from tests.unit import test_titan_state as titan

POOL = "0x" + "44" * 20
CATALOG = canonical_json(
    [
        {"pyth_lazer_id": 2, "symbol": "Crypto.ETH/USD", "exponent": -8},
        {"pyth_lazer_id": 7, "symbol": "Crypto.USDC/USD", "exponent": -8},
    ]
)


def plan_data():
    return {
        "schema": "oasis-metric-builder-feed-plan-v1",
        "layout": "metric-pyth-lazer-1.1.0-solc-0.8.33",
        "chain_id": 1,
        "oracle": titan.QUOTE,
        "pool": POOL,
        "catalog_sha256": sha256_hex(CATALOG),
        "feeds": [
            {
                "feed_id": 2,
                "symbol": "Crypto.ETH/USD",
                "token": titan.TOKEN_A,
                "max_age_seconds": 15,
            },
            {
                "feed_id": 7,
                "symbol": "Crypto.USDC/USD",
                "token": titan.TOKEN_B,
                "max_age_seconds": 15,
            },
        ],
    }


def plan(data=None):
    return m.BuilderFeedPlan.model_validate_json(json.dumps(data or plan_data()))


def word(mid=123456789012, spread=17, ref_ms=None):
    ms = titan.NOW // 1_000_000 - 4000 if ref_ms is None else ref_ms
    return "0x" + (mid | (spread << 64) | (65535 << 80) | (ms << 96)).to_bytes(32, "big").hex()


def snapshot():
    obj = titan.snapshot()
    v = obj["venues"][0]
    v["pricedAddresses"].append(POOL)
    v["stateOverride"] = {
        titan.QUOTE: {
            "balance": "0x1",
            "nonce": "0x2",
            "stateDiff": {m.feed_slot(i): word() for i in (2, 7)},
        },
        "0x" + "55" * 20: {"balance": "0x0", "nonce": "0x1"},
    }
    return obj


def inspect(obj=None):
    p = plan()
    return m.interpret(p, snapshot() if obj is None else obj, titan.NOW, m.catalog(p, CATALOG))


def test_layout_units_and_unproved_authority_are_explicit():
    # Independently fixed Solidity keccak mapping vector: abi.encode(uint32(2), uint256(1)).
    assert m.feed_slot(2) == "0xd9d16d34ffb15ba3a3d852f0d403e2ce1d691fb54de27ac87cd2f993f3ec330f"
    r = inspect()
    assert r["status"] == "decoded_builder_claims"
    assert len(r["feeds"]) == 2 and len(r["override_accounts"]) == 2
    f = r["feeds"][0]
    assert (f["mid_e8"], f["spread_0_01_bps"], f["spread1"]) == (123456789012, 17, 65535)
    assert f["reference_time_seconds"] == f["reference_time_ms"] // 1000
    assert f["target_age_seconds"] == 4 and f["reasons"] == []
    assert r["required_canonical_parent_number"] == 25999999
    assert r["canonical_base_block_hash"] is None and r["net_profit"] is None
    assert all(v is False for k, v in r.items() if k.endswith("verified"))
    assert not r["state_overrides_applied"] and not r["execution_eligible"]


@pytest.mark.parametrize("field,bad", [("chain_id", True), ("chain_id", 8453), ("layout", "other")])
def test_plan_rejects_unsupported_layout_or_chain(field, bad):
    data = plan_data()
    data[field] = bad
    with pytest.raises(ValueError):
        plan(data)


@pytest.mark.parametrize("bad", [True, 2.0, "2", 0, -1, 1 << 32])
def test_feed_id_is_exact_uint32_nonzero(bad):
    with pytest.raises(ValueError):
        m.feed_slot(bad)


@pytest.mark.parametrize(
    "field,bad",
    [
        ("feed_id", True),
        ("max_age_seconds", True),
        ("max_age_seconds", 0),
        ("max_age_seconds", 604801),
    ],
)
def test_plan_feed_bounds_are_strict(field, bad):
    data = plan_data()
    data["feeds"][0][field] = bad
    with pytest.raises(ValueError):
        plan(data)


@pytest.mark.parametrize("bad", ["0x01", "0x" + "01" * 32, "not_hex"])
def test_malformed_word_or_unmapped_padding_rejected(bad):
    with pytest.raises(ValueError):
        m.decode_word(bad)


@pytest.mark.parametrize(
    "age_ms,bad", [(15000, False), (15001, True), (15999, True), (16000, True)]
)
def test_solidity_seconds_floor_controls_age_boundary(age_ms, bad):
    obj = snapshot()
    obj["venues"][0]["stateOverride"][titan.QUOTE]["stateDiff"][m.feed_slot(2)] = word(
        ref_ms=titan.NOW // 1_000_000 - age_ms
    )
    # Target is integral seconds, so 15.999 seconds actual age floors refTime to 16 sec old.
    assert ("reference_too_old_at_target" in inspect(obj)["feeds"][0]["reasons"]) is bad


@pytest.mark.parametrize("spread,bad", [(0, False), (65533, False), (65534, True), (65535, True)])
def test_only_spread0_saturation_stalls(spread, bad):
    obj = snapshot()
    obj["venues"][0]["stateOverride"][titan.QUOTE]["stateDiff"][m.feed_slot(2)] = word(
        spread=spread
    )
    assert ("saturated_or_stalled_spread" in inspect(obj)["feeds"][0]["reasons"]) is bad


def test_feed_after_snapshot_is_not_accepted_based_only_on_target_time():
    obj = snapshot()
    v = obj["venues"][0]
    v["slot"] += 1  # Building a future block does not permit a future signed-feed claim.
    v["stateOverride"][titan.QUOTE]["stateDiff"][m.feed_slot(2)] = word(
        ref_ms=titan.NOW // 1_000_000 + 1
    )
    assert "reference_after_snapshot_or_retrieval" in inspect(obj)["feeds"][0]["reasons"]


@pytest.mark.parametrize(
    "change",
    ["code", "state", "other_storage", "unknown_slot", "missing_feed", "padding", "pool", "pair"],
)
def test_unknown_or_partial_overlay_cannot_publish_partial_prices(change):
    obj = snapshot()
    v = obj["venues"][0]
    fields = v["stateOverride"][titan.QUOTE]
    if change == "code":
        fields["code"] = "0x00"
    elif change == "state":
        fields["state"] = fields.pop("stateDiff")
    elif change == "other_storage":
        v["stateOverride"][POOL] = {"stateDiff": {m.feed_slot(2): word()}}
    elif change == "unknown_slot":
        fields["stateDiff"]["0x" + "00" * 32] = word()
    elif change == "missing_feed":
        del fields["stateDiff"][m.feed_slot(7)]
    elif change == "padding":
        fields["stateDiff"][m.feed_slot(2)] = "0x" + "01" * 32
    elif change == "pool":
        v["pricedAddresses"].remove(POOL)
    elif change == "pair":
        v["assetPairs"][0]["token_1"] = POOL
    r = inspect(obj)
    assert r["status"] == "rejected_or_incomplete" and r["reasons"]
    assert r["feeds"] == []
    assert inspect({"venues": []})["feeds"] == []  # No stale carry-forward.


def test_catalog_is_byte_bound_and_rejects_duplicate_or_ambiguous_ids():
    with pytest.raises(ValueError, match="digest"):
        m.catalog(plan(), CATALOG + b" ")
    for raw in (CATALOG[:-1] + b"," + CATALOG[1:], b'[{"pyth_lazer_id":2,"pyth_lazer_id":7}]'):
        p = plan_data()
        p["catalog_sha256"] = sha256_hex(raw)
        with pytest.raises(ValueError):
            m.catalog(plan(p), raw)
    p = plan_data()
    p["feeds"][0]["symbol"] = "Wrong/USD"
    with pytest.raises(ValueError, match="binding"):
        m.catalog(plan(p), CATALOG)


def test_valid_non_identifier_symbol_labels_remain_in_catalog():
    raw = json.loads(CATALOG) + [
        {"pyth_lazer_id": i, "symbol": s, "exponent": -8}
        for i, s in enumerate(
            ("Crypto.USD*/USD", "Crypto.USD0++/USD", "DEPRECATED FEED - A/USD"), 30
        )
    ]
    payload = canonical_json(raw)
    p = plan_data()
    p["catalog_sha256"] = sha256_hex(payload)
    assert len(m.catalog(plan(p), payload)) == 5


@pytest.mark.parametrize(
    "offset,mid,status",
    [
        (0, 123456789012, "duplicate"),
        (0, 8, "same_time_conflict"),
        (-1, 8, "reference_time_regression"),
        (1, 8, "advanced"),
    ],
)
def test_feed_version_conflicts_even_when_pair_sequence_advanced(offset, mid, status):
    old = inspect()
    obj = snapshot()
    obj["venues"][0]["pairSequences"][0] += 1
    obj["venues"][0]["stateOverride"][titan.QUOTE]["stateDiff"][m.feed_slot(2)] = word(
        mid=mid, ref_ms=titan.NOW // 1_000_000 - 4000 + offset
    )
    assert m.compare(old, inspect(obj))[0] == {"feed_id": 2, "status": status}


def test_normal_capture_replay_and_cli_retain_raw_byte_binding(tmp_path, monkeypatch):
    monkeypatch.setattr(titan, "snapshot", snapshot_for_capture)
    root, analysis, result, requests = titan.build_capture(tmp_path, monkeypatch)
    assert result == 0 and len(requests) == 3
    fp, cp = tmp_path / "feeds.json", tmp_path / "catalog.json"
    fp.write_bytes(canonical_json(plan_data()))
    cp.write_bytes(CATALOG)
    r = cli.replay(analysis, root / "states", fp, cp)
    assert r["status"] == "observed_declarations"
    assert all(s["metric_feeds"]["status"] == "decoded_builder_claims" for s in r["snapshots"])
    assert all(t["feed_changes"][0]["status"] == "duplicate" for t in r["transitions"])
    assert canonical_json(r) == canonical_json(cli.replay(analysis, root / "states", fp, cp))
    out = tmp_path / "result.json"
    monkeypatch.setattr(
        "sys.argv",
        [
            "research_titan_state",
            "--plan",
            str(analysis),
            "--capture",
            str(root / "states"),
            "--feed-plan",
            str(fp),
            "--feed-catalog",
            str(cp),
            "--output",
            str(out),
        ],
    )
    assert cli.main() == 0 and out.exists()
    assert cli.main() == 2
    with pytest.raises(ValueError, match="together"):
        cli.replay(analysis, root / "states", fp)
    cp.write_bytes(CATALOG + b" ")
    with pytest.raises(ValueError, match="digest"):
        cli.replay(analysis, root / "states", fp, cp)


@pytest.mark.parametrize("change", ["same_time_conflict", "reference_time_regression", "missing"])
def test_capture_replay_retains_but_rejects_inconsistent_feed_claims(tmp_path, monkeypatch, change):
    first, altered = snapshot(), snapshot()
    v = altered["venues"][0]
    v["pairSequences"][0] += 1
    diff = v["stateOverride"][titan.QUOTE]["stateDiff"]
    if change == "missing":
        del diff[m.feed_slot(2)]
    else:
        diff[m.feed_slot(2)] = word(
            mid=123, ref_ms=titan.NOW // 1_000_000 - 4000 - (change == "reference_time_regression")
        )
    snapshots = iter((first, altered, altered))
    monkeypatch.setattr(titan, "snapshot", lambda: copy.deepcopy(next(snapshots)))
    root, analysis, result, _ = titan.build_capture(tmp_path, monkeypatch)
    assert result == 0  # Transport completion does not approve the interpretation.
    fp, cp = tmp_path / "feeds.json", tmp_path / "catalog.json"
    fp.write_bytes(canonical_json(plan_data()))
    cp.write_bytes(CATALOG)
    r = cli.replay(analysis, root / "states", fp, cp)
    assert r["status"] == "incomplete_or_rejected"
    assert len(r["snapshots"]) == 3 and r["retained_bytes_verified"]
    reason = (
        "rejected_or_incomplete_feed_declaration"
        if change == "missing"
        else "conflicting_or_regressed_feed_declarations"
    )
    assert reason in r["reasons"]


# Capture fixture monkeypatch must not recurse through snapshot's original base helper.
_base_snapshot = titan.snapshot


def snapshot_for_capture():
    obj = _base_snapshot()
    v = obj["venues"][0]
    v["pricedAddresses"].append(POOL)
    v["stateOverride"] = {titan.QUOTE: {"stateDiff": {m.feed_slot(i): word() for i in (2, 7)}}}
    return copy.deepcopy(obj)
