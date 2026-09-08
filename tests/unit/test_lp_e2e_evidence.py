"""Independent decoding of actual locally signed JSON-RPC lifecycle receipts."""

import hashlib
import json
from pathlib import Path

import pytest

from scripts.verify_lp_e2e import verify_run

JOURNAL = Path(__file__).parents[2] / "reports/evidence/lp-e2e-local-2026-09-07/events.jsonl"


def test_signed_local_lifecycle_receipts_reconcile():
    result = verify_run(JOURNAL)
    assert result["transactions"] == 35
    assert result["swaps"] == 4
    assert result["conservation_checks"] == 8
    assert result["execution_eligible"] is False


def test_receipt_semantics_reject_a_rehashed_failed_transaction(tmp_path):
    events = [json.loads(line)["event"] for line in JOURNAL.read_bytes().splitlines()]
    first_receipt = next(event for event in events if event["kind"] == "receipt")
    first_receipt["raw_receipt"]["status"] = 0
    previous = "0" * 64
    output = tmp_path / "mutated.jsonl"
    with output.open("w") as handle:
        for event in events:
            event["previous"] = previous
            raw = json.dumps(event, sort_keys=True, separators=(",", ":")).encode()
            previous = hashlib.sha256(raw).hexdigest()
            handle.write(json.dumps({"event": event, "digest": previous}) + "\n")
    with pytest.raises(ValueError, match="receipt identity/status"):
        verify_run(output)


def test_truncated_lifecycle_is_not_success(tmp_path):
    output = tmp_path / "truncated.jsonl"
    output.write_bytes(b"\n".join(JOURNAL.read_bytes().splitlines()[:-1]) + b"\n")
    with pytest.raises(ValueError, match="did not complete"):
        verify_run(output)
