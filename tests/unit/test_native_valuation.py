"""Oracle wire reconstruction and conservative integer valuation boundaries."""

from dataclasses import replace

import pytest
from eth_abi.abi import encode
from eth_hash.auto import keccak

from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.evidence.wallet_balance import NativeWalletSnapshot
from oasis_agent.farming.native_valuation import (
    EthUsdReference,
    native_usd_micros,
    replay_eth_usd_reference,
)
from oasis_agent.models.common import BlockRef, canonical_json

BLOCK = BlockRef(8453, 100, "0x" + "ab" * 32, 5000)
FEED = "0x" + "12" * 20
ACCOUNT = "0x" + "34" * 20
PRICE = EthUsdReference(
    BLOCK, FEED, 8, 239687030000, 7, 4900, 4950, 7, "0x" + keccak(b"\x01").hex()
)


def witness(store, *, fault=None):
    header = {"number": "0x64", "hash": BLOCK.block_hash, "timestamp": hex(5000)}
    answer = -1 if fault == "negative" else PRICE.answer
    updated = 1 if fault == "stale" else 4950
    started = 1 if fault == "stale" else 4900
    rounds = [7, answer, started, updated, 6 if fault == "round" else 7]
    calls = [
        ("eth_chainId", [], hex(8453)),
        ("eth_getBlockByNumber", ["0x64", False], header),
        (
            "eth_call",
            [{"to": FEED, "data": "0x" + keccak(b"decimals()")[:4].hex()}, "0x64"],
            "0x" + encode(["uint8"], [8]).hex(),
        ),
        (
            "eth_call",
            [{"to": FEED, "data": "0x" + keccak(b"latestRoundData()")[:4].hex()}, "0x64"],
            "0x" + encode(["uint80", "int256", "uint256", "uint256", "uint80"], rounds).hex(),
        ),
        ("eth_getCode", [FEED, "0x64"], "0x01"),
        ("eth_getBlockByNumber", ["0x64", False], header),
    ]
    if fault == "binding":
        calls[2][1][0]["to"] = ACCOUNT
    if fault == "reorg":
        calls[-1] = (calls[-1][0], calls[-1][1], dict(header, hash="0x" + "cd" * 32))
    envelopes = []
    for response in (False, True):
        items = []
        for i, (method, params, value) in enumerate(calls, 1):
            wire = {"jsonrpc": "2.0", "id": i}
            wire.update({"result": value} if response else {"method": method, "params": params})
            items.append(
                {
                    "method": method,
                    "request_id": i,
                    "attempt": 1,
                    "payload": canonical_json(wire).hex(),
                }
            )
        envelopes.append({"encoding": "hex_exact_bytes", "exchanges": items})
    payload = (
        replace(PRICE, answer=PRICE.answer + 1).payload if fault == "forged" else PRICE.payload
    )
    return store.put(
        "wallet_eth_usd_reference",
        payload,
        BLOCK,
        "fixture",
        request=envelopes[0],
        raw_response=envelopes[1],
    )


def test_oracle_reconstructs_wire(tmp_path):
    store = EvidenceStore(tmp_path / "e.db")
    try:
        assert replay_eth_usd_reference(store, witness(store), expected_feed=FEED) == PRICE
    finally:
        store.close()


@pytest.mark.parametrize("fault", ["negative", "stale", "round", "binding", "reorg", "forged"])
def test_bad_oracle_fails_closed(tmp_path, fault):
    store = EvidenceStore(tmp_path / "e.db")
    try:
        with pytest.raises(ValueError):
            replay_eth_usd_reference(store, witness(store, fault=fault), expected_feed=FEED)
    finally:
        store.close()


@pytest.mark.parametrize("wei", [0, 1, 10**18, 2**256 - 1] + [2**n - 1 for n in range(1, 256)])
def test_rounding_bounds_exact_value(wei):
    snapshot = NativeWalletSnapshot(ACCOUNT, BLOCK, wei)
    floor = native_usd_micros(snapshot, PRICE, round_up=False)
    ceil = native_usd_micros(snapshot, PRICE, round_up=True)
    denominator = 10**26
    numerator = wei * PRICE.answer * 1_000_000
    assert floor * denominator <= numerator <= ceil * denominator
    assert ceil - floor in (0, 1)


def test_cross_chain_reference_cannot_be_used_as_same_chain_valuation():
    snapshot = NativeWalletSnapshot(ACCOUNT, replace(BLOCK, chain_id=4663), 10**18)
    with pytest.raises(ValueError, match="exact block"):
        native_usd_micros(snapshot, PRICE, round_up=False)
