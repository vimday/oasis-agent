"""Read-only journal access: hash-chain validation and fail-closed behavior."""

import hashlib
import sqlite3
from dataclasses import replace

import pytest

from oasis_agent.models.common import canonical_json
from oasis_agent.server.databases import ReadOnlyDatabases, ReadOnlyError

from .server_helpers import (
    BLOCK_HASH,
    CHAIN_ID,
    build_readiness_manifest,
    make_server_config,
)


def databases(tmp_path, **overrides):
    config = make_server_config(tmp_path)
    return ReadOnlyDatabases(config)


def test_health_chain_validates_and_returns_digests(tmp_path):
    db = databases(tmp_path)
    try:
        chain = db.health_chain()
        assert len(chain) == 1
        assert chain[0]["schema"] == "oasis.shadow-health.v1"
        assert chain[0]["execution_eligible"] is False
        assert chain[0]["digest"]
    finally:
        db.close()


def test_decision_chain_validates(tmp_path):
    db = databases(tmp_path)
    try:
        chain = db.decision_chain()
        assert len(chain) == 1
        assert chain[0]["decision"] == "hold"
        assert chain[0]["health_digest"]
    finally:
        db.close()


def test_tampered_digest_fails_closed(tmp_path):
    config = make_server_config(tmp_path)
    path = config.databases.shadow
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE observations SET digest = ? WHERE seq = 1", ("0" * 64,))
    db = ReadOnlyDatabases(config)
    try:
        with pytest.raises(ReadOnlyError, match="digest mismatch"):
            db.health_chain()
    finally:
        db.close()


def test_tampered_payload_fails_closed(tmp_path):
    config = make_server_config(tmp_path)
    path = config.databases.shadow
    with sqlite3.connect(path) as connection:
        row = connection.execute("SELECT payload FROM observations WHERE seq=1").fetchone()
        payload = bytearray(row[0])
        payload[0] ^= 0xFF
        connection.execute("UPDATE observations SET payload = ? WHERE seq=1", (bytes(payload),))
    db = ReadOnlyDatabases(config)
    try:
        with pytest.raises(ReadOnlyError):
            db.health_chain()
    finally:
        db.close()


def test_evidence_hash_mismatch_fails_closed(tmp_path):
    config = make_server_config(tmp_path)
    path = config.databases.evidence
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE evidence SET content_hash = ? WHERE evidence_key='evidence-1'", ("0" * 64,)
        )
    db = ReadOnlyDatabases(config)
    try:
        with pytest.raises(ReadOnlyError, match="hash mismatch"):
            db.evidence_page()
    finally:
        db.close()


def test_evidence_page_filters_and_paginates_in_sql(tmp_path):
    config = make_server_config(tmp_path)
    path = config.databases.evidence
    with sqlite3.connect(path) as connection:
        for index in (2, 3):
            payload = canonical_json(
                {"kind": "swap-observation", "chain_id": CHAIN_ID, "block": 200 + index}
            )
            content_hash = hashlib.sha256(payload).hexdigest()
            connection.execute(
                "INSERT INTO evidence (evidence_key, content_hash, kind, payload, chain_id, "
                "block_number, block_hash, source, retrieved_at, request_hash, "
                "raw_response_hash, interpretation_hash, request_payload, "
                "raw_response_payload, interpretation_payload, schema_version) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, 1)",
                (
                    f"evidence-{index}",
                    content_hash,
                    "swap-observation",
                    payload,
                    CHAIN_ID,
                    200 + index,
                    BLOCK_HASH,
                    "rpc-a",
                    f"2026-09-08T00:00:0{index}+00:00",
                    "1" * 64,
                    "2" * 64,
                    "3" * 64,
                ),
            )
    db = ReadOnlyDatabases(config)
    try:
        filtered = db.evidence_page(kind="swap-observation", limit=10, offset=0)
        assert filtered.total == 2
        assert len(filtered.items) == 2
        assert all(item["kind"] == "swap-observation" for item in filtered.items)
        paged = db.evidence_page(limit=1, offset=0)
        assert paged.total == 3
        assert len(paged.items) == 1
        by_key = db.evidence_by_key("evidence-1")
        assert by_key is not None and by_key["content_hash"]
        assert db.evidence_by_key("missing") is None
    finally:
        db.close()


def test_execution_journal_validates_chain_and_policy(tmp_path):
    db = databases(tmp_path)
    try:
        events, policy_hash = db.execution_events()
        assert len(events) == 1
        assert events[0]["policy_hash"] == policy_hash
        assert events[0]["nonce"] == 0
        summary = db.execution_summary()
        assert summary["policy_hash"] == policy_hash
        assert summary["total_events"] == 1
        assert summary["open_intents"] == 1
    finally:
        db.close()


def test_execution_sequence_gap_fails_closed(tmp_path):
    config = make_server_config(tmp_path)
    path = config.databases.execution
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO events VALUES (?, ?, ?)",
            (
                3,
                canonical_json(
                    {
                        "schema": "oasis.lp-execution-journal.v2",
                        "seq": 3,
                        "previous": "0" * 64,
                        "policy_hash": "0" * 64,
                        "intent_hash": "0x" + "33" * 32,
                        "nonce": 1,
                        "state": "reserved",
                        "transaction_hash": None,
                        "evidence_hash": "0x" + "44" * 32,
                        "intent": {},
                        "recovery": {},
                    }
                ),
                "0" * 64,
            ),
        )
    db = ReadOnlyDatabases(config)
    try:
        with pytest.raises(ReadOnlyError, match="sequence gap"):
            db.execution_events()
    finally:
        db.close()


def test_unconfigured_source_is_reported_and_read_fails(tmp_path):
    config = replace(
        make_server_config(tmp_path),
        databases=replace(make_server_config(tmp_path).databases, shadow=None),
    )
    db = ReadOnlyDatabases(config)
    try:
        status = db.sources_status()
        assert status["shadow"].available is False
        assert status["shadow"].integrity == "unconfigured"
        with pytest.raises(ReadOnlyError, match="not configured"):
            db.health_chain()
    finally:
        db.close()


def test_readiness_manifest_parsed_strictly(tmp_path):
    config = make_server_config(tmp_path)
    build_readiness_manifest(config.databases.readiness_manifest)
    db = ReadOnlyDatabases(config)
    try:
        assessment = db.readiness()
        assert assessment.execution_eligible is False
        assert len(assessment.gates) == 9
    finally:
        db.close()


def test_readiness_reparses_when_manifest_replaced(tmp_path):
    config = make_server_config(tmp_path)
    manifest = config.databases.readiness_manifest
    build_readiness_manifest(manifest, status="pass", findings=())
    db = ReadOnlyDatabases(config)
    try:
        assert db.readiness().gates[0].status.value == "pass"
        # A replaced manifest with identical path must invalidate the cache.
        build_readiness_manifest(manifest, status="unresolved")
        assert db.readiness().gates[0].status.value == "unresolved"
    finally:
        db.close()


def test_sources_status_verified_when_healthy(tmp_path):
    db = databases(tmp_path)
    try:
        status = db.sources_status()
        assert status["shadow"].available is True
        assert status["shadow"].integrity == "verified"
        assert status["evidence"].available is True
        assert status["execution"].available is True
        assert status["readiness"].available is True
    finally:
        db.close()
