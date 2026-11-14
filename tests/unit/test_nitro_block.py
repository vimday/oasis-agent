"""Nitro header, body and receipt evidence checked without a live RPC."""

import copy
import json

import httpx
import pytest
import rlp
from eth_account import Account
from eth_hash.auto import keccak

from oasis_agent.arbitrage.cycle_protocols import calldata
from oasis_agent.evidence import nitro_block as nitro
from oasis_agent.evidence.state_trie import PRAGUE_FIELDS
from oasis_agent.models.common import canonical_json
from scripts import capture_public_rpc as rpc
from tests.unit.test_public_rpc_capture import counts, setup
from tests.unit.test_public_state_proofs import ACCOUNT, CODE, hex_data, vector


def nitro_fixture():
    chain, number = nitro.ELYSIUM_CHAIN_ID, 102
    action = calldata(
        "startBlock(uint256,uint64,uint64,uint64)", nitro.START_BLOCK_ABI, (0, 1000, number, 1)
    )
    internal_raw = bytes([nitro.SYSTEM_TYPE]) + rlp.encode([chain, bytes.fromhex(action[2:])])
    signer = Account.create()
    tx = {
        "type": 2,
        "chainId": chain,
        "nonce": 3,
        "gas": 21000,
        "to": Account.create().address,
        "value": 17,
        "maxPriorityFeePerGas": 1,
        "maxFeePerGas": 8,
        "accessList": [],
        "data": "0x",
    }
    signed = signer.sign_transaction(tx)
    tx["input"] = tx.pop("data")
    tx = {k: hex(v) if isinstance(v, int) else v for k, v in tx.items()}
    tx.update(
        {
            "from": signer.address,
            "v": hex(signed.v),
            "yParity": hex(signed.v),
            "r": hex(signed.r),
            "s": hex(signed.s),
        }
    )
    txs = [
        dict(
            type="0x6a",
            chainId=hex(chain),
            nonce="0x0",
            gas="0x0",
            to=nitro.ARBOS,
            value="0x0",
            input=action,
            v="0x0",
            r="0x0",
            s="0x0",
            **{"from": nitro.ARBOS},
        ),
        tx,
    ]
    hashes = [hex_data(keccak(raw)) for raw in (internal_raw, signed.raw_transaction)]
    empty_bloom = bytes(256)
    receipt_values = [
        b"\x6a" + rlp.encode([1, 0, empty_bloom, []]),
        b"\x02" + rlp.encode([0, 21000, empty_bloom, []]),
    ]
    fields = [
        bytes.fromhex("11" * 32),
        keccak(rlp.encode([])),
        bytes(20),
        bytes.fromhex(vector()[0]["stateRoot"][2:]),
        nitro.indexed_trie_root([internal_raw, signed.raw_transaction]),
        nitro.indexed_trie_root(receipt_values),
        empty_bloom,
        1,
        number,
        30_000_000,
        21000,
        1_770_000_000,
        bytes(32),
        bytes(32),
        bytes(8),
        7,
    ]
    header = {
        key: hex(value) if isinstance(value, int) else hex_data(value)
        for (key, _), value in zip(PRAGUE_FIELDS[:16], fields, strict=True)
    }
    block_hash = hex_data(keccak(rlp.encode(fields)))
    header.update(hash=block_hash, transactions=hashes)
    receipts = []
    for index, raw in enumerate(txs):
        coord = {
            "blockNumber": hex(number),
            "blockHash": block_hash,
            "transactionIndex": hex(index),
        }
        raw.update(coord, hash=hashes[index])
        receipts.append(
            {
                **coord,
                "transactionHash": hashes[index],
                "type": raw["type"],
                "from": raw["from"],
                "to": raw["to"],
                "status": hex(1 - index),
                "gasUsed": hex(index * 21000),
                "cumulativeGasUsed": hex(index * 21000),
                "effectiveGasPrice": hex(index * 8),
                "logsBloom": hex_data(empty_bloom),
                "logs": [],
            }
        )
    return header, {**header, "transactions": txs}, receipts


def test_indexed_mpt_uses_rlp_keys_and_embeds_short_children():
    # RLP index 0 is 0x80, index 1 is 0x01, with distinct first nibbles.
    branch = [b""] * 17
    branch[0] = [b"\x31", b"b"]
    branch[8] = [b"\x30", b"a"]
    assert nitro.indexed_trie_root([b"a", b"b"]) == keccak(rlp.encode(branch))
    assert nitro.indexed_trie_root([b"a"]) == keccak(rlp.encode([b"\x20\x80", b"a"]))
    assert nitro.indexed_trie_root([]) == keccak(b"\x80")


@pytest.mark.parametrize("fault", [None, "receipt", "role", "sender", "prefix", "header"])
def test_block_commitments_validate_both_tries_and_ordinals(fault):
    header, full, receipts = nitro_fixture()
    expected = header["hash"]
    if fault == "receipt":
        receipts[1]["status"] = "0x1"
    elif fault == "role":
        receipts[1]["to"] = nitro.ARBOS
    elif fault == "sender":
        full["transactions"][1]["from"] = nitro.ARBOS
    elif fault == "prefix":
        full["transactions"][0]["input"] = full["transactions"][0]["input"][:-2] + "02"
    elif fault == "header":
        header["stateRoot"] = "0x" + "37" * 32

    def verify():
        block = nitro.nitro_header(header, 102, expected)
        rows = nitro.verify_receipt_root(block, receipts)
        return nitro.verify_transactions_root(block, full, rows), rows

    if fault:
        with pytest.raises(ValueError):
            verify()
    else:
        start, rows = verify()
        assert start.l2_block == 102 and start.elapsed_seconds == 1
        assert rows[0].type == 106 and rows[1].status == 0


@pytest.mark.parametrize(
    "proof, invalid", [(False, False), (False, True), (True, False), (True, True)]
)
def test_public_capture_verifies_receipts_or_state_and_stops(tmp_path, monkeypatch, proof, invalid):
    block, _, receipts = nitro_fixture()
    root, path, _ = setup(tmp_path, "publicnode_elysium", cap=8)
    payload = {
        "schema": "oasis-public-nitro-state-proof-v1"
        if proof
        else "oasis-public-nitro-receipts-v1",
        "source": "publicnode_elysium",
        "chain_id": 99901,
        "block_number": 102,
        "block_hash": block["hash"],
        **({"accounts": [{"address": ACCOUNT, "slots": []}]} if proof else {}),
    }
    path.write_bytes(canonical_json(payload))
    _, proof_response = vector()
    proof_response["storageProof"] = []
    if invalid and proof:
        proof_response["nonce"] = "0x2"
    if invalid and not proof:
        receipts = copy.deepcopy(receipts)
        receipts[1]["status"] = "0x1"
    calls = []
    original = httpx.Client

    def handler(request):
        req = json.loads(request.content)
        calls.append(req)
        assert counts(root)["publicnode_elysium"] == len(calls)
        if req["method"] == "eth_getProof":
            assert req["params"] == [ACCOUNT, [], hex(102)]
        if req["method"] == "eth_getBlockReceipts":
            assert req["params"] == [block["hash"]]
        returned = {
            "eth_chainId": hex(99901),
            "eth_getBlockByNumber": block,
            "eth_getBlockReceipts": receipts,
            "eth_getProof": proof_response,
            "eth_getCode": hex_data(CODE),
        }
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": req["id"], "result": returned[req["method"]]}
        )

    monkeypatch.setattr(
        rpc.httpx, "Client", lambda **kw: original(transport=httpx.MockTransport(handler), **kw)
    )
    assert rpc.capture(root, path, "historical") == (3 if invalid else 0)
    result = rpc.replay(root / "historical")
    assert result["status"] == ("incomplete" if invalid else "complete")
    assert len(calls) == (3 if invalid else 5 if proof else 4)
    assert (root / "STOP").exists() == invalid
    if proof:
        assert (result["block"] is None) == invalid
        if not invalid:
            assert result["block"]["accounts"][0]["native_balance_wei"] == 7
