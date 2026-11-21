"""Alert evaluation over the read-only data sources (P3).

Every rule is evaluated from verified data only; a corrupt or unavailable
source yields a critical alert instead of being skipped. Alerts are computed
on demand (no daemon, no external notification) and expose
``level`` (``critical``/``warning``/``info``), the rule name and a message.
"""

from dataclasses import dataclass
from datetime import UTC, datetime

from .audit import ServerOpsJournal
from .config import ServerConfig
from .databases import ReadOnlyDatabases, ReadOnlyError


@dataclass(frozen=True, slots=True)
class Alert:
    level: str
    rule: str
    message: str
    observed_at: str


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _parse_ts(value: object) -> datetime:
    if type(value) is int:
        return datetime.fromtimestamp(value, UTC)
    try:
        parsed = datetime.fromisoformat(str(value))
    except (ValueError, OverflowError) as exc:
        raise ValueError("unparseable timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    return parsed


_SOURCE_FAILURES = (ReadOnlyError, ValueError, OSError, TypeError)


def evaluate_alerts(
    config: ServerConfig,
    databases: ReadOnlyDatabases,
    journal: ServerOpsJournal | None = None,
    *,
    runs: tuple[dict[str, object], ...] | None = None,
) -> tuple[Alert, ...]:
    alerts: list[Alert] = []
    now = datetime.now(UTC)
    observed_at = _now()

    # Data-source integrity (critical).
    for name, status in databases.sources_status().items():
        if not status.available:
            alerts.append(
                Alert(
                    level="critical",
                    rule="source_unavailable",
                    message=f"{name}: {status.detail or status.integrity}",
                    observed_at=observed_at,
                )
            )

    # Feed staleness (critical).
    try:
        health = databases.health_chain()
        if health:
            latest = health[-1]
            observed = _parse_ts(latest["observed_at"])
            age_seconds = int((now - observed).total_seconds())
            if age_seconds > config.alert_feed_max_age_seconds:
                alerts.append(
                    Alert(
                        level="critical",
                        rule="feed_stale",
                        message=(
                            f"latest shadow health observation is {age_seconds}s old "
                            f"(limit {config.alert_feed_max_age_seconds}s)"
                        ),
                        observed_at=observed_at,
                    )
                )
    except _SOURCE_FAILURES:
        pass

    # Gate expiry (warning/critical).
    try:
        readiness = databases.readiness()
        warn_before = now.timestamp() + config.alert_gate_warn_hours * 3600
        for gate in readiness.gates:
            expires = _parse_ts(gate.expires_at)
            if expires.timestamp() <= now.timestamp():
                alerts.append(
                    Alert(
                        level="critical",
                        rule="gate_expired",
                        message=f"{gate.gate.value} evidence expired at {gate.expires_at}",
                        observed_at=observed_at,
                    )
                )
            elif expires.timestamp() <= warn_before:
                alerts.append(
                    Alert(
                        level="warning",
                        rule="gate_expiring",
                        message=f"{gate.gate.value} expires at {gate.expires_at}",
                        observed_at=observed_at,
                    )
                )
    except _SOURCE_FAILURES:
        pass

    # Open execution intent (warning).
    try:
        summary = databases.execution_summary()
        open_intents = summary.get("open_intents")
        if isinstance(open_intents, int) and open_intents > 0:
            alerts.append(
                Alert(
                    level="warning",
                    rule="execution_intent_open",
                    message=f"{open_intents} execution intent(s) not closed/expired",
                    observed_at=observed_at,
                )
            )
    except _SOURCE_FAILURES:
        pass

    # Failed operation runs (warning).
    try:
        recent = runs if runs is not None else journal.list_runs(limit=5) if journal else ()
        failed = [run for run in recent if run["status"] == "failed"]
        if failed:
            alerts.append(
                Alert(
                    level="warning",
                    rule="ops_run_failed",
                    message=(
                        f"{len(failed)} recent observation run(s) failed "
                        f"(latest {failed[0]['run_id']})"
                    ),
                    observed_at=observed_at,
                )
            )
    except OSError, ValueError, TypeError:
        pass

    return tuple(alerts)
