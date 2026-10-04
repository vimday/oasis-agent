"""Hash-bound whole acquisition with durable quotas and retained failed attempts."""

import hashlib
import itertools
import json
import sqlite3
from dataclasses import asdict, replace

import pytest
from eth_abi.abi import decode, encode
from eth_hash.auto import keccak

from oasis_agent.evidence.rpc_capture import BoundedExchangeRecorder
from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.farming.quote_grid_capture import (
    QuoteCapturePlan,
    QuoteSourceCapture,
    collect_quote_source,
    retain_quote_grid,
)
from oasis_agent.farming.quote_grid_replay import QuoteRuntimePin, replay_quote_grid
from oasis_agent.models.common import canonical_json
from oasis_agent.rpc.budget import BudgetedPoster
from oasis_agent.rpc.transport import JsonRpcTransport
from oasis_agent.uniswap_v4.quotes import (
    QuoteDirection,
    QuoteKind,
    QuotePoolKey,
    SinglePoolQuoteRequest,
)
from scripts import capture_quote_grid as cli
from scripts.replay_quote_grid import Request

HASH = "0x" + keccak(bytes.fromhex("6000")).hex()
HEADER = {
    "number": "0x64",
    "hash": "0x" + "ab" * 32,
    "parentHash": "0x" + "cd" * 32,
    "timestamp": "0x3e8",
}
ADDRESSES = ["0x" + c * 40 for c in ("1", "2", "3", "4")]


def plan():
    return QuoteCapturePlan(
        8453,
        100,
        QuotePoolKey("0x" + "00" * 20, "0x" + "77" * 20, 500, 10, "0x" + "00" * 20),
        tuple(
            QuoteRuntimePin(role, address, HASH)
            for role, address in zip(
                ("pool_manager", "quoter", "universal_router"), ADDRESSES, strict=False
            )
        ),
        ADDRESSES[3],
        HASH,
        ("rpc_a", "rpc_b"),
        tuple(
            SinglePoolQuoteRequest(k, d, n)
            for k in QuoteKind
            for d in QuoteDirection
            for n in (10000, 100000, 1000000)
        ),
    )


class Poster:
    def __init__(self, url, mode="ok"):
        self.url = url
        self.mode = mode
        self.calls = []

    def post(self, payload):
        req = json.loads(payload)
        method, params = req["method"], req["params"]
        self.calls.append(req)
        if method == "eth_chainId":
            value = "0x2105" if self.mode != "chain" else "0x1"
        elif method == "eth_getBlockByNumber":
            value = {**HEADER}
            if self.mode == "reorg" and len(self.calls) > 2:
                value["hash"] = "0x" + "cc" * 32
        elif method == "eth_getCode":
            value = "0x6000" if self.mode != "code" else "0x6001"
        elif params[0]["data"] == "0xdc4c90d3":
            value = (
                "0x"
                + encode(
                    ["address"], [ADDRESSES[0] if self.mode != "getter" else ADDRESSES[2]]
                ).hex()
            )
        elif params[0]["to"] == ADDRESSES[3]:
            value = (
                "0x" + encode(["uint160", "int24", "uint24", "uint24"], [1 << 96, 0, 0, 500]).hex()
            )
        else:
            if self.mode == "transport":
                raise OSError("private URL details should never escape")
            _, _, amount, _ = decode(
                ["((address,address,uint24,int24,address),bool,uint128,bytes)"],
                bytes.fromhex(params[0]["data"][10:]),
            )[0]
            quoted = amount - amount // 2000
            if self.mode == "zero":
                quoted = 0
            value = "0x" + encode(["uint256", "uint256"], [quoted, 50000]).hex()
        return canonical_json({"jsonrpc": "2.0", "id": req["id"], "result": value})


def capture_pair(p, mode="ok"):
    gathered = []
    posters = []
    for source in p.providers:
        poster = Poster("https://example.com", mode)
        posters.append(poster)
        recorder = BoundedExchangeRecorder()
        transport = JsonRpcTransport(
            "https://example.com", http=poster, retries=0, recorder=recorder
        )
        gathered.append(collect_quote_source((transport, recorder, source), p))
    return tuple(gathered), posters


def test_whole_capture_replays_true_hash_pins_and_retains_header_quorum(tmp_path):
    p = plan()
    captures, posters = capture_pair(p)
    store = EvidenceStore(tmp_path / "evidence.db")
    try:
        request = retain_quote_grid(store, captures)
        result = replay_quote_grid(store, store, request)
        assert result.block.number == 100 and len(result.quotes) == 12
        assert not result.target_admitted and not result.execution_eligible
        for poster in posters:
            assert len(poster.calls) == p.successful_request_count == 22
            for req in poster.calls:
                if req["method"] in ("eth_getCode", "eth_call"):
                    assert req["params"][-1] == {
                        "blockHash": HEADER["hash"],
                        "requireCanonical": True,
                    }
        assert len(request.references) == 32 and len(request.states) == 2
    finally:
        store.close()


@pytest.mark.parametrize("mode", ["chain", "reorg", "code", "getter", "zero"])
def test_wrong_actual_raw_capture_refuses_before_evidence_retention(mode):
    with pytest.raises(ValueError):
        capture_pair(plan(), mode)


def test_mutant_number_pins_and_boolean_alias_refuse_whole_raw_capture():
    p = plan()
    captures, _ = capture_pair(p)
    c = captures[0]
    for pin in ("0x64", {"blockHash": HEADER["hash"], "requireCanonical": 1}):
        xs = list(c.exchanges)
        req = json.loads(xs[4].request_payload)
        req["params"][-1] = pin
        xs[4] = replace(xs[4], request_payload=canonical_json(req))
        with pytest.raises(ValueError):
            QuoteSourceCapture(p, c.source, c.header, tuple(xs))


def setup_cli(tmp_path, monkeypatch, mode="ok"):
    p = plan()
    root = tmp_path / "capture"
    root.mkdir()
    config = root / "config.json"
    raw = {
        "schema": "oasis.hash-quote-capture.v1",
        "purpose": "new_block_hash_bound_quote_spot_benchmark",
        "output_directory": str(root),
        "plan": asdict(p),
        "sources": [
            {"alias": "rpc_a", "env": "RPC_A", "endpoint": "https://a.example"},
            {"alias": "rpc_b", "env": "RPC_B", "endpoint": "https://b.example"},
        ],
        "max_requests_per_source": 48,
        "min_interval_ms": 3000,
        "automatic_retries": 0,
    }
    config.write_bytes(canonical_json(raw))
    posters = []

    def poster(url):
        instance = Poster(url, mode)
        posters.append(instance)
        return instance

    monkeypatch.setattr(cli, "CurlPoster", poster)
    ticks = itertools.count(1_000_000, 3000)

    def budget(*args, **kwargs):
        return BudgetedPoster(*args, **kwargs, clock=lambda: next(ticks), sleep=lambda _: None)

    monkeypatch.setattr(cli, "BudgetedPoster", budget)
    urls = {"rpc_a": "https://a.example", "rpc_b": "https://b.example"}
    return root, config, urls, posters


def counts(root):
    db = sqlite3.connect(f"file:{root}/budget.db?mode=ro", uri=True)
    try:
        result = {}
        for (raw,) in db.execute("SELECT payload FROM rpc_usage"):
            source = json.loads(raw)["source"]
            result[source] = result.get(source, 0) + 1
        return result
    finally:
        db.close()


def test_cli_durable_budget_complete_offline_replay_and_no_implicit_restarts(tmp_path, monkeypatch):
    root, config, urls, posters = setup_cli(tmp_path, monkeypatch)
    cli.capture(config, root, urls)
    assert counts(root) == {"rpc_a": 22, "rpc_b": 22}
    request = Request.from_json((root / "input.json").read_bytes()).request
    store = EvidenceStore(root / "evidence.db")
    try:
        result = replay_quote_grid(store, store, request)
    finally:
        store.close()
    report = json.loads((root / "report.json").read_bytes())
    assert report["quotes"] == json.loads(canonical_json(asdict(result)))["quotes"]
    assert not report["execution_eligible"] and not report["target_admitted"]
    before = hashlib.sha256((root / "budget.db").read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="investigation"):
        cli.capture(config, root, urls)
    assert hashlib.sha256((root / "budget.db").read_bytes()).hexdigest() == before
    assert sum(len(p.calls) for p in posters) == 44
    alternate = tmp_path / "new-ledger"
    with pytest.raises(ValueError, match="directory"):
        cli.capture(config, alternate, urls)
    assert not alternate.exists()


def test_failed_network_attempt_spends_budget_is_retained_and_refuses_implicit_retry(
    tmp_path, monkeypatch
):
    root, config, urls, posters = setup_cli(tmp_path, monkeypatch, "transport")
    with pytest.raises(ValueError, match="retained attempts"):
        cli.capture(config, root, urls)
    assert counts(root) == {"rpc_a": 10, "rpc_b": 10}
    for source in ("rpc_a", "rpc_b"):
        rows = [
            json.loads(line)
            for line in (root / f"{source}-attempts.jsonl").read_bytes().splitlines()
        ]
        assert len(rows) == 10 and rows[-1]["response"] is None
        assert rows[-1]["transport_status"] == "transport_failure_redacted"
    assert not (root / "report.json").exists() and not (root / "input.json").exists()
    assert b"private URL" not in (root / "failure.json").read_bytes()
    before = counts(root)
    with pytest.raises(ValueError, match="investigation"):
        cli.capture(config, root, urls)
    assert counts(root) == before and sum(len(p.calls) for p in posters) == 20


@pytest.mark.parametrize("change", ["scope", "deleted_ledger"])
def test_replaced_scope_or_missing_original_ledger_never_creates_fresh_quota(
    tmp_path, monkeypatch, change
):
    root, config, urls, _ = setup_cli(tmp_path, monkeypatch)
    cli.capture(config, root, urls)
    if change == "scope":
        raw = json.loads(config.read_bytes())
        raw["max_requests_per_source"] = 49
        config.write_bytes(canonical_json(raw))
    else:
        (root / "budget.db").unlink()  # synthetic attack only; no real ledger touched
    with pytest.raises(ValueError, match="replaced"):
        cli.capture(config, root, urls)
    if change == "deleted_ledger":
        assert not (root / "budget.db").exists()


@pytest.mark.parametrize(
    "case", ["quota", "source", "duplicate_size", "extra", "retry", "boolean_retry"]
)
def test_invalid_scope_refuses_before_root_or_network(tmp_path, case):
    p = plan()
    raw = {
        "schema": "oasis.hash-quote-capture.v1",
        "purpose": "new_block_hash_bound_quote_spot_benchmark",
        "output_directory": str(tmp_path / "out"),
        "plan": asdict(p),
        "sources": [
            {"alias": "rpc_a", "env": "RPC_A", "endpoint": "https://a.example"},
            {"alias": "rpc_b", "env": "RPC_B", "endpoint": "https://b.example"},
        ],
        "max_requests_per_source": 48,
        "min_interval_ms": 3000,
        "automatic_retries": 0,
    }
    if case == "quota":
        raw["max_requests_per_source"] = 21
    elif case == "source":
        raw["sources"][1]["alias"] = "rpc_a"
    elif case == "duplicate_size":
        raw["plan"]["probes"] = (*p.probes, p.probes[0])
        raw["plan"]["probes"] = [asdict(p) for p in raw["plan"]["probes"]]
    elif case == "extra":
        raw["unknown_field"] = "forbidden extra"
    elif case == "boolean_retry":
        raw["automatic_retries"] = False
    else:
        raw["automatic_retries"] = 1
    with pytest.raises(ValueError):
        cli.Scope.from_json(canonical_json(raw))
