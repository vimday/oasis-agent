"""Release-gated local Docker entry point; default is offline readiness only.

Starting requires the existing code release gate, reviewed image bytes, explicit
RPC environment variables and existing state/key volumes. Never builds, pulls,
initializes budgets, reads wallet files or retries an uncertain startup.
"""

import argparse
import json
import os
import re
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

from oasis_agent.live.config import LiveConfig
from oasis_agent.live.gas_oracle import require_gas_oracle_pins
from oasis_agent.models.common import canonical_json, sha256_hex
from oasis_agent.reporting.live_release import live_release, require_live_start
from oasis_agent.reporting.live_startup import read_local_startup_readiness
from scripts import verify_live_compose as compose
from scripts.verify_live_image_sources import source_manifest, verify

VOLUMES = {
    "authority-state": "oasis-live_authority-state",
    "strategy-state": "oasis-live_strategy-state",
    "authority-socket": "oasis-live_authority-socket",
    "canary-keys": "oasis-canary-keys",
}


def local_environment() -> dict[str, str]:
    # Source/image verification uses the same default Docker context. Refuse
    # ambient overrides instead of inspecting one daemon and starting on another.
    if any(k.startswith("DOCKER_") for k in os.environ):
        raise ValueError("Docker environment overrides are not allowed")
    return {k: os.environ[k] for k in ("PATH", "HOME") if k in os.environ}


def command(args: list[str], env: dict[str, str], *, payload: bytes | None = None) -> bytes:
    return subprocess.run(  # nosec B603 B607 - fixed local Docker commands; no shell
        ["docker", *args], env=env, input=payload, capture_output=True, check=True, timeout=60
    ).stdout


def runtime_document(image: str, rpc: dict[str, str]) -> dict:
    """Build only the two production services from an independent fixed contract."""
    if re.fullmatch(r"sha256:[0-9a-f]{64}", image) is None or set(rpc) != set(compose.RPC_NAMES):
        raise ValueError("immutable image and exact RPC variable pair required")
    urls = [urlsplit(v) for v in rpc.values()]
    hosts = {u.hostname.rstrip(".") for u in urls if u.hostname}
    if (
        any(
            u.scheme != "https"
            or not u.hostname
            or "$" in u.hostname
            or u.username
            or u.password
            or u.fragment
            for u in urls
        )
        or "" in hosts
        or len(hosts) != 2
    ):
        raise ValueError("two distinct HTTPS RPC hosts required")
    services = {}
    for name in ("authority", "strategy"):
        services[name] = {
            "image": image,
            "user": "10001:10001",
            "read_only": True,
            "cap_drop": ["ALL"],
            "security_opt": ["no-new-privileges:true"],
            "pids_limit": 128,
            "mem_limit": "512m",
            "restart": "no",
            # Container-private tmpfs specification, not a host temporary file.
            "tmpfs": ["/tmp:rw,noexec,nosuid,size=16m,mode=1777"],  # nosec B108
            # Compose interpolates document values. Import the validated RPC
            # strings by name so dollar signs in credentials stay literal.
            "environment": list(compose.RPC_NAMES),
            "command": compose.COMMANDS[name],
            "volumes": [
                {
                    "type": "volume",
                    "source": source,
                    "target": target,
                    "read_only": target == "/secrets",
                }
                for target, source in compose.MOUNTS[name].items()
            ],
        }
    return {
        "name": "oasis-live",
        "services": services,
        "volumes": {k: {"external": True, "name": v} for k, v in VOLUMES.items()},
    }


def state_preflight(image: str, env: dict[str, str]) -> dict:
    """Run verified code with only original state volumes, read-only and offline."""
    result = json.loads(
        command(
            [
                "run",
                "--rm",
                "--pull=never",
                "--network=none",
                "--read-only",
                "--user",
                "10001:10001",
                "--cap-drop=ALL",
                "--security-opt=no-new-privileges:true",
                "--pids-limit",
                "128",
                "--memory",
                "512m",
                "--mount",
                f"type=volume,src={VOLUMES['strategy-state']},dst=/strategy,readonly,volume-nocopy",
                "--mount",
                f"type=volume,src={VOLUMES['authority-state']},dst=/authority,readonly,volume-nocopy",
                image,
                "python",
                "-m",
                "scripts.inspect_live_startup",
                "--strategy-dir",
                "/strategy",
                "--authority-dir",
                "/authority",
                *compose.CONFIG,
                *compose.PROFILE,
            ],
            env,
        )
    )
    if (
        not isinstance(result, dict)
        or result.get("schema") != "oasis.live-startup-state.v1"
        or result.get("startup_prerequisites_passed") is not True
        or result.get("execution_eligible") is not False
        or result.get("historical_only") is not True
        or not isinstance(result.get("services"), dict)
        or set(result.get("services", {})) != {"strategy", "authority"}
    ):
        raise ValueError("original runtime state preflight refused")
    services = result["services"]
    for service in services.values():
        if (
            not isinstance(service, dict)
            or service.get("checkpoint_verified") is not True
            or service.get("settled_history_replayed") is not True
            or type(service.get("settlement_count")) is not int
            or not 0 <= service["settlement_count"] <= 10_000
            or any(
                not isinstance(service.get(field), str)
                or re.fullmatch(r"[0-9a-f]{64}", service[field]) is None
                for field in (
                    "config_hash",
                    "journal_tip",
                    "position_identity_hash",
                    "settled_history_hash",
                    "financial_continuity_hash",
                )
            )
        ):
            raise ValueError("complete original financial history report required")
    for field in (
        "config_hash",
        "position_identity_hash",
        "settlement_count",
        "settled_history_hash",
        "financial_continuity_hash",
    ):
        if services["strategy"][field] != services["authority"][field]:
            raise ValueError("original financial history reports disagree")
    return result


def require_local_runtime_policy() -> None:
    config = LiveConfig.load(compose.ROOT / "configs/live.base-eth-usdc.json")
    require_gas_oracle_pins(config.expected_pins)


def start(image: str, output: Path) -> dict:
    require_live_start()  # Before Docker, environment, output or state access.
    require_local_runtime_policy()
    output.mkdir(mode=0o700, parents=False)  # One attempt; a timeout is never retried here.
    report = {
        "schema": "oasis.live-start.v1",
        "status": "refused",
        "start_attempted": False,
        "services_ready": False,
        "execution_eligible": False,
    }
    stage = "local_context"
    try:
        env = local_environment()
        contexts = json.loads(command(["context", "inspect"], env))
        if len(contexts) != 1 or not contexts[0]["Endpoints"]["docker"]["Host"].startswith(
            "unix://"
        ):
            raise ValueError("local Unix-socket Docker context required")
        stage = "deployment_policy"
        policy = compose.inspect()
        stage = "image_sources"
        identity = verify(image, output / "image-sources.json")
        report.update(
            image=identity["image"],
            manifest_hash=identity["manifest_hash"],
            compose_hash=policy["compose_hash"],
        )
        stage = "rpc_environment"
        rpc = {name: os.environ[name] for name in compose.RPC_NAMES}
        document = runtime_document(identity["image"], rpc)
        stage = "existing_volumes"
        volumes = json.loads(command(["volume", "inspect", *VOLUMES.values()], env))
        if (
            len(volumes) != len(VOLUMES)
            or {v["Name"] for v in volumes} != set(VOLUMES.values())
            or any(v.get("Driver") != "local" or v.get("Options") for v in volumes)
        ):
            raise ValueError("existing local volumes without remote driver options required")
        stage = "existing_services"
        if command(
            ["ps", "-aq", "--filter", "label=com.docker.compose.project=oasis-live"], env
        ).strip():
            raise ValueError("existing project containers require explicit recovery")
        stage = "original_runtime_state"
        report["state_preflight"] = state_preflight(identity["image"], env)
        stage = "final_recheck"
        require_live_start()
        if sha256_hex(canonical_json(source_manifest())) != identity["manifest_hash"]:
            raise ValueError("runtime sources changed before start")
        if sha256_hex(compose.COMPOSE.read_bytes()) != policy["compose_hash"]:
            raise ValueError("deployment configuration changed before start")
        stage = "compose_start"
        report["start_attempted"] = True
        report["status"] = "unresolved"  # Persist before the only state-changing command.
        with (output / "attempt.json").open("xb") as handle:
            handle.write(canonical_json(report))
            handle.flush()
            os.fsync(handle.fileno())
        for directory in (output, output.parent):
            fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        command(
            [
                "compose",
                "--env-file",
                os.devnull,
                "--project-name",
                "oasis-live",
                "-f",
                "-",
                "up",
                "--detach",
                "--no-build",
                "--pull",
                "never",
                "--no-deps",
                "authority",
                "strategy",
            ],
            env | rpc,
            payload=canonical_json(document),
        )
        # Compose success says nothing about service health, receipts or profitability.
        report["status"] = "start_command_completed"
    except Exception:  # noqa: BLE001 - never echo endpoints, Docker output or credentials
        report["failure_stage"] = stage
    report["evidence_hash"] = sha256_hex(canonical_json(report))
    with (output / "report.json").open("x") as handle:
        json.dump(report, handle, indent=2)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", action="store_true")
    parser.add_argument("--image", default="oasis-live:local")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    release = live_release()
    startable = release.acceptance_permit is not None or (
        release.new_actions_allowed and not release.blockers
    )
    if not args.start or not startable:
        readiness = read_local_startup_readiness(
            compose.ROOT / "configs/live.base-eth-usdc.json", release=release
        )
        print(
            json.dumps(
                {
                    **release.model_dump(),
                    "startup": readiness.model_dump(by_alias=True),
                }
            )
        )
        raise SystemExit(0 if readiness.local_guards_passed else 3)
    if args.out is None:
        parser.error("--start requires a new --out directory for one retained attempt")
    try:
        result = start(args.image, args.out)
    except ValueError:
        parser.exit(3, "Startup refused: release or approved gas oracle runtime pins unresolved.\n")
    except Exception:  # noqa: BLE001 - no raw exception diagnostics
        parser.exit(
            3,
            "Startup outcome unavailable; preserve the attempt directory and inspect services.\n",
        )
    print(json.dumps(result))
    raise SystemExit(0 if result["status"] == "start_command_completed" else 3)


if __name__ == "__main__":
    main()
