"""Discovery only: twelve synthetic raw exchanges never confer runtime approval."""

import json
import sys
from contextlib import closing
from copy import deepcopy
from dataclasses import asdict, replace

import pytest

from oasis_agent.evidence.exact_capture import wire_envelope
from oasis_agent.evidence.store import EvidenceStore, ReadOnlyEvidenceStore
from oasis_agent.live.gas_oracle import LEGACY_PIN_NAMES
from oasis_agent.live.gas_oracle_evidence import (
    INTERPRETATION,
    KIND,
    candidate_configuration,
    discovery_requests,
    replay_discovery_quorum,
    retain_discovery_quorum,
)
from oasis_agent.models.common import BlockRef, canonical_json, sha256_hex
from oasis_agent.rpc.transport import JsonRpcTransport, RpcExchange
from scripts import review_live_gas_oracle as cli
from tests.unit.gas_oracle_fixtures import synthetic_gas_pins
from tests.unit.test_live_lp import CONFIG as FULL_CONFIG

CONFIG = replace(
    FULL_CONFIG,
    expected_pins={k: v for k, v in FULL_CONFIG.expected_pins.items() if k in LEGACY_PIN_NAMES},
)
SOURCES = ("live_primary", "live_secondary")
BLOCK = BlockRef(8453, 42, "0x" + "42" * 32, 1_800_000_000)
IMPLEMENTATION = "0x" + "ab" * 20


def captures(*, secondary_change=None):
    header = {
        "number": hex(BLOCK.number),
        "hash": BLOCK.block_hash,
        "parentHash": "0x" + "41" * 32,
        "timestamp": hex(BLOCK.timestamp),
    }
    pair = []
    for source in SOURCES:
        values = [
            hex(BLOCK.chain_id),
            header,
            "0x6000",
            "0x" + "00" * 12 + "ab" * 20,
            "0x6000",
            header,
        ]
        calls = discovery_requests(BLOCK, IMPLEMENTATION)
        wires = [
            RpcExchange(
                method,
                i + 1,
                canonical_json({"jsonrpc": "2.0", "id": i + 1, "method": method, "params": params}),
                canonical_json({"jsonrpc": "2.0", "id": i + 1, "result": value}),
                1,
            )
            for i, ((method, params), value) in enumerate(zip(calls, values, strict=True))
        ]
        if secondary_change is not None and source == SOURCES[1]:
            secondary_change(wires)
        pair.append(wires)
    return pair[0], pair[1]


def change_result(wires, index, value):
    response = json.loads(wires[index].response_payload)
    response["result"] = value
    wires[index] = replace(wires[index], response_payload=canonical_json(response))


def change_request(wires, index, change):
    request = json.loads(wires[index].request_payload)
    change(request)
    wires[index] = replace(wires[index], request_payload=canonical_json(request))


def inputs(tmp_path, pair=None):
    wire = tmp_path / "wire.json"
    pair = captures() if pair is None else pair
    wire.write_bytes(
        canonical_json(
            {
                "block": asdict(BLOCK),
                "providers": [
                    {
                        "source": source,
                        "request": wire_envelope(raw, response=False),
                        "response": wire_envelope(raw, response=True),
                    }
                    for source, raw in zip(SOURCES, pair, strict=True)
                ],
            }
        )
    )
    config = tmp_path / "config.json"
    config.write_bytes(canonical_json(asdict(CONFIG)))
    return wire, config


def test_discovery_reopens_and_replays_twelve_wire_reads_without_online_capability(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        JsonRpcTransport, "request", lambda *_: pytest.fail("offline review attempted RPC")
    )
    path = tmp_path / "proof.db"
    with closing(EvidenceStore(path)) as store:
        refs = retain_discovery_quorum(store, BLOCK, captures(), sources=SOURCES)
        assert refs[0].source != refs[1].source and refs[0].content_hash == refs[1].content_hash
        for ref in refs:
            record = store.get_record(ref)
            assert len(json.loads(record.request_payload)["exchanges"]) == 6
            assert json.loads(record.payload)["runtime_approved"] is False
    with closing(ReadOnlyEvidenceStore(path)) as store:
        assert replay_discovery_quorum(store, refs, BLOCK, sources=SOURCES) == synthetic_gas_pins()
        candidate = candidate_configuration(
            store, refs, BLOCK, CONFIG, sources=SOURCES, now=BLOCK.timestamp
        )
    assert candidate["execution_eligible"] is False and candidate["runtime_approved"] is False
    assert (
        candidate["configuration_applied"] is False and candidate["state_write_supported"] is False
    )
    expected = asdict(CONFIG)
    expected["expected_pins"] |= synthetic_gas_pins()
    assert candidate["candidate_config"] == expected
    assert candidate["source_config_hash"] == sha256_hex(canonical_json(asdict(CONFIG)))
    assert candidate["candidate_config_hash"] == sha256_hex(canonical_json(expected))
    assert candidate["evidence"] == [asdict(ref) for ref in refs]


@pytest.mark.parametrize(
    "fault",
    [
        "chain",
        "opening",
        "closing",
        "parent",
        "empty_proxy",
        "empty_impl",
        "zero_slot",
        "padded_slot",
        "uppercase_slot",
        "bad_code",
        "impl_target",
        "slot_target",
        "slot_key",
        "latest",
        "noncanonical",
        "extra",
        "missing",
        "retry",
        "id",
        "error",
        "method",
        "proxy_target",
        "timestamp",
        "raw_code_disagreement",
        "impl_disagreement",
        "both_parents",
    ],
)
def test_bad_or_disagreeing_wire_cannot_write_even_one_evidence_record(tmp_path, fault):
    pair = captures()
    raw = pair[1]
    if fault == "chain":
        change_result(raw, 0, "0x1")
    elif fault in {"opening", "closing", "parent", "timestamp", "both_parents"}:
        indices = [1, 5] if fault == "both_parents" else [5 if fault == "closing" else 1]
        for index in indices:
            header = json.loads(raw[index].response_payload)["result"]
            header[
                {
                    "opening": "hash",
                    "closing": "hash",
                    "parent": "parentHash",
                    "timestamp": "timestamp",
                    "both_parents": "parentHash",
                }[fault]
            ] = "0x1" if fault == "timestamp" else "0x" + "99" * 32
            change_result(raw, index, header)
    elif fault in {
        "empty_proxy",
        "empty_impl",
        "zero_slot",
        "padded_slot",
        "uppercase_slot",
        "bad_code",
        "raw_code_disagreement",
        "impl_disagreement",
    }:
        index, value = {
            "empty_proxy": (2, "0x"),
            "empty_impl": (4, "0x"),
            "zero_slot": (3, "0x" + "00" * 32),
            "padded_slot": (3, "0x" + "11" * 32),
            "uppercase_slot": (3, "0x" + "00" * 12 + "AB" * 20),
            "bad_code": (2, "0x60 00"),
            "raw_code_disagreement": (2, "0x6001"),
            "impl_disagreement": (4, "0x6001"),
        }[fault]
        change_result(raw, index, value)
    elif fault in {
        "impl_target",
        "slot_target",
        "slot_key",
        "latest",
        "noncanonical",
        "proxy_target",
    }:

        def change(request):
            if fault == "latest":
                request["params"][-1] = "latest"
            elif fault == "noncanonical":
                request["params"][-1]["requireCanonical"] = False
            else:
                request["params"][1 if fault == "slot_key" else 0] = "0x" + "12" * (
                    32 if fault == "slot_key" else 20
                )

        change_request(
            raw,
            4 if fault == "impl_target" else 3 if fault in {"slot_target", "slot_key"} else 2,
            change,
        )
    elif fault == "extra":
        raw.append(raw[-1])
    elif fault == "missing":
        raw.pop()
    elif fault == "retry":
        raw[2] = replace(raw[2], attempt=2)
    elif fault == "id":
        raw[2] = replace(raw[2], request_id=raw[1].request_id)
    elif fault == "error":
        raw[2] = replace(
            raw[2],
            response_payload=canonical_json(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "error": {"code": -1, "message": "do not echo provider diagnostic"},
                }
            ),
        )
    else:
        raw[2] = replace(raw[2], method="eth_call")
    with closing(EvidenceStore(tmp_path / "proof.db")) as store:
        with pytest.raises(ValueError):
            retain_discovery_quorum(store, BLOCK, pair, sources=SOURCES)
        assert store._connection.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == 0


@pytest.mark.parametrize(
    "sources",
    [("same", "same"), ("https://secret.invalid", "secondary"), ("primary", ""), ("one",)],
)
def test_source_scope_refuses_before_evidence_write(tmp_path, sources):
    with closing(EvidenceStore(tmp_path / "proof.db")) as store:
        with pytest.raises(ValueError):
            retain_discovery_quorum(store, BLOCK, captures(), sources=sources)
        assert store._connection.execute("SELECT COUNT(*) FROM evidence").fetchone()[0] == 0


@pytest.mark.parametrize(
    "fault",
    ["summary", "approval", "interpretation", "block", "kind", "source", "duplicate", "wire"],
)
def test_rehashed_summary_or_wrong_scope_cannot_replace_raw_replay(tmp_path, fault):
    with closing(EvidenceStore(tmp_path / "proof.db")) as store:
        refs = retain_discovery_quorum(store, BLOCK, captures(), sources=SOURCES)
        record = store.get_record(refs[0])
        payload = json.loads(record.payload)
        request = json.loads(record.request_payload)
        interpretation = dict(INTERPRETATION)
        if fault == "summary":
            payload["observed_pins"]["gas_oracle_proxy"] = "0x" + "99" * 32
        elif fault == "approval":
            payload["runtime_approved"] = True
        elif fault == "interpretation":
            interpretation["execution_eligible"] = True
        elif fault == "wire":
            item = request["exchanges"][2]
            wire = json.loads(bytes.fromhex(item["payload"]))
            wire["params"][-1] = "latest"
            item["payload"] = canonical_json(wire).hex()
        forged = store.put(
            KIND if fault != "kind" else "other",
            canonical_json(payload),
            replace(BLOCK, number=43) if fault == "block" else BLOCK,
            "foreign" if fault == "source" else SOURCES[0],
            request=request,
            raw_response=record.raw_response_payload,
            interpretation=interpretation,
        )
        with pytest.raises(ValueError):
            replay_discovery_quorum(
                store, (forged, forged if fault == "duplicate" else refs[1]), BLOCK, sources=SOURCES
            )


@pytest.mark.parametrize("offset, accepted", [(-1, False), (0, True), (1800, True), (1801, False)])
def test_proposal_expiry_is_explicit_and_never_widens_past_1800(tmp_path, offset, accepted):
    with closing(EvidenceStore(tmp_path / "proof.db")) as store:
        refs = retain_discovery_quorum(store, BLOCK, captures(), sources=SOURCES)
        if not accepted:
            with pytest.raises(ValueError, match="current"):
                candidate_configuration(
                    store, refs, BLOCK, CONFIG, sources=SOURCES, now=BLOCK.timestamp + offset
                )
        else:
            result = candidate_configuration(
                store, refs, BLOCK, CONFIG, sources=SOURCES, now=BLOCK.timestamp + offset
            )
            assert result["valid_until"] == BLOCK.timestamp + 1800


@pytest.mark.parametrize(
    "change",
    [
        {"usdc_oracle_max_age_seconds": 90000},
        {"oracle_max_age_seconds": 1801},
        {"expected_pins": CONFIG.expected_pins | synthetic_gas_pins()},
    ],
)
def test_candidate_cannot_change_an_existing_runtime_policy_or_relax_feed_age(tmp_path, change):
    with closing(EvidenceStore(tmp_path / "proof.db")) as store:
        refs = retain_discovery_quorum(store, BLOCK, captures(), sources=SOURCES)
        with pytest.raises(ValueError):
            candidate_configuration(
                store, refs, BLOCK, replace(CONFIG, **change), sources=SOURCES, now=BLOCK.timestamp
            )


def test_cli_writes_only_a_new_review_and_preserves_inputs_and_previous_output(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        JsonRpcTransport, "request", lambda *_: pytest.fail("offline CLI attempted RPC")
    )
    wire, config = inputs(tmp_path)
    before = wire.read_bytes(), config.read_bytes()
    out = tmp_path / "review"
    report = cli.review(wire, config, out, sources=SOURCES, now=BLOCK.timestamp)
    assert report["status"] == "candidate_requires_review" and report["rpc_requests"] == 0
    assert (wire.read_bytes(), config.read_bytes()) == before
    assert sha256_hex((out / "candidate.json").read_bytes()) == report["candidate_hash"]
    contents = {p.name: p.read_bytes() for p in out.iterdir()}
    with pytest.raises(FileExistsError):
        cli.review(wire, config, out, sources=SOURCES, now=BLOCK.timestamp)
    assert {p.name: p.read_bytes() for p in out.iterdir()} == contents


@pytest.mark.parametrize(
    "fault",
    [
        "symlink",
        "oversize",
        "schema",
        "extra_provider",
        "source",
        "stale",
        "error",
        "duplicate_json",
    ],
)
def test_cli_redacts_refusal_and_publishes_no_candidate(tmp_path, fault):
    pair = captures()
    if fault == "error":
        pair[1][0] = replace(
            pair[1][0],
            response_payload=canonical_json(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "error": {"code": -1, "message": "secret-provider-diagnostic"},
                }
            ),
        )
    wire, config = inputs(tmp_path, pair)
    if fault == "symlink":
        link = tmp_path / "link.json"
        link.symlink_to(wire)
        wire = link
    elif fault == "oversize":
        wire.write_bytes(b" " * 8_000_001)
    elif fault == "duplicate_json":
        wire.write_bytes(b'{"block":{},"block":{},"providers":[]}')
    elif fault in {"schema", "source", "extra_provider"}:
        value = json.loads(wire.read_bytes())
        if fault == "schema":
            value["secret-provider-diagnostic"] = True
        elif fault == "source":
            value["providers"][1]["source"] = SOURCES[0]
        else:
            value["providers"].append(deepcopy(value["providers"][0]))
        wire.write_bytes(canonical_json(value))
    out = tmp_path / "review"
    report = cli.review(
        wire, config, out, sources=SOURCES, now=BLOCK.timestamp + (1801 if fault == "stale" else 0)
    )
    assert report["status"] == "refused" and report["execution_eligible"] is False
    assert report["rpc_requests"] == 0 and not (out / "candidate.json").exists()
    assert b"secret-provider-diagnostic" not in (out / "report.json").read_bytes()


@pytest.mark.parametrize("accepted", [True, False])
def test_cli_exit_does_not_claim_execution_eligibility(tmp_path, monkeypatch, capsys, accepted):
    wire, config = inputs(tmp_path)
    monkeypatch.setattr(cli.time, "time", lambda: BLOCK.timestamp + (0 if accepted else 1801))
    monkeypatch.setattr(
        sys,
        "argv",
        ["review", "--wire", str(wire), "--config", str(config), "--out", str(tmp_path / "review")],
    )
    with pytest.raises(SystemExit) as caught:
        cli.main()
    assert caught.value.code == (0 if accepted else 3)
    assert json.loads(capsys.readouterr().out)["execution_eligible"] is False
