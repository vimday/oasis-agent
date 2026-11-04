"""Only explicitly scoped retained free-price grants can spend the original quota."""

import json
import os
import sys
from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest

from oasis_agent.live.okx_budget import OkxPriceReadBudget, OkxPriceReadGrant
from oasis_agent.live.okx_price import NATIVE, OkxPriceSource
from oasis_agent.rpc.budget import RpcBudget
from scripts import run_live_lp as cli
from tests.unit.test_okx_price import CREDS, NOW, USDC, budget, grant, row


@pytest.mark.parametrize(
    "changes",
    [
        {"billing_mode": "unknown"},
        {"billing_mode": "paid"},
        {"billing_mode": None},
        {"free_requests_remaining": 0},
        {"pricing_evidence_hash": "0" * 64},
        {"authorization_evidence_hash": "0" * 64},
        {"endpoint": "https://other.invalid/api/v6/dex/market/price"},
        {"max_requests": True},
        {"max_requests": 4097},
        {"valid_until_ms": int(NOW * 1000) + 86_400_000},
    ],
)
def test_unknown_pricing_or_unbounded_grants_refuse(changes):
    with pytest.raises(ValueError):
        grant(**changes)


def test_original_budget_required_and_changed_grant_cannot_rebind(tmp_path):
    path = tmp_path / "absent.db"
    with pytest.raises(ValueError):
        OkxPriceReadBudget(path, grant())
    assert not path.exists()
    retained = budget(tmp_path)
    raw = retained.path.read_bytes()
    with pytest.raises(ValueError, match="policy changed"):
        OkxPriceReadBudget(retained.path, replace(retained.grant, free_requests_remaining=5))
    assert retained.path.read_bytes() == raw


@pytest.mark.parametrize("fault", ["expired", "wrong_key", "chain", "currency"])
def test_scope_refused_before_any_request_or_spend(tmp_path, fault):
    retained = budget(tmp_path)
    calls, creds, chain, currency, now = [], CREDS, 8453, USDC, NOW
    if fault == "expired":
        now += 60
    elif fault == "wrong_key":
        creds = {**CREDS, "OKX_API_KEY": CREDS["OKX_API_KEY"] + "x"}
    elif fault == "chain":
        chain = 1
    elif fault == "currency":
        currency = "0x" + "11" * 20
    source = OkxPriceSource(
        creds, budget=retained, transport=lambda *args: calls.append(args), clock=lambda: now
    )
    assert source.fetch_usd_price(chain, currency) is None and not calls
    with RpcBudget(retained.path, retained.grant.policies(), require_existing=True) as ledger:
        assert sum(ledger.counts().values()) == 0


def test_failure_spends_once_and_restart_preserves_consumed_quota(tmp_path):
    retained = budget(tmp_path, max_requests=1)
    calls = []

    def failed(*args):
        calls.append("failed")
        raise RuntimeError("HTTP 402 or timeout, private diagnostics")

    first = OkxPriceSource(CREDS, budget=retained, transport=failed, clock=lambda: NOW)
    assert first.fetch_usd_price(8453, USDC) is None
    assert first.fetch_usd_price(8453, NATIVE) is None
    reopened = OkxPriceReadBudget(retained.path, retained.grant)
    second = OkxPriceSource(CREDS, budget=reopened, transport=failed, clock=lambda: NOW + 1)
    assert second.fetch_usd_price(8453, USDC) is None
    assert calls == ["failed"]
    with RpcBudget(retained.path, retained.grant.policies(), require_existing=True) as ledger:
        assert sum(ledger.counts().values()) == 1


def test_expiry_during_transport_never_accepts_response(tmp_path):
    retained = budget(tmp_path, valid_until_ms=int(NOW * 1000) + 1000)
    clock = [NOW]

    def transport(*_):
        clock[0] += 1
        return row(USDC)

    source = OkxPriceSource(CREDS, budget=retained, transport=transport, clock=lambda: clock[0])
    assert source.fetch_usd_price(8453, USDC) is None
    with RpcBudget(retained.path, retained.grant.policies(), require_existing=True) as ledger:
        assert sum(ledger.counts().values()) == 1


def test_admission_wait_cannot_send_after_grant_expires(tmp_path, monkeypatch):
    retained = budget(tmp_path, valid_until_ms=int(NOW * 1000) + 1000)
    calls, clock = [], [NOW]
    reserve = retained.reserve

    def delayed(*args):
        reserve(*args)
        clock[0] += 1

    monkeypatch.setattr(retained, "reserve", delayed)
    source = OkxPriceSource(
        CREDS, budget=retained, transport=lambda *args: calls.append(args), clock=lambda: clock[0]
    )
    assert source.fetch_usd_price(8453, USDC) is None and calls == []
    with RpcBudget(retained.path, retained.grant.policies(), require_existing=True) as ledger:
        assert sum(ledger.counts().values()) == 1


def test_clock_regression_after_admission_refuses(tmp_path):
    clock = iter((NOW, NOW + 5, NOW + 4))
    source = OkxPriceSource(
        CREDS, budget=budget(tmp_path), transport=lambda *_: row(USDC), clock=lambda: next(clock)
    )
    assert source.fetch_usd_price(8453, USDC) is None


def test_grant_file_rejects_extra_duplicate_and_missing_fields(tmp_path):
    path = tmp_path / "grant.json"
    raw = json.dumps(asdict(grant()))
    path.write_text(raw)
    assert OkxPriceReadGrant.load(path) == grant()
    for wrong in (raw[:-1] + ',"schema":"other"}', raw[:-1] + ',"extra":1}', "{}"):
        path.write_text(wrong)
        with pytest.raises(ValueError):
            OkxPriceReadGrant.load(path)


def test_grant_file_refuses_symlink(tmp_path):
    target = tmp_path / "original.json"
    raw = json.dumps(asdict(grant()))
    target.write_text(raw)
    path = tmp_path / "linked.json"
    path.symlink_to(target)
    with pytest.raises(OSError):
        OkxPriceReadGrant.load(path)
    assert target.read_text() == raw


def test_grant_file_refuses_fifo_without_blocking(tmp_path, monkeypatch):
    path = tmp_path / "grant.fifo"
    os.mkfifo(path)
    original_open = os.open

    def require_nonblocking_open(path, flags):
        # Assert before opening so removing O_NONBLOCK cannot hang this test.
        assert flags & os.O_NONBLOCK
        assert flags & os.O_NOFOLLOW
        return original_open(path, flags)

    monkeypatch.setattr(os, "open", require_nonblocking_open)
    with pytest.raises(ValueError, match="regular OKX grant file required"):
        OkxPriceReadGrant.load(path)


def test_grant_file_retains_size_limit(tmp_path):
    path = tmp_path / "large.json"
    path.write_bytes(b" " * 16_385)
    with pytest.raises(ValueError, match="OKX grant too large"):
        OkxPriceReadGrant.load(path)


@pytest.mark.parametrize(
    "flags", [[], ["--okx-price-grant", "missing", "--okx-price-budget", "missing"]]
)
def test_cli_refuses_unconfigured_guard_before_rpc_or_state(tmp_path, monkeypatch, flags):
    state = tmp_path / "state"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_live_lp",
            "--mode",
            "shadow",
            "--state-dir",
            str(state),
            "--external-price-guard-bps",
            "50",
            *flags,
        ],
    )

    def forbidden(*_):
        pytest.fail("unadmitted OKX guard must refuse before RPC resolution")

    monkeypatch.setattr(cli, "rpc_url", forbidden)
    with pytest.raises(SystemExit) as caught:
        cli.main()
    assert caught.value.code == (3 if flags else 2)
    assert not state.exists()


@pytest.mark.parametrize("mode", ["live", "shadow"])
@pytest.mark.parametrize("bps", [0, 201])
def test_cli_refuses_invalid_guard_range_before_any_runtime_access(
    tmp_path, monkeypatch, capsys, mode, bps
):
    state = tmp_path / "state"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_live_lp",
            "--mode",
            mode,
            "--state-dir",
            str(state),
            "--external-price-guard-bps",
            str(bps),
            "--okx-price-grant",
            str(tmp_path / "grant.json"),
            "--okx-price-budget",
            str(tmp_path / "budget.db"),
        ],
    )

    def forbidden(*_, **__):
        pytest.fail("invalid OKX threshold must refuse before runtime access")

    monkeypatch.setattr(cli, "os", SimpleNamespace(environ=SimpleNamespace(get=forbidden)))
    monkeypatch.setattr(cli.LiveConfig, "load", forbidden)
    monkeypatch.setattr(cli.LiveRpcProfile, "load", forbidden)
    monkeypatch.setattr(cli.OkxPriceReadGrant, "load", forbidden)
    for name in (
        "require_live_start",
        "OkxPriceReadBudget",
        "rpc_url",
        "StateStore",
        "TransactionSocketClient",
    ):
        monkeypatch.setattr(cli, name, forbidden)
    with pytest.raises(SystemExit) as caught:
        cli.main()
    assert caught.value.code == 2
    assert "1-200 bps" in capsys.readouterr().err
    assert not state.exists()
