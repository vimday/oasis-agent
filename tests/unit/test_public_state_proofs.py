"""Frozen RPC capture and secure trie inclusion/exclusion with synthetic roots."""

import copy
import json

import httpx
import pytest
import rlp
from eth_hash.auto import keccak

from oasis_agent.arbitrage.metric_pair import Design, headers
from oasis_agent.evidence import state_trie as trie
from oasis_agent.models.common import canonical_json
from scripts import capture_public_rpc as capture

ACCOUNT = "0x" + "34" * 20
SLOT = "0x" + "00" * 32
MISSING = "0x" + "00" * 31 + "01"
CODE = b"\x60\x00"


def hex_data(value):
    return "0x" + value.hex()


def leaf(key, value):
    return rlp.encode([b"\x20" + keccak(key), value])


def test_metric_block_overrides_preserve_leading_zero_prev_randao():
    parent, _ = vector()
    target = dict(
        parent,
        number="0x65",
        timestamp=hex(int(parent["timestamp"], 16) + 12),
        parentHash=parent["hash"],
        mixHash="0x0" + "f" * 63,
    )
    fields = [
        int(target[name], 16) if width < 0 else bytes.fromhex(target[name][2:])
        for name, width in trie.PRAGUE_FIELDS
    ]
    target["hash"] = hex_data(keccak(rlp.encode(fields)))
    design = Design.model_construct(
        parent_hash=parent["hash"],
        target_hash=target["hash"],
        target_number=101,
        target_timestamp=int(target["timestamp"], 16),
    )
    _, overrides = headers(design, parent, target)
    assert overrides["prevRandao"] == target["mixHash"]


def vector():
    slot = leaf(bytes(32), rlp.encode(257))
    storage_root = keccak(slot)
    code_hash = keccak(CODE)
    account = leaf(bytes.fromhex(ACCOUNT[2:]), rlp.encode([1, 7, storage_root, code_hash]))
    root = keccak(account)
    # Same leaf can prove a present key and exclusion of a different key.
    proof = {
        "address": ACCOUNT,
        "nonce": "0x1",
        "balance": "0x7",
        "storageHash": hex_data(storage_root),
        "codeHash": hex_data(code_hash),
        "accountProof": [hex_data(account)],
        "storageProof": [
            {"key": SLOT, "value": "0x101", "proof": [hex_data(slot)]},
            {"key": "0x1", "value": "0x0", "proof": [hex_data(slot)]},
        ],
    }
    fields = [
        b"a" * 32,
        b"b" * 32,
        b"c" * 20,
        root,
        b"d" * 32,
        b"e" * 32,
        bytes(256),
        0,
        100,
        30_000_000,
        21_000,
        1_770_000_000,
        b"proof",
        b"f" * 32,
        bytes(8),
        7,
        b"g" * 32,
        0,
        0,
        b"h" * 32,
        b"i" * 32,
    ]
    names = [
        "parentHash",
        "sha3Uncles",
        "miner",
        "stateRoot",
        "transactionsRoot",
        "receiptsRoot",
        "logsBloom",
        "difficulty",
        "number",
        "gasLimit",
        "gasUsed",
        "timestamp",
        "extraData",
        "mixHash",
        "nonce",
        "baseFeePerGas",
        "withdrawalsRoot",
        "blobGasUsed",
        "excessBlobGas",
        "parentBeaconBlockRoot",
        "requestsHash",
    ]
    header = {
        k: hex(v) if isinstance(v, int) else hex_data(v) for k, v in zip(names, fields, strict=True)
    }
    header["hash"] = hex_data(keccak(rlp.encode(fields)))
    return header, proof


def test_inclusion_and_exclusion_and_code():
    header, proof = vector()
    _, root = trie.prague_header(header, 100, header["hash"])
    view = trie.account_proof(root, ACCOUNT, (SLOT, MISSING), proof)
    assert view.present and view.native_balance_wei == 7 and view.nonce == 1
    assert [(s.value_raw, s.present) for s in view.slots] == [(257, True), (0, False)]
    view.verify_code(hex_data(CODE))
    with pytest.raises(ValueError, match="code_mismatch"):
        view.verify_code("0x")


def test_branch_with_inline_leaf_and_extension():
    prefix = "ab" * 31 + "c"
    branch = [b""] * 17
    branch[0] = [b"\x20", b"\x05"]
    branch[1] = [b"\x20", b"\x06"]
    # Long extension contains the branch as an embedded child.
    node = rlp.encode([bytes.fromhex("1" + prefix), branch])
    proof = [hex_data(node)]
    root = keccak(node)
    assert trie.trie_value(root, bytes.fromhex(prefix + "0"), proof) == b"\x05"
    assert trie.trie_value(root, bytes.fromhex(prefix + "2"), proof) is None
    with pytest.raises(ValueError, match="extra_trie_nodes"):
        trie.trie_value(root, bytes.fromhex(prefix + "0"), [*proof, hex_data(rlp.encode(branch))])


def test_missing_hash_child_is_not_absence():
    child = rlp.encode([b"\x3a" + b"\x11" * 31, b"validated value"])
    branch = [b""] * 17
    branch[0] = keccak(child)
    branch[1] = keccak(b"another node")
    node = rlp.encode(branch)
    key = bytes.fromhex("0a" + "11" * 31)
    proof = [hex_data(node)]
    with pytest.raises(ValueError, match="missing_or_hash_mismatch"):
        trie.trie_value(keccak(node), key, proof)
    assert trie.trie_value(keccak(node), key, [*proof, hex_data(child)]) == b"validated value"


def test_excluded_account_cannot_claim_nonzero_storage_or_nonempty_code():
    header, proof = vector()
    other = "0x" + "2a" * 20
    proof.update(
        address=other,
        nonce="0x0",
        balance="0x0",
        storageHash=hex_data(trie.EMPTY_TRIE),
        codeHash=hex_data(trie.EMPTY_CODE),
        storageProof=[{"key": SLOT, "value": "0x0", "proof": []}],
    )
    account = trie.account_proof(header["stateRoot"], other, (SLOT,), proof)
    assert not account.present and account.slots[0].value_raw == 0
    proof.update(storageHash=SLOT, codeHash=SLOT)
    assert trie.account_proof(header["stateRoot"], other, (SLOT,), proof) == account
    proof["storageProof"][0]["value"] = "0x1"
    with pytest.raises(ValueError, match="value_mismatch"):
        trie.account_proof(header["stateRoot"], other, (SLOT,), proof)


@pytest.mark.parametrize("bad", ["account", "storage", "key", "nonce", "omitted", "root"])
def test_invalid_proofs_cannot_become_known_state(bad):
    head, proof = vector()
    if bad == "account":
        proof["accountProof"].clear()
    elif bad == "storage":
        proof["storageProof"][0]["value"] = "0x100"
    elif bad == "key":
        proof["storageProof"].reverse()
    elif bad == "nonce":
        proof["nonce"] = "0x01"
    elif bad == "omitted":
        proof["storageProof"].pop()
    else:
        head["stateRoot"] = "0x" + "ab" * 32
    with pytest.raises(ValueError):
        trie.account_proof(head["stateRoot"], ACCOUNT, (SLOT, MISSING), proof)


@pytest.mark.parametrize("bad", [None, "opening", "proof", "runtime", "closing"])
def test_capture_stops_and_clears_partial_verified_state(tmp_path, monkeypatch, bad):
    header, proof = vector()
    scope_path, plan_path = tmp_path / "scope", tmp_path / "plan"
    root = tmp_path / "campaign"
    scope_path.write_bytes(
        canonical_json(
            {
                "schema": "oasis-public-rpc-scope-v1",
                "purpose": "synthetic_proof_test",
                "expires_utc": "2100-01-01T00:00:00Z",
                "max_capture_seconds": 30,
                "sources": [{"source": "publicnode_ethereum", "max_requests": 5}],
            }
        )
    )
    plan_path.write_bytes(
        canonical_json(
            {
                "schema": "oasis-public-state-proof-plan-v1",
                "source": "publicnode_ethereum",
                "chain_id": 1,
                "block_number": 100,
                "block_hash": header["hash"],
                "accounts": [{"address": ACCOUNT, "slots": [SLOT, MISSING]}],
            }
        )
    )
    capture.initialize(scope_path, root)
    calls = []
    original = httpx.Client

    def handler(req):
        call = json.loads(req.content)
        calls.append(call)
        n = call["id"]
        result = copy.deepcopy({1: "0x1", 2: header, 3: proof, 4: hex_data(CODE), 5: header}[n])
        if bad == "proof" and n == 3:
            result["accountProof"] = []
        elif bad == "runtime" and n == 4:
            result = "0x"
        elif (bad == "opening" and n == 2) or (bad == "closing" and n == 5):
            result["stateRoot"] = "0x" + "00" * 32
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": n, "result": result})

    monkeypatch.setattr(
        httpx, "Client", lambda **kw: original(transport=httpx.MockTransport(handler), **kw)
    )
    assert capture.capture(root, plan_path, "proof") == (3 if bad else 0)
    report = capture.replay(root / "proof")
    assert report["plan_kind"] == "pinned_state_proofs"
    assert (
        report["reserved_requests"]
        == len(calls)
        == {None: 5, "opening": 2, "proof": 3, "runtime": 4, "closing": 5}[bad]
    )
    assert report["original_http_bytes_verified"] and not report["execution_eligible"]
    assert (root / "STOP").exists() == bool(bad)
    if bad != "opening":
        assert calls[2]["params"] == [
            ACCOUNT,
            [SLOT, MISSING],
            {"blockHash": header["hash"], "requireCanonical": True},
        ]
    if bad:
        assert report["block"] is None
    else:
        assert report["block"]["state_root"] == header["stateRoot"]
        assert report["block"]["accounts"][0]["slots"][0]["value_raw"] == 257
