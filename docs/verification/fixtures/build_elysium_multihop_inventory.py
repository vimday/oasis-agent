"""Recover the Elysium anomaly, its controls and retained quote-trigger identities."""

import argparse
import hashlib
import json
from pathlib import Path

HISTORICAL = "docs/verification/ELYSIUM_NITRO_COMMITMENTS_20261114.md"
OBSERVED = "docs/verification/EVENT_QUOTE_RESULTS_20261114.md"


def build(research):
    cases = {}
    cohorts = {}
    fingerprints = {}

    def read(relative):
        raw = (research / relative).read_bytes()
        fingerprints[relative] = hashlib.sha256(raw).hexdigest()
        return json.loads(raw)

    def observe(tx, block, shape, scope, evidence, **fields):
        row = cases.setdefault(
            tx,
            {
                "tx_hash": tx,
                "shape": shape,
                "block_number": block,
                "observations": [],
                "entity_net": None,
            },
        )
        if (row["block_number"], row["shape"]) != (block, shape):
            raise ValueError("conflicting Elysium sample")
        row["observations"].append({"source": evidence, "cohort": scope, **fields})

    hist = read("elysium-commitments-20261114/committed-2.json")
    assert hist["chain_id"] == 99901
    for role in ("trigger", "winner"):
        event = hist["historical_case"][role]
        assert event["receipt_flows_reconciled"] and event["block"]["ref"]["chain_id"] == 99901
        block = event["block"]["ref"]["number"]
        commitment = next(
            c for c in hist["nitro_block_commitments"] if c["block"] == event["block"]["ref"]
        )
        assert commitment["selected_index"] == event["transaction_index"]
        observe(
            event["hash"],
            block,
            "serial" if role == "winner" else "control",
            "anomaly",
            HISTORICAL,
            role=role,
            receipt_decoding=event["decoded"],
            transaction_index=event["transaction_index"],
            block_hash=event["block"]["ref"]["block_hash"],
            receipts_root=commitment["receipts_root"],
        )
        for failed in commitment["failed_transactions"]:
            observe(
                failed["tx_hash"],
                block,
                "control",
                "anomaly",
                HISTORICAL,
                role="failed_same_block_unknown_strategy",
                gas_used=failed["gas_used"],
                transaction_index=failed["index"],
            )
    cohorts["anomaly"] = {
        "rooted_blocks": 2,
        "transactions_in_blocks": sum(c["receipt_count"] for c in hist["nitro_block_commitments"]),
        "failed_same_block_attribution": None,
        "winner_prestate_replayed": False,
    }
    for folder in ("elysium", "elysium-live"):
        relative = "event-quote-20261114/" + folder + "/summary.json"
        data = read(relative)
        assert data["chain_id"] == 99901
        sampled = {e["tx"].lower() for e in data["event"]}
        seen = set()
        for log in data["stream_logs"]:
            if log["removed"]:
                raise ValueError("removed Elysium log")
            tx = log["transaction_hash"]
            seen.add(tx)
            observe(
                tx,
                log["block_number"],
                "control",
                folder,
                OBSERVED,
                role="quote_trigger_only",
                log_index=log["log_index"],
                transaction_index=log["transaction_index"],
                block_hash=log["block_hash"],
                selected_for_quote=tx in sampled,
            )
        assert sampled <= seen
        cohorts[folder] = {
            "stream_logs": len(data["stream_logs"]),
            "distinct_transactions": len(seen),
            "sampled_quotes": len(sampled),
            "reported_failure": data["failure"],
        }
    return {
        "chain_id": 99901,
        "chain": "elysium",
        "cohorts": cohorts,
        "source_sha256": fingerprints,
        "samples": [cases[k] for k in sorted(cases)],
    }


if __name__ == "__main__":
    if not __debug__:
        raise SystemExit("run without Python optimization")
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("research", type=Path)
    print(json.dumps(build(p.parse_args().research), indent=2, sort_keys=True))
