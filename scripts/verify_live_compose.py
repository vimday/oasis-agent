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
# Independent deployment contract, never derived from the document being checked.
CONFIG = ["--config", "/app/configs/live.base-eth-usdc.json"]
PROFILE = ["--rpc-profile", "/app/configs/live.rpc.canary.json"]
STATE = ["--state-dir", "/state"]
AUTHORITY = ["python", "-m", "scripts.run_live_authority"]
STRATEGY = ["python", "-m", "scripts.run_live_lp", "--mode", "live"]
SOCKET = "/run/oasis/authority.sock"
COMMANDS = {
    "initialize": [
        "python",
        "-c",
        (
            "import os; [(os.chown(p,10001,10001),os.chmod(p,0o700)) "
            "for p in ('/authority-state','/strategy-state','/socket')]"
        ),
    ],
    "authority": [
        *AUTHORITY,
        *CONFIG,
        *PROFILE,
        *STATE,
        "--socket",
        SOCKET,
        "--key-file",
        "/secrets/canary.key",
        "--max-signatures",
        "12",
        # Owner 2026-10-31: 24h so exits and recenters stay possible while the canary holds.
        # The 12-signature cap and per-action capital/loss checks are unchanged.
        "--ttl-seconds",
        "86400",
    ],
    # Holding observation reads ~40 requests per provider per tick against a lifetime
    # budget of 4096; 300s polling stretches it to hours. Exit lag is at most one interval.
    "strategy": [
        *STRATEGY,
        *CONFIG,
        *PROFILE,
        *STATE,
        "--signer-socket",
        SOCKET,
        "--poll-seconds",
        "300",
    ],
    "authority-recovery": [*AUTHORITY, "--reconcile-only", *CONFIG, *PROFILE, *STATE],
    "authority-status": [
        *AUTHORITY,
        "--status-only",
        *CONFIG,
        *STATE,
        "--socket",
        SOCKET,
        "--max-status-requests",
        "12",
        "--ttl-seconds",
        "60",
    ],
    "strategy-status-recovery": [
        *STRATEGY,
        "--reconcile-only",
        *CONFIG,
        *PROFILE,
        *STATE,
        "--signer-socket",
        SOCKET,
    ],
    "strategy-recovery": [*STRATEGY, "--reconcile-only", *CONFIG, *PROFILE, *STATE],
}
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
        profiles = ["setup"] if setup else [] if name in {"authority", "strategy"} else ["recovery"]
        if (
            service.get("command") != COMMANDS[name]
            or service.get("entrypoint") is not None
            or service.get("profiles", []) != profiles
        ):
            raise ValueError("deployment command, entrypoint or activation profile differs")
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
        "schema": "oasis.compose-policy-verification.v2",
        "passed": True,
        "compose_hash": sha256_hex(COMPOSE.read_bytes()),
        "verifier_hash": sha256_hex(Path(__file__).read_bytes()),
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
