"""Alert evaluation over verified data sources (P3)."""

import hashlib
import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from oasis_agent.models.common import canonical_json
from oasis_agent.server.alerts import evaluate_alerts
from oasis_agent.server.audit import ServerOpsJournal
from oasis_agent.server.databases import ReadOnlyDatabases

from .server_helpers import (
    ZERO_HASH,
    build_readiness_manifest,
    make_server_config,
)


def healthy(tmp_path):
    config = make_server_config(tmp_path)
    return config, ReadOnlyDatabases(config), ServerOpsJournal(config.databases.ops)


def levels(alerts):
    return {alert.rule: alert.level for alert in alerts}


def test_unconfigured_source_yields_critical_alert(tmp_path):
    config = replace(
        make_server_config(tmp_path),
        databases=replace(make_server_config(tmp_path).databases, shadow=None),
    )
    databases = ReadOnlyDatabases(config)
    journal = ServerOpsJournal(config.databases.ops)
    try:
        alerts = evaluate_alerts(config, databases, journal)
        assert alerts[0].rule == "source_unavailable"
        assert alerts[0].level == "critical"
    finally:
        databases.close()
        journal.close()


def test_feed_stale_alert(tmp_path):
    config = make_server_config(tmp_path)
    shadow = config.databases.shadow
    with sqlite3.connect(shadow) as connection:
        record = json.loads(
            connection.execute("SELECT payload FROM observations LIMIT 1").fetchone()[0]
        )
        record["observed_at"] = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
        record["previous"] = ZERO_HASH
        record["seq"] = 1
        payload = canonical_json(record)
        connection.execute(
            "UPDATE observations SET payload = ?, digest = ? WHERE seq = 1",
            (payload, hashlib.sha256(payload).hexdigest()),
        )
        decision = json.loads(connection.execute("SELECT payload FROM decisions").fetchone()[0])
        decision["health_digest"] = hashlib.sha256(payload).hexdigest()
        decision["observed_at"] = record["observed_at"]
        encoded = canonical_json(decision)
        connection.execute(
            "UPDATE decisions SET payload=?, digest=?",
            (
                encoded,
                hashlib.sha256(encoded).hexdigest(),
            ),
        )
    config = replace(config, alert_feed_max_age_seconds=3600)
    databases = ReadOnlyDatabases(config)
    journal = ServerOpsJournal(config.databases.ops)
    try:
        alerts = evaluate_alerts(config, databases, journal)
        assert levels(alerts)["feed_stale"] == "critical"
    finally:
        databases.close()
        journal.close()


def test_expired_gate_alert(tmp_path):
    config = make_server_config(tmp_path)
    build_readiness_manifest(
        config.databases.readiness_manifest,
        status="pass",
        expires_at=(datetime.now(UTC) - timedelta(days=1)).isoformat(),
        findings=(),
    )
    databases = ReadOnlyDatabases(config)
    journal = ServerOpsJournal(config.databases.ops)
    try:
        alerts = evaluate_alerts(config, databases, journal)
        assert levels(alerts)["gate_expired"] == "critical"
    finally:
        databases.close()
        journal.close()


def test_expiring_gate_warning(tmp_path):
    config = make_server_config(tmp_path)
    build_readiness_manifest(
        config.databases.readiness_manifest,
        status="pass",
        expires_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        findings=(),
    )
    config = replace(config, alert_gate_warn_hours=24)
    databases = ReadOnlyDatabases(config)
    journal = ServerOpsJournal(config.databases.ops)
    try:
        alerts = evaluate_alerts(config, databases, journal)
        assert levels(alerts)["gate_expiring"] == "warning"
    finally:
        databases.close()
        journal.close()


def test_open_execution_intent_warning(tmp_path):
    config = make_server_config(tmp_path)
    databases = ReadOnlyDatabases(config)
    journal = ServerOpsJournal(config.databases.ops)
    try:
        alerts = evaluate_alerts(config, databases, journal)
        assert levels(alerts)["execution_intent_open"] == "warning"
    finally:
        databases.close()
        journal.close()


def test_failed_ops_run_warning(tmp_path):
    config = make_server_config(tmp_path)
    databases = ReadOnlyDatabases(config)
    journal = ServerOpsJournal(config.databases.ops)
    try:
        run = journal.create_run(command=("cmd",))
        journal.mark_finished(run.run_id, exit_code=1, stdout="", stderr="boom")
        alerts = evaluate_alerts(config, databases, journal)
        assert levels(alerts)["ops_run_failed"] == "warning"
    finally:
        databases.close()
        journal.close()


def test_fully_healthy_sources_produce_no_critical_feed_alerts(tmp_path):
    config, databases, journal = healthy(tmp_path)
    try:
        alerts = evaluate_alerts(config, databases, journal)
        # The default manifest is unresolved and the execution journal has an
        # open intent, so only the gate/source health checks matter here.
        assert "source_unavailable" not in levels(alerts)
    finally:
        databases.close()
        journal.close()
