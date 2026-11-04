"""Static release approval is insufficient when runtime pins are incomplete."""

import json
import os
import subprocess
import sys
from dataclasses import asdict, replace

import pytest

from oasis_agent.evm.calldata import calldata, selector
from oasis_agent.live import chain
from oasis_agent.live.gas_oracle import LEGACY_PIN_NAMES
from oasis_agent.models.common import canonical_json
from oasis_agent.reporting import live_release as release
from oasis_agent.reporting import live_startup as report
from oasis_agent.reporting import live_status as status_report
from oasis_agent.reporting.live_status import read_live_status
from scripts import start_live_deployment as cli
from tests.unit.gas_oracle_fixtures import synthetic_gas_pins
from tests.unit.test_live_lp import CONFIG
from tests.unit.test_server_api import client_for

FULL_CONFIG = replace(CONFIG, expected_pins=CONFIG.expected_pins | synthetic_gas_pins())


def test_default_readiness_refuses_legacy_runtime_without_external_access(
    tmp_path, monkeypatch, capsys
):
    config = replace(
        CONFIG,
        expected_pins={k: v for k, v in CONFIG.expected_pins.items() if k in LEGACY_PIN_NAMES},
    )
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs/live.base-eth-usdc.json").write_bytes(canonical_json(asdict(config)))
    monkeypatch.setattr(cli.compose, "ROOT", tmp_path)
    monkeypatch.setattr(release, "RELEASE_OPEN", True)
    monkeypatch.setattr(release, "ACCEPTANCE_PERMIT", None)
    monkeypatch.setattr("sys.argv", ["start"])

    def forbidden(*_, **__):
        pytest.fail("offline readiness must not access Docker, state, keys or RPC")

    monkeypatch.setattr(cli, "command", forbidden)
    monkeypatch.setattr(cli, "local_environment", forbidden)
    with pytest.raises(SystemExit) as outcome:
        cli.main()
    assert outcome.value.code == 3
    report = json.loads(capsys.readouterr().out)
    assert report["new_actions_allowed"] is True
    assert report["startup"]["local_guards_passed"] is False
    assert report["startup"]["startup_prerequisites_passed"] is False


@pytest.mark.parametrize("full", [False, True])
def test_configuration_shape_is_declaration_not_chain_acceptance(tmp_path, full):
    config = (
        FULL_CONFIG
        if full
        else replace(
            CONFIG,
            expected_pins={k: v for k, v in CONFIG.expected_pins.items() if k in LEGACY_PIN_NAMES},
        )
    )
    path = tmp_path / "policy.json"
    path.write_bytes(canonical_json(asdict(config)))
    before = path.read_bytes()
    result = report.read_local_startup_readiness(path)
    assert result.local_guards_passed is full and result.release_gate_passed
    assert result.configured_runtime_roles == (8 if full else 5)
    assert result.missing_runtime_roles == ([] if full else list(report.GAS_ROLES))
    assert result.unverified_checks == list(report.UNVERIFIED)
    assert not result.startup_prerequisites_passed and not result.evidence_replayed
    assert not result.execution_eligible and result.rpc_requests == 0
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "fault",
    [
        "json",
        "duplicate",
        "partial_pins",
        "zero_runtime",
        "limit",
        "old_feed",
        "secret",
        "oversized",
        "fifo",
        "symlink",
    ],
)
def test_hostile_local_policy_is_redacted_and_never_ready(tmp_path, fault):
    path = tmp_path / "policy.json"
    body = asdict(FULL_CONFIG)
    if fault == "partial_pins":
        body["expected_pins"].pop("gas_oracle_proxy")
    if fault == "zero_runtime":
        body["expected_pins"]["gas_oracle_proxy"] = "0x" + "00" * 32
    if fault == "limit":
        body["max_capital_usd"] = 501
    if fault == "old_feed":
        body["usdc_oracle_max_age_seconds"] = 90000
    if fault == "secret":
        body["private_key"] = "synthetic-private-content"
    raw = canonical_json(body)
    if fault == "json":
        raw = b'{"private":synthetic-private-content}'
    if fault == "duplicate":
        raw = b'{"chain_id":8453,"chain_id":1}'
    if fault == "oversized":
        raw = b"synthetic-private-content" * (report.MAX_CONFIG_BYTES // 24 + 2)
    if fault == "fifo":
        os.mkfifo(path)
    elif fault == "symlink":
        path.symlink_to(tmp_path / "private-target")
    else:
        path.write_bytes(raw)
    result = report.read_local_startup_readiness(path)
    assert result.configuration_status == "invalid" and not result.local_guards_passed
    assert result.blockers == ["live_configuration_invalid"]
    assert result.configuration_hash is None and result.configured_runtime_roles is None
    assert "synthetic-private-content" not in result.model_dump_json()
    assert str(tmp_path) not in result.model_dump_json()


def test_missing_unconfigured_and_closed_release_never_create_or_read_inputs(tmp_path, monkeypatch):
    missing = tmp_path / "missing.json"
    assert report.read_local_startup_readiness(missing).configuration_status == "missing"
    assert not missing.exists()
    assert report.read_local_startup_readiness(None).configuration_status == "unconfigured"

    def forbidden(*_):
        pytest.fail("closed release must not read a policy")

    monkeypatch.setattr(report, "_read", forbidden)
    closed = release.LiveRelease(
        new_actions_allowed=False, acceptance_permit=None, blockers=[release.CLOSED_BLOCKER]
    )
    result = report.read_local_startup_readiness(missing, release=closed)
    assert result.configuration_status == "not_checked" and not result.local_guards_passed
    assert result.blockers == ["release_gate_closed"]


def test_existing_acceptance_permit_semantics_are_not_widened(tmp_path):
    path = tmp_path / "policy.json"
    path.write_bytes(canonical_json(asdict(FULL_CONFIG)))
    synthetic = release.LiveRelease(
        new_actions_allowed=False,
        acceptance_permit=release.AcceptancePermit(
            kind="approve_usdc", account_nonce=3, authorized_by="synthetic test only"
        ),
        blockers=[release.CLOSED_BLOCKER],
    )
    result = report.read_local_startup_readiness(path, release=synthetic)
    assert result.release_gate_passed and result.local_guards_passed
    assert not result.execution_eligible and not result.startup_prerequisites_passed


def test_status_contains_the_same_local_declaration_without_claiming_state_checks():
    status = read_live_status(None)
    assert status.startup == report.read_local_startup_readiness(release=status.release)
    assert status.status == "unconfigured" and not status.execution_eligible
    assert "stop_controls" in status.startup.unverified_checks


def test_api_refreshes_configuration_etag_without_claiming_live_eligibility(tmp_path, monkeypatch):
    path = tmp_path / "policy.json"
    legacy = replace(
        FULL_CONFIG,
        expected_pins={k: v for k, v in FULL_CONFIG.expected_pins.items() if k in LEGACY_PIN_NAMES},
    )
    path.write_bytes(canonical_json(asdict(legacy)))

    def local(*, release):
        return report.read_local_startup_readiness(path, release=release)

    monkeypatch.setattr(status_report, "read_local_startup_readiness", local)
    with client_for(tmp_path) as client:
        first = client.get("/api/v1/live/status")
        assert first.status_code == 200
        assert first.json()["startup"]["configured_runtime_roles"] == 5
        replacement = tmp_path / "replacement.json"
        replacement.write_bytes(canonical_json(asdict(FULL_CONFIG)))
        replacement.replace(path)
        second = client.get("/api/v1/live/status", headers={"If-None-Match": first.headers["etag"]})
        assert second.status_code == 200 and second.headers["etag"] != first.headers["etag"]
        assert second.json()["startup"]["local_guards_passed"] is True
        assert second.json()["startup"]["startup_prerequisites_passed"] is False
        assert second.json()["execution_eligible"] is False
        path.write_text('{"private_key":"synthetic-private-content"}')
        invalid = client.get(
            "/api/v1/live/status", headers={"If-None-Match": second.headers["etag"]}
        )
        assert invalid.status_code == 200 and invalid.headers["etag"] != second.headers["etag"]
        assert invalid.json()["startup"]["configuration_status"] == "invalid"
        assert "synthetic-private-content" not in invalid.text and str(path) not in invalid.text
        assert client.post("/api/v1/live/status").status_code == 405


def test_readonly_reporting_imports_no_signing_or_network_capability(tmp_path):
    code = """
import importlib.abc,sys
class DenyCapability(importlib.abc.MetaPathFinder):
    def find_spec(self,fullname,path=None,target=None):
        blocked=('eth_account','web3','oasis_agent.live.chain','oasis_agent.live.runner',
                 'oasis_agent.live.authority','oasis_agent.execution.lp_broadcast')
        if any(fullname==p or fullname.startswith(p+'.') for p in blocked):
            raise AssertionError('readonly report imported execution capability')
sys.meta_path.insert(0,DenyCapability())
from oasis_agent.reporting.live_status import read_live_status
result=read_live_status(None)
assert not result.execution_eligible and not result.startup.execution_eligible
assert result.startup.rpc_requests==0
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        env={k: os.environ[k] for k in ("PATH", "PYTHONPATH") if k in os.environ},
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_abi_extraction_keeps_the_canonical_erc20_vector_and_legacy_exports():
    assert chain.selector("transfer(address,uint256)") == selector("transfer(address,uint256)")
    assert chain.calldata(
        "transfer(address,uint256)", ["address", "uint256"], ["0x" + "11" * 20, 7]
    ) == calldata("transfer(address,uint256)", ["address", "uint256"], ["0x" + "11" * 20, 7])
    assert (
        calldata("transfer(address,uint256)", ["address", "uint256"], ["0x" + "11" * 20, 7]).hex()
        == "a9059cbb" + "00" * 12 + "11" * 20 + "00" * 31 + "07"
    )
