"""Expanded deployment policy rejects credential and custody boundary regressions."""

import copy
import json
import os
import subprocess
from types import SimpleNamespace

import pytest

from scripts import verify_live_compose as cli


@pytest.fixture
def document():
    # A minimal expanded Compose document, not a container isolation simulation.
    services = {}
    for name, mounts in cli.MOUNTS.items():
        setup = name == "initialize"
        offline = setup or name == "authority-status"
        service = {
            "user": "0:0" if setup else "10001:10001",
            "read_only": True,
            "cap_drop": ["ALL"],
            "cap_add": ["CHOWN", "FOWNER", "DAC_OVERRIDE"] if setup else [],
            "security_opt": ["no-new-privileges:true"],
            "restart": "no",
            "pids_limit": 128,
            "mem_limit": "536870912",
            "environment": {} if offline else dict.fromkeys(cli.RPC_NAMES),
            "volumes": [
                {
                    "type": "volume",
                    "source": source,
                    "target": target,
                    "read_only": target == "/secrets",
                }
                for target, source in mounts.items()
            ],
            "command": ["--status-only"] if name == "authority-status" else ["--reconcile-only"],
        }
        if offline:
            service["network_mode"] = "none"
        services[name] = service
    return {
        "services": services,
        "volumes": {"canary-keys": {"external": True, "name": "oasis-canary-keys"}},
    }


def test_minimal_deployment_contract(document):
    cli.verify_services(document, dict.fromkeys(cli.RPC_NAMES))


@pytest.mark.parametrize(
    "fault",
    [
        "key_mount",
        "state_sharing",
        "bind_mount",
        "writable_key",
        "root_strategy",
        "network_status",
        "key_environment",
        "restart",
        "capability",
        "writable_root",
        "signing_recovery",
        "status_signing",
        "key_volume_created",
        "missing_service",
    ],
)
def test_confinement_regressions_refused(document, fault):
    services = document["services"]
    if fault == "key_mount":
        services["strategy"]["volumes"].append(copy.deepcopy(services["authority"]["volumes"][-1]))
    elif fault == "state_sharing":
        services["strategy"]["volumes"][0]["source"] = "authority-state"
    elif fault == "bind_mount":
        services["authority-status"]["volumes"][0]["type"] = "bind"
    elif fault == "writable_key":
        services["authority"]["volumes"][-1]["read_only"] = False
    elif fault == "root_strategy":
        services["strategy"]["user"] = "0:0"
    elif fault == "network_status":
        services["authority-status"].pop("network_mode")
    elif fault == "key_environment":
        services["authority-status"]["environment"] = {"OASIS_PRIVATE_KEY": "synthetic-marker"}
    elif fault == "restart":
        services["authority"]["restart"] = "always"
    elif fault == "capability":
        services["strategy"]["cap_add"] = ["SYS_ADMIN"]
    elif fault == "writable_root":
        services["strategy"]["read_only"] = False
    elif fault == "signing_recovery":
        services["authority-recovery"]["command"] += ["--key-file", "/key"]
    elif fault == "status_signing":
        services["authority-status"]["command"] = ["--key-env", "KEY"]
    elif fault == "key_volume_created":
        document["volumes"]["canary-keys"]["external"] = False
    else:
        services.pop("strategy-recovery")
    with pytest.raises(ValueError):
        cli.verify_services(document, dict.fromkeys(cli.RPC_NAMES))


def test_parser_gets_no_host_credentials_or_compose_overrides(document, monkeypatch):
    monkeypatch.setenv("BASE_RPC_PRIMARY", "private-marker")
    monkeypatch.setenv("OASIS_PRIVATE_KEY", "private-marker")
    monkeypatch.setenv("COMPOSE_FILE", "unrelated-file")
    calls = []

    def run(command, **kwargs):
        env = kwargs["env"]
        assert "private-marker" not in env.values()
        assert not {"COMPOSE_FILE", "OASIS_PRIVATE_KEY"}.intersection(env)
        assert command[2:4] == ["--env-file", os.devnull]
        assert command[-5:] == ["--profile", "*", "config", "--format", "json"]
        assert kwargs["timeout"] == 20
        calls.append(command)
        result = copy.deepcopy(document)
        for name, service in result["services"].items():
            if name not in ("initialize", "authority-status"):
                service["environment"] = {k: env.get(k) for k in cli.RPC_NAMES}
        return SimpleNamespace(stdout=json.dumps(result).encode())

    monkeypatch.setattr(subprocess, "run", run)
    report = cli.inspect()
    assert len(calls) == 2 and report["passed"] is True
    assert report["execution_eligible"] is False and report["network_calls"] == 0
