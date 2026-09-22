"""Shared builders for observation-server tests.

Every builder writes a database with the exact schema the read-only layer
expects and closes the connection, so :class:`ReadOnlyDatabases` can open the
file with ``mode=ro``. Records are written through ``canonical_json`` so the
server's canonical-JSON and hash-chain validation passes.
"""

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from oasis_agent.models.common import canonical_json, sha256_hex
from oasis_agent.reporting.lp_readiness import LpGate
from oasis_agent.server.config import (
    ServerChainConfig,
    ServerConfig,
    ServerDatabaseConfig,
    ServerShadowRunnerConfig,
)

ZERO_HASH = "0" * 64
CHAIN_ID = 4663
ACCOUNT = "0x" + "45" * 20
BLOCK_HASH = "0x" + "ab" * 32


def build_shadow_db(path: Path, *, health_count: int = 1, decision_count: int = 1) -> Path:
    db = sqlite3.connect(path)
    db.execute("DROP TABLE IF EXISTS observations")
    db.execute("DROP TABLE IF EXISTS decisions")
    db.execute(
        "CREATE TABLE observations "
        "(seq INTEGER PRIMARY KEY, payload BLOB NOT NULL, digest TEXT NOT NULL)"
    )
    db.execute(
        "CREATE TABLE decisions "
        "(seq INTEGER PRIMARY KEY, payload BLOB NOT NULL, digest TEXT NOT NULL)"
    )
    health_digests: list[str] = []
    previous = ZERO_HASH
    for seq in range(1, health_count + 1):
        record = {
            "schema": "oasis.shadow-health.v1",
            "seq": seq,
            "previous": previous,
            "block": {
                "chain_id": CHAIN_ID,
                "number": 100 + seq,
                "block_hash": BLOCK_HASH,
                "timestamp": 1_000 + seq,
            },
            "observed_at": 2_000 + seq,
            "evidence": None,
            "decision": "HOLD",
            "reasons": [],
            "execution_eligible": False,
            "strategy_qualification": "unresolved",
        }
        payload = canonical_json(record)
        digest = sha256_hex(payload)
        db.execute("INSERT INTO observations VALUES (?, ?, ?)", (seq, payload, digest))
        health_digests.append(digest)
        previous = digest
    previous = ZERO_HASH
    for seq in range(1, decision_count + 1):
        record = {
            "schema": "oasis.shadow-lp-decision.v1",
            "seq": seq,
            "previous": previous,
            "block": {
                "chain_id": CHAIN_ID,
                "number": 100 + seq,
                "block_hash": BLOCK_HASH,
                "timestamp": 1_000 + seq,
            },
            "observed_at": 2_000 + seq,
            "evidence": None,
            "health": "HOLD",
            "decision": "hold",
            "execution_eligible": False,
            "strategy_qualification": "unresolved",
            "health_digest": health_digests[-1],
        }
        payload = canonical_json(record)
        digest = sha256_hex(payload)
        db.execute("INSERT INTO decisions VALUES (?, ?, ?)", (seq, payload, digest))
        previous = digest
    db.commit()
    db.close()
    return path


def build_evidence_db(path: Path, *, rows: int = 1) -> Path:
    db = sqlite3.connect(path)
    db.execute("DROP TABLE IF EXISTS evidence")
    db.execute(
        """
        CREATE TABLE evidence (
            evidence_key TEXT PRIMARY KEY,
            content_hash TEXT NOT NULL,
            kind TEXT NOT NULL,
            payload BLOB NOT NULL,
            chain_id INTEGER,
            block_number INTEGER,
            block_hash TEXT,
            source TEXT NOT NULL,
            retrieved_at TEXT NOT NULL,
            request_hash TEXT NOT NULL,
            raw_response_hash TEXT NOT NULL,
            interpretation_hash TEXT NOT NULL,
            request_payload BLOB,
            raw_response_payload BLOB,
            interpretation_payload BLOB,
            schema_version INTEGER NOT NULL
        )
        """
    )
    for index in range(1, rows + 1):
        payload = canonical_json(
            {"kind": "shadow_header", "chain_id": CHAIN_ID, "block": 100 + index}
        )
        content_hash = sha256_hex(payload)
        db.execute(
            "INSERT INTO evidence (evidence_key, content_hash, kind, payload, chain_id, "
            "block_number, block_hash, source, retrieved_at, request_hash, "
            "raw_response_hash, interpretation_hash, request_payload, "
            "raw_response_payload, interpretation_payload, schema_version) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, 1)",
            (
                f"evidence-{index}",
                content_hash,
                "shadow_header",
                payload,
                CHAIN_ID,
                100 + index,
                BLOCK_HASH,
                "rpc-a",
                "2026-09-07T00:00:00+00:00",
                "1" * 64,
                "2" * 64,
                "3" * 64,
            ),
        )
    db.commit()
    db.close()
    return path


def build_execution_db(path: Path) -> Path:
    db = sqlite3.connect(path)
    db.execute("DROP TABLE IF EXISTS policy")
    db.execute("DROP TABLE IF EXISTS events")
    db.execute("CREATE TABLE policy (id INTEGER PRIMARY KEY CHECK(id=1), payload BLOB NOT NULL)")
    db.execute(
        "CREATE TABLE events (seq INTEGER PRIMARY KEY, payload BLOB NOT NULL, digest TEXT NOT NULL)"
    )
    policy = canonical_json(
        {"schema": "oasis.lp-execution-policy.v1", "chain_id": CHAIN_ID, "account": ACCOUNT}
    )
    db.execute("INSERT INTO policy (id, payload) VALUES (1, ?)", (policy,))
    event = {
        "schema": "oasis.lp-execution-journal.v2",
        "seq": 1,
        "previous": ZERO_HASH,
        "policy_hash": sha256_hex(policy),
        "intent_hash": "0x" + "11" * 32,
        "nonce": 0,
        "state": "reserved",
        "transaction_hash": None,
        "evidence_hash": "0x" + "22" * 32,
        "intent": {"intent_id": "intent-1", "action": "recenter"},
        "recovery": {"recovery_ready": True, "finalized_block": 100},
    }
    payload = canonical_json(event)
    digest = sha256_hex(payload)
    db.execute("INSERT INTO events VALUES (?, ?, ?)", (1, payload, digest))
    db.commit()
    db.close()
    return path


def build_readiness_manifest(
    path: Path,
    *,
    status: str = "unresolved",
    expires_at: str | None = None,
    findings: tuple[str, ...] = ("not yet evaluated",),
) -> Path:
    if expires_at is None:
        expires_at = (datetime.now(UTC) + timedelta(days=30)).isoformat()
    evidence: list[dict[str, object]] = []
    if status == "pass":
        content_hash = "c" * 64
        block = {"chain_id": CHAIN_ID, "number": 101, "block_hash": BLOCK_HASH, "timestamp": 1001}
        evidence = [
            {
                "kind": "gate-evidence",
                "content_hash": content_hash,
                "block": block,
                "source": "provider-a",
                "retrieved_at": "2026-09-07T00:00:00+00:00",
                "request_hash": "1" * 64,
                "raw_response_hash": "2" * 64,
                "interpretation_hash": "3" * 64,
            },
            {
                "kind": "gate-evidence",
                "content_hash": content_hash,
                "block": block,
                "source": "provider-b",
                "retrieved_at": "2026-09-07T00:00:00+00:00",
                "request_hash": "4" * 64,
                "raw_response_hash": "5" * 64,
                "interpretation_hash": "3" * 64,
            },
        ]
    manifest = {
        "gates": [
            {
                "gate": gate.value,
                "status": status,
                "evidence": evidence,
                "findings": list(findings),
                "expires_at": expires_at,
                "independent_reviewer": "reviewer-1" if status == "pass" else "",
            }
            for gate in LpGate
        ]
    }
    path.write_bytes(canonical_json(manifest))
    return path


def make_server_config(
    tmp_path: Path,
    *,
    ops_token_required: bool = False,
    shadow_runner: ServerShadowRunnerConfig | None = None,
    chains: tuple[ServerChainConfig, ...] = (),
) -> ServerConfig:
    shadow = build_shadow_db(tmp_path / "shadow.db")
    evidence = build_evidence_db(tmp_path / "evidence.db")
    execution = build_execution_db(tmp_path / "execution.db")
    readiness = build_readiness_manifest(tmp_path / "readiness.json")
    return ServerConfig(
        host="127.0.0.1",
        port=8666,
        max_rows_per_page=100,
        ops_token_env="OASIS_SERVER_OPS_TOKEN",
        ops_token_required=ops_token_required,
        alert_feed_max_age_seconds=3600,
        alert_gate_warn_hours=24,
        databases=ServerDatabaseConfig(
            shadow=shadow,
            evidence=evidence,
            execution=execution,
            ops=tmp_path / "ops.db",
            readiness_manifest=readiness,
        ),
        shadow_runner=shadow_runner,
        chains=chains,
    )
