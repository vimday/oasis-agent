"""Raw balance witnesses bind account, chain, block and exact integer wei."""

import json
import sys
from dataclasses import replace
from itertools import count
from types import SimpleNamespace

import pytest
from pydantic import TypeAdapter

from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.evidence.wallet_balance import reconstruct_native_balance, replay_native_balance
from oasis_agent.models.common import BlockRef, EvidenceRef, canonical_json
from oasis_agent.rpc.budget import BudgetedPoster
from oasis_agent.rpc.transport import RpcExchange
from scripts import capture_wallet_balances as cli

ACCOUNT = "0x" + "12" * 20
BLOCK = BlockRef(8453, 100, "0x" + "ab" * 32, 1000)


def capture():
    header = {"number": "0x64", "hash": BLOCK.block_hash, "timestamp": "0x3e8"}
    return tuple(
        RpcExchange(
            method,
            i,
            canonical_json({"jsonrpc": "2.0", "id": i, "method": method, "params": params}),
            canonical_json({"jsonrpc": "2.0", "id": i, "result": result}),
            1,
        )
        for i, (method, params, result) in enumerate(
            [
                ("eth_chainId", [], hex(BLOCK.chain_id)),
                ("eth_getBlockByNumber", ["0x64", False], header),
                ("eth_getBalance", [ACCOUNT, "0x64"], hex(2**100 + 1)),
                ("eth_getBlockByNumber", ["0x64", False], header),
            ],
            1,
        )
    )


def test_balance_is_exact_even_above_float_integer_precision():
    result = reconstruct_native_balance(capture(), account=ACCOUNT, block=BLOCK)
    assert result.native_wei == 2**100 + 1


@pytest.mark.parametrize("failure", ["account", "chain", "number", "hash", "timestamp", "missing"])
def test_native_balance_fails_on_misbound_or_missing_wire(failure):
    account, block, exchanges = ACCOUNT, BLOCK, capture()
    if failure == "account":
        account = "0x" + "34" * 20
    elif failure == "chain":
        block = replace(block, chain_id=4663)
    elif failure == "number":
        block = replace(block, number=101)
    elif failure == "hash":
        block = replace(block, block_hash="0x" + "cd" * 32)
    elif failure == "timestamp":
        block = replace(block, timestamp=1001)
    else:
        exchanges = exchanges[:-1]
    with pytest.raises(ValueError):
        reconstruct_native_balance(exchanges, account=account, block=block)


def test_recheck_reorg_rejected():

    exchanges = list(capture())
    value = json.loads(exchanges[-1].response_payload)
    value["result"]["hash"] = "0x" + "ef" * 32
    exchanges[-1] = replace(exchanges[-1], response_payload=canonical_json(value))
    with pytest.raises(ValueError, match="identity"):
        reconstruct_native_balance(tuple(exchanges), account=ACCOUNT, block=BLOCK)


@pytest.mark.parametrize("bad_primary", [False, True])
def test_capture_cli_preserves_valid_peer_when_primary_fails(tmp_path, monkeypatch, bad_primary):

    class Poster:
        def __init__(self, url, timeout):
            self.bad = bad_primary and url.endswith("primary")

        def post(self, raw):
            request = json.loads(raw)
            method, params = request["method"], request["params"]
            if method == "eth_chainId":
                value = hex(1 if self.bad else 8453)
            elif method == "eth_getBalance":
                assert params == [ACCOUNT, "0x64"]
                value = hex(2**100 + 1)
            elif method == "eth_getBlockByNumber":
                value = {
                    "number": "0x67" if params[0] == "latest" else "0x64",
                    "hash": BLOCK.block_hash,
                    "timestamp": "0x3e8",
                }
            else:
                raise AssertionError("unexpected RPC")
            return canonical_json({"jsonrpc": "2.0", "id": request["id"], "result": value})

    env = tmp_path / "rpc.env"
    env.write_text(
        "RPC_PRIMARY=https://fixture.invalid/primary\nRPC_SECONDARY=https://fixture.invalid/secondary\n"
    )
    out = tmp_path / "output"
    clock = count(1000, 3000)
    monkeypatch.setattr(cli, "_UrlLibPoster", Poster)
    monkeypatch.setattr(
        cli, "datetime", SimpleNamespace(now=lambda _: SimpleNamespace(timestamp=lambda: 1005))
    )
    monkeypatch.setattr(
        cli,
        "BudgetedPoster",
        lambda http, path, policies, source: BudgetedPoster(
            http, path, policies, source, clock=lambda: next(clock)
        ),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "capture",
            "--account",
            ACCOUNT,
            "--chain-id",
            "8453",
            "--primary-env",
            "RPC_PRIMARY",
            "--secondary-env",
            "RPC_SECONDARY",
            "--env-file",
            str(env),
            "--budget",
            str(tmp_path / "budget.db"),
            "--output",
            str(out),
        ],
    )
    if bad_primary:
        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 2
    else:
        cli.main()
    report = json.loads((out / "report.json").read_text())
    assert report["provider_agreement"] is (not bad_primary)
    store = EvidenceStore(out / "evidence.db")
    try:
        for source in report["sources"]:
            if "evidence" in source:
                value = replay_native_balance(
                    store, TypeAdapter(EvidenceRef).validate_python(source["evidence"])
                )
                assert value.native_wei == 2**100 + 1
    finally:
        store.close()
