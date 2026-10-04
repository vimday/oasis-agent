"""Complete receipt coverage, adverse outcomes and independent raw replay."""

import hashlib
import itertools
import json
from dataclasses import asdict, replace

import pytest
from eth_abi.abi import encode
from eth_hash.auto import keccak

from oasis_agent.errors import FeeEncodingError
from oasis_agent.evidence.exact_capture import decode_header
from oasis_agent.evidence.rpc_capture import BoundedExchangeRecorder
from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.farming.router_activity import (
    RouterActivityCapture,
    RouterActivityPlan,
    collect_router_activity,
    replay_router_activity,
    retain_router_activity,
)
from oasis_agent.models.common import canonical_json
from oasis_agent.rpc.budget import BudgetedPoster
from oasis_agent.rpc.transport import JsonRpcTransport
from oasis_agent.uniswap_v4.events import SWAP_TOPIC
from scripts import capture_router_activity as cli

MANAGER, ROUTER, OTHER = ("0x" + c * 40 for c in ("1", "2", "3"))
POOL, BLOCK, PARENT = ("0x" + c * 64 for c in ("a", "b", "c"))
CODE_HASH = "0x" + keccak(bytes.fromhex("6000")).hex()
TX_HASHES = ["0x" + f"{n:064x}" for n in (101, 102, 103)]
HEADER = {"number": "0x64", "hash": BLOCK, "parentHash": PARENT, "timestamp": "0x3e8"}


def plan():
    return RouterActivityPlan(
        decode_header(HEADER, chain_id=8453),
        MANAGER,
        CODE_HASH,
        ROUTER,
        CODE_HASH,
        POOL,
        ("rpc_a", "rpc_b"),
        4,
    )


def tx(index):
    return {
        "hash": TX_HASHES[index],
        "transactionIndex": hex(index),
        "blockHash": BLOCK,
        "blockNumber": "0x64",
        "from": OTHER,
        "to": ROUTER if index != 1 else OTHER,
        "input": "0x12345678",
        "value": "0x64",
    }


def receipt(index):
    logs = []
    if index == 0:
        logs = [
            {
                "address": MANAGER,
                "topics": [SWAP_TOPIC, POOL, "0x" + "0" * 24 + ROUTER[2:]],
                "data": "0x"
                + encode(
                    ["int128", "int128", "uint160", "uint128", "int24", "uint24"],
                    [-100, 90, 1 << 96, 1000, 0, 500],
                ).hex(),
                "blockHash": BLOCK,
                "blockNumber": "0x64",
                "transactionIndex": "0x0",
                "transactionHash": TX_HASHES[0],
                "logIndex": "0x4",
                "removed": False,
            }
        ]
    return {
        "transactionHash": TX_HASHES[index],
        "transactionIndex": hex(index),
        "blockHash": BLOCK,
        "blockNumber": "0x64",
        "from": OTHER,
        "to": ROUTER,
        "status": "0x1" if index == 0 else "0x0",
        "logs": logs,
    }


class Poster:
    def __init__(self, mode="ok"):
        self.mode, self.calls = mode, []

    def post(self, payload):
        req = json.loads(payload)
        self.calls.append(req)
        method, params = req["method"], req["params"]
        if method == "eth_chainId":
            value = "0x2105"
        elif method == "eth_getBlockByNumber":
            value = {
                **HEADER,
                "transactions": [tx(n) for n in range(3)] if params[-1] else TX_HASHES,
            }
        elif method == "eth_getCode":
            value = "0x6000"
        else:
            if self.mode == "transport":
                raise OSError("redacted upstream details")
            value = receipt(TX_HASHES.index(params[0]))
            if self.mode == "disagree" and value["logs"]:
                value["logs"][0]["data"] = (
                    "0x"
                    + encode(
                        ["int128", "int128", "uint160", "uint128", "int24", "uint24"],
                        [-100, 89, 1 << 96, 1000, 0, 500],
                    ).hex()
                )
        return canonical_json({"jsonrpc": "2.0", "id": req["id"], "result": value})


def capture(p=None):
    p = p or plan()
    recorder = BoundedExchangeRecorder()
    poster = Poster()
    transport = JsonRpcTransport("https://example.com", http=poster, retries=0, recorder=recorder)
    return collect_router_activity((transport, recorder, "rpc_a"), p), poster


def test_actual_wire_path_covers_every_router_receipt_and_keeps_reverted(tmp_path):
    observed, poster = capture()
    facts = observed.facts()
    assert len(poster.calls) == 7
    assert [r["status"] for r in facts["outer_router_receipts"]] == [1, 0]
    swap = facts["outer_router_receipts"][0]["target_swaps"][0]
    assert swap["amount0"] == -100 and swap["amount1"] == 90 and swap["sender_is_router"]
    store = EvidenceStore(tmp_path / "evidence.db")
    try:
        ref = retain_router_activity(store, observed)
        assert replay_router_activity(store, ref, observed.plan) == facts
        assert not facts["target_admitted"] and facts["net_pnl"] is None
        with pytest.raises(ValueError):
            replay_router_activity(store, ref, replace(observed.plan, pool_id="0x" + "d" * 64))
    finally:
        store.close()


@pytest.mark.parametrize(
    "case",
    [
        "missing",
        "hash",
        "index",
        "removed",
        "status",
        "abi",
        "reorg",
        "enumeration",
        "duplicate",
        "chain",
        "code",
        "pin",
    ],
)
def test_incomplete_foreign_or_adverse_mutant_wire_refuses(case):
    observed, _ = capture()
    xs = list(observed.exchanges)
    if case == "missing":
        xs.pop(5)
    else:
        index = 4
        if case in ("reorg", "enumeration"):
            index = 6
        elif case == "duplicate":
            index = 1
        elif case == "chain":
            index = 0
        elif case in ("code", "pin"):
            index = 3
        response = json.loads(xs[index].response_payload)
        value = response["result"]
        if case == "hash":
            value["transactionHash"] = TX_HASHES[1]
        elif case == "index":
            value["transactionIndex"] = "0x2"
        elif case == "removed":
            value["logs"][0]["removed"] = 1
        elif case == "status":
            value["status"] = "0x0"
        elif case == "abi":
            value["logs"][0]["data"] += "00"
        elif case == "reorg":
            value["hash"] = "0x" + "d" * 64
        elif case == "enumeration":
            value["transactions"].pop()
        elif case == "duplicate":
            value["transactions"][1]["hash"] = TX_HASHES[0]
        elif case == "chain":
            response["result"] = "0x1"
        elif case == "code":
            response["result"] = "0x6001"
        if case == "pin":
            request = json.loads(xs[index].request_payload)
            request["params"][-1]["requireCanonical"] = 1
            xs[index] = replace(xs[index], request_payload=canonical_json(request))
        else:
            xs[index] = replace(xs[index], response_payload=canonical_json(response))
    with pytest.raises(ValueError):
        RouterActivityCapture(observed.plan, observed.source, tuple(xs)).facts()


def test_more_router_receipts_than_frozen_bound_never_gets_truncated():
    with pytest.raises(ValueError, match="bound"):
        capture(replace(plan(), max_router_transactions=1))


@pytest.mark.parametrize(
    "sqrt,tick,fee", [(0, 0, 500), (1 << 96, 887273, 500), (1 << 96, 0, 1000001)]
)
def test_canonical_abi_but_impossible_protocol_state_refuses(sqrt, tick, fee):
    observed, _ = capture()
    xs = list(observed.exchanges)
    response = json.loads(xs[4].response_payload)
    response["result"]["logs"][0]["data"] = (
        "0x"
        + encode(
            ["int128", "int128", "uint160", "uint128", "int24", "uint24"],
            [-100, 90, sqrt, 1000, tick, fee],
        ).hex()
    )
    xs[4] = replace(xs[4], response_payload=canonical_json(response))
    with pytest.raises(FeeEncodingError if fee > 1_000_000 else ValueError):
        RouterActivityCapture(observed.plan, observed.source, tuple(xs)).facts()


@pytest.mark.parametrize("mode", ["ok", "transport", "disagree"])
def test_cli_durable_charge_offline_replay_and_no_silent_restart(tmp_path, monkeypatch, mode):
    root = tmp_path / "capture"
    root.mkdir()
    raw = {
        "schema": "oasis.router-activity-scope.v1",
        "purpose": "complete_outer_router_receipts_at_quote_block",
        "output_directory": str(root),
        "plan": asdict(plan()),
        "sources": [
            {"alias": "rpc_a", "env": "RPC_A", "endpoint": "https://a.example"},
            {"alias": "rpc_b", "env": "RPC_B", "endpoint": "https://b.example"},
        ],
        "max_requests_per_source": 20,
        "min_interval_ms": 3000,
        "automatic_retries": 0,
    }
    config = root / "config.json"
    config.write_bytes(canonical_json(raw))
    posters = []

    def make_poster(url):
        p = Poster(mode if (mode != "disagree" or url.endswith("b.example")) else "ok")
        posters.append(p)
        return p

    monkeypatch.setattr(cli, "CurlPoster", make_poster)
    monkeypatch.setenv("RPC_A", "https://a.example")
    monkeypatch.setenv("RPC_B", "https://b.example")
    ticks = itertools.count(1_000_000, 3000)
    monkeypatch.setattr(
        cli,
        "BudgetedPoster",
        lambda *a, **k: BudgetedPoster(*a, **k, clock=lambda: next(ticks), sleep=lambda _: None),
    )
    if mode == "ok":
        cli.capture(config, root)
        before = hashlib.sha256((root / "evidence.db").read_bytes()).hexdigest()
        cli.replay(config, root, tmp_path / "replay.json")
        assert (tmp_path / "replay.json").read_bytes() == (root / "report.json").read_bytes()
        assert hashlib.sha256((root / "evidence.db").read_bytes()).hexdigest() == before
        with pytest.raises(FileExistsError):
            cli.replay(config, root, tmp_path / "replay.json")
    else:
        with pytest.raises(ValueError, match="retained attempts"):
            cli.capture(config, root)
        assert (root / "failure.json").exists() and not (root / "report.json").exists()
    expected = 5 if mode == "transport" else 7
    assert len(posters) == 2 and all(len(p.calls) == expected for p in posters)
    for source in ("rpc_a", "rpc_b"):
        rows = (root / f"{source}-attempts.jsonl").read_bytes().splitlines()
        assert len(rows) == expected
        if mode == "transport":
            assert json.loads(rows[-1])["response"] is None
    before = hashlib.sha256((root / "budget.db").read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="existing router attempt"):
        cli.capture(config, root)
    assert hashlib.sha256((root / "budget.db").read_bytes()).hexdigest() == before
    assert sum(len(p.calls) for p in posters) == 2 * expected
