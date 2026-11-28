"""Verify Aleph Base Reth genesis wire evidence and preserved historical startup failure."""

import argparse
import json
from pathlib import Path

import rlp
from eth_hash.auto import keccak
from replay_base_mixed_whole import digest, read

from oasis_agent.evidence.state_trie import PRAGUE_FIELDS, hex_bytes, quantity
from oasis_agent.rpc.protocol import response_envelope

ROOT = Path("reports/research/base-reth-parent-20261128")
REPORT = Path("docs/verification/BASE_RETH_PARENT_20261128.json")


def genesis_control(root):
    wire = root / "genesis"
    expected = (
        ("chain", 1, "eth_chainId", []),
        ("head", 2, "eth_getBlockByNumber", ["0x0", False]),
        (
            "whole",
            3,
            "eth_simulateV1",
            [
                {
                    "blockStateCalls": [
                        {
                            "calls": [
                                {
                                    "from": "0x000000000000000000000000000000000000dead",
                                    "to": "0x0000000000000000000000000000000000000004",
                                    "gas": "0xc350",
                                    "value": "0x0",
                                    "data": "0x42617365",
                                }
                            ]
                        }
                    ],
                    "validation": False,
                    "traceTransfers": False,
                },
                "0x0",
            ],
        ),
    )
    results, proofs = {}, {}
    for label, idx, method, params in expected:
        request, response = wire / f"{label}.request", wire / f"{label}.response"
        assert read(request) == {"jsonrpc": "2.0", "id": idx, "method": method, "params": params}
        results[label] = response_envelope(response.read_bytes(), idx)["result"]
        proofs[label] = {"request_sha256": digest(request), "response_sha256": digest(response)}
    head = results["head"]
    fields = [
        quantity(head[k], -width)
        if width < 0
        else hex_bytes(head[k], width or 8192, exact=bool(width))
        for k, width in PRAGUE_FIELDS[:16]
    ]
    assert results["chain"] == "0x2105"
    assert head["number"] == "0x0" and head["hash"] == "0x" + keccak(rlp.encode(fields)).hex()
    [child] = results["whole"]
    [call] = child["calls"]
    assert child["parentHash"] == head["hash"] and child["number"] == "0x1"
    assert call["status"] == "0x1" and call["returnData"] == "0x42617365" and call["logs"] == []
    assert quantity(call["gasUsed"], 8) == quantity(child["gasUsed"], 8)
    startup = root / "first-startup.log"
    assert (
        "--disable-discovery' cannot be used with '--disable-dns-discovery" in startup.read_text()
    )
    return {
        "chain_id": 8453,
        "local_parent_number": 0,
        "local_parent_hash": head["hash"],
        "parent_state_root": head["stateRoot"],
        "simulated_parent_hash": child["parentHash"],
        "simulated_child_number": 1,
        "gas_used": quantity(call["gasUsed"], 8),
        "return_data": call["returnData"],
        "rpc_public_calls": 0,
        "historical_startup_log_sha256": digest(startup),
        "historical_startup_cause": "mutually exclusive discovery flags",
        "wire": proofs,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--replay", action="store_true")
    args = parser.parse_args()
    result = genesis_control(ROOT)
    if args.replay:
        manifest = read(REPORT)
        for name, expected in manifest["retained"]["local_sha256"].items():
            assert digest(ROOT / name) == expected
        assert manifest["attempt"]["historical_simulations_on_canonical_state"] == 0
        assert manifest["genesis_wire_control"] == result
    else:
        print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
