"""Reproduce the three-day venue comparison and audit the two preselected nearest peers."""

import argparse
import json
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory

from replay_eth_twenty_flow import load
from replay_funded_walk_cases import WETH, examine

from oasis_agent.arbitrage.cycle_protocols import abi, topic
from oasis_agent.evidence.research_traces import first_failure, nested_calls
from scripts.screen_multihop_archive import audit_queue, screen

MORPHO = "0xbbbbbbbbbb9cc5e90e3b3af64bdaf62c37eeffcb"
MANAGER = "0x000000000004444c5dc75cb358380d2e3de08a90"


def decode(frames, path, sender, recipient, signature, types):
    f = frames[path]
    assert f.sender == sender and f.recipient == recipient and f.kind == "CALL" and f.value == 0
    assert f.data[:4].hex() == topic(signature)[2:10] and first_failure(frames, path) is None
    return abi(types, f.data[4:])


def cashout(root, pick):
    tx, height, index = pick["tx"], pick["block"], pick["index"]
    audited = examine(root, height, index, tx, "")
    assert audited["hash"] == pick["block_hash"]
    for stem, wire in audited["wires"].items():
        source = {
            "trace": "https://eth.drpc.org",
            "receipts": "https://eth.nodely.dev",
            "header": "https://ethereum.publicnode.com",
        }[stem.rsplit("-", 1)[1]]
        assert wire["source"] == source
    header = load(root, f"{height}-header", "eth_getBlockByNumber", [hex(height), False])
    frames = nested_calls(
        load(
            root,
            tx[2:14] + "-trace",
            "debug_traceTransaction",
            [tx, {"tracer": "callTracer", "tracerConfig": {"withLog": True}, "timeout": "10s"}],
        )
    )
    executor, sender = audited["executor"], audited["sender"]
    if pick["day"] == "2026-11-13":
        token, principal, payload = decode(
            frames,
            (0, 1),
            executor,
            MORPHO,
            "flashLoan(address,uint256,bytes)",
            ("address", "uint256", "bytes"),
        )
        assert token == WETH
        assert decode(
            frames, (0, 1, 0), MORPHO, WETH, "transfer(address,uint256)", ("address", "uint256")
        ) == (executor, principal)
        assert decode(
            frames,
            (0, 1, 1),
            MORPHO,
            executor,
            "onMorphoFlashLoan(uint256,bytes)",
            ("uint256", "bytes"),
        ) == (principal, payload)
        assert decode(
            frames,
            (0, 1, 2),
            MORPHO,
            WETH,
            "transferFrom(address,address,uint256)",
            ("address", "address", "uint256"),
        ) == (executor, MORPHO, principal)
        assert all(frames[(0, 1, n)].output == (1).to_bytes(32, "big") for n in (0, 2))
        settlement = {
            "checked_calls": "WETH flashLoan, callback, transfer and transferFrom",
            "principal_weth_wei": str(principal),
            "financing_contract": MORPHO,
        }
        branch, start = (0,), 3
    else:
        (payload,) = decode(frames, (0,), executor, MANAGER, "unlock(bytes)", ("bytes",))
        assert decode(frames, (0, 0), MANAGER, executor, "unlockCallback(bytes)", ("bytes",)) == (
            payload,
        )
        settlement = {
            "checked_calls": "PoolManager unlock and matching callback",
            "financing_contract": None,
            "principal_weth_wei": None,
        }
        branch, start = (), 2
    (amount,) = decode(frames, (*branch, start), executor, WETH, "withdraw(uint256)", ("uint256",))
    for offset, recipient in ((1, header["miner"]), (2, sender)):
        path = (*branch, start + offset)
        f = frames[path]
        assert f.sender == executor and f.recipient == recipient and f.kind == "CALL"
        assert not f.data and first_failure(frames, path) is None
    fee = frames[(*branch, start + 1)].value
    forwarded = frames[(*branch, start + 2)].value
    assert amount == fee + forwarded and fee == int(audited["direct_fee_recipient_wei"])
    assert forwarded - int(audited["gas_paid_wei"]) == int(
        audited["combined_visible_eth_weth_after_gas_wei"]
    )
    assert not audited["other_erc20_visible_deltas"]
    return {
        **audited,
        "archive_events": pick["events"],
        "similarity_intersection_union": [pick["shared_observations"], pick["union_observations"]],
        "settlement": {
            **settlement,
            "weth_unwrapped_wei": str(amount),
            "native_to_sender_wei": str(forwarded),
        },
    }


def reviewed_inputs(peers):
    docs = Path(__file__).resolve().parents[1]
    target = json.loads((docs / "ETH_109_MOTIVE_20261127.json").read_bytes())
    entries = [(target["block_hash"], target["target_tx"])]
    cohort = json.loads((docs / "ETH_BATCH_COHORT_20261127.json").read_bytes())
    entries += [(r["block_hash"], r["transaction_hash"]) for r in cohort["rooted_samples"]]
    prior = json.loads((docs / "ETH_ARCHIVE_SERIAL_SEARCH_20261127.json").read_bytes())
    for row in prior["decoded_cases"]:
        r = row["archive_selection"]
        entries.append((r["block_hash"], r["tx"]))
    prior = json.loads((docs / "FUNDED_WALK_SAMPLES_20261127.json").read_bytes())
    entries += [(r["hash"], r["tx"]) for r in prior + peers]
    return [{"chain_id": 1, "block_hash": b, "tx": t} for b, t in sorted(set(entries))]


def replay(research):
    root = research / "multihop-family-screen-20261127"
    ref = research / "eth-transfer-fingerprint-20261127/logs.json"
    prior = screen(research / "eth-open-archive-20261127/candidates.csv", ref, chain_id=1)
    added = screen(root / "newday.csv", ref, chain_id=1)
    assert prior["reference_in_archive"] and not added["reference_in_archive"]
    assert (
        prior["reference"] == added["reference"] and not prior["days"].keys() & added["days"].keys()
    )
    picks = [v["closest"][0] for v in prior["days"].values()]
    frozen = json.loads((root / "rpc-plan.json").read_bytes())
    assert [(p["tx"], p["block_hash"], p["index"]) for p in picks] == [
        (p["tx"], p["block_hash"], p["index"]) for p in frozen["selection"]
    ]
    peers = [cashout(root, pick) for pick in picks]
    days = dict(sorted((prior["days"] | added["days"]).items()))
    reviewed = reviewed_inputs(peers)
    with TemporaryDirectory() as d:
        path = Path(d) / "reviewed.json"
        path.write_bytes(json.dumps(reviewed, sort_keys=True, indent=2).encode())
        queue = audit_queue({"days": days, "chain_id": 1}, path)
    return {
        "schema": "oasis-multihop-family-evidence-v1",
        "chain_id": 1,
        "source_hashes": {
            "earlier_csv": prior["archive_sha256"],
            "new_csv": added["archive_sha256"],
            "reference_logs": prior["reference_logs_sha256"],
            "scope": sha256((root / "rpc-plan.json").read_bytes()).hexdigest(),
        },
        "reference": prior["reference"],
        "matched_rows": prior["event_rows"] + added["event_rows"],
        "days": days,
        "peer_audits": peers,
        "reviewed_identities": reviewed,
        "audit_queue": queue,
    }


if __name__ == "__main__":
    if not __debug__:
        raise RuntimeError("evidence verification requires assertions")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("research_root", type=Path)
    print(json.dumps(replay(parser.parse_args().research_root), sort_keys=True, indent=2))
