"""Retained discovery mutations and comparison; only feed bytes are historical."""

import base64
import hashlib
import json
import struct
from pathlib import Path

import pytest

from oasis_agent.evidence.pyth_candidate import (
    GENESIS,
    INITIAL,
    LOADER,
    RECEIVER,
    _base58,
    _receiver_config,
    compare_pyth_candidates,
    replay_pyth_candidate,
)
from oasis_agent.evidence.pyth_price import MAX_AGE_MS
from scripts.verify_pyth_candidate import main

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/pyth-solana-single-source-historical.json"
OPTIONS = {"commitment": "finalized", "encoding": "base64"}
BLOCK_OPTIONS = {
    "commitment": "finalized",
    "maxSupportedTransactionVersion": 1,
    "rewards": False,
    "transactionDetails": "none",
}


def _body(row, result, *, response_id=None):
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": row["request"]["id"] if response_id is None else response_id,
            "result": result,
        }
    ).encode()
    row["body_base64"] = base64.b64encode(body).decode()
    row["body_sha256"] = hashlib.sha256(body).hexdigest()


def _result(row):
    return json.loads(base64.b64decode(row["body_base64"]))["result"]


def _account(raw, owner, *, executable=False):
    return {
        "data": [base64.b64encode(raw).decode(), "base64"],
        "owner": owner,
        "executable": executable,
        "space": len(raw),
    }


def _config(*, target=None, emitters=((26, b"E" * 32),), minimum=3, tail=b""):
    fields = (
        hashlib.sha256(b"account:Config").digest()[:8]
        + b"G" * 32
        + (bytes(1) if target is None else bytes([1]) + target)
        + b"W" * 32
        + struct.pack("<I", len(emitters))
        + b"".join(struct.pack("<H32s", chain, emitter) for chain, emitter in emitters)
        + struct.pack("<QB", 7, minimum)
        + tail
    )
    return fields.ljust(370, bytes(1))


def _rows(source="first", *, shift=0, slot_shift=0):
    fixture = json.loads(FIXTURE.read_bytes())["responses"]
    feed_context = _result(fixture[0])
    slot = feed_context["context"]["slot"] + slot_shift
    time = _result(fixture[1])["blockTime"]
    pointers = (bytes(range(32)), bytes(range(1, 33)))
    programs = [_account(b"\x02\0\0\0" + ptr, LOADER, executable=True) for ptr in pointers]
    config = _account(_config(tail=b"earlier state"), RECEIVER)
    deployment = (
        b"\x03\0\0\0" + (slot - 100 - slot_shift).to_bytes(8, "little") + bytes(33) + b"\x7fELFmock"
    )
    data = [_account(deployment + ptr, LOADER) for ptr in pointers]
    values = feed_context["value"] + programs + [config]
    calls = [("getGenesisHash", [], GENESIS)]
    for index, selected in enumerate((values, values + data)):
        s = slot + index
        addresses = list(INITIAL) + (list(map(_base58, pointers)) if index else [])
        calls += [
            (
                "getMultipleAccounts",
                [addresses, OPTIONS.copy()],
                {
                    "context": {"slot": s},
                    "value": selected,
                },
            ),
            (
                "getBlock",
                [s, BLOCK_OPTIONS.copy()],
                {
                    "blockhash": _base58(hashlib.sha256(str(s).encode()).digest()),
                    "blockTime": time,
                },
            ),
        ]
    rows = []
    for i, (method, params, result) in enumerate(calls, 1):
        row = {
            "source": source,
            "status": 200,
            "retrieved_at_ms": time * 1000 + shift + i,
            "request": {"jsonrpc": "2.0", "id": i, "method": method, "params": params},
        }
        _body(row, result)
        rows.append(row)
    return rows


def _write(tmp_path, rows, name="first"):
    path = tmp_path / name
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    return path


@pytest.mark.parametrize(
    ("slot_shift", "scope"), [(0, "same_finalized_slot"), (2, "matching_bytes_distinct_slots")]
)
def test_candidate_agreement_is_clock_and_context_bound(tmp_path, slot_shift, scope):
    left = replay_pyth_candidate(_write(tmp_path, _rows()))
    right = replay_pyth_candidate(
        _write(tmp_path, _rows("second", shift=50, slot_shift=slot_shift), "second")
    )
    at_ms = right.exchanges[-1][0]
    assert compare_pyth_candidates(left, right, at_ms=at_ms) == scope
    with pytest.raises(ValueError, match="future"):
        compare_pyth_candidates(left, right, at_ms=at_ms - 1)
    boundary = min(price.publish_time for price in right.snapshots[1].prices) * 1000 + MAX_AGE_MS
    assert compare_pyth_candidates(left, right, at_ms=boundary) == scope
    with pytest.raises(ValueError, match="stale"):
        compare_pyth_candidates(left, right, at_ms=boundary + 1)


def test_alias_only_copy_does_not_supply_second_capture(tmp_path):
    left = replay_pyth_candidate(_write(tmp_path, _rows()))
    copied = replay_pyth_candidate(_write(tmp_path, _rows("second"), "second"))
    with pytest.raises(ValueError, match="distinct source"):
        compare_pyth_candidates(left, copied, at_ms=left.exchanges[-1][0])


@pytest.mark.parametrize("account_index", [0, 5])
def test_different_slot_disagreement_is_detected(tmp_path, account_index):
    left = replay_pyth_candidate(_write(tmp_path, _rows()))
    rows = _rows("second", slot_shift=2, shift=50)
    result = _result(rows[3])
    account = result["value"][account_index]
    raw = bytearray(base64.b64decode(account["data"][0]))
    raw[73 if account_index == 0 else -1] ^= 1
    account["data"][0] = base64.b64encode(raw).decode()
    _body(rows[3], result)
    right = replay_pyth_candidate(_write(tmp_path, rows, "second"))
    with pytest.raises(ValueError, match="bytes disagree"):
        compare_pyth_candidates(left, right, at_ms=right.exchanges[-1][0])


def test_same_slot_block_identity_cannot_disagree(tmp_path):
    left = replay_pyth_candidate(_write(tmp_path, _rows()))
    rows = _rows("second", shift=50)
    result = _result(rows[4])
    result["blockhash"] = _result(rows[2])["blockhash"]
    _body(rows[4], result)
    right = replay_pyth_candidate(_write(tmp_path, rows, "second"))
    with pytest.raises(ValueError, match="same-slot"):
        compare_pyth_candidates(left, right, at_ms=right.exchanges[-1][0])


@pytest.mark.parametrize(
    "mutation", ["pointer", "response_id", "response_hash", "block_slot", "deployment", "trailing"]
)
def test_discovery_rejects_wrong_evidence_even_without_peer(tmp_path, mutation):
    rows = _rows()
    if mutation == "pointer":
        rows[3]["request"]["params"][0][5] = RECEIVER
    elif mutation == "response_id":
        _body(rows[3], _result(rows[3]), response_id=True)
    elif mutation == "response_hash":
        rows[4]["body_sha256"] = "0" * 64
    elif mutation == "block_slot":
        rows[4]["request"]["params"][0] += 1
    elif mutation == "deployment":
        result = _result(rows[3])
        account = result["value"][5]
        raw = bytearray(base64.b64decode(account["data"][0]))
        raw[4:12] = rows[2]["request"]["params"][0].to_bytes(8, "little")
        account["data"][0] = base64.b64encode(raw).decode()
        _body(rows[3], result)
    else:
        rows.append(rows[-1])
    with pytest.raises(ValueError):
        replay_pyth_candidate(_write(tmp_path, rows))


def test_command_single_source_never_promotes(tmp_path, monkeypatch, capsys):

    path = _write(tmp_path, _rows())
    monkeypatch.setattr("sys.argv", ["verify_pyth_candidate", "--wire", str(path)])
    assert main() == 0
    report = json.loads(capsys.readouterr().out)
    assert report["scope"] == "single_source_historical"
    assert report["rpc_requests"] == 0
    for key in (
        "source_independence_verified",
        "receiver_trust_verified",
        "oracle_validated",
        "nav_verified",
        "execution_eligible",
    ):
        assert report[key] is False


def test_receiver_config_reads_variable_borsh_fields_and_allocation_tail():
    raw = _config(target=b"T" * 32, emitters=((26, b"E" * 32), (1, b"B" * 32)), tail=b"old")
    config = _receiver_config(raw)
    assert config.governance_authority == _base58(b"G" * 32)
    assert config.target_governance_authority == _base58(b"T" * 32)
    assert config.wormhole_program == _base58(b"W" * 32)
    assert [(e.chain, e.emitter_hex) for e in config.emitters] == [
        (26, (b"E" * 32).hex()),
        (1, (b"B" * 32).hex()),
    ]
    assert config.minimum_signatures == 3 and config.single_update_fee_lamports == 7
    assert config.allocation_tail_sha256 == hashlib.sha256(b"old".ljust(184, bytes(1))).hexdigest()


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        (_config()[:-1], "layout"),
        (_config()[:40] + bytes([2]) + _config()[41:], "authority option"),
        (_config()[:73] + bytes([255]) * 4 + _config()[77:], "emitter vector"),
        (_config(emitters=()), "emitter vector"),
        (_config(emitters=((0, b"E" * 32),)), "emitter identity"),
        (_config(emitters=((26, b"E" * 32),) * 2), "duplicate receiver emitter"),
        (_config(minimum=0), "minimum signatures"),
    ],
)
def test_receiver_config_rejects_malformed_fields(raw, reason):
    with pytest.raises(ValueError, match=reason):
        _receiver_config(raw)


def test_replay_binds_programdata_authority_and_payload(tmp_path):
    rows = _rows()
    result = _result(rows[3])
    account = result["value"][5]
    raw = bytearray(base64.b64decode(account["data"][0]))
    raw[12] = 1
    raw[13:45] = b"U" * 32
    account["data"][0] = base64.b64encode(raw).decode()
    _body(rows[3], result)
    capture = replay_pyth_candidate(_write(tmp_path, rows))
    assert capture.receiver_config.target_governance_authority is None
    receiver, push = capture.programdata
    assert receiver.last_deployed_slot == capture.snapshots[0].slot - 100
    assert receiver.upgrade_authority == _base58(b"U" * 32)
    assert receiver.program_payload_sha256 == hashlib.sha256(raw[45:]).hexdigest()
    assert push.upgrade_authority is None
    assert capture.snapshots[1].account_hashes[5] == hashlib.sha256(raw).hexdigest()

    raw[13:45] = bytes(32)
    account["data"][0] = base64.b64encode(raw).decode()
    _body(rows[3], result)
    with pytest.raises(ValueError, match="public key"):
        replay_pyth_candidate(_write(tmp_path, rows))
