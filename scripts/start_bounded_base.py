"""Operator-triggered bounded Base deployment using the existing stopped volumes."""

import argparse
import io
import json
import os
import re
import signal
import subprocess
import tarfile
import time
import uuid
from contextlib import suppress
from pathlib import Path

from oasis_agent.evidence.rpc_validation import strict_json_object
from oasis_agent.live import base_acceptance_rpc as rpc_scope
from oasis_agent.live.base_run_limits import MAX_TICKS, authority_ttl, wait_seconds
from oasis_agent.live.startup_state import StartupBlocker, refusal_report
from oasis_agent.models.common import canonical_json, sha256_hex
from oasis_agent.reporting.live_release import require_live_start
from oasis_agent.reporting.live_startup import CONFIG_INPUT, RPC_PROFILE_NAME
from scripts import live_bounded_worker as worker
from scripts import start_live_deployment as deploy
from scripts import verify_live_compose as compose
from scripts.verify_live_image_sources import source_manifest, verify

PROJECT = "com.docker.compose.project=oasis-live"
RUN_LABEL = "org.oasis-agent.bounded-run"
ROLES = worker.ROLES
STOPPED = {"created", "exited"}
MAX_ARCHIVE = 64 * 1024 * 1024


class Interrupted(Exception):
    """An operator signal aborts the workflow and enters cleanup."""


def _interrupt(_signum, _frame):
    raise Interrupted


def _retain(path: Path, body: dict) -> None:
    deploy._retain_record(path, body | {"evidence_hash": sha256_hex(canonical_json(body))})


def _archive(raw: bytes, destination: Path) -> dict:
    if not 0 < len(raw) <= MAX_ARCHIVE:
        raise ValueError("bounded capture archive required")
    names = set()
    destination.mkdir(mode=0o700)
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as archive:
        for member in archive:
            if (
                not member.isfile()
                or member.name not in worker.ARCHIVE_FILES
                or member.name in names
                or member.size > MAX_ARCHIVE
            ):
                raise ValueError("unexpected capture evidence")
            data = archive.extractfile(member)
            if data is None:
                raise ValueError("capture evidence missing")
            payload = data.read()
            with (destination / member.name).open("xb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            names.add(member.name)
    directory_fd = os.open(destination, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    report = strict_json_object((destination / "report.json").read_bytes())
    body = {key: value for key, value in report.items() if key != "evidence_hash"}
    if report.get("evidence_hash") != sha256_hex(canonical_json(body)):
        raise ValueError("capture report digest differs")
    artifacts = report.get("artifacts")
    rate_limited = report.get("rpc_rate_limited_sources", [])
    if (
        type(report.get("passed")) is not bool
        or not isinstance(artifacts, dict)
        or set(artifacts) != names - {"report.json"}
        or any(
            sha256_hex((destination / name).read_bytes()) != digest
            for name, digest in artifacts.items()
        )
        or (report["passed"] and names != worker.ARCHIVE_FILES)
        or not isinstance(rate_limited, list)
        or any(source not in worker.SOURCES for source in rate_limited)
        or len(rate_limited) != len(set(rate_limited))
        or (rate_limited and (report["passed"] or "wire.jsonl" not in artifacts))
    ):
        raise ValueError("capture evidence archive does not match its report")
    return report


class Run:
    def __init__(self, image: str, output: Path, *, ticks: int = 1):
        self.image = image
        self.output = output
        self.ticks = ticks
        self.wait_seconds = wait_seconds(ticks)
        self.attempt = uuid.uuid4().hex
        self.env: dict[str, str] = {}
        self.rpc: dict[str, str] = {}
        self.calls: dict = {}
        self.guard = None
        self.start_attempted = False
        self.strategy_started = False
        self.safety_scope_checked = False
        self.keyless_unresolved = False
        self.stage = "docker_context"
        self.service_ids: dict[str, str] = {}

    def docker(self, name, args, *, payload=None, rpc=False, timeout=30, check=True):
        # No unvalidated Docker output enters the report (provider URLs may be present).
        try:
            process = subprocess.run(  # nosec B603 B607 - fixed local Docker verbs, no shell
                ["docker", *args],
                env=self.env | (self.rpc if rpc else {}),
                input=payload,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except (subprocess.TimeoutExpired, Interrupted) as error:
            self.calls[name] = {
                "timed_out": isinstance(error, subprocess.TimeoutExpired),
                "interrupted": isinstance(error, Interrupted),
                "exit_code": None,
            }
            raise
        self.calls[name] = {
            "exit_code": process.returncode,
            "timed_out": False,
            "stdout_sha256": sha256_hex(process.stdout),
            "stderr_sha256": sha256_hex(process.stderr),
        }
        if check and process.returncode != 0:
            raise ValueError("Docker operation refused")
        return process

    def project(self) -> dict[str, dict]:
        output = (
            self.docker(
                f"project_{len(self.calls)}",
                ["ps", "-aq", "--no-trunc", "--filter", f"label={PROJECT}"],
            )
            .stdout.decode()
            .splitlines()
        )
        if len(output) > 2 or len(output) != len(set(output)):
            raise ValueError("unexpected Base service inventory")
        services = {}
        fmt = (
            "{{.Id}} {{.State.Status}} {{.State.ExitCode}} {{.State.OOMKilled}} {{.Image}} "
            '{{index .Config.Labels "com.docker.compose.service"}} '
            '{{json (index .Config.Labels "org.oasis-agent.bounded-run")}}'
        )
        for identity in output:
            if re.fullmatch(r"[a-f0-9]{64}", identity) is None:
                raise ValueError("invalid local service identity")
            row = self.docker(f"inspect_{len(self.calls)}", ["inspect", "-f", fmt, identity])
            cid, status, code, oom, image, role, owner_json = row.stdout.decode().split(maxsplit=6)
            owner = json.loads(owner_json)
            if (
                cid != identity
                or role not in ROLES
                or role in services
                or (
                    owner not in (None, "")
                    and (not isinstance(owner, str) or re.fullmatch(r"[a-f0-9]{32}", owner) is None)
                )
                or re.fullmatch(r"sha256:[a-f0-9]{64}", image) is None
                or oom not in ("true", "false")
            ):
                raise ValueError("unrecognized local service")
            services[role] = {
                "id": identity,
                "state": status,
                "exit_code": int(code),
                "oom_killed": oom == "true",
                "image": image,
                "attempt": owner or None,
            }
        return services

    def keyless(self, mode: str, payload: dict, *, timeout: int, check: bool = True):
        name = f"oasis-base-{self.attempt}-{mode}"
        args = [
            "create",
            "--name",
            name,
            "--label",
            f"{RUN_LABEL}={self.attempt}",
            "--pull=never",
            "--read-only",
            "--user",
            "10001:10001",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges:true",
            "--pids-limit",
            "128",
            "--memory",
            "512m",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,nodev,size=67108864",  # nosec B108 - private tmpfs
        ]
        for role in ROLES:
            readonly = (
                ",readonly"
                if mode == "budget" or (mode == "capture" and role == "authority")
                else ""
            )
            volume = deploy.VOLUMES[f"{role}-state"]
            args += ["--mount", f"type=volume,src={volume},dst=/{role},volume-nocopy{readonly}"]
        network = mode == "capture"
        if network:
            for key in compose.RPC_NAMES:
                args += ["-e", key]
        else:
            args += ["--network=none"]
        identity = (
            self.docker(
                f"{mode}_create",
                [
                    *args,
                    "-i",
                    self.image,
                    "python",
                    "-m",
                    "scripts.live_bounded_worker",
                    mode,
                    *compose.CONFIG,
                    *compose.PROFILE,
                ],
                timeout=20,
                rpc=network,
            )
            .stdout.decode()
            .strip()
        )
        if re.fullmatch(r"[a-f0-9]{64}", identity) is None:
            raise ValueError("verified worker container identity required")
        try:
            return self.docker(
                mode,
                ["start", "-ai", identity],
                payload=canonical_json(payload),
                timeout=timeout,
                check=check,
            )
        finally:
            # The daemon can outlive its CLI. Remove only this attempt's known
            # worker before restoring markers or declaring the guard releasable.
            try:
                self.docker(f"{mode}_remove", ["rm", "-f", identity], timeout=20)
            except Exception:  # noqa: BLE001 - preserve uncertainty and continue safety cleanup
                self.keyless_unresolved = True

    def prepare(self, config_hash: str, policy_files: dict) -> tuple[dict, dict]:
        self.env = deploy.local_environment()
        contexts = json.loads(self.docker("context", ["context", "inspect"]).stdout)
        if not isinstance(contexts, list) or len(contexts) != 1:
            raise ValueError("single local Docker context required")
        host = contexts[0]["Endpoints"]["docker"]["Host"]
        if (
            not isinstance(host, str)
            or re.fullmatch(r"unix:///(?!/)[^?#\x00-\x1f\x7f]+", host) is None
        ):
            raise ValueError("local Unix Docker context required")
        self.env["DOCKER_HOST"] = host
        self.stage = "local_release_and_image"
        policy = compose.inspect()
        identity = verify(self.image, self.output / "image-sources.json", env=self.env)
        self.image = identity["image"]
        if any(identity["files"].get(name) != digest for name, digest in policy_files.items()):
            raise ValueError("reviewed image policies differ")
        self.rpc = {name: os.environ[name] for name in compose.RPC_NAMES}
        urls = worker.endpoints(self.rpc)
        document = deploy.runtime_document(self.image, self.rpc, once=True)
        for service in document["services"].values():
            service["labels"] = {RUN_LABEL: self.attempt}
            service["stop_grace_period"] = "8s"
            service["command"].append("--base-acceptance-rpc")
            for mount in service["volumes"]:
                mount["volume"] = {"nocopy": True}
        authority = document["services"]["authority"]["command"]
        authority[authority.index("--max-signatures") + 1] = str(self.ticks)
        authority[authority.index("--ttl-seconds") + 1] = str(authority_ttl(self.ticks))
        authority.extend(["--no-rpc-retry", "--bounded-ticks", str(self.ticks)])
        strategy = document["services"]["strategy"]["command"]
        strategy[strategy.index("--ticks") + 1] = str(self.ticks)
        names = set(deploy.VOLUMES.values())
        volumes = json.loads(self.docker("volumes", ["volume", "inspect", *sorted(names)]).stdout)
        if (
            len(volumes) != len(names)
            or {v["Name"] for v in volumes} != names
            or any(v.get("Driver") != "local" or v.get("Options") for v in volumes)
        ):
            raise ValueError("original local Docker volumes required")
        plan = {
            "schema": "oasis.base-bounded-plan.v1",
            "attempt_id": self.attempt,
            "config_hash": config_hash,
            "image": self.image,
            "manifest_hash": identity["manifest_hash"],
            "compose_hash": policy["compose_hash"],
            "docker_host_hash": sha256_hex(host.encode()),
            "endpoint_hashes": {source: sha256_hex(url.encode()) for source, url in urls.items()},
            "rpc_ceiling_micros": rpc_scope.CEILING_MICROS,
            "rpc_lifetime_upper_micros": rpc_scope.COST_UPPER_MICROS,
            "capture_request_limits": worker.CAPTURE_LIMITS,
            "requested_ticks": self.ticks,
            "wait_seconds": self.wait_seconds,
            "automatic_retries": 0,
            "execution_eligible": False,
        }
        _retain(self.output / "plan.json", plan)
        self.stage = "exclusive_start_guard"
        self.guard = deploy._acquire_startup_guard(
            self.image, self.env, sha256_hex(canonical_json(plan))
        )
        return plan, document

    def execute(self, config_hash, rpc_max_requests, policy_files, report):
        plan, document = self.prepare(config_hash, policy_files)
        self.stage = "stopped_original_services"
        old = self.project()
        if any(row["state"] not in STOPPED for row in old.values()):
            raise ValueError("stop both services before using this entry")
        self.safety_scope_checked = True
        _retain(self.output / "original-containers.json", {"services": old})
        self.stage = "cumulative_rpc_budget"
        budget = strict_json_object(self.keyless("budget", {"plan": plan}, timeout=25).stdout)
        report["rpc_budget"] = budget
        if (
            budget.get("ceiling_micros") != rpc_scope.CEILING_MICROS
            or budget.get("cumulative_scope_upper_micros") != rpc_scope.COST_UPPER_MICROS
            or budget.get("requested_ticks") != self.ticks
            or budget.get("enough_for_run") is not True
        ):
            raise ValueError("insufficient cumulative RPC allowance")
        self.stage = "fresh_evidence"
        capture = self.keyless("capture", {"plan": plan}, timeout=150, check=False)
        fresh = _archive(capture.stdout, self.output / "capture")
        report["fresh_evidence_hash"] = fresh["evidence_hash"]
        if fresh.get("rpc_rate_limited_sources"):
            report["rpc_rate_limited_sources"] = fresh["rpc_rate_limited_sources"]
        if self.keyless_unresolved or capture.returncode != 0 or fresh["passed"] is not True:
            raise ValueError("fresh NAV preflight refused")
        report.update(nav_micros=fresh["nav_micros"], nav_block=fresh["block"])
        self.stage = "prepare_service_containers"
        if self.project() != old:
            raise ValueError("original containers changed under guard")
        for role, row in old.items():
            self.docker(f"retire_{role}", ["rm", row["id"]])
        self.docker(
            "create_services",
            [
                "compose",
                "--env-file",
                os.devnull,
                "--project-name",
                "oasis-live",
                "-f",
                "-",
                "create",
                "--no-build",
                "--pull",
                "never",
                *ROLES,
            ],
            payload=canonical_json(document),
            rpc=True,
            timeout=45,
        )
        services = self.project()
        if set(services) != set(ROLES) or any(
            row["state"] != "created"
            or row["image"] != self.image
            or row["attempt"] != self.attempt
            for row in services.values()
        ):
            raise ValueError("unverified prepared containers")
        self.service_ids = {role: row["id"] for role, row in services.items()}
        self.stage = "final_release_recheck"
        require_live_start()
        if (
            sha256_hex(canonical_json(source_manifest())) != plan["manifest_hash"]
            or sha256_hex(compose.COMPOSE.read_bytes()) != plan["compose_hash"]
        ):
            raise ValueError("reviewed sources or deployment changed")
        self.stage = "stop_handoff"
        opened = strict_json_object(
            self.keyless("open", {"plan": plan, "fresh": fresh}, timeout=30).stdout
        )
        if self.keyless_unresolved or opened != {
            "opened": True,
            "fresh_hash": fresh["evidence_hash"],
        }:
            raise ValueError("unverified STOP handoff")
        self.stage = "historical_startup_gate"
        state = deploy.state_preflight(
            self.image, self.env, config_hash=config_hash, rpc_max_requests=rpc_max_requests
        )
        _retain(self.output / "state-preflight.json", state)
        if state["startup_prerequisites_passed"] is not True:
            raise ValueError("original state startup gate refused")
        report["opening_settlement_count"] = state["services"]["strategy"]["settlement_count"]
        if time.time() >= fresh["nav_valid_until"] - 5 or self.project() != services:
            raise ValueError("fresh NAV expired or prepared services changed")
        self.stage = "bounded_service_start"
        _retain(
            self.output / "attempt.json",
            {
                "schema": "oasis.base-bounded-attempt.v1",
                "attempt_id": self.attempt,
                "started_at": int(time.time()),
                "requested_ticks": self.ticks,
                "wait_seconds": self.wait_seconds,
                "fresh_evidence_hash": fresh["evidence_hash"],
                "containers": self.service_ids,
                "execution_eligible": False,
            },
        )
        self.start_attempted = True
        self.docker("start_authority", ["start", self.service_ids["authority"]], timeout=15)
        self.stage = "authority_ready"
        self.wait_for_authority(fresh["nav_valid_until"])
        self.docker("start_strategy", ["start", self.service_ids["strategy"]], timeout=15)
        self.strategy_started = True
        self.stage = "strategy_ticks"
        result = self.docker(
            "wait_strategy", ["wait", self.service_ids["strategy"]], timeout=self.wait_seconds
        )
        if result.stdout.strip() != b"0":
            raise ValueError("strategy did not complete its bounded ticks")

    def wait_for_authority(self, nav_valid_until: int) -> None:
        # Local socket-bind confirmation only. Never probe the signer or RPC,
        # and never launch the strategy if the retained NAV expired meanwhile.
        authority = self.service_ids["authority"]
        end = time.monotonic() + 12
        index = 0
        while time.monotonic() < end and time.time() < nav_valid_until - 5:
            logs = self.docker(
                f"authority_ready_{index}", ["logs", "--tail", "1", authority], timeout=3
            )
            if logs.stdout.strip() == b"oasis authority ready":
                if time.monotonic() < end and time.time() < nav_valid_until - 5:
                    return
                break
            index += 1
            time.sleep(1)
        raise TimeoutError("authority readiness or fresh NAV deadline")

    def cleanup(self) -> dict:
        stopped, stops = False, dict.fromkeys(ROLES, False)
        final = {}
        if not self.safety_scope_checked:
            return {"verified": False, "services_stopped": False, "stop_present": stops}
        try:
            initial = self.project()
        except Exception:  # noqa: BLE001 - still try each known container and both STOPs
            initial = {}
        for role in ROLES:
            row = initial.get(role, {})
            cid = self.service_ids.get(role)
            if row.get("attempt") == self.attempt and row.get("image") == self.image:
                cid = row["id"]
            if cid is not None and (row.get("id") != cid or row.get("state") not in STOPPED):
                # One role's stop error cannot prevent the other role or marker recovery.
                with suppress(Exception):
                    self.docker(f"stop_{role}", ["stop", "--timeout", "8", cid], timeout=20)
        try:
            recovery = self.keyless(
                "restore", {"attempt_id": self.attempt}, timeout=25, check=False
            )
            actual = strict_json_object(recovery.stdout)
            if (
                set(actual) == set(ROLES)
                and all(type(value) is bool for value in actual.values())
                and recovery.returncode == (0 if all(actual.values()) else 3)
            ):
                stops = actual
        except Exception:  # noqa: BLE001 - keep guard if STOP readback is missing
            pass
        try:
            final = self.project()
            stopped = all(row["state"] in STOPPED for row in final.values()) and all(
                final.get(role, {}).get("id") == cid for role, cid in self.service_ids.items()
            )
        except Exception:  # noqa: BLE001 - absence of a verified read is not successful cleanup
            pass
        return {
            "verified": stopped and all(stops.values()) and not self.keyless_unresolved,
            "services_stopped": stopped,
            "stop_present": stops,
            "services": final,
        }

    def _runtime_evidence(self, cleanup: dict) -> dict:
        # Logs can carry provider diagnostics. Keep raw digests and a parsed
        # allowlisted projection of the verified strategy's final JSON.
        stdout = b""
        for role in ("strategy", "authority"):
            proc = self.docker(
                f"logs_{role}", ["logs", "--tail", "1", self.service_ids[role]], timeout=15
            )
            if role == "strategy":
                stdout = proc.stdout
        final = cleanup["services"]
        if len(stdout) > 2_000_000:
            raise ValueError("bounded strategy output required")
        result = strict_json_object(stdout)
        count, reconciled = result.get("completed_ticks"), result.get("reconciled_transactions")
        completed, tick = result.get("bounded_completed"), result.get("tick_status")
        if (
            result.get("mode") != "live"
            or type(count) is not int
            or type(reconciled) is not int
            or not 0 <= reconciled <= count <= self.ticks
            or type(completed) is not bool
            or (tick is not None and not isinstance(tick, str))
            or (count == 0) != (tick is None)
            or (
                completed
                and (
                    count != self.ticks
                    or result.get("halted") is not None
                    or result.get("pending_transaction") is not False
                    or result.get("signing_unresolved") is not False
                )
            )
        ):
            raise ValueError("terminal strategy result invalid")
        kind = (
            "incomplete"
            if not completed
            else "hold"
            if " | hold" in tick or tick.startswith("hold ")
            else "deferred"
            if " | wait: " in tick
            else "decision_completed"
        )
        _retain(
            self.output / "strategy-result.json",
            {
                "schema": "oasis.base-bounded-tick.v1",
                "attempt_id": self.attempt,
                "kind": kind,
                "completed_ticks": count,
                "reconciled_transactions": reconciled,
                "tick_status_hash": sha256_hex(tick.encode()) if tick is not None else None,
                "execution_eligible": False,
            },
        )
        exit_codes = {role: final[role]["exit_code"] for role in ROLES}
        return {
            "service_exit_codes": exit_codes,
            "completed_ticks": count,
            "reconciled_transactions": reconciled,
            "runtime_completed": completed
            and all(exit_codes[role] == 0 and not final[role]["oom_killed"] for role in ROLES),
        }


def _post_stop_replay(operation: Run, config_hash: str, rpc_max_requests: int) -> dict:
    try:
        return deploy.state_preflight(
            operation.image,
            operation.env,
            config_hash=config_hash,
            rpc_max_requests=rpc_max_requests,
            expect_stop=True,
        )
    except Exception:  # noqa: BLE001 - report no Docker or provider diagnostics
        return refusal_report(StartupBlocker.UNRESOLVED)


def run(image: str, output: Path, *, ticks: int = 1) -> dict:
    operation = Run(image, output, ticks=ticks)
    require_live_start()
    config_hash, rpc_max_requests, files = deploy.require_local_runtime_policy()
    worker.require_policy(
        worker.LiveConfig.load(compose.ROOT / CONFIG_INPUT),
        worker.LiveRpcProfile.load(compose.ROOT / "configs" / RPC_PROFILE_NAME),
    )
    output.mkdir(mode=0o700)  # An output directory authorizes no retry.
    report = {
        "schema": "oasis.base-bounded-run.v1",
        "attempt_id": operation.attempt,
        "created_at": int(time.time()),
        "result": "failed",
        "execution_eligible": False,
        "requested_ticks": ticks,
        "rpc_ceiling_micros": rpc_scope.CEILING_MICROS,
    }
    completed = False
    try:
        operation.execute(config_hash, rpc_max_requests, files, report)
        completed = True
    except (subprocess.TimeoutExpired, TimeoutError, Interrupted) as error:
        report.update(
            result="interrupted" if isinstance(error, Interrupted) else "timed_out",
            failure_stage=operation.stage,
        )
    except Exception:  # noqa: BLE001 - redact endpoint, Docker and production volume errors
        report["failure_stage"] = operation.stage
    finally:
        handlers = {
            sig: signal.signal(sig, signal.SIG_IGN) for sig in (signal.SIGINT, signal.SIGTERM)
        }
        try:
            cleanup = operation.cleanup()
            report["cleanup"] = cleanup
            if operation.start_attempted and cleanup["verified"]:
                report["post_stop_replay"] = _post_stop_replay(
                    operation, config_hash, rpc_max_requests
                )
            if operation.strategy_started and cleanup["verified"]:
                with suppress(Exception):  # Unverified runtime output cannot complete the run.
                    report.update(operation._runtime_evidence(cleanup))
            if completed and cleanup["verified"]:
                if report.get("runtime_completed") is not True:
                    report["failure_stage"] = "runtime_evidence"
                else:
                    replay = report.get("post_stop_replay", {})
                    if (
                        replay.get("startup_prerequisites_passed") is True
                        and replay.get("stop_markers_present") is True
                        and all(
                            replay.get("services", {}).get(role, {}).get("settlement_count")
                            == report.get("opening_settlement_count", -1)
                            + report["reconciled_transactions"]
                            for role in ROLES
                        )
                    ):
                        report["result"] = "completed"
                    else:
                        report["failure_stage"] = "post_stop_replay"
            elif completed:
                report["failure_stage"] = "cleanup"
            if operation.guard is not None:
                report["guard_id"] = operation.guard
                report["guard"] = "held_for_recovery"
                if cleanup["verified"]:
                    try:
                        operation.docker("release_guard", ["rm", operation.guard])
                    except Exception:  # noqa: BLE001 - record unresolved guard release
                        report["result"] = "failed"
                        report["failure_stage"] = "release_guard"
                    else:
                        report["guard"] = "released"
            report.update(
                start_attempted=operation.start_attempted,
                commands=operation.calls,
                finished_at=int(time.time()),
            )
            _retain(output / "report.json", report)
        finally:
            for sig, handler in handlers.items():
                signal.signal(sig, handler)
    return report


def run_operator(image: str, output: Path, *, ticks: int = 1) -> dict:
    signals = (signal.SIGINT, signal.SIGTERM)
    previous = {sig: signal.signal(sig, _interrupt) for sig in signals}
    try:
        return run(image, output, ticks=ticks)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="oasis-live:local")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--ticks", type=int, choices=range(1, MAX_TICKS + 1), default=1)
    args = parser.parse_args()
    try:
        report = run_operator(args.image, args.out, ticks=args.ticks)
    except Exception:  # noqa: BLE001 - never echo Docker or RPC internals
        parser.exit(3, "Bounded startup refused; preserve any existing run evidence.\n")
    print(
        json.dumps(
            {
                "result": report["result"],
                "cleanup_verified": report["cleanup"]["verified"],
                "evidence": str(args.out / "report.json"),
            }
        )
    )
    raise SystemExit(0 if report["result"] == "completed" else 3)


if __name__ == "__main__":
    main()
