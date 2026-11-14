"""Synthetic state/IPC boundaries; FakeRPC below does not execute EVM bytecode."""

import copy
import gzip
import json
import time

import pytest
from eth_abi.abi import encode

from oasis_agent.arbitrage import metric_local as local
from oasis_agent.arbitrage import metric_probe as probe
from oasis_agent.models.common import canonical_json, sha256_hex
from scripts import run_metric_local_probe as cli
from tests.unit import test_metric_probe as fixture

ENDPOINT = sha256_hex(b"synthetic-unit-ipc")


def setup():
    obj, artifact, _ = fixture.setup()
    accounts = []
    for i, address in enumerate([obj["metric"], obj["v3"], *[a["address"] for a in obj["assets"]]]):
        code = "0x60" + f"{i:02x}" + "00"
        accounts.append(
            {
                "address": address,
                "nonce": 1,
                "balance": 0,
                "code": code,
                "storage": {fixture.digest(1): fixture.digest(i + 1)},
            }
        )
        if i < 2:
            obj[("metric_runtime_hash", "v3_runtime_hash")[i]] = fixture.h(bytes.fromhex(code[2:]))
        else:
            obj["assets"][i - 2]["runtime_hash"] = fixture.h(bytes.fromhex(code[2:]))
    values = probe.immutable_values(fixture.parse(obj))
    runtime = b"".join(encode((kind,), (v,)) for kind, v in values.values())
    obj["probe_runtime_hash"] = fixture.h(runtime)
    image_obj = {
        "schema": "oasis-metric-local-image-v1",
        "origin": "synthetic_fixture",
        "accounts": accounts,
    }
    image = local.Image.model_validate_json(json.dumps(image_obj))
    return fixture.parse(obj), image, artifact, runtime


class FakeRPC:
    def __init__(self, plan, image):
        self.plan, self.image = plan, image
        self.records = []
        self.change = None

    def state(self):
        obj = {
            "block": {
                "number": hex(self.plan.block_number),
                "timestamp": hex(self.plan.block_timestamp),
            },
            "best_block_number": self.plan.block_number,
            "transactions": [],
            "accounts": {a.address: a.state() for a in self.image.accounts},
        }
        return "0x" + gzip.compress(canonical_json(obj), mtime=0).hex()

    def call(self, method, params):
        identity = len(self.records) + 1
        res = {"jsonrpc": "2.0", "id": identity}
        if method == "web3_clientVersion":
            value = "anvil/synthetic-unit-test"
        elif method == "eth_accounts":
            value = []
        elif method == "eth_chainId":
            value = hex(self.plan.chain_id)
        elif method == "eth_getBlockByNumber":
            value = {
                "number": hex(self.plan.block_number),
                "hash": fixture.digest(777),
                "parentHash": fixture.digest(776),
                "timestamp": hex(self.plan.block_timestamp),
            }
        elif method == "anvil_dumpState":
            value = self.state()
        elif method == "eth_getProof":
            value = {
                "address": self.plan.probe,
                "nonce": "0x0",
                "balance": "0x0",
                "codeHash": local.EMPTY_CODE,
                "storageHash": local.EMPTY_STORAGE,
                "storageProof": [],
                "accountProof": [],
            }
        elif method == "eth_getCode":
            value = "0x"
        elif method == "eth_call":
            value = None
            res["error"] = {
                "code": 3,
                "message": "synthetic",
                "data": "0x" + fixture.result(self.plan).hex(),
            }
        else:
            raise AssertionError(method)
        if "error" not in res:
            res["result"] = value
        if self.change:
            self.change(identity, res)
        self.records.append(
            ({"jsonrpc": "2.0", "id": identity, "method": method, "params": params}, res)
        )
        return res


def exercise(plan, image, runtime, rpc):
    return local.exercise(plan, image, runtime, ENDPOINT, rpc.call)


def test_bound_override_rebases_only_synthetic_header_and_checks_full_state():
    plan, image, _, runtime = setup()
    rpc = FakeRPC(plan, image)
    outcome = exercise(plan, image, runtime, rpc)
    assert len(rpc.records) == 11 and outcome["status"] == "local_probe_observed"
    assert outcome["state_unchanged"] and outcome["probe_absent_before_and_after"]
    assert outcome["local_plan"]["block_hash"] == fixture.digest(777)
    assert plan.block_hash == fixture.digest(1)
    params = rpc.records[7][0]["params"]
    assert params[2] == {plan.probe: {"code": "0x" + runtime.hex(), "state": {}}}
    assert params[0]["value"] == "0x0" and params[1]["requireCanonical"] is True
    assert outcome["observed_result"]["gross_profit_raw"] == 100
    assert outcome["observed_result"]["net_profit_raw"] is None
    assert not outcome["historical_state_verified"] and not outcome["execution_eligible"]


@pytest.mark.parametrize(
    "mode",
    [
        "historical",
        "probe_present",
        "observer_present",
        "missing_role",
        "code_hash",
        "duplicate",
        "zero_slot",
    ],
)
def test_image_rejects_unknown_or_unsafe_initial_state(mode):
    plan, image, _, _ = setup()
    obj = image.model_dump(by_alias=True, mode="json")
    if mode == "historical":
        plan = plan.model_copy(update={"scenario": "retained_simulation_claim"})
    elif mode in ("probe_present", "observer_present"):
        a = copy.deepcopy(obj["accounts"][0])
        a["address"] = plan.probe if mode == "probe_present" else plan.observer
        obj["accounts"].append(a)
    elif mode == "missing_role":
        obj["accounts"][0]["address"] = fixture.address(100)
    elif mode == "code_hash":
        obj["accounts"][0]["code"] = "0x00"
    elif mode == "duplicate":
        obj["accounts"].append(copy.deepcopy(obj["accounts"][0]))
    else:
        obj["accounts"][0]["storage"][fixture.digest(1)] = fixture.digest(0)
    with pytest.raises(ValueError):
        local.Image.model_validate_json(json.dumps(obj)).validate_plan(plan)


@pytest.mark.parametrize(
    "identity,mutate",
    [
        (1, lambda r: r.update(result="other-client")),
        (2, lambda r: r.update(result=[fixture.address(99)])),
        (3, lambda r: r.update(result="0x1")),
        (4, lambda r: r["result"].update(timestamp="0x1")),
        (6, lambda r: r["result"].update(nonce="0x1")),
        (6, lambda r: r["result"].update(balance="0x1")),
        (6, lambda r: r["result"].update(codeHash=fixture.digest(99))),
        (6, lambda r: r["result"].update(storageHash=fixture.digest(99))),
        (7, lambda r: r.update(result="0x00")),
        (9, lambda r: r.update(result="0x00")),
        (11, lambda r: r["result"].update(hash=fixture.digest(99))),
    ],
)
def test_node_or_state_mismatch_stops_without_optimistic_result(identity, mutate):
    plan, image, _, runtime = setup()
    rpc = FakeRPC(plan, image)
    rpc.change = lambda i, r: mutate(r) if i == identity else None
    with pytest.raises(ValueError):
        exercise(plan, image, runtime, rpc)
    assert len(rpc.records) == identity
    if identity < 8:
        assert not any(req["method"] == "eth_call" for req, _ in rpc.records)


def test_implicit_account_or_corrupt_snapshot_cannot_hide_in_genesis():
    plan, image, _, runtime = setup()
    rpc = FakeRPC(plan, image)

    def change(i, r):
        if i == 5:
            obj = json.loads(gzip.decompress(bytes.fromhex(r["result"][2:])))
            obj["accounts"][fixture.address(999)] = obj["accounts"][plan.metric]
            r["result"] = "0x" + gzip.compress(canonical_json(obj), mtime=0).hex()

    rpc.change = change
    with pytest.raises(ValueError, match="implicit_accounts"):
        exercise(plan, image, runtime, rpc)
    assert len(rpc.records) == 5


def test_legitimate_probe_rejection_still_records_unchanged_poststate():
    plan, image, _, runtime = setup()
    rpc = FakeRPC(plan, image)
    rpc.change = lambda i, r: r["error"].update(data="0xdeadbeef") if i == 8 else None
    result = exercise(plan, image, runtime, rpc)
    assert result["status"] == "local_probe_rejected" and result["observed_result"] is None
    assert len(rpc.records) == 11 and result["state_unchanged"]


def retained_fixture(directory):
    plan, image, artifact, runtime = setup()
    rpc = FakeRPC(plan, image)
    outcome = exercise(plan, image, runtime, rpc)
    initial = {
        "schema": "oasis-metric-local-run-v1",
        "inputs": {},
        "genesis_sha256": None,
        "anvil_binary_sha256": "a" * 64,
        "started_ns": 100,
        "max_seconds": 60,
        "max_requests": 11,
        "remote_rpc_requests": 0,
        "request_framing": "newline_included",
        "implementation_sha256": cli.implementation(),
        "status": "incomplete",
    }
    for name, obj in (
        ("plan.json", plan.model_dump(by_alias=True, mode="json")),
        ("image.json", image.model_dump(by_alias=True, mode="json")),
        ("artifact.json", artifact),
    ):
        raw = canonical_json(obj)
        (directory / name).write_bytes(raw)
        initial["inputs"][name] = sha256_hex(raw)
    genesis = canonical_json(image.genesis(plan))
    (directory / "genesis.json").write_bytes(genesis)
    initial["genesis_sha256"] = sha256_hex(genesis)
    rows = []
    for i, (req, res) in enumerate(rpc.records, 1):
        a, b = canonical_json(req) + b"\n", canonical_json(res) + b"\n"
        (directory / f"{i}.request.json").write_bytes(a)
        (directory / f"{i}.response.json").write_bytes(b)
        rows.append(
            {
                "id": i,
                "request_sha256": sha256_hex(a),
                "response_sha256": sha256_hex(b),
                "started_ns": 100 + 2 * i,
                "received_ns": 101 + 2 * i,
                "complete": True,
            }
        )
    child_raw = canonical_json(
        {
            "pid": 1234,
            "ipc_path": "synthetic-unit-ipc",
            "anvil_binary_sha256": "a" * 64,
            "spawned_ns": 101,
        }
    )
    (directory / "child.json").write_bytes(child_raw)
    record = {
        **initial,
        "status": "complete",
        "finished_ns": 200,
        "child_exit": 0,
        "child_sha256": sha256_hex(child_raw),
        "reason": None,
        "endpoint_sha256": ENDPOINT,
        "exchanges": rows,
        "outcome": outcome,
    }
    (directory / "manifest.json").write_bytes(canonical_json(initial))
    (directory / "result.json").write_bytes(canonical_json(record))
    return record


def test_retained_replay_and_cli_exclusive_output(tmp_path, monkeypatch):
    retained_fixture(tmp_path)
    result = cli.replay(tmp_path)
    assert result["retained_bytes_verified"] and not result["execution_eligible"]
    output = tmp_path / "replayed.json"
    monkeypatch.setattr(
        "sys.argv", ["local", "replay", "--capture", str(tmp_path), "--output", str(output)]
    )
    assert cli.main() == 0
    raw = output.read_bytes()
    assert cli.main() == 2 and output.read_bytes() == raw


@pytest.mark.parametrize(
    "mode",
    ["bytes", "method", "time_bool", "time_reverse", "exit_bool", "missing", "changed_outcome"],
)
def test_replay_rejects_raw_and_semantic_tampering(tmp_path, mode):
    record = retained_fixture(tmp_path)
    if mode == "bytes":
        (tmp_path / "8.response.json").write_bytes(b"{}")
    elif mode == "method":
        path = tmp_path / "8.request.json"
        obj = json.loads(path.read_text())
        obj["method"] = "anvil_setCode"
        raw = canonical_json(obj) + b"\n"
        path.write_bytes(raw)
        record["exchanges"][7]["request_sha256"] = sha256_hex(raw)
    elif mode == "time_bool":
        record["exchanges"][0]["started_ns"] = True
    elif mode == "time_reverse":
        record["exchanges"][1]["started_ns"] = 101
    elif mode == "exit_bool":
        record["child_exit"] = False
    elif mode == "missing":
        record["exchanges"].pop()
    else:
        record["outcome"]["observed_result"]["gross_profit_raw"] += 1
    (tmp_path / "result.json").write_bytes(canonical_json(record))
    with pytest.raises(ValueError):
        cli.replay(tmp_path)


def test_partial_ipc_bytes_are_retained_and_not_retried(tmp_path, monkeypatch):
    class Partial:
        sends = 0

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def settimeout(self, _):
            pass

        def connect(self, _):
            pass

        def sendall(self, _):
            self.sends += 1

        def recv(self, _):
            if not hasattr(self, "read"):
                self.read = True
                return b'{"jsonrpc":'
            return b""

    client = Partial()
    monkeypatch.setattr(cli.socket, "socket", lambda *_: client)
    journal = cli.Journal(tmp_path, tmp_path / "unused.ipc", time.monotonic() + 5)
    with pytest.raises(ValueError, match="eof"):
        journal.call("eth_chainId", [])
    assert client.sends == 1 and journal.rows[0]["complete"] is False
    assert (tmp_path / "1.response.json").read_bytes() == b'{"jsonrpc":'
    assert journal.rows[0]["response_sha256"] == sha256_hex(b'{"jsonrpc":')


def test_ipc_newline_in_separate_read_is_retained(tmp_path, monkeypatch):
    payload = b'{"jsonrpc":"2.0","id":1,"result":"0x1"}'

    class Split:
        def __init__(self):
            self.chunks = [payload, b"\n"]

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def settimeout(self, _):
            pass

        def connect(self, _):
            pass

        def sendall(self, _):
            pass

        def recv(self, _):
            return self.chunks.pop(0)

    monkeypatch.setattr(cli.socket, "socket", lambda *_: Split())
    journal = cli.Journal(tmp_path, tmp_path / "unused.ipc", time.monotonic() + 5)
    assert journal.call("eth_chainId", [])["result"] == "0x1"
    assert (tmp_path / "1.response.json").read_bytes() == payload + b"\n"


def test_existing_capture_and_forbidden_rpc_never_spawn_or_send(tmp_path, monkeypatch):
    def forbidden(*_, **__):
        raise AssertionError("must not spawn")

    monkeypatch.setattr(cli.subprocess, "Popen", forbidden)
    with pytest.raises(FileExistsError):
        cli.run(tmp_path / "p", tmp_path / "i", tmp_path / "a", tmp_path)
    journal = cli.Journal(tmp_path, tmp_path / "missing.ipc", time.monotonic() + 5)
    with pytest.raises(ValueError):
        journal.call("eth_sendRawTransaction", ["opaque"])
    assert journal.rows == []


def test_snapshot_expansion_is_bounded(monkeypatch):
    plan, image, _, _ = setup()
    monkeypatch.setattr(local, "MAX_STATE_BYTES", 100)
    raw = "0x" + gzip.compress(b"x" * 200, mtime=0).hex()
    with pytest.raises(ValueError, match="expansion"):
        local.snapshot(raw, plan, image)


@pytest.mark.parametrize("kill", [False, True])
def test_failed_child_is_reaped_and_parent_environment_is_filtered(tmp_path, monkeypatch, kill):
    source = tmp_path / "source"
    source.mkdir()
    retained_fixture(source)
    binary = tmp_path / "fake-anvil"
    binary.write_bytes(b"synthetic mock executable; never executed")
    monkeypatch.setattr(cli.shutil, "which", lambda _: str(binary))
    monkeypatch.setenv("RPC_URL", "https://unused.invalid")

    class Child:
        pid = 1234
        terminated = 0
        killed = 0
        waits = 0

        def poll(self):
            return 0

        def terminate(self):
            self.terminated += 1

        def kill(self):
            self.killed += 1

        def wait(self, timeout):
            self.waits += 1
            if kill and self.waits == 1:
                raise cli.subprocess.TimeoutExpired("synthetic-child", timeout)
            return -9 if kill else 0

    child = Child()

    def popen(command, **kwargs):
        assert command[command.index("--accounts") + 1] == "0"
        assert "--fork-url" not in command and "--mnemonic" not in command
        assert "RPC_URL" not in kwargs["env"] and set(kwargs["env"]) <= {"PATH", "LANG", "TMPDIR"}
        return child

    monkeypatch.setattr(cli.subprocess, "Popen", popen)
    result = cli.run(
        source / "plan.json", source / "image.json", source / "artifact.json", tmp_path / "run"
    )
    assert result["status"] == "incomplete" and result["outcome"] is None
    assert result["reason"] == (
        "local_child_required_kill" if kill else "local_anvil_startup_failed"
    )
    assert result["exchanges"] == [] and child.terminated == 1 and child.killed == int(kill)
    with pytest.raises(ValueError, match="incomplete"):
        cli.replay(tmp_path / "run")
