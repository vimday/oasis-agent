"""Verify pinned-fork evidence and isolate simulation time using only retained state."""

import argparse
import json
from collections import Counter
from copy import deepcopy
from pathlib import Path
from statistics import median

from measure_base_fork_latency import (
    DIR,
    HEIGHT,
    POLICIES,
    PRIORITY,
    REFERENCE,
    SAMPLES,
    WETH,
    Archive,
    allow,
    bound_state,
    digest,
    historical_header,
    local_exchange,
    pinned_fork,
    read,
    settlement,
    simulation,
    state_params,
    write,
)

from oasis_agent.models.common import canonical_json, sha256_hex
from oasis_agent.rpc.budget import RpcBudget
from oasis_agent.rpc.protocol import response_envelope
from scripts.measure_robinhood_quote_delivery import _body, _read

PANEL = DIR.parent
PUBLIC = PANEL / "base-free-simulators-hash-20261128"
RUNS = (
    "base-fork-latency-20261128",
    "base-fork-latency-params-20261128",
    "base-fork-latency-node-20261128",
    "base-fork-latency-local-20261128",
    "base-fork-latency-retained-20261128",
    "base-fork-latency-warm-20261128",
    "base-fork-latency-standard-20261128",
)
STANDARD = PANEL / RUNS[-1]


def verify(root):
    scope = read(root / "scope.json")
    recorded = read(root / "report.json")
    assert read(root / "STOP.json") == {"fault": recorded["fault"]}
    assert digest(root / "scope.json") == recorded["scope_sha256"]
    assert digest(root / "capture.py") == scope["source_sha256"]
    assert digest(REFERENCE) == scope["reference_sha256"]
    assert digest(PRIORITY) == scope["previous_live_sha256"]
    runtime = "0x" + (root / "BaseMixedWholeProbe.bin-runtime").read_text().strip()
    assert sha256_hex(bytes.fromhex(runtime[2:])) == scope["runtime_sha256"]
    params = simulation(HEIGHT, runtime, WETH)
    assert sha256_hex(canonical_json(params)) == scope["request_sha256"]
    block = historical_header()
    assert (block.number, block.block_hash, block.parent_hash) == (
        scope["height"],
        scope["hash"],
        scope["prior_hash"],
    )
    with RpcBudget(root / "original.sqlite3", POLICIES, read_only=True) as budget:
        counts = budget.counts()
    assert counts == recorded["archive_requests"]
    assert len(list(root.glob("archive-*.request"))) == counts["archive"]
    methods = Counter()
    for i, row in enumerate(recorded["archive_wires"], 1):
        label = f"archive-{i:03d}"
        assert label == row["label"]
        path = root / label
        request = read(path.with_suffix(".request"))
        method = row["method"]
        assert method == request["method"]
        normalized, expected = allow(scope, method, request["params"])
        assert normalized == request["params"]
        value, _ = _read(path, _body(i, method, normalized), i, 1)
        if expected is not None:
            assert value["hash"] == expected
        assert row["meta"] == read(path.with_suffix(".meta"))
        methods[method] += 1
    assert dict(methods) == recorded["archive_methods"]
    assert len(recorded["archive_wires"]) == counts["archive"]
    state_params_at_h, spec = state_params(
        {"blockHash": block.block_hash, "requireCanonical": True}
    )
    original, _ = _read(DIR / "11-state", _body(11, "eth_call", state_params_at_h), 11, 1)
    state = bound_state(original, spec)
    reference = next(
        r for r in read(REFERENCE)["rows"] if r["height"] == HEIGHT and r["token"] == WETH
    )
    payment_fields = (
        "status",
        "principal_raw",
        "middle_raw",
        "final_received_raw",
        "settlement_delta_raw",
        "payment_log_indices",
    )
    elapsed = []
    for i, row in enumerate(recorded["rows"], 1):
        assert row["label"] == f"local-{i:03d}"
        path = root / row["label"]
        result, _ = _read(path, _body(i, "eth_simulateV1", params), i, 1)
        decoded = settlement(
            result, block, state, WETH, (reference["middle_raw"], reference["final_received_raw"])
        )
        assert row["meta"] == read(path.with_suffix(".meta"))
        assert row["result"] == decoded
        elapsed.append(row["meta"]["received_ns"] - row["meta"]["sent_ns"])
    if recorded["fault"] is None:
        assert len(elapsed) == SAMPLES + 1 and recorded["blocked_after_seal"] == 0
    warm = elapsed[1:]
    return {
        "name": root.name,
        "scope_sha256": recorded["scope_sha256"],
        "fault": recorded["fault"],
        "archive_requests": counts["archive"],
        "archive_methods": recorded["archive_methods"],
        "archive_response_sha256": [
            row["meta"]["response_sha256"] for row in recorded["archive_wires"]
        ],
        "anvil": recorded["anvil"],
        "platform": recorded["platform"],
        "cold_wire_and_execution_ns": elapsed[0] if elapsed else None,
        "cold_result": recorded["rows"][0]["result"] if elapsed else None,
        "warm_samples_ns": warm,
        "warm_median_ns": median(warm) if warm else None,
        "warm_max_ns": max(warm, default=None),
        "byte_shas_local": [row["meta"]["response_sha256"] for row in recorded["rows"]],
        "blocked_after_seal": recorded["blocked_after_seal"],
        "payments_match_official": [
            all(row["result"].get(field) == reference[field] for field in payment_fields)
            for row in recorded["rows"]
        ],
        "gas_matches_official": [
            row["result"]["gas_used"] == reference["gas_used"] for row in recorded["rows"]
        ],
    }


class RetainedArchive(Archive):
    def __init__(self, root, scope, retained):
        self.root = root
        self.plan = scope
        self.error = None
        self.blocked_after_seal = 0
        self.sealed = False
        self.memo = {}
        self.retained = retained

    def fetch_account(self, key, method, params):
        if key not in self.retained:
            raise ValueError(f"retained fork lacks {method} for account {params[0]}")
        return self.retained[key]


def timestamp_inputs():
    # Verify the original scopes before allowing any loopback replay.
    verify(STANDARD)
    runtime = "0x" + (STANDARD / "BaseMixedWholeProbe.bin-runtime").read_text().strip()
    params = simulation(HEIGHT, runtime, WETH)
    public = [
        _read(PUBLIC / name / "call-2", _body(2, "eth_simulateV1", params), 2, 1)[0][0]
        for name in ("official", "drpc")
    ]
    assert public[0]["timestamp"] == public[1]["timestamp"]
    local = _read(STANDARD / "local-001", _body(1, "eth_simulateV1", params), 1, 1)[0][0]
    timed = deepcopy(params)
    timed[0]["blockStateCalls"][0]["blockOverrides"] = {"time": public[0]["timestamp"]}
    return (params, timed), local, public[0]


def timestamp_control(root, capture):
    variants, historical, official = timestamp_inputs()
    scope = read(STANDARD / "scope.json")
    pin = historical_header()
    fee_args, spec = state_params({"blockHash": pin.block_hash, "requireCanonical": True})
    fees = bound_state(_read(DIR / "11-state", _body(11, "eth_call", fee_args), 11, 1)[0], spec)
    reference = settlement([official], pin, fees, WETH)
    retained = {}
    for i, row in enumerate(read(STANDARD / "report.json")["archive_wires"], 1):
        prefix = STANDARD / row["label"]
        request = read(prefix.with_suffix(".request"))
        method, params = request["method"], request["params"]
        value, _ = _read(prefix, _body(i, method, params), i, 1)
        retained[canonical_json([method, params])] = value
    if capture:
        root.mkdir()
        archive = RetainedArchive(STANDARD, scope, retained)
        with pinned_fork(root, archive) as (client, endpoint, local_pin, _):
            assert local_pin == pin
            for i, params in enumerate(variants, 1):
                local_exchange(
                    client, endpoint, root / f"time-{i}", _body(i, "eth_simulateV1", params)
                )
                if archive.error:
                    write(root / "STOP.json", {"fault": archive.error})
                    raise ValueError(archive.error)
        write(root / "STOP.json", {"fault": None})
    assert read(root / "STOP.json") == {"fault": None}
    rows = []
    for i, params in enumerate(variants, 1):
        prefix = root / f"time-{i}"
        proof = read(prefix.with_suffix(".meta"))
        try:
            raw, _ = _read(prefix, _body(i, "eth_simulateV1", params), i, 1)
        except ValueError as exc:
            if i != 2 or str(exc) != "RPC refusal -38021":
                raise
            envelope = response_envelope(prefix.with_suffix(".response").read_bytes(), i)
            rows.append({"rpc_error": envelope["error"], "proof": proof})
        else:
            result = settlement(
                raw, pin, fees, WETH, (reference["middle_raw"], reference["final_received_raw"])
            )
            rows.append({"timestamp": raw[0]["timestamp"], "result": result, "proof": proof})
    assert rows[0]["timestamp"] == historical["timestamp"]
    assert rows[0]["result"]["gas_used"] == int(historical["calls"][0]["gasUsed"], 16)
    if "rpc_error" not in rows[1]:
        assert rows[1]["timestamp"] == official["timestamp"]
    return {
        "chain_id": scope["chain"],
        "fork_hash": pin.block_hash,
        "fork_height": pin.number,
        "upstream_requests": 0,
        "original_scope_sha256": digest(STANDARD / "scope.json"),
        "reference_public_response_sha256": digest(PUBLIC / "official/call-2.response"),
        "public_gas_used": reference["gas_used"],
        "public_timestamp": official["timestamp"],
        "fixed_envelope": rows,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timestamp-control", type=Path)
    parser.add_argument("--capture", action="store_true")
    args = parser.parse_args()
    print(
        json.dumps(
            timestamp_control(args.timestamp_control, args.capture)
            if args.timestamp_control
            else {
                "runs": [verify(PANEL / folder) for folder in RUNS],
                "usd": 0,
                "live_timely_rate": None,
            },
            indent=2,
            sort_keys=True,
        )
    )
