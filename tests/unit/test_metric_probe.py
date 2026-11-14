"""Hostile packet/build/return tests. Tiny artifacts below are synthetic, not compiler outputs."""

import copy
import json

import pytest
from eth_abi.abi import encode
from eth_hash.auto import keccak

from oasis_agent.arbitrage import metric_probe as p
from oasis_agent.arbitrage import metric_probe_build as build
from oasis_agent.models.common import canonical_json, sha256_hex
from scripts import research_metric_probe as cli


def address(n):
    return "0x" + f"{n:040x}"


def digest(n):
    return "0x" + f"{n:064x}"


def h(raw):
    return "0x" + keccak(raw).hex()


def plan_data():
    return {
        "schema": "oasis-metric-probe-plan-v1",
        "scenario": "synthetic_local_evm",
        "source": "synthetic",
        "endpoint_sha256": "a" * 64,
        "chain_id": 1234567,
        "block_number": 1234,
        "block_hash": digest(1),
        "block_timestamp": 1800000000,
        "cutoff_utc": "2027-01-15T08:00:10Z",
        "probe": address(10),
        "observer": address(11),
        "metric": address(12),
        "v3": address(13),
        "assets": [
            {"address": address(20), "symbol": "A", "decimals": 6, "runtime_hash": digest(20)},
            {"address": address(21), "symbol": "B", "decimals": 18, "runtime_hash": digest(21)},
        ],
        "metric_token0_is_a": False,
        "metric_runtime_hash": digest(12),
        "v3_runtime_hash": digest(13),
        "build_artifact_sha256": "b" * 64,
        "probe_runtime_hash": digest(99),
        "gas_limit": 2_000_000,
        "request": {
            "metric_first": True,
            "start_with_a": True,
            "amount_in": 10000,
            "minimum_profit": 1,
            "metric_price_limit_x64": 123,
            "v3_sqrt_price_limit_x96": 456,
            "extension_data": "0xabcd",
        },
    }


def parse(obj):
    return p.Plan.model_validate_json(json.dumps(obj))


def setup():
    obj = plan_data()
    values = p.immutable_values(parse(obj))
    artifact = {
        "metadata": {
            "compiler": {"version": "0.8.26+commit.8a97fa7a"},
            "settings": {
                "compilationTarget": {build.SOURCE: build.CONTRACT},
                "optimizer": {"enabled": True, "runs": 200},
                "evmVersion": "cancun",
                "metadata": {"bytecodeHash": "none", "appendCBOR": False},
                "libraries": {},
            },
            "sources": {n: {"keccak256": v} for n, v in build.SOURCE_HASHES.items()},
        },
        "methodIdentifiers": {p.PROBE_SIGNATURE: p.topic(p.PROBE_SIGNATURE)[2:10]},
        "ast": {
            "absolutePath": build.SOURCE,
            "nodes": [
                {
                    "nodeType": "ContractDefinition",
                    "name": build.CONTRACT,
                    "nodes": [
                        {
                            "id": i,
                            "name": name,
                            "mutability": "immutable",
                            "typeDescriptions": {"typeString": kind},
                        }
                        for i, (name, (kind, _)) in enumerate(values.items())
                    ],
                }
            ],
        },
        "deployedBytecode": {
            "object": "0x" + "00" * (32 * 13),
            "linkReferences": {},
            "immutableReferences": {str(i): [{"start": i * 32, "length": 32}] for i in range(13)},
        },
    }
    runtime = b"".join(encode((kind,), (value,)) for kind, value in values.values())
    obj["probe_runtime_hash"] = h(runtime)
    obj["build_artifact_sha256"] = sha256_hex(canonical_json(artifact))
    return obj, artifact, runtime


def result(plan, **changes):
    fields = {
        "request_hash": bytes.fromhex(p.request_hash(plan)[2:]),
        "principal": 10000,
        "middle": 10000,
        "output": 10100,
        "profit": 100,
        "gas": 100000,
    }
    fields.update(changes)
    return p.data_bytes(p.topic(p.RESULT_SIGNATURE)[:10]) + encode(
        p.RESULT_TYPES, tuple(fields.values())
    )


def capture_data(plan, runtime):
    responses = [
        hex(plan.chain_id),
        {
            "number": hex(plan.block_number),
            "hash": plan.block_hash,
            "parentHash": digest(2),
            "timestamp": hex(plan.block_timestamp),
        },
        "0x" + runtime.hex(),
        None,
    ]
    exchanges = []
    for i, req in enumerate(p.requests(plan)):
        res = {"jsonrpc": "2.0", "id": i + 1, "result": responses[i]}
        if i == 3:
            del res["result"]
            res["error"] = {
                "code": 3,
                "message": "execution reverted",
                "data": "0x" + result(plan).hex(),
            }
        exchanges.append(
            {
                "request_hex": "0x" + canonical_json(req).hex(),
                "response_hex": "0x" + canonical_json(res).hex(),
                "received_utc": "2027-01-15T08:00:01Z",
            }
        )
    return {
        "schema": "oasis-metric-probe-retained-v1",
        "plan_file_sha256": "c" * 64,
        "source": plan.source,
        "endpoint_sha256": plan.endpoint_sha256,
        "exchanges": exchanges,
    }


def mutate_exchange(obj, index, side, mutate):
    key = side + "_hex"
    value = json.loads(bytes.fromhex(obj["exchanges"][index][key][2:]))
    mutate(value)
    obj["exchanges"][index][key] = "0x" + canonical_json(value).hex()


def evaluate(obj, plan, runtime):
    return p.evaluate(plan, p.Capture.model_validate_json(json.dumps(obj)), runtime)


@pytest.mark.parametrize(
    "first,start", [(False, False), (False, True), (True, False), (True, True)]
)
def test_packet_and_result_bind_both_directions_without_authority(first, start):
    obj, artifact, runtime = setup()
    obj["request"].update(metric_first=first, start_with_a=start)
    plan = parse(obj)
    assert build.materialize(plan, canonical_json(artifact)) == runtime
    r = evaluate(capture_data(plan, runtime), plan, runtime)
    assert r["status"] == "bound_revert_payload"
    assert r["observed_result"]["gross_profit_raw"] == 100
    assert r["observed_result"]["start_asset"]["decimals"] == (6 if start else 18)
    assert r["observed_result"]["net_profit_raw"] is None
    assert all(v is False for k, v in r.items() if k.endswith("verified"))
    assert not r["execution_eligible"] and r["realized_pnl_raw"] is None
    assert len(r["exchanges"]) == 4


@pytest.mark.parametrize(
    "field,value",
    [
        ("amount_in", True),
        ("amount_in", 1.1),
        ("amount_in", "1"),
        ("amount_in", 1 << 127),
        ("minimum_profit", -1),
        ("metric_first", 1),
        ("extension_data", "0xa"),
        ("extension_data", "0x" + "aa" * 1025),
        ("v3_sqrt_price_limit_x96", 0),
    ],
)
def test_strict_request_boundary(field, value):
    obj = plan_data()
    obj["request"][field] = value
    with pytest.raises(ValueError):
        parse(obj)


@pytest.mark.parametrize(
    "change",
    [
        {"principal": 9999},
        {"middle": 0},
        {"output": 9999},
        {"profit": 101},
        {"gas": 0},
        {"gas": 2000001},
        {"middle": 1 << 127},
        {"output": 1 << 127},
        {"request_hash": bytes(32)},
    ],
)
def test_result_conservation_bounds_and_hash(change):
    plan = parse(plan_data())
    with pytest.raises(ValueError):
        p.decode_result(plan, result(plan, **change))


@pytest.mark.parametrize("mode", ["truncated", "trailing", "wrong_selector"])
def test_reject_noncanonical_revert_bytes(mode):
    plan = parse(plan_data())
    raw = result(plan)
    raw = (
        raw[:-1]
        if mode == "truncated"
        else raw + b"\0"
        if mode == "trailing"
        else b"xxxx" + raw[4:]
    )
    with pytest.raises(ValueError):
        p.decode_result(plan, raw)


@pytest.mark.parametrize(
    "which,change",
    [
        (0, lambda r: r.update(result="0x1")),
        (1, lambda r: r["result"].update(hash=digest(99))),
        (1, lambda r: r["result"].update(timestamp="0x1")),
        (2, lambda r: r.update(result="0x00")),
        (3, lambda r: r["error"].update(code=-32603)),
        (3, lambda r: r["error"].update(data={"nested": "0xdeadbeef"})),
        (3, lambda r: r["error"].update(data="0xdeadbeef", message="PRIVATE_HOSTILE_TEXT")),
    ],
)
def test_context_or_revert_failure_clears_values(which, change):
    obj, _, runtime = setup()
    plan = parse(obj)
    cap = capture_data(plan, runtime)
    mutate_exchange(cap, which, "response", change)
    report = evaluate(cap, plan, runtime)
    assert report["status"] == "rejected_or_incomplete" and report["observed_result"] is None
    assert "PRIVATE_HOSTILE_TEXT" not in json.dumps(report)


def test_normal_return_with_success_selector_is_never_success():
    obj, _, runtime = setup()
    plan = parse(obj)
    cap = capture_data(plan, runtime)

    def change(r):
        r["result"] = r.pop("error")["data"]

    mutate_exchange(cap, 3, "response", change)
    assert evaluate(cap, plan, runtime)["reason"] == "probe_unexpected_nonreverting_return"


@pytest.mark.parametrize(
    "change",
    [
        lambda r: r["params"][0].update(to=address(999)),
        lambda r: r["params"][0].update(value="0x1"),
        lambda r: r["params"][0].update(gas="0x1"),
        lambda r: r["params"].append({address(10): {"code": "0x00"}}),
        lambda r: r["params"].__setitem__(1, "latest"),
        lambda r: r.update(id=True),
        lambda r: r.update(method="eth_sendTransaction"),
    ],
)
def test_packet_cannot_widen_target_value_gas_pin_or_override(change):
    obj, _, runtime = setup()
    plan = parse(obj)
    cap = capture_data(plan, runtime)
    mutate_exchange(cap, 3, "request", change)
    with pytest.raises(ValueError, match="request_outside"):
        evaluate(cap, plan, runtime)


@pytest.mark.parametrize(
    "time", ["2027-01-15T07:59:59Z", "2027-01-15T08:00:11Z", "2027-01-15T08:00:00Z"]
)
def test_time_bounds_and_regression(time):
    obj, _, runtime = setup()
    plan = parse(obj)
    cap = capture_data(plan, runtime)
    cap["exchanges"][3]["received_utc"] = time
    with pytest.raises(ValueError, match="time_outside"):
        evaluate(cap, plan, runtime)


@pytest.mark.parametrize(
    "mode",
    [
        "source",
        "compiler",
        "settings",
        "missing",
        "overlap",
        "nonzero",
        "bounds",
        "type",
        "method",
        "duplicate",
    ],
)
def test_build_materialization_rejects_tampered_artifact_even_if_rehashed(mode):
    obj, artifact, _ = setup()
    refs = artifact["deployedBytecode"]["immutableReferences"]
    if mode == "source":
        artifact["metadata"]["sources"][build.SOURCE]["keccak256"] = digest(0)
    elif mode == "compiler":
        artifact["metadata"]["compiler"]["version"] = "0.8.35"
    elif mode == "settings":
        artifact["metadata"]["settings"]["optimizer"]["runs"] = 1
    elif mode == "missing":
        del refs["0"]
    elif mode == "overlap":
        refs["1"][0]["start"] = 0
    elif mode == "nonzero":
        artifact["deployedBytecode"]["object"] = "0x01" + artifact["deployedBytecode"]["object"][4:]
    elif mode == "bounds":
        refs["1"][0]["start"] = -1
    elif mode == "type":
        artifact["ast"]["nodes"][0]["nodes"][0]["typeDescriptions"]["typeString"] = "uint256"
    elif mode == "method":
        artifact["methodIdentifiers"][p.PROBE_SIGNATURE] = "deadbeef"
    elif mode == "duplicate":
        artifact["ast"]["nodes"][0]["nodes"].append(
            copy.deepcopy(artifact["ast"]["nodes"][0]["nodes"][0])
        )
    raw = canonical_json(artifact)
    obj["build_artifact_sha256"] = sha256_hex(raw)
    with pytest.raises(ValueError):
        build.materialize(parse(obj), raw)


def test_cli_replay_binding_exclusive_output_and_failure_status(tmp_path, monkeypatch):
    obj, artifact, runtime = setup()
    plan = parse(obj)
    plan_path = tmp_path / "plan.json"
    plan_path.write_bytes(canonical_json(obj))
    artifact_path = tmp_path / "artifact.json"
    artifact_path.write_bytes(canonical_json(artifact))
    cap = capture_data(plan, runtime)
    cap["plan_file_sha256"] = sha256_hex(plan_path.read_bytes())
    cap_path = tmp_path / "capture.json"
    cap_path.write_bytes(canonical_json(cap))
    out = tmp_path / "result.json"
    args = [
        "probe",
        "replay",
        "--plan",
        str(plan_path),
        "--artifact",
        str(artifact_path),
        "--capture",
        str(cap_path),
        "--output",
        str(out),
    ]
    monkeypatch.setattr("sys.argv", args)
    assert cli.main() == 0
    saved = out.read_bytes()
    assert cli.main() == 2 and out.read_bytes() == saved
    mutate_exchange(cap, 3, "response", lambda r: r["error"].update(data="0x00"))
    cap_path.write_bytes(canonical_json(cap))
    args[-1] = str(tmp_path / "failed.json")
    assert cli.main() == 3
    cap["plan_file_sha256"] = "f" * 64
    cap_path.write_bytes(canonical_json(cap))
    assert cli.main() == 2
    assert cli.run(plan_path, artifact_path, None)["network_requests_sent"] == 0


@pytest.mark.parametrize(
    "mode", ["source", "endpoint", "count", "bool_id", "duplicate_json", "extra_response"]
)
def test_capture_provenance_envelope_and_coverage(mode):
    obj, _, runtime = setup()
    plan = parse(obj)
    cap = capture_data(plan, runtime)
    if mode == "source":
        cap["source"] = "other"
    elif mode == "endpoint":
        cap["endpoint_sha256"] = "f" * 64
    elif mode == "count":
        cap["exchanges"].pop()
    elif mode == "bool_id":
        mutate_exchange(cap, 0, "response", lambda r: r.update(id=True))
    elif mode == "extra_response":
        mutate_exchange(cap, 3, "response", lambda r: r.update(result="0x"))
    else:
        cap["exchanges"][0]["response_hex"] = (
            "0x" + b'{"jsonrpc":"2.0","id":1,"id":1,"result":"0x12d687"}'.hex()
        )
    with pytest.raises(ValueError):
        evaluate(cap, plan, runtime)


@pytest.mark.parametrize("field", ["observer", "block_number", "chain_id", "metric_runtime_hash"])
def test_immutable_config_change_cannot_reuse_runtime(field):
    obj, artifact, _ = setup()
    obj[field] = (
        address(99) if field == "observer" else digest(99) if field.endswith("hash") else 999
    )
    with pytest.raises(ValueError, match="materialized_hash_mismatch"):
        build.materialize(parse(obj), canonical_json(artifact))


def test_request_change_cannot_reuse_previous_revert():
    obj, _, _ = setup()
    before = result(parse(obj))
    obj["request"]["extension_data"] = "0xabce"
    with pytest.raises(ValueError, match="binding_mismatch"):
        p.decode_result(parse(obj), before)


def test_build_file_hash_and_missing_ast_are_not_trusted():
    obj, artifact, _ = setup()
    raw = canonical_json(artifact)
    with pytest.raises(ValueError, match="artifact_hash_mismatch"):
        build.materialize(parse(obj), raw + b" ")
    del artifact["ast"]
    raw = canonical_json(artifact)
    obj["build_artifact_sha256"] = sha256_hex(raw)
    with pytest.raises(ValueError, match="AST"):
        build.materialize(parse(obj), raw)


def test_evaluator_cannot_substitute_runtime_outside_plan():
    obj, _, runtime = setup()
    plan = parse(obj)
    cap = capture_data(plan, b"\0")
    with pytest.raises(ValueError, match="materialized_runtime_hash_mismatch"):
        evaluate(cap, plan, b"\0")
    assert runtime != b"\0"
