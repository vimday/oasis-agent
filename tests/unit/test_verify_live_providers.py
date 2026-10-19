"""Acceptance budgets persist across output directories and record safe failures."""

import json
import ssl
from pathlib import Path
from urllib.error import HTTPError, URLError

import pytest

from oasis_agent.rpc.budget import BudgetPolicy, RpcBudget
from scripts import verify_live_providers as cli
from tests.unit.test_live_provider_acceptance import Fixture

URLS = {
    "drpc": "https://base.drpc.org/",
    "alchemy": "https://base-mainnet.g.alchemy.com/v2/fixture",
}


def test_cli_budget_is_repository_bound_even_when_working_directory_changes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert cli.BUDGET_PATH.is_absolute()
    assert (
        Path(cli.__file__).resolve().parents[1]
        / "reports/evidence/live-provider-acceptance-budget.sqlite"
    ) == cli.BUDGET_PATH


def prepare(monkeypatch, failure=False):
    monkeypatch.setattr(
        cli, "POLICIES", (BudgetPolicy("drpc", 12, 0), BudgetPolicy("alchemy", 12, 0))
    )
    calls = []
    fixtures = {url: Fixture() for url in URLS.values()}

    class Poster:
        def __init__(self, url, timeout):
            self.url = url

        def post(self, payload):
            calls.append(self.url)
            if failure and len(calls) == 1:
                raise TimeoutError("SECRET_ENDPOINT_MUST_NOT_LEAK")
            return fixtures[self.url].post(payload)

    monkeypatch.setattr(cli, "_UrlLibPoster", Poster)
    monkeypatch.setattr("oasis_agent.rpc.transport.time.sleep", lambda _: None)
    return calls


def test_transport_retry_charged_and_failure_redacted(tmp_path, monkeypatch):
    calls = prepare(monkeypatch, True)
    fixture = Fixture()
    report = cli.verify(
        tmp_path / "run",
        fixture.block,
        fixture.transaction_hash,
        URLS,
        budget_path=tmp_path / "budget.db",
    )
    assert report["passed"] and report["request_counts"] == {"drpc": 10, "alchemy": 9}
    assert len(calls) == 19 and not report["execution_eligible"]
    assert report["transport_failures"]["drpc"][0]["category"] == "timeout"
    assert "SECRET_ENDPOINT" not in json.dumps(report)
    assert "/v2/fixture" not in json.dumps(report)
    before = len(calls)
    with pytest.raises(ValueError, match="insufficient"):
        cli.verify(
            tmp_path / "another-output",
            fixture.block,
            fixture.transaction_hash,
            URLS,
            budget_path=tmp_path / "budget.db",
        )
    assert len(calls) == before and not (tmp_path / "another-output").exists()


def test_existing_output_never_repeats_requests(tmp_path, monkeypatch):
    calls = prepare(monkeypatch)
    fixture = Fixture()
    with pytest.raises(ValueError, match="terminal"):
        cli.verify(
            tmp_path,
            fixture.block,
            fixture.transaction_hash,
            URLS,
            budget_path=tmp_path / "budget.db",
        )
    assert calls == [] and not (tmp_path / "budget.db").exists()


def test_trace_timeout_is_never_retried_and_paid_peer_is_not_called(tmp_path, monkeypatch):
    prepare(monkeypatch)
    fixture = Fixture()
    calls = []

    class TraceTimeout:
        def post(self, payload):
            method = json.loads(payload)["method"]
            calls.append(method)
            if method == "debug_traceTransaction":
                raise HTTPError("https://secret.invalid", 408, "private message", {}, None)
            return fixture.post(payload)

    monkeypatch.setattr(cli, "_UrlLibPoster", lambda *_: TraceTimeout())
    report = cli.verify(
        tmp_path / "run",
        fixture.block,
        fixture.transaction_hash,
        URLS,
        budget_path=tmp_path / "budget.db",
    )
    assert not report["passed"]
    assert report["request_counts"] == {"drpc": 8, "alchemy": 0}
    assert len(calls) == 8 and calls.count("debug_traceTransaction") == 1
    assert report["transport_failures"]["drpc"][0]["category"] == "http_408"
    assert "secret.invalid" not in json.dumps(report)
    assert "private message" not in json.dumps(report)


def test_failed_trace_does_not_contact_paid_peer(tmp_path, monkeypatch):
    prepare(monkeypatch)
    fixture = Fixture(fault="single")
    monkeypatch.setattr(cli, "_UrlLibPoster", lambda *_: fixture)
    report = cli.verify(
        tmp_path / "run",
        fixture.block,
        fixture.transaction_hash,
        URLS,
        budget_path=tmp_path / "budget.db",
    )
    assert not report["passed"] and report["request_counts"] == {"drpc": 9, "alchemy": 0}
    with RpcBudget(tmp_path / "budget.db", cli.POLICIES) as budget:
        assert budget.counts() == report["request_counts"]


@pytest.mark.parametrize(
    "error,category",
    [
        (HTTPError("secret", 429, "secret", {}, None), "http_429"),
        (URLError(ssl.SSLError("secret")), "tls_failure"),
        (URLError(TimeoutError("secret")), "timeout"),
        (OSError("secret"), "transport_failure"),
    ],
)
def test_transport_reason_never_contains_provider_text(error, category):
    assert cli.failure_category(error) == category
