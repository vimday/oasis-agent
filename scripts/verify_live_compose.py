"""Inspect expanded local deployment policy without daemon, credentials or network."""

import argparse
import json
import os
import subprocess
from pathlib import Path

from oasis_agent.models.common import canonical_json, sha256_hex

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "deploy/live/compose.yaml"
RPC_NAMES = ("BASE_RPC_PRIMARY_ALT", "BASE_RPC_PRIMARY")
MOUNTS = {
    "initialize": {
        "/authority-state": "authority-state",
        "/strategy-state": "strategy-state",
        "/socket": "authority-socket",
    },
    "authority": {
        "/state": "authority-state",
        "/run/oasis": "authority-socket",
        "/secrets": "canary-keys",
    },
    "strategy": {"/state": "strategy-state", "/run/oasis": "authority-socket"},
    "authority-status": {"/state": "authority-state", "/run/oasis": "authority-socket"},
    "authority-recovery": {"/state": "authority-state"},
    "strategy-recovery": {"/state": "strategy-state"},
    "strategy-status-recovery": {"/state": "strategy-state", "/run/oasis": "authority-socket"},
}


def verify_services(document: dict, expected_rpc: dict) -> None:
    services = document.get("services", {})
    if set(services) != set(MOUNTS):
        raise ValueError("deployment service inventory differs")
    for name, service in services.items():
        setup = name == "initialize"
        offline = setup or name == "authority-status"
        if (
            service.get("user") != ("0:0" if setup else "10001:10001")
            or service.get("read_only") is not True
            or service.get("cap_drop") != ["ALL"]
            or service.get("cap_add", []) != (["CHOWN", "FOWNER", "DAC_OVERRIDE"] if setup else [])
            or service.get("security_opt") != ["no-new-privileges:true"]
            or service.get("privileged", False) is not False
            or service.get("restart") != "no"
            or service.get("pids_limit") != 128
            or service.get("mem_limit") != "536870912"
        ):
            raise ValueError("deployment confinement differs")
        if offline and service.get("network_mode") != "none":
            raise ValueError("offline service gained network capability")
        environment = service.get("environment") or {}
        if environment != ({} if offline else expected_rpc):
            raise ValueError("deployment environment differs")
        mounts = service.get("volumes", [])
        if len(mounts) != len(MOUNTS[name]):
            raise ValueError("deployment volume inventory differs")
        actual = {}
        for mount in mounts:
            if mount.get("type") != "volume" or mount.get("target") in actual:
                raise ValueError("deployment requires unique named-volume mounts")
            target = mount["target"]
            actual[target] = mount.get("source")
            if mount.get("read_only", False) is not (target == "/secrets"):
                raise ValueError("deployment volume access differs")
        if actual != MOUNTS[name]:
            raise ValueError("deployment state/key isolation differs")
        command = service.get("command", [])
        if name == "authority-status" and (
            "--status-only" not in command or any(k in command for k in ("--key-file", "--key-env"))
        ):
            raise ValueError("status service gained signing configuration")
        if name.endswith("recovery") and (
            "--reconcile-only" not in command
            or any(k in command for k in ("--key-file", "--key-env"))
        ):
            raise ValueError("recovery service gained signing configuration")
    key_volume = document.get("volumes", {}).get("canary-keys", {})
    if key_volume.get("external") is not True or key_volume.get("name") != "oasis-canary-keys":
        raise ValueError("production key volume must remain external")


def inspect() -> dict:
    # Do not inherit RPC credentials, COMPOSE_FILE, profiles, .env or Docker context.
    environment = {k: os.environ[k] for k in ("PATH", "HOME") if k in os.environ}
    for configured in (False, True):
        expected = {
            name: f"https://provider-{i}.invalid" if configured else None
            for i, name in enumerate(RPC_NAMES)
        }
        env = environment | {k: v for k, v in expected.items() if v is not None}
        result = subprocess.run(  # nosec B603 B607 - fixed local config parser; no services started
            [
                "docker",
                "compose",
                "--env-file",
                os.devnull,
                "-f",
                str(COMPOSE),
                "--profile",
                "*",
                "config",
                "--format",
                "json",
            ],
            env=env,
            capture_output=True,
            timeout=20,
            check=True,
        )
        verify_services(json.loads(result.stdout), expected)
    report = {
        "schema": "oasis.compose-policy-verification.v1",
        "passed": True,
        "compose_hash": sha256_hex(COMPOSE.read_bytes()),
        "services": sorted(MOUNTS),
        "cases": ["rpc_absent", "synthetic_rpc_present"],
        "execution_eligible": False,
        "network_calls": 0,
        "scope": "expanded deployment configuration only; no container or production acceptance",
    }
    report["evidence_hash"] = sha256_hex(canonical_json(report))
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    if args.out is not None and args.out.exists():
        parser.error("output exists; preserve previous evidence")
    try:
        report = inspect()
    except Exception:  # noqa: BLE001 - never echo arbitrary parser output or environment
        parser.exit(3, "Deployment configuration verification refused; details redacted.\n")
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        with args.out.open("x") as handle:
            json.dump(report, handle, indent=2)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
