"""Inspect expanded profit-engine Compose confinement without daemon, keys, or network."""

import argparse
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from oasis_agent.models.common import canonical_json, sha256_hex

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "deploy/profit-engine/compose.yaml"
RPC_NAMES = ("BASE_RPC_PRIMARY_ALT", "BASE_RPC_PRIMARY")
POLICY_FLAG = ["--policy-config", "/state/profit_policy.json"]
STATE_FLAG = ["--state-dir", "/state"]
SOCKET_PATH = "/run/oasis/profit_authority.sock"
RISK_CONFIG_TARGET = "/etc/oasis/profit-engine-risk.json"
RISK_CONFIG_SOURCE_REL = "configs/profit-engine-risk.example.json"
AUTHORITY_CMD = ["python", "-m", "scripts.run_profit_authority"]
STRATEGY_CMD = ["python", "-m", "scripts.run_profit_strategy"]
TMPFS_OPT_SUFFIX = ":rw,noexec,nosuid,size=16m,mode=1777"
FORBIDDEN_ENV_RE = re.compile(r"(KEY|SECRET|MNEMONIC|SEED|PASSWORD|TOKEN)", re.IGNORECASE)
READONLY_KEYS_TARGET = "/secrets"

COMMANDS: dict[str, list[str]] = {
    "initialize": [
        "python",
        "-c",
        (
            "import os; [(os.chown(p,10001,10001),os.chmod(p,0o700)) "
            "for p in ('/authority-state','/strategy-state','/socket')]"
        ),
    ],
    "profit-authority": [
        *AUTHORITY_CMD,
        *POLICY_FLAG,
        *STATE_FLAG,
        "--socket",
        SOCKET_PATH,
        "--key-file",
        "/secrets/canary.key",
        "--max-signatures",
        "12",
        "--ttl-seconds",
        "86400",
    ],
    "profit-strategy": [
        *STRATEGY_CMD,
        "--mode",
        "shadow",
        "--authority-socket",
        SOCKET_PATH,
        "--risk-config",
        RISK_CONFIG_TARGET,
        *STATE_FLAG,
        "--out-json",
        "/state/strategy_status.json",
        "--poll-interval-ms",
        "1000",
    ],
    "profit-authority-status": [
        *AUTHORITY_CMD,
        "--status-only",
        *POLICY_FLAG,
        *STATE_FLAG,
        "--socket",
        SOCKET_PATH,
        "--max-status-requests",
        "12",
        "--ttl-seconds",
        "60",
    ],
    "profit-authority-recovery": [
        *AUTHORITY_CMD,
        "--reconcile-only",
        *POLICY_FLAG,
        *STATE_FLAG,
    ],
    "profit-strategy-recovery": [
        *STRATEGY_CMD,
        "--mode",
        "shadow",
        "--reconcile-only",
        "--authority-socket",
        SOCKET_PATH,
        "--risk-config",
        RISK_CONFIG_TARGET,
        *STATE_FLAG,
        "--out-json",
        "/state/strategy_recovery.json",
    ],
}

MOUNTS: dict[str, dict[str, str]] = {
    "initialize": {
        "/authority-state": "profit-authority-state",
        "/strategy-state": "profit-strategy-state",
        "/socket": "profit-authority-socket",
    },
    "profit-authority": {
        "/state": "profit-authority-state",
        "/run/oasis": "profit-authority-socket",
        READONLY_KEYS_TARGET: "canary-keys",
    },
    "profit-strategy": {
        "/state": "profit-strategy-state",
        "/run/oasis": "profit-authority-socket",
        RISK_CONFIG_TARGET: RISK_CONFIG_SOURCE_REL,
    },
    "profit-authority-status": {
        "/state": "profit-authority-state",
        "/run/oasis": "profit-authority-socket",
    },
    "profit-authority-recovery": {
        "/state": "profit-authority-state",
    },
    "profit-strategy-recovery": {
        "/state": "profit-strategy-state",
        "/run/oasis": "profit-authority-socket",
        RISK_CONFIG_TARGET: RISK_CONFIG_SOURCE_REL,
    },
}


def verify_services(
    document: dict[str, Any],
    expected_rpc: dict[str, str | None],
    *,
    expected_risk_config: Path | None = None,
) -> None:
    """Verify every service, confinement boundary, volume mount, and command in Compose."""
    services = document.get("services", {})
    if not isinstance(services, dict) or set(services) != set(MOUNTS):
        raise ValueError("profit-engine service inventory differs")

    risk_config_path = (
        (ROOT / RISK_CONFIG_SOURCE_REL).resolve()
        if expected_risk_config is None
        else expected_risk_config.resolve()
    )

    for name, service in services.items():
        if not isinstance(service, dict):
            raise ValueError("profit-engine service entry malformed")
        setup = name == "initialize"
        offline = setup or name == "profit-authority-status"
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
            raise ValueError("profit-engine confinement differs")

        tmpfs = service.get("tmpfs")
        if (
            not isinstance(tmpfs, list)
            or len(tmpfs) != 1
            or not isinstance(tmpfs[0], str)
            or not tmpfs[0].startswith("/")
            or not tmpfs[0].endswith(TMPFS_OPT_SUFFIX)
            or len(tmpfs[0]) != 4 + len(TMPFS_OPT_SUFFIX)
        ):
            raise ValueError("profit-engine tmpfs mount options differ")

        if offline and service.get("network_mode") != "none":
            raise ValueError("offline service gained network capability")

        environment = service.get("environment") or {}
        if not isinstance(environment, dict):
            raise ValueError("profit-engine service environment malformed")
        if any(FORBIDDEN_ENV_RE.search(str(key)) for key in environment):
            raise ValueError("secret-like environment variable present in deployment")
        if environment != ({} if offline else expected_rpc):
            raise ValueError("profit-engine environment differs")

        mounts = service.get("volumes", [])
        if not isinstance(mounts, list) or len(mounts) != len(MOUNTS[name]):
            raise ValueError("profit-engine volume inventory differs")
        actual: dict[str, object] = {}
        for mount in mounts:
            if not isinstance(mount, dict) or mount.get("target") in actual:
                raise ValueError("profit-engine requires unique mounts")
            target = str(mount["target"])
            source = mount.get("source")
            if target == RISK_CONFIG_TARGET:
                if mount.get("type") != "bind" or mount.get("read_only", False) is not True:
                    raise ValueError("risk config mount must be a read-only bind")
                if not isinstance(source, str) or Path(source).resolve() != risk_config_path:
                    raise ValueError("risk config bind source differs")
                actual[target] = RISK_CONFIG_SOURCE_REL
                continue

            if mount.get("type") != "volume":
                raise ValueError("profit-engine requires named-volume mounts for state/sockets")
            actual[target] = source
            if mount.get("read_only", False) is not (target == READONLY_KEYS_TARGET):
                raise ValueError("profit-engine volume access differs")
            if source == "canary-keys" and name != "profit-authority":
                raise ValueError("canary-keys volume leaked outside profit-authority")
            if source == "profit-authority-state" and "strategy" in name:
                raise ValueError("authority state volume leaked to strategy service")
        if actual != MOUNTS[name]:
            raise ValueError("profit-engine state/key isolation differs")

        profiles = (
            ["setup"]
            if setup
            else []
            if name in {"profit-authority", "profit-strategy"}
            else ["recovery"]
        )
        command = service.get("command")
        if (
            command != COMMANDS[name]
            or service.get("entrypoint") is not None
            or service.get("profiles", []) != profiles
        ):
            raise ValueError("profit-engine command, entrypoint, or activation profile differs")
        if name != "profit-authority" and any(
            arg in ("--key-file", "--key-env") for arg in command
        ):
            raise ValueError("key argument leaked outside profit-authority")
        if "strategy" in name and "--mode" in command:
            mode_idx = command.index("--mode")
            if mode_idx + 1 >= len(command) or command[mode_idx + 1] != "shadow":
                raise ValueError("profit-strategy service must remain bounded to shadow mode")

    if (
        actual_socket := {
            m.get("target"): m.get("source") for m in services["profit-strategy"].get("volumes", [])
        }.get("/run/oasis")
    ) != "profit-authority-socket" or actual_socket != {
        m.get("target"): m.get("source") for m in services["profit-authority"].get("volumes", [])
    }.get("/run/oasis"):
        raise ValueError("profit-strategy must share profit-authority-socket with profit-authority")

    key_volume = document.get("volumes", {}).get("canary-keys", {})
    if key_volume.get("external") is not True or key_volume.get("name") != "oasis-canary-keys":
        raise ValueError("production key volume must remain external")


def inspect(compose_file: Path = COMPOSE) -> dict[str, Any]:
    """Expand `deploy/profit-engine/compose.yaml` offline and verify confinement."""
    target = compose_file
    if target.is_symlink() or not target.is_file():
        raise ValueError("compose file must be a regular non-symlink file")
    snapshot = target.read_bytes()
    expected_risk_config = (target.resolve().parent / "../../" / RISK_CONFIG_SOURCE_REL).resolve()
    environment = {k: os.environ[k] for k in ("PATH", "HOME") if k in os.environ}
    for configured in (False, True):
        expected = {
            name: f"https://provider-{i}.invalid" if configured else None
            for i, name in enumerate(RPC_NAMES)
        }
        env = environment | {k: v for k, v in expected.items() if v is not None}
        # Fixed non-shell `docker compose ... config --format json` invocation: parses local YAML
        # from stdin with stripped host env and `--env-file os.devnull`; starts no containers.
        result = subprocess.run(
            [
                "docker",
                "compose",
                "--env-file",
                os.devnull,
                "--project-directory",
                str(target.parent),
                "-f",
                "-",
                "--profile",
                "*",
                "config",
                "--format",
                "json",
            ],
            env=env,
            input=snapshot,
            capture_output=True,
            timeout=20,
            check=True,
        )
        verify_services(
            json.loads(result.stdout),
            expected,
            expected_risk_config=expected_risk_config,
        )
    if target.read_bytes() != snapshot:
        raise ValueError("profit-engine compose configuration changed during verification")
    report: dict[str, Any] = {
        "schema": "oasis.profit-compose-policy-verification.v1",
        "passed": True,
        "compose_hash": sha256_hex(snapshot),
        "verifier_hash": sha256_hex(Path(__file__).read_bytes()),
        "services": sorted(MOUNTS),
        "cases": ["rpc_absent", "synthetic_rpc_present"],
        "execution_eligible": False,
        "network_calls": 0,
        "scope": "expanded profit-engine deployment configuration only; no container or live run",
    }
    report["evidence_hash"] = sha256_hex(canonical_json(report))
    return report


def main() -> None:
    """CLI entrypoint for offline profit-engine compose verification."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compose-file", type=Path, default=COMPOSE)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    if args.out is not None and (args.out.exists() or args.out.is_symlink()):
        parser.error("output exists or is a symlink; preserve previous evidence")
    try:
        report = inspect(compose_file=args.compose_file)
    except Exception:  # noqa: BLE001 - never echo arbitrary parser output or environment
        parser.exit(3, "Profit-engine deployment verification refused; details redacted.\n")
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        with args.out.open("x", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2)
            handle.write("\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
