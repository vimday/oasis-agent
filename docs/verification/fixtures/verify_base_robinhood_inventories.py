"""Validate the two historical inventories against retained wire hashes and full topic windows."""

import json
import re
from collections import Counter
from hashlib import sha256
from pathlib import Path

from oasis_agent.arbitrage.cycle_protocols import topic
from oasis_agent.arbitrage.event_quote_replay import replay as replay_trigger_receipts

REPORTS = Path("docs/verification")
INPUTS = Path("reports/research")
# Each source contains historical pool-event observations, including all hashes below the cutoff.
WINDOWS = {
    "sqd-4097": [INPUTS / "long-dex-20261123/base-mainnet/response.ndjson"],
    "sqd-4096": [INPUTS / "pretrade-tail-20261123/base-3/response.ndjson"],
    "recent-384": [
        INPUTS / f"recent-dex-arbitrage-20261123/base/{k:02}.response.json" for k in range(5, 9)
    ],
    "rpc-4096": [INPUTS / f"long-dex-20261123/robinhood-rpc/{k:02}.res" for k in range(2, 10)],
    "rpc-512": [INPUTS / "robinhood-tail-512-20261124/run/01-official.response"],
    "ordinary-closed-screen": [
        INPUTS / f"multichain-depth-20261123/robinhood/{k:02}.response.json" for k in (3, 4, 5)
    ],
}
SINGLETON = {
    topic("Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)"),
    topic("Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24,uint16)"),
}


def logs_in(path):
    if path.suffix == ".ndjson":
        with path.open() as blocks:
            for line in blocks:
                block = json.loads(line)
                for log in block.get("logs", []):
                    yield {
                        **log,
                        "blockNumber": block["header"]["number"],
                        "blockHash": block["header"]["hash"],
                    }
    else:
        yield from json.loads(path.read_bytes())["result"]


def quantity(v):
    return v if isinstance(v, int) else int(v, 16)


def verify(chain, chain_id):
    inv = json.loads(
        (REPORTS / f"{chain.upper()}_MULTIHOP_SAMPLE_INVENTORY_20261127.json").read_bytes()
    )
    assert (inv["chain"], inv["chain_id"]) == (chain, chain_id)
    samples = {r["tx_hash"]: r for r in inv["samples"]}
    assert len(samples) == len(inv["samples"]) == inv["sample_count"]
    assert Counter(r["shape"] for r in samples.values()) == inv["shape_counts"]
    assert set(inv["shape_counts"]) <= set(inv["shape_semantics"])
    groups = {c["id"]: c for c in inv["cohorts"]}
    assert len(groups) == len(inv["cohorts"])
    for tx, row in samples.items():
        assert re.fullmatch("0x[0-9a-f]{64}", tx) and row["observations"]
        assert set(row["evidence"]) == {
            groups[o["cohort"]]["evidence"] for o in row["observations"]
        }
        assert not any(
            "shadow_non_transaction" in groups[o["cohort"]].get("kind", "")
            for o in row["observations"]
        )
    for key, c in groups.items():
        assert Path(c["evidence"]).is_file()
        for filename, h in c["local_inputs_sha256"].items():
            assert sha256(Path(filename).read_bytes()).hexdigest() == h
        if key in {"event-quote-nov14", "event-quote-nov23"}:
            summary = next(Path(f) for f in c["local_inputs_sha256"] if f.endswith("/summary.json"))
            result = replay_trigger_receipts(summary.parent)
            capture = json.loads(summary.read_bytes())
            selected = {r["tx"] for r in capture["event"] if r["receipt_verified"]}
            indexed = {
                s["tx_hash"]
                for s in samples.values()
                if any(
                    o["cohort"] == key and o["kind"] == "receipt_checked_swap_trigger"
                    for o in s["observations"]
                )
            }
            assert selected == indexed and len(indexed) == result["verified_swap_logs"] == 4
        if key not in WINDOWS:
            continue
        by_tx = {}
        for f in WINDOWS[key]:
            assert str(f) in c["local_inputs_sha256"]
            for r in logs_in(f):
                assert not r.get("removed", False)
                tx = r["transactionHash"]
                pos = (quantity(r["blockNumber"]), r["blockHash"], quantity(r["transactionIndex"]))
                state = by_tx.setdefault(tx, (pos, {}, set()))
                assert pos == state[0]
                index = quantity(r["logIndex"])
                assert index not in state[1]
                state[1][index] = True
                state[2].add(
                    (r["address"], r["topics"][1] if r["topics"][0] in SINGLETON else None)
                )
        assert (
            len(by_tx) == c["matched_transactions"]
            and sum(len(v[1]) for v in by_tx.values()) == c["matched_events"]
        )
        assert Counter(len(v[1]) for v in by_tx.values()) == {
            int(k): v for k, v in c["event_count_histogram"].items()
        }
        expected = {
            tx: (*pos, len(idx), len(pools))
            for tx, (pos, idx, pools) in by_tx.items()
            if len(idx) >= 5
        }
        observed = {
            tx: (
                o["height"],
                o["block_hash"],
                o["index"],
                o["matched_events"],
                o["matched_pool_keys"],
            )
            for tx, r in samples.items()
            for o in r["observations"]
            if o["cohort"] == key and o["kind"] == "matched_landed_logs"
        }
        assert observed == expected
    return {
        "chain": chain,
        "hashes": len(samples),
        "cohorts": len(groups),
        "shapes": inv["shape_counts"],
    }


if __name__ == "__main__":
    if not __debug__:
        raise RuntimeError("inventory verification requires assertions")
    print(json.dumps([verify("Base", 8453), verify("Robinhood", 4663)], sort_keys=True))
