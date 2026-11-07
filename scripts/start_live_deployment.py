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
import uuid
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlsplit

from oasis_agent.evidence.rpc_validation import strict_digest, strict_json_object, strict_object
from oasis_agent.live.gas_oracle import require_gas_oracle_pins
from oasis_agent.live.startup_state import (
    SOURCES,
    STARTUP_LIMITATIONS,
    StartupBlocker,
    refusal_report,
)
from oasis_agent.models.common import canonical_json, sha256_hex
from oasis_agent.reporting.live_release import live_release, require_live_start
from oasis_agent.reporting.live_startup import (
    CONFIG_INPUT,
    RPC_PROFILE_NAME,
    read_local_rpc_profile,
    read_local_startup_config,
    read_local_startup_readiness,
)
from scripts import verify_live_compose as compose
from scripts.verify_live_image_sources import source_manifest, verify

VOLUMES = {
    "authority-state": "oasis-live_authority-state",
    "strategy-state": "oasis-live_strategy-state",
    "authority-socket": "oasis-live_authority-socket",
    "canary-keys": "oasis-canary-keys",
}
MAX_STATE_REPORT_BYTES = 4096
STARTUP_GUARD_NAME = "oasis-live-start-guard"
STARTUP_GUARD_LABEL = "org.oasis-agent.live-start-request"


def _retain_record(path: Path, record: dict) -> None:
    """Publish complete bytes exclusively, then persist the record and its path."""
    pending = path.with_name("." + path.name + ".pending")
    with pending.open("xb") as handle:
        handle.write(canonical_json(record))
        handle.flush()
        os.fsync(handle.fileno())
    # A hard link publishes atomically without replacing prior evidence.
    # A failed publication keeps the pending file for diagnosis.
    os.link(pending, path)
    pending.unlink()
    for directory in (path.parent, path.parent.parent):
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def local_environment() -> dict[str, str]:
    # Resolve the operator's selected context without ambient overrides.
    if any(k.startswith("DOCKER_") for k in os.environ):
        raise ValueError("Docker environment overrides are not allowed")
    return {k: os.environ[k] for k in ("PATH", "HOME") if k in os.environ}


def command(
    args: list[str], env: dict[str, str], *, payload: bytes | None = None, timeout: int = 60
) -> bytes:
    if type(timeout) is not int or timeout not in (60, 210):
        raise ValueError("fixed Docker command deadline required")
    return subprocess.run(  # nosec B603 B607 - fixed local Docker commands; no shell
        ["docker", *args], env=env, input=payload, capture_output=True, check=True, timeout=timeout
    ).stdout


def runtime_document(image: str, rpc: dict[str, str], *, once: bool = False) -> dict:
    """Build only the two production services from an independent fixed contract."""
    if re.fullmatch(r"sha256:[0-9a-f]{64}", image) is None or set(rpc) != set(compose.RPC_NAMES):
        raise ValueError("immutable image and exact RPC variable pair required")
    if type(once) is not bool:
        raise ValueError("explicit bounded startup flag required")
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
            "command": list(compose.COMMANDS[name]),
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
    if once:
        services["strategy"]["command"].extend(["--ticks", "1"])
        authority = services["authority"]["command"]
        authority[authority.index("--ttl-seconds") + 1] = "180"
    return {
        "name": "oasis-live",
        "services": services,
        "volumes": {k: {"external": True, "name": v} for k, v in VOLUMES.items()},
    }


def _state_refusal(error: subprocess.CalledProcessError) -> dict:
    if (
        error.returncode != 3
        or not isinstance(error.output, bytes)
        or len(error.output) > MAX_STATE_REPORT_BYTES
    ):
        raise ValueError("original runtime state inspection failed")
    result = strict_json_object(error.output)
    blockers = result.get("blockers")
    if not isinstance(blockers, list) or len(blockers) != 1:
        raise ValueError("invalid runtime state refusal")
    code = StartupBlocker(blockers[0])
    role = result.get("blocked_service")
    if code is StartupBlocker.UNRESOLVED:
        expected = refusal_report(code)
    elif role in ("strategy", "authority"):
        expected = refusal_report(code, role)
    else:
        raise ValueError("invalid runtime state refusal service")
    if canonical_json(result) != canonical_json(expected):
        raise ValueError("invalid runtime state refusal envelope")
    return expected


def state_preflight(
    image: str, env: dict[str, str], *, config_hash: str, rpc_max_requests: int
) -> dict:
    """Run verified code with only original state volumes, read-only and offline."""
    try:
        output = command(
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
    except subprocess.CalledProcessError as error:
        return _state_refusal(error)
    if len(output) > MAX_STATE_REPORT_BYTES:
        raise ValueError("original runtime state report exceeds size limit")
    result = strict_json_object(output)
    if (
        set(result)
        != {
            "schema",
            "startup_prerequisites_passed",
            "execution_eligible",
            "historical_only",
            "limitations",
            "services",
        }
        or result.get("schema") != "oasis.live-startup-state.v1"
        or result.get("startup_prerequisites_passed") is not True
        or result.get("execution_eligible") is not False
        or result.get("historical_only") is not True
        or result.get("limitations") != list(STARTUP_LIMITATIONS)
    ):
        raise ValueError("original runtime state preflight refused")
    raw_services = strict_object(result["services"], "original services")
    if set(raw_services) != {"strategy", "authority"}:
        raise ValueError("exact original service roles required")
    services = {
        name: strict_object(data, "original service") for name, data in raw_services.items()
    }
    digests = {
        "config_hash",
        "journal_tip",
        "position_identity_hash",
        "settled_history_hash",
        "financial_continuity_hash",
    }
    service_fields = digests | {
        "rpc_remaining",
        "checkpoint_verified",
        "settled_history_replayed",
        "settlement_count",
    }
    for service in services.values():
        if (
            set(service) != service_fields
            or service.get("checkpoint_verified") is not True
            or service.get("settled_history_replayed") is not True
            or type(service.get("settlement_count")) is not int
            or not 0 <= service["settlement_count"] <= 10_000
            or service.get("config_hash") != config_hash
        ):
            raise ValueError("complete original financial history report required")
        for field in digests:
            strict_digest(service[field], field)
        remaining = strict_object(service["rpc_remaining"], "remaining RPC quotas")
        if set(remaining) != set(SOURCES) or any(
            type(count) is not int or not 0 < count < rpc_max_requests
            for count in remaining.values()
        ):
            raise ValueError("original nonempty unexhausted RPC quotas required")
    for field in (
        "position_identity_hash",
        "settlement_count",
        "settled_history_hash",
        "financial_continuity_hash",
    ):
        if services["strategy"][field] != services["authority"][field]:
            raise ValueError("original financial history reports disagree")
    return result


def require_local_runtime_policy() -> tuple[str, int, dict[str, str]]:
    config, config_file_hash = read_local_startup_config(compose.ROOT / CONFIG_INPUT)
    require_gas_oracle_pins(config.expected_pins)
    profile_input = f"configs/{RPC_PROFILE_NAME}"
    try:
        profile, profile_file_hash = read_local_rpc_profile(compose.ROOT / profile_input)
    except (OSError, ValueError, RecursionError):
        raise ValueError("valid local RPC profile required") from None
    return (
        sha256_hex(canonical_json(asdict(config))),
        profile.max_requests,
        {CONFIG_INPUT: config_file_hash, profile_input: profile_file_hash},
    )


def _require_no_existing_services(env: dict[str, str]) -> None:
    if command(
        ["ps", "-aq", "--filter", "label=com.docker.compose.project=oasis-live"], env
    ).strip():
        raise ValueError("existing project containers require explicit recovery")


def _acquire_startup_guard(image: str, env: dict[str, str], request_hash: str) -> str:
    """Reserve one daemon-wide name with a stopped, isolated container."""
    strict_digest(request_hash, "startup guard request")
    identity = command(
        [
            "create",
            "--name",
            STARTUP_GUARD_NAME,
            "--label",
            f"{STARTUP_GUARD_LABEL}={request_hash}",
            "--pull=never",
            "--network=none",
            "--read-only",
            "--user=10001:10001",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges:true",
            "--entrypoint",
            "/bin/false",
            image,
        ],
        env,
    ).strip()
    if re.fullmatch(rb"[0-9a-f]{64}", identity) is None:
        raise ValueError("exact startup guard identity required")
    return identity.decode("ascii")


def start(image: str, output: Path, *, once: bool = False) -> dict:
    if type(once) is not bool:
        raise ValueError("explicit bounded startup flag required")
    require_live_start()  # Before Docker, environment, output or state access.
    config_hash, rpc_max_requests, policy_files = require_local_runtime_policy()
    output.mkdir(mode=0o700, parents=False)  # One attempt; a timeout is never retried here.
    report = {
        "schema": "oasis.live-start.v1",
        "status": "refused",
        "start_attempted": False,
        "services_ready": False,
        "execution_eligible": False,
    }
    if once:
        report.update(requested_ticks=1, authority_ttl_seconds=180, compose_timeout_seconds=210)
    guard = None
    stage = "local_context"
    try:
        env = local_environment()
        contexts = json.loads(command(["context", "inspect"], env))
        if not isinstance(contexts, list) or len(contexts) != 1:
            raise ValueError("local Unix-socket Docker context required")
        host = contexts[0]["Endpoints"]["docker"]["Host"]
        if (
            not isinstance(host, str)
            or re.fullmatch(r"unix:///(?!/)[^?#\x00-\x1f\x7f]+", host) is None
        ):
            raise ValueError("local Unix-socket Docker context required")
        # Pin every daemon-bound subprocess to this endpoint. The selected
        # Docker context may change while preflight is running.
        env = env | {"DOCKER_HOST": host}
        report["docker_host_hash"] = sha256_hex(host.encode())
        stage = "deployment_policy"
        policy = compose.inspect()
        stage = "image_sources"
        identity = verify(image, output / "image-sources.json", env=env)
        if any(identity["files"].get(name) != digest for name, digest in policy_files.items()):
            raise ValueError("verified image policies differ from local preflight inputs")
        report.update(
            image=identity["image"],
            manifest_hash=identity["manifest_hash"],
            compose_hash=policy["compose_hash"],
        )
        stage = "rpc_environment"
        rpc = {name: os.environ[name] for name in compose.RPC_NAMES}
        document = runtime_document(identity["image"], rpc, once=once)
        stage = "existing_volumes"
        volumes = json.loads(command(["volume", "inspect", *VOLUMES.values()], env))
        if (
            len(volumes) != len(VOLUMES)
            or {v["Name"] for v in volumes} != set(VOLUMES.values())
            or any(v.get("Driver") != "local" or v.get("Options") for v in volumes)
        ):
            raise ValueError("existing local volumes without remote driver options required")
        stage = "existing_services"
        _require_no_existing_services(env)
        stage = "startup_guard_record"
        guard_request = {
            "schema": "oasis.live-start-guard-request.v1",
            "attempt_id": uuid.uuid4().hex,
            "startup_guard_name": STARTUP_GUARD_NAME,
            "docker_host_hash": report["docker_host_hash"],
            "image": identity["image"],
            "manifest_hash": identity["manifest_hash"],
            "compose_hash": policy["compose_hash"],
            "config_hash": config_hash,
            "execution_eligible": False,
        }
        if once:
            guard_request.update(requested_ticks=1, authority_ttl_seconds=180)
        request_hash = sha256_hex(canonical_json(guard_request))
        _retain_record(
            output / "guard-request.json", guard_request | {"evidence_hash": request_hash}
        )
        stage = "startup_guard"
        report.update(
            startup_guard_name=STARTUP_GUARD_NAME,
            startup_guard_status="creation_unresolved",
            startup_guard_request_hash=request_hash,
        )
        guard = _acquire_startup_guard(identity["image"], env, request_hash)
        report.update(startup_guard_id=guard, startup_guard_status="retained_for_recovery")
        stage = "existing_services_recheck"
        _require_no_existing_services(env)
        # Keep the original-state replay inside the same guard as Compose.
        # Another startup can otherwise change the journals after this read.
        stage = "original_runtime_state"
        preflight = state_preflight(
            identity["image"], env, config_hash=config_hash, rpc_max_requests=rpc_max_requests
        )
        report["state_preflight"] = preflight
        report["state_preflight_exit_code"] = 0 if preflight["startup_prerequisites_passed"] else 3
        if preflight["startup_prerequisites_passed"] is not True:
            raise ValueError("original runtime state preflight refused")
        stage = "final_recheck"
        require_live_start()
        if sha256_hex(canonical_json(source_manifest())) != identity["manifest_hash"]:
            raise ValueError("runtime sources changed before start")
        if sha256_hex(compose.COMPOSE.read_bytes()) != policy["compose_hash"]:
            raise ValueError("deployment configuration changed before start")
        stage = "existing_services_before_start"
        _require_no_existing_services(env)
        stage = "attempt_record"
        attempt = report | {"start_attempted": True, "status": "unresolved"}
        _retain_record(output / "attempt.json", attempt)
        stage = "compose_start"
        report.update(attempt)
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
                *(
                    ["--abort-on-container-exit", "--exit-code-from", "strategy"]
                    if once
                    else ["--detach"]
                ),
                "--no-build",
                "--pull",
                "never",
                "--no-deps",
                "authority",
                "strategy",
            ],
            env | rpc,
            payload=canonical_json(document),
            **({"timeout": 210} if once else {}),
        )
        # Compose success says nothing about service health, receipts or profitability.
        report["status"] = "start_command_completed"
    except Exception:  # noqa: BLE001 - never echo endpoints, Docker output or credentials
        report["failure_stage"] = stage
    if guard is not None and (
        not report["start_attempted"] or report["status"] == "start_command_completed"
    ):
        try:
            command(["rm", guard], env)
        except Exception:  # noqa: BLE001 - preserve uncertain cleanup without Docker diagnostics
            report["startup_guard_status"] = "release_unresolved"
        else:
            report["startup_guard_status"] = "released"
    report["evidence_hash"] = sha256_hex(canonical_json(report))
    _retain_record(output / "report.json", report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", action="store_true")
    parser.add_argument(
        "--once", action="store_true", help="one strategy tick; stop both services on exit"
    )
    parser.add_argument("--image", default="oasis-live:local")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    release = live_release()
    startable = release.acceptance_permit is not None or (
        release.new_actions_allowed and not release.blockers
    )
    if not args.start or not startable:
        readiness = read_local_startup_readiness(compose.ROOT / CONFIG_INPUT, release=release)
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
        result = start(args.image, args.out, once=args.once)
    except ValueError:
        parser.exit(
            3,
            "Startup refused: release, runtime configuration, or RPC profile unresolved.\n",
        )
    except Exception:  # noqa: BLE001 - no raw exception diagnostics
        parser.exit(
            3,
            "Startup outcome unavailable; preserve the attempt directory and inspect services.\n",
        )
    print(json.dumps(result))
    completed = (
        result["status"] == "start_command_completed"
        and result["startup_guard_status"] == "released"
    )
    raise SystemExit(0 if completed else 3)


if __name__ == "__main__":
    main()
