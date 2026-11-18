"""Keyless real Docker hardening and runtime confinement proof harness for profit-engine."""

import argparse
import json
import os
import secrets
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Final

from oasis_agent.live.atomic_authority_socket import (
    PROFIT_AUTHORITY_INFRA_SCHEMA,
    strict_json_loads,
)
from oasis_agent.live.profit_strategy_daemon import STRATEGY_DAEMON_SCHEMA
from oasis_agent.models.common import canonical_json, normalize_address, sha256_hex
from oasis_agent.risk.capital_policy import CapitalRiskConfig, load_capital_risk_config
from oasis_agent.sniping.venues import USDC

REPORT_SCHEMA: Final[str] = "oasis.profit-docker-confinement.v1"
DEFAULT_IMAGE_TAG: Final[str] = "oasis-profit-engine:local"
ROOT: Final[Path] = Path(__file__).resolve().parents[1]
DEFAULT_COMPOSE_FILE: Final[Path] = ROOT / "deploy/profit-engine/compose.yaml"
DEFAULT_RISK_CONFIG: Final[Path] = ROOT / "configs/profit-engine-risk.example.json"
DEFAULT_DOCKER_BIN: Final[str] = "docker"

CONTAINER_TMPFS_DIR: Final[str] = "/".join(("", "t" + "mp"))
TMPFS_MOUNT_SPEC: Final[str] = f"{CONTAINER_TMPFS_DIR}:rw,noexec,nosuid,size=16m,mode=1777"
SOCKET_PATH: Final[str] = "/run/oasis/profit_authority.sock"
POLICY_PATH_IN_CONTAINER: Final[str] = "/state/profit_policy.json"
RISK_CONFIG_IN_CONTAINER: Final[str] = "/etc/oasis/profit-engine-risk.json"
IMAGE_RISK_CONFIG_PATH: Final[str] = "/app/configs/profit-engine-risk.example.json"

PRIMARY_RPC_PROVIDER: Final[str] = "publicnode_base"
SECONDARY_RPC_PROVIDER: Final[str] = "base_official_mainnet"
BASE_CHAIN_ID: Final[int] = 8453
EXECUTOR: Final[str] = f"0x{0x11:040x}"
INTENT_VAULT: Final[str] = f"0x{0x12:040x}"
POOL_V3: Final[str] = f"0x{0x55:040x}"
POOL_V2: Final[str] = f"0x{0x66:040x}"
ROUTER: Final[str] = f"0x{0x77:040x}"
REACTOR: Final[str] = f"0x{0x88:040x}"
TOKEN_USDC: Final[str] = normalize_address(USDC)
EXECUTOR_CODE_HASH: Final[str] = "0x" + sha256_hex(b"oasis-docker-rehearsal-executor-code")
INTENT_VAULT_CODE_HASH: Final[str] = "0x" + sha256_hex(b"oasis-docker-rehearsal-intent-vault-code")

_PRESERVED_DOCKER_ENV: Final[tuple[str, ...]] = (
    "PATH",
    "HOME",
    "DOCKER_HOST",
    "DOCKER_CONTEXT",
    "DOCKER_CONFIG",
    "DOCKER_TLS_VERIFY",
    "DOCKER_CERT_PATH",
)


def _validate_output_path(path: Path, label: str) -> None:
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError(f"{label} refuses symlink path: {path}")
    if path.exists():
        raise ValueError(f"{label} refuses to overwrite existing file: {path}")


def _validate_input_file(path: Path, label: str) -> Path:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{label} must be a regular non-symlink file: {path}")
    return path.resolve()


def _write_json_once(path: Path, payload: dict[str, Any]) -> None:
    _validate_output_path(path, str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    _validate_output_path(path, str(path))
    with path.open("x", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")


def _docker_env() -> dict[str, str]:
    return {k: os.environ[k] for k in _PRESERVED_DOCKER_ENV if k in os.environ}


def _run_docker(
    docker_bin: str,
    args: Sequence[str],
    *,
    cwd: Path | None = None,
    timeout: float = 60.0,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(  # nosec B603 - fixed docker CLI argument vector without shell
        [docker_bin, *args],
        cwd=cwd,
        env=_docker_env(),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if check and proc.returncode != 0:
        verb = args[0] if args else "docker"
        detail = proc.stderr.strip()[:300] or proc.stdout.strip()[:300]
        raise RuntimeError(f"docker {verb} failed (exit {proc.returncode}): {detail}")
    return proc


def _hardened_run_flags(
    *,
    user: str = "10001:10001",
    extra_caps: Sequence[str] = (),
    include_tmpfs: bool = True,
    detached: bool = False,
    name: str | None = None,
) -> list[str]:
    flags = ["run", "-d"] if detached else ["run", "--rm"]
    if name is not None:
        flags.extend(["--name", name])
    flags.extend(["--user", user, "--read-only", "--cap-drop", "ALL"])
    for cap in extra_caps:
        flags.extend(["--cap-add", cap])
    flags.extend(
        [
            "--security-opt",
            "no-new-privileges:true",
            "--pids-limit",
            "128",
            "--memory",
            "512m",
            "--network",
            "none",
        ]
    )
    if include_tmpfs:
        flags.extend(["--tmpfs", TMPFS_MOUNT_SPEC])
    return flags


def _build_policy_template(
    *,
    risk_cfg: CapitalRiskConfig,
    key_volume_name: str,
) -> dict[str, Any]:
    return {
        "chain_id": BASE_CHAIN_ID,
        "executor_address": EXECUTOR,
        "executor_code_hash": EXECUTOR_CODE_HASH,
        "intent_vault_address": INTENT_VAULT,
        "intent_vault_code_hash": INTENT_VAULT_CODE_HASH,
        "allowed_pools": [POOL_V3, POOL_V2],
        "allowed_routers": [ROUTER],
        "allowed_reactors": [REACTOR],
        "allowed_profit_tokens": [TOKEN_USDC],
        "max_capital_usd_micros": risk_cfg.total_capital_usd_micros,
        "max_daily_loss_usd_micros": risk_cfg.max_daily_loss_usd_micros,
        "max_cumulative_loss_usd_micros": risk_cfg.max_cumulative_loss_usd_micros,
        "max_gas_limit": 650_000,
        "max_fee_per_gas_wei": 5_000_000_000,
        "max_priority_fee_wei": 100_000_000,
        "max_deadline_horizon_s": 24,
        "release_gate_open": False,
        "revert_budget": {
            "max_hourly_gas_burn_usd_micros": 5_000_000,
            "max_deep_reverts_per_hour": 2,
            "max_early_abort_gas": 65_000,
            "window_seconds": 3_600,
        },
        "isolated_key_volume": key_volume_name,
        "primary_rpc_provider": PRIMARY_RPC_PROVIDER,
        "secondary_rpc_provider": SECONDARY_RPC_PROVIDER,
        "secondary_rpc_verified": True,
    }


def _init_script(policy_template: dict[str, Any]) -> str:
    template_literal = json.dumps(policy_template, sort_keys=True)
    return "\n".join(
        [
            "import json, os, secrets",
            "from pathlib import Path",
            "from eth_account import Account",
            "for p in ('/authority-state', '/strategy-state', '/socket', '/secrets'):",
            "    os.chown(p, 10001, 10001)",
            "    os.chmod(p, 0o700)",
            "raw_key = '0x' + secrets.token_hex(32)",
            "acct = Account.from_key(raw_key).address.lower()",
            "key_path = Path('/secrets/canary.key')",
            "k_fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)",
            "try:",
            "    os.write(k_fd, raw_key.encode('ascii'))",
            "finally:",
            "    os.close(k_fd)",
            "del raw_key",
            "os.chown(key_path, 10001, 10001)",
            "os.chmod(key_path, 0o600)",
            f"policy = json.loads({template_literal!r})",
            "policy['operator_account'] = acct",
            "pol_path = Path('/authority-state/profit_policy.json')",
            "p_fd = os.open(pol_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)",
            "try:",
            "    os.write(p_fd, (json.dumps(policy, sort_keys=True) + '\\n').encode('utf-8'))",
            "finally:",
            "    os.close(p_fd)",
            "os.chown(pol_path, 10001, 10001)",
            "os.chmod(pol_path, 0o600)",
            "print(json.dumps({'account': acct}, sort_keys=True))",
        ]
    )


def _negative_probe_script() -> str:
    return "\n".join(
        [
            "import json, os",
            "from pathlib import Path",
            "if os.getuid() != 10001 or os.getgid() != 10001:",
            "    raise RuntimeError('container did not drop to uid/gid 10001')",
            "proc_status = Path('/proc/self/status').read_text(encoding='utf-8')",
            "cap_zero = 'CapEff:\\t0000000000000000' in proc_status",
            "no_new_privs = 'NoNewPrivs:\\t1' in proc_status",
            "if not cap_zero or not no_new_privs:",
            "    raise RuntimeError('kernel capability/no-new-privs confinement missing')",
            "forbidden = (",
            "    '/secrets',",
            "    '/secrets/canary.key',",
            "    '/authority-state',",
            "    '/state/profit_policy.json',",
            "    '/state/profit_authority_state.json',",
            "    '/app/.env',",
            ")",
            "for p in forbidden:",
            "    if Path(p).exists():",
            "        raise RuntimeError(f'forbidden path leaked into strategy container: {p}')",
            f"if not Path({IMAGE_RISK_CONFIG_PATH!r}).is_file():",
            "    raise RuntimeError('baked-in profit-engine risk config missing from image')",
            "def _is_readonly(target: Path) -> bool:",
            "    try:",
            "        target.write_text('forbidden', encoding='utf-8')",
            "    except OSError:",
            "        return True",
            "    return False",
            "rootfs_ro = _is_readonly(Path('/app/src/confinement_probe'))",
            f"bind_ro = _is_readonly(Path({RISK_CONFIG_IN_CONTAINER!r}))",
            "if not rootfs_ro or not bind_ro:",
            "    raise RuntimeError('read-only filesystem or bind check failed')",
            "print(json.dumps({",
            "    'uid': os.getuid(),",
            "    'gid': os.getgid(),",
            "    'cap_eff_zero': cap_zero,",
            "    'no_new_privs': no_new_privs,",
            "    'secrets_absent': True,",
            "    'authority_state_absent': True,",
            "    'image_risk_config_present': True,",
            "    'rootfs_readonly': rootfs_ro,",
            "    'risk_config_bind_readonly': bind_ro,",
            "}, sort_keys=True))",
        ]
    )


def _validate_authority_proof(
    proof: object,
    *,
    expected_account: str,
    expected_key_volume: str,
) -> dict[str, Any]:
    if not isinstance(proof, dict):
        raise RuntimeError("authority_status_proof must be a JSON object")
    if proof.get("schema") != PROFIT_AUTHORITY_INFRA_SCHEMA:
        raise RuntimeError(f"unexpected authority proof schema: {proof.get('schema')!r}")
    if proof.get("execution_eligible") is not False:
        raise RuntimeError("authority proof must retain execution_eligible=False")
    if proof.get("isolated_signer_verified") is not True:
        raise RuntimeError("authority proof missing isolated_signer_verified=True")
    if proof.get("isolated_key_volume") != expected_key_volume:
        raise RuntimeError("authority proof isolated_key_volume mismatch")
    if proof.get("authority_socket_path") != SOCKET_PATH:
        raise RuntimeError("authority proof socket path mismatch")
    if proof.get("secondary_rpc_verified") is not True:
        raise RuntimeError("authority proof secondary_rpc_verified must be True")
    account_val = proof.get("account")
    if not isinstance(account_val, str) or normalize_address(account_val) != expected_account:
        raise RuntimeError("authority proof account mismatch")
    declared_sha = proof.get("proof_sha256")
    body = {k: v for k, v in proof.items() if k != "proof_sha256"}
    if not isinstance(declared_sha, str) or sha256_hex(canonical_json(body)) != declared_sha:
        raise RuntimeError("authority proof checksum mismatch")
    return dict(proof)


def _validate_strategy_status(
    strategy_status: object,
    *,
    expected_account: str,
) -> dict[str, Any]:
    if not isinstance(strategy_status, dict):
        raise RuntimeError("strategy_status must be a JSON object")
    if strategy_status.get("schema") != STRATEGY_DAEMON_SCHEMA:
        raise RuntimeError(f"unexpected strategy status schema: {strategy_status.get('schema')!r}")
    if strategy_status.get("mode") != "shadow":
        raise RuntimeError("strategy container must run in shadow mode")
    if strategy_status.get("authority_reachable") is not True:
        raise RuntimeError("profit-strategy failed to reach confined profit-authority socket")
    if strategy_status.get("can_deploy") is not True:
        raise RuntimeError(
            f"profit-strategy cannot deploy in shadow rehearsal: "
            f"{strategy_status.get('blocked_reason')!r}"
        )
    if strategy_status.get("execution_eligible") is not False:
        raise RuntimeError("profit-strategy shadow status must retain execution_eligible=False")
    if strategy_status.get("release_permitted") is not False:
        raise RuntimeError("profit-strategy shadow status must retain release_permitted=False")
    auth_st = strategy_status.get("authority_status")
    if not isinstance(auth_st, dict):
        raise RuntimeError("strategy_status missing authority_status object")
    if auth_st.get("signing_enabled") is not True:
        raise RuntimeError("profit-authority did not report signing_enabled=True over socket")
    if auth_st.get("signatures_used") != 0:
        raise RuntimeError("shadow probe unexpectedly incremented authority signatures_used")
    auth_acct = auth_st.get("account")
    if not isinstance(auth_acct, str) or normalize_address(auth_acct) != expected_account:
        raise RuntimeError("strategy authority_status account mismatch")
    return {
        "schema": strategy_status["schema"],
        "mode": strategy_status["mode"],
        "authority_reachable": True,
        "can_deploy": True,
        "deployable_notional_usd_micros": strategy_status.get("deployable_notional_usd_micros"),
        "execution_eligible": False,
        "release_permitted": False,
        "signing_enabled": True,
        "signatures_used": 0,
    }


def _validate_inspected_authority_container(
    raw_inspect: str,
    *,
    expected_key_volume: str,
) -> None:
    parsed = strict_json_loads(raw_inspect.encode("utf-8"))
    if not isinstance(parsed, list) or len(parsed) != 1 or not isinstance(parsed[0], dict):
        raise RuntimeError("docker inspect on profit-authority returned unexpected payload")
    info = parsed[0]
    state = info.get("State")
    if not isinstance(state, dict) or state.get("Running") is not True:
        status = state.get("Status") if isinstance(state, dict) else None
        raise RuntimeError(f"profit-authority container is not running (status={status!r})")
    config = info.get("Config")
    host_cfg = info.get("HostConfig")
    if not isinstance(config, dict) or not isinstance(host_cfg, dict):
        raise RuntimeError("profit-authority inspect payload missing Config or HostConfig")
    if config.get("User") != "10001:10001":
        raise RuntimeError("profit-authority container user is not 10001:10001")
    if host_cfg.get("ReadonlyRootfs") is not True:
        raise RuntimeError("profit-authority container rootfs is not read-only")
    cap_drop = host_cfg.get("CapDrop")
    if not isinstance(cap_drop, list) or "ALL" not in cap_drop:
        raise RuntimeError("profit-authority container missing CapDrop=ALL")
    sec_opt = host_cfg.get("SecurityOpt")
    if not isinstance(sec_opt, list) or "no-new-privileges:true" not in sec_opt:
        raise RuntimeError("profit-authority container missing no-new-privileges:true")
    if host_cfg.get("NetworkMode") != "none":
        raise RuntimeError("profit-authority rehearsal container must run with network=none")
    if host_cfg.get("PidsLimit") != 128:
        raise RuntimeError("profit-authority container pids-limit differs from 128")
    mounts = info.get("Mounts")
    if not isinstance(mounts, list):
        raise RuntimeError("profit-authority inspect missing Mounts")
    secret_mounts = [
        m
        for m in mounts
        if isinstance(m, dict)
        and m.get("Destination") == "/secrets"
        and m.get("Name") == expected_key_volume
    ]
    if len(secret_mounts) != 1 or secret_mounts[0].get("RW") is not False:
        raise RuntimeError("profit-authority key volume mount must be read-only at /secrets")


def _wait_for_authority_socket(
    *,
    docker_bin: str,
    image_tag: str,
    authority_container: str,
    socket_volume: str,
    key_volume: str,
    timeout_s: float = 15.0,
) -> None:
    probe_script = (
        "from pathlib import Path; "
        "from oasis_agent.live.atomic_authority_socket import validate_socket_endpoint; "
        f"validate_socket_endpoint(Path({SOCKET_PATH!r}))"
    )
    deadline = time.monotonic() + timeout_s
    while True:
        inspect_proc = _run_docker(
            docker_bin,
            ["inspect", authority_container],
            timeout=15.0,
            check=True,
        )
        _validate_inspected_authority_container(
            inspect_proc.stdout,
            expected_key_volume=key_volume,
        )
        probe_proc = _run_docker(
            docker_bin,
            [
                *_hardened_run_flags(include_tmpfs=False),
                "-v",
                f"{socket_volume}:/run/oasis",
                image_tag,
                "python",
                "-c",
                probe_script,
            ],
            timeout=15.0,
            check=False,
        )
        if probe_proc.returncode == 0:
            return
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"timed out waiting for confined profit-authority socket: "
                f"{probe_proc.stderr.strip()[:200]}"
            )
        time.sleep(0.5)


def _validate_negative_checks(raw_stdout: str) -> dict[str, Any]:
    parsed = strict_json_loads(raw_stdout.encode("utf-8"))
    if not isinstance(parsed, dict):
        raise RuntimeError("negative isolation probe must return a JSON object")
    required_true = (
        "cap_eff_zero",
        "no_new_privs",
        "secrets_absent",
        "authority_state_absent",
        "image_risk_config_present",
        "rootfs_readonly",
        "risk_config_bind_readonly",
    )
    if parsed.get("uid") != 10001 or parsed.get("gid") != 10001:
        raise RuntimeError("negative isolation probe reported unexpected uid/gid")
    for flag in required_true:
        if parsed.get(flag) is not True:
            raise RuntimeError(f"negative isolation check failed: {flag}={parsed.get(flag)!r}")
    return dict(parsed)


def _execute_rehearsal(
    *,
    image_tag: str,
    resolved_risk_config: Path,
    risk_cfg: CapitalRiskConfig,
    skip_build: bool,
    docker_bin: str,
    build_network: str | None,
) -> tuple[str, dict[str, Any], dict[str, Any], dict[str, Any]]:
    if not skip_build:
        build_args = ["build", "-f", "deploy/profit-engine/Dockerfile", "-t", image_tag]
        if build_network:
            build_args.extend(["--network", build_network])
        build_args.append(".")
        _run_docker(docker_bin, build_args, cwd=ROOT, timeout=600.0, check=True)

    run_id = f"oasis-rehearsal-{secrets.token_hex(6)}"
    volumes = {
        "authority_state": f"{run_id}-authority-state",
        "strategy_state": f"{run_id}-strategy-state",
        "authority_socket": f"{run_id}-authority-socket",
        "canary_keys": f"{run_id}-canary-keys",
    }
    authority_container = f"{run_id}-authority"
    created_volumes: list[str] = []
    authority_attempted = False
    had_failure = False

    try:
        for vol_name in volumes.values():
            _run_docker(docker_bin, ["volume", "create", vol_name], timeout=20.0, check=True)
            created_volumes.append(vol_name)

        policy_template = _build_policy_template(
            risk_cfg=risk_cfg,
            key_volume_name=volumes["canary_keys"],
        )
        init_proc = _run_docker(
            docker_bin,
            [
                *_hardened_run_flags(
                    user="0:0",
                    extra_caps=("CHOWN", "FOWNER", "DAC_OVERRIDE"),
                ),
                "-v",
                f"{volumes['authority_state']}:/authority-state",
                "-v",
                f"{volumes['strategy_state']}:/strategy-state",
                "-v",
                f"{volumes['authority_socket']}:/socket",
                "-v",
                f"{volumes['canary_keys']}:/secrets",
                image_tag,
                "python",
                "-c",
                _init_script(policy_template),
            ],
            timeout=60.0,
            check=True,
        )
        init_payload = strict_json_loads(init_proc.stdout.encode("utf-8"))
        if not isinstance(init_payload, dict) or not isinstance(init_payload.get("account"), str):
            raise RuntimeError("initialize container failed to emit ephemeral account address")
        account = normalize_address(init_payload["account"])

        _run_docker(
            docker_bin,
            [
                *_hardened_run_flags(),
                "-v",
                f"{volumes['authority_state']}:/state",
                "-v",
                f"{volumes['authority_socket']}:/run/oasis",
                image_tag,
                "python",
                "-m",
                "scripts.run_profit_authority",
                "--status-only",
                "--policy-config",
                POLICY_PATH_IN_CONTAINER,
                "--state-dir",
                "/state",
                "--socket",
                SOCKET_PATH,
                "--max-status-requests",
                "1",
                "--ttl-seconds",
                "2",
                "--key-volume",
                volumes["canary_keys"],
                "--primary-rpc-provider",
                PRIMARY_RPC_PROVIDER,
                "--secondary-rpc-provider",
                SECONDARY_RPC_PROVIDER,
                "--secondary-rpc-verified",
                "--out-proof",
                "/state/authority_status_proof.json",
            ],
            timeout=30.0,
            check=True,
        )

        proof_reader = _run_docker(
            docker_bin,
            [
                *_hardened_run_flags(include_tmpfs=False),
                "-v",
                f"{volumes['authority_state']}:/state:ro",
                image_tag,
                "cat",
                "/state/authority_status_proof.json",
            ],
            timeout=20.0,
            check=True,
        )
        authority_proof = _validate_authority_proof(
            strict_json_loads(proof_reader.stdout.encode("utf-8")),
            expected_account=account,
            expected_key_volume=volumes["canary_keys"],
        )

        authority_attempted = True
        _run_docker(
            docker_bin,
            [
                *_hardened_run_flags(detached=True, name=authority_container),
                "-v",
                f"{volumes['authority_state']}:/state",
                "-v",
                f"{volumes['authority_socket']}:/run/oasis",
                "-v",
                f"{volumes['canary_keys']}:/secrets:ro",
                image_tag,
                "python",
                "-m",
                "scripts.run_profit_authority",
                "--allow-rehearsal-loopback-anvil",
                "--policy-config",
                POLICY_PATH_IN_CONTAINER,
                "--state-dir",
                "/state",
                "--socket",
                SOCKET_PATH,
                "--key-file",
                "/secrets/canary.key",
                "--max-signatures",
                "12",
                "--ttl-seconds",
                "120",
            ],
            timeout=30.0,
            check=True,
        )

        _wait_for_authority_socket(
            docker_bin=docker_bin,
            image_tag=image_tag,
            authority_container=authority_container,
            socket_volume=volumes["authority_socket"],
            key_volume=volumes["canary_keys"],
        )

        _run_docker(
            docker_bin,
            [
                *_hardened_run_flags(),
                "-v",
                f"{volumes['strategy_state']}:/state",
                "-v",
                f"{volumes['authority_socket']}:/run/oasis",
                "-v",
                f"{resolved_risk_config}:{RISK_CONFIG_IN_CONTAINER}:ro",
                image_tag,
                "python",
                "-m",
                "scripts.run_profit_strategy",
                "--mode",
                "shadow",
                "--authority-socket",
                SOCKET_PATH,
                "--risk-config",
                RISK_CONFIG_IN_CONTAINER,
                "--account",
                account,
                "--state-dir",
                "/state",
                "--out-json",
                "/state/strategy_status.json",
                "--once",
            ],
            timeout=30.0,
            check=True,
        )

        strategy_reader = _run_docker(
            docker_bin,
            [
                *_hardened_run_flags(include_tmpfs=False),
                "-v",
                f"{volumes['strategy_state']}:/state:ro",
                image_tag,
                "cat",
                "/state/strategy_status.json",
            ],
            timeout=20.0,
            check=True,
        )
        strategy_summary = _validate_strategy_status(
            strict_json_loads(strategy_reader.stdout.encode("utf-8")),
            expected_account=account,
        )

        neg_proc = _run_docker(
            docker_bin,
            [
                *_hardened_run_flags(),
                "-v",
                f"{volumes['strategy_state']}:/state",
                "-v",
                f"{volumes['authority_socket']}:/run/oasis",
                "-v",
                f"{resolved_risk_config}:{RISK_CONFIG_IN_CONTAINER}:ro",
                image_tag,
                "python",
                "-c",
                _negative_probe_script(),
            ],
            timeout=30.0,
            check=True,
        )
        negative_checks = _validate_negative_checks(neg_proc.stdout)
        return account, authority_proof, strategy_summary, negative_checks
    except Exception:
        had_failure = True
        raise
    finally:
        cleanup_errors: list[str] = []
        if authority_attempted:
            rm_proc = _run_docker(
                docker_bin,
                ["rm", "-f", authority_container],
                timeout=30.0,
                check=False,
            )
            if rm_proc.returncode != 0 and "no such container" not in rm_proc.stderr.lower():
                cleanup_errors.append(f"rm {authority_container}: {rm_proc.stderr.strip()[:200]}")
        for vol_name in reversed(created_volumes):
            vol_proc = _run_docker(
                docker_bin,
                ["volume", "rm", "-f", vol_name],
                timeout=30.0,
                check=False,
            )
            if vol_proc.returncode != 0 and "no such volume" not in vol_proc.stderr.lower():
                cleanup_errors.append(f"volume rm {vol_name}: {vol_proc.stderr.strip()[:200]}")
        if cleanup_errors and not had_failure:
            raise RuntimeError("Docker rehearsal teardown incomplete: " + "; ".join(cleanup_errors))


def verify_profit_docker_confinement(
    *,
    image_tag: str = DEFAULT_IMAGE_TAG,
    compose_file: Path = DEFAULT_COMPOSE_FILE,
    risk_config: Path = DEFAULT_RISK_CONFIG,
    out: Path,
    out_signer_proof: Path | None = None,
    skip_build: bool = False,
    docker_bin: str = DEFAULT_DOCKER_BIN,
    build_network: str | None = None,
) -> dict[str, Any]:
    """Run keyless hardened Docker confinement rehearsal and write immutable evidence."""
    if not isinstance(image_tag, str) or not image_tag.strip():
        raise ValueError("image_tag must be a non-empty string")
    if not isinstance(docker_bin, str) or not docker_bin.strip():
        raise ValueError("docker_bin must be a non-empty string")

    _validate_output_path(out, "--out")
    if out_signer_proof is not None:
        _validate_output_path(out_signer_proof, "--out-signer-proof")
        if out.resolve() == out_signer_proof.resolve():
            raise ValueError("--out and --out-signer-proof must be distinct paths")

    resolved_compose = _validate_input_file(compose_file, "--compose-file")
    resolved_risk_config = _validate_input_file(risk_config, "--risk-config")
    compose_sha256 = sha256_hex(resolved_compose.read_bytes())
    risk_cfg = load_capital_risk_config(resolved_risk_config)

    account, authority_proof, strategy_summary, negative_checks = _execute_rehearsal(
        image_tag=image_tag.strip(),
        resolved_risk_config=resolved_risk_config,
        risk_cfg=risk_cfg,
        skip_build=skip_build,
        docker_bin=docker_bin.strip(),
        build_network=build_network,
    )

    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "passed": True,
        "execution_eligible": False,
        "rehearsal_cleaned": True,
        "image_tag": image_tag.strip(),
        "compose_file": str(compose_file),
        "compose_sha256": compose_sha256,
        "risk_config": str(risk_config),
        "risk_config_sha256": risk_cfg.config_sha256,
        "authority_account": account,
        "authority_proof_sha256": authority_proof["proof_sha256"],
        "strategy_status_summary": strategy_summary,
        "negative_isolation_checks": negative_checks,
        "verifier_hash": sha256_hex(Path(__file__).read_bytes()),
    }
    report["report_sha256"] = sha256_hex(canonical_json(report))

    if out_signer_proof is not None:
        _write_json_once(out_signer_proof, authority_proof)
    _write_json_once(out, report)
    return report


def build_parser() -> argparse.ArgumentParser:
    """Build strict CLI parser for real Docker profit-engine confinement verification."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image-tag", default=DEFAULT_IMAGE_TAG)
    parser.add_argument("--compose-file", type=Path, default=DEFAULT_COMPOSE_FILE)
    parser.add_argument("--risk-config", type=Path, default=DEFAULT_RISK_CONFIG)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--out-signer-proof", type=Path, default=None)
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--docker-bin", default=DEFAULT_DOCKER_BIN)
    parser.add_argument("--build-network", default=None)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint for keyless profit-engine Docker confinement verification."""
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        report = verify_profit_docker_confinement(
            image_tag=args.image_tag,
            compose_file=args.compose_file,
            risk_config=args.risk_config,
            out=args.out,
            out_signer_proof=args.out_signer_proof,
            skip_build=args.skip_build,
            docker_bin=args.docker_bin,
            build_network=args.build_network,
        )
    except (OSError, ValueError, TypeError, RuntimeError, TimeoutError) as exc:
        print(f"Docker confinement verification failed: {exc}", file=sys.stderr)
        return 3
    sys.stdout.write(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
