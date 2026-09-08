import json
import subprocess
import sys
import time
from dataclasses import asdict, replace
from decimal import Decimal
from pathlib import Path

import pytest

from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.models.common import canonical_json
from oasis_agent.models.v4 import SwapDirection
from oasis_agent.shadow.journal import ShadowJournal
from oasis_agent.shadow.service import LpCycle, LpShadowService, parse_policy, serializable
from tests.unit.test_shadow_lp_runner import (
    band_strategy,
    blocks,
    passing_gate,
    pool_state,
    recenter_candidates,
)


def setup(tmp_path):
    store = EvidenceStore(tmp_path / "evidence.db")
    b, _ = blocks()
    ref = store.put(
        "header",
        canonical_json(asdict(b)),
        b.ref,
        "fixture",
        request={},
        raw_response=asdict(b),
        interpretation={"fixture": True},
    )
    cycle = LpCycle(
        b,
        ref,
        ref,
        pool_state(),
        SwapDirection.ZERO_FOR_ONE,
        100,
        True,
        False,
        recenter_candidates(),
        passing_gate(),
    )
    inputs = store.put(
        "lp_inputs",
        cycle.input_payload,
        b.ref,
        "fixture",
        request={},
        raw_response={"fixture": True},
        interpretation={"fixture": True},
    )
    cycle = replace(cycle, input_evidence=inputs)
    journal = ShadowJournal(tmp_path / "shadow.db", chain_id=1, max_age_seconds=10)
    service = LpShadowService(
        store, journal, band_strategy(), min_benefit=Decimal(0), canary_limits=None
    )
    return store, journal, service, cycle


def test_evidence_to_decision_round_trip(tmp_path):
    store, journal, service, cycle = setup(tmp_path)
    parsed = LpCycle.from_json(canonical_json(serializable(cycle)))
    assert parsed == cycle
    assert service.consume(parsed, now=100).decision == "recenter"
    assert journal.decision_records()[-1]["execution_eligible"] is False
    assert (
        journal.decision_records()[-1]["evidence"]["content_hash"]
        == cycle.input_evidence.content_hash
    )
    journal.close()
    store.close()


def test_modified_features_cannot_reuse_evidence(tmp_path):
    store, journal, service, cycle = setup(tmp_path)
    changed = replace(cycle, amount_specified=101)
    with pytest.raises(ValueError, match=r"binding|validation"):
        service.consume(changed, now=100)
    assert journal._last()["decision"] == "HALT"
    assert service.consume(cycle, now=100).decision == "halt"
    journal.close()
    store.close()


def test_config_change_on_restart_is_rejected(tmp_path):
    store, journal, _, _ = setup(tmp_path)
    with pytest.raises(ValueError, match="code/config"):
        LpShadowService(store, journal, band_strategy(), min_benefit=Decimal(1), canary_limits=None)
    journal.close()
    store.close()


@pytest.mark.parametrize("change", ["float", "unknown", "malformed_candidate"])
def test_malformed_cycle_fails_closed(tmp_path, change):
    store, journal, _, cycle = setup(tmp_path)
    data = serializable(cycle)
    if change == "float":
        data["state"]["volatility"] = 0.1
    elif change == "unknown":
        data["header"]["allow_trade"] = True
    else:
        data["candidates"]["recenter"] = []
    with pytest.raises((TypeError, ValueError)):
        LpCycle.from_json(json.dumps(data).encode())
    journal.close()
    store.close()


def test_duplicate_cycle_and_policy_keys_fail_closed():
    with pytest.raises(ValueError, match="duplicate JSON key"):
        LpCycle.from_json(b'{"header":{},"header":{}}')
    with pytest.raises(ValueError, match="duplicate JSON key"):
        parse_policy(b'{"chain_id":1,"chain_id":2}')


def test_cli_runs_evidence_consumer_and_blocks_changed_policy(tmp_path):
    store, journal, _, cycle = setup(tmp_path)
    timestamp = int(time.time())
    block = replace(cycle.header.ref, timestamp=timestamp)
    header = replace(cycle.header, ref=block)
    ref = store.put(
        "header",
        canonical_json(asdict(header)),
        block,
        "fixture",
        request={},
        raw_response=asdict(header),
        interpretation={"fixture": True},
    )
    cycle = replace(
        cycle,
        header=header,
        header_evidence=ref,
        state=replace(
            cycle.state, block=block, observed_at_timestamp=timestamp, oracle_timestamp=timestamp
        ),
    )
    inputs = store.put(
        "lp_inputs",
        cycle.input_payload,
        block,
        "fixture",
        request={},
        raw_response={"fixture": True},
        interpretation={"fixture": True},
    )
    cycle = replace(cycle, input_evidence=inputs)
    store.close()
    journal.close()
    policy = {
        "chain_id": 1,
        "max_age_seconds": 10,
        "strategy": serializable(band_strategy()),
        "min_benefit": "0",
        "canary_limits": None,
    }
    policy_path = tmp_path / "policy.json"
    input_path = tmp_path / "cycles.jsonl"
    policy_path.write_bytes(canonical_json(policy))
    input_path.write_bytes(canonical_json(serializable(cycle)) + b"\n")
    command = [
        sys.executable,
        "-m",
        "scripts.run_lp_shadow",
        "--policy",
        str(policy_path),
        "--input",
        str(input_path),
        "--evidence",
        str(tmp_path / "evidence.db"),
        "--journal",
        str(tmp_path / "shadow.db"),
        "--cycles",
        "1",
    ]
    root = Path(__file__).parents[2]
    result = subprocess.run(
        command, cwd=root, capture_output=True, text=True, timeout=20, check=False
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "recenter"
    policy["min_benefit"] = "1"
    policy_path.write_bytes(canonical_json(policy))
    result = subprocess.run(
        command, cwd=root, capture_output=True, text=True, timeout=20, check=False
    )
    assert result.returncode != 0
    assert "details redacted" in result.stderr
