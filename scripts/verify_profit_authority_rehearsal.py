"""Keyless ephemeral rehearsal of configurable profit-engine authority policies over AF_UNIX."""

import argparse
import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Final

from eth_account import Account

from oasis_agent.execution.atomic_bundle import (
    AtomicCycleIntent,
    PrecheckGuardSpec,
    SwapLegSpec,
    encode_execute_cycle_calldata,
)
from oasis_agent.live.atomic_authority import (
    AtomicExecutionPolicy,
    DualSimulationProof,
    EnvelopeProposal,
    admit_atomic_or_intent_proposal,
    verify_signed_bytes_match_admission,
)
from oasis_agent.live.atomic_authority_socket import (
    PROFIT_AUTHORITY_INFRA_SCHEMA,
    PROFIT_AUTHORITY_SCHEMA,
    AtomicOrIntentProposal,
    ProfitAuthoritySocketClient,
    envelope_fields_from_proposal,
    load_atomic_execution_policy,
    proposal_to_dict,
    read_frame,
)
from oasis_agent.live.authority_socket import fields_json
from oasis_agent.live.canary_readiness import evaluate_profit_engine_readiness
from oasis_agent.models.common import canonical_json, normalize_address, sha256_hex
from oasis_agent.risk.revert_budget import EARLY_ABORTED_TOPIC0
from oasis_agent.sniping.venues import USDC, WETH

REHEARSAL_SCHEMA_VERSION: Final[str] = "oasis.profit-authority-rehearsal.v1"
REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[1]

BASE_CHAIN_ID: Final[int] = 8453
EXECUTOR: Final[str] = f"0x{0x11:040x}"
INTENT_VAULT: Final[str] = f"0x{0x12:040x}"
POOL_V3: Final[str] = f"0x{0x55:040x}"
POOL_V2: Final[str] = f"0x{0x66:040x}"
ROUTER: Final[str] = f"0x{0x77:040x}"
REACTOR: Final[str] = f"0x{0x88:040x}"
TOKEN_USDC: Final[str] = normalize_address(USDC)
TOKEN_WETH: Final[str] = normalize_address(WETH)
EXECUTOR_CODE_HASH: Final[str] = "0x" + sha256_hex(b"oasis-rehearsal-executor-code")
INTENT_VAULT_CODE_HASH: Final[str] = "0x" + sha256_hex(b"oasis-rehearsal-intent-vault-code")
SIM_BLOCK_HASH: Final[str] = "0x" + sha256_hex(b"oasis-rehearsal-sim-block")
RECEIPT_BLOCK_HASH: Final[str] = "0x" + sha256_hex(b"oasis-rehearsal-receipt-block")
UINT160_SQRT_1: Final[int] = 1 << 96


def _short_ephemeral_dir() -> tempfile.TemporaryDirectory[str]:
    base = Path(tempfile.gettempdir()).resolve()
    return tempfile.TemporaryDirectory(prefix="oa-r-", dir=base)


def _subprocess_env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT / "src")
    return env


def _write_private_key_file(key_path: Path, key_hex: str) -> None:
    fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, key_hex.encode("ascii"))
    finally:
        os.close(fd)


def _build_policy_dict(
    *,
    operator_account: str,
    max_capital_usd_micros: int,
    max_daily_loss_usd_micros: int,
    max_cumulative_loss_usd_micros: int,
) -> dict[str, Any]:
    return {
        "chain_id": BASE_CHAIN_ID,
        "operator_account": normalize_address(operator_account),
        "executor_address": EXECUTOR,
        "executor_code_hash": EXECUTOR_CODE_HASH,
        "intent_vault_address": INTENT_VAULT,
        "intent_vault_code_hash": INTENT_VAULT_CODE_HASH,
        "allowed_pools": [POOL_V3, POOL_V2],
        "allowed_routers": [ROUTER],
        "allowed_reactors": [REACTOR],
        "allowed_profit_tokens": [TOKEN_USDC],
        "max_capital_usd_micros": max_capital_usd_micros,
        "max_daily_loss_usd_micros": max_daily_loss_usd_micros,
        "max_cumulative_loss_usd_micros": max_cumulative_loss_usd_micros,
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
        "isolated_key_volume": "oasis-rehearsal-ephemeral",
        "primary_rpc_provider": "base-publicnode-archive",
        "secondary_rpc_provider": "alchemy-base-archive",
        "secondary_rpc_verified": True,
    }


def _build_proposal(
    *,
    account: str,
    nonce: int,
    now_s: int,
    initial_amount_usdc: int,
    min_net_profit_raw: int,
    daily_loss_usd_micros: int,
    cumulative_loss_usd_micros: int,
) -> AtomicOrIntentProposal:
    deadline_s = now_s + 18
    intent = AtomicCycleIntent(
        chain_id=BASE_CHAIN_ID,
        executor_address=EXECUTOR,
        executor_code_hash=EXECUTOR_CODE_HASH,
        nonce=nonce,
        deadline=deadline_s,
        profit_token=TOKEN_USDC,
        initial_amount=initial_amount_usdc,
        min_net_profit=min_net_profit_raw,
        guard=PrecheckGuardSpec(
            pool=POOL_V3,
            min_sqrt_price_x96=UINT160_SQRT_1 - 100,
            max_sqrt_price_x96=UINT160_SQRT_1 + 100,
        ),
        legs=(
            SwapLegSpec(
                pool=POOL_V3,
                token_in=TOKEN_USDC,
                token_out=TOKEN_WETH,
                zero_for_one=True,
                sqrt_price_limit_x96=UINT160_SQRT_1 - 1,
                v2_fee_bps=0,
            ),
            SwapLegSpec(
                pool=POOL_V2,
                token_in=TOKEN_WETH,
                token_out=TOKEN_USDC,
                zero_for_one=False,
                sqrt_price_limit_x96=0,
                v2_fee_bps=30,
            ),
        ),
    )
    calldata, _ = encode_execute_cycle_calldata(intent)
    envelope = EnvelopeProposal(
        chain_id=BASE_CHAIN_ID,
        account=account,
        nonce=nonce,
        target=EXECUTOR,
        target_observed_code_hash=EXECUTOR_CODE_HASH,
        value_wei=0,
        calldata=calldata,
        gas_limit=350_000,
        max_fee_per_gas_wei=1_000_000_000,
        max_priority_fee_wei=50_000_000,
        profit_token=TOKEN_USDC,
        min_net_profit_raw=min_net_profit_raw,
        referenced_pools=(POOL_V3, POOL_V2),
        referenced_routers=(),
        referenced_reactor=None,
        deadline_s=deadline_s,
    )
    simulation = DualSimulationProof(
        block_number=58_900_001,
        block_hash=SIM_BLOCK_HASH,
        primary_source="base-publicnode-archive",
        secondary_source="alchemy-base-archive",
        primary_reverted=False,
        secondary_reverted=False,
        primary_early_aborted=False,
        secondary_early_aborted=False,
        primary_gas_used=195_000,
        secondary_gas_used=196_000,
        primary_profit_delta_raw=min_net_profit_raw + 1_000_000,
        secondary_profit_delta_raw=min_net_profit_raw + 1_000_000,
        l2_gas_fee_wei=200_000_000_000_000,
        l1_data_fee_wei=50_000_000_000_000,
        simulated_profit_value_wei=600_000_000_000_000,
    )
    return AtomicOrIntentProposal(
        envelope=envelope,
        simulation=simulation,
        now_s=now_s,
        daily_loss_usd_micros=daily_loss_usd_micros,
        cumulative_loss_usd_micros=cumulative_loss_usd_micros,
        rehearsal_loopback_anvil=True,
    )


def _early_abort_settlement_payload(
    *,
    tx_hash: str,
    timestamp_s: int,
    gas_cost_usd_micros: int = 3_000_000,
) -> dict[str, Any]:
    gas_used = 55_000
    gas_price = 1_000_000_000
    l1_fee = 25_000_000_000_000
    expected_spend = gas_used * gas_price + l1_fee
    native_before = 1_000_000_000_000_000_000
    native_after = native_before - expected_spend

    def _receipt(source: str) -> dict[str, Any]:
        return {
            "source": source,
            "tx_hash": tx_hash,
            "block_number": 58_900_005,
            "block_hash": RECEIPT_BLOCK_HASH,
            "status": 1,
            "gas_used": gas_used,
            "effective_gas_price_wei": gas_price,
            "l1_fee_wei": l1_fee,
            "logs": [
                {
                    "address": EXECUTOR,
                    "topics": [EARLY_ABORTED_TOPIC0],
                    "data": "0x",
                }
            ],
        }

    return {
        "tx_hash": tx_hash,
        "primary_receipt": _receipt("base-publicnode-archive"),
        "secondary_receipt": _receipt("alchemy-base-archive"),
        "native_balance_before_wei": native_before,
        "native_balance_after_wei": native_after,
        "profit_token_balance_before_raw": 100_000_000,
        "profit_token_balance_after_raw": 100_000_000,
        "gas_cost_usd_micros": gas_cost_usd_micros,
        "realized_profit_usd_micros": 0,
        "timestamp_s": timestamp_s,
    }


def _wait_for_socket_and_optional_proof(
    proc: subprocess.Popen[bytes],
    endpoint: Path,
    proof_path: Path | None = None,
    timeout_s: float = 10.0,
) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        rc = proc.poll()
        if rc is not None:
            _out, err = proc.communicate(timeout=2.0)
            raise RuntimeError(
                f"profit authority exited early (rc={rc}): {err.decode('utf-8', 'replace')}"
            )
        sock_ready = endpoint.exists() and stat.S_ISSOCK(endpoint.lstat().st_mode)
        proof_ready = proof_path is None or proof_path.is_file()
        if sock_ready and proof_ready:
            return
        time.sleep(0.02)
    proc.terminate()
    proc.wait(timeout=5.0)
    raise TimeoutError(f"timed out waiting for authority socket: {endpoint}")


def _finish_subprocess(proc: subprocess.Popen[bytes], *, expected_rc: int = 0) -> tuple[str, str]:
    try:
        stdout_b, stderr_b = proc.communicate(timeout=10.0)
    except subprocess.TimeoutExpired:
        proc.terminate()
        stdout_b, stderr_b = proc.communicate(timeout=5.0)
        raise TimeoutError("profit authority daemon did not terminate cleanly") from None
    stdout_s = stdout_b.decode("utf-8", "replace")
    stderr_s = stderr_b.decode("utf-8", "replace")
    if proc.returncode != expected_rc:
        raise RuntimeError(
            f"profit authority exited with {proc.returncode} != {expected_rc}: {stderr_s}"
        )
    return stdout_s, stderr_s


def _raw_sign_over_socket(
    endpoint: Path,
    account: str,
    proposal: AtomicOrIntentProposal,
) -> dict[str, Any]:
    frame = canonical_json(
        {
            "schema": PROFIT_AUTHORITY_SCHEMA,
            "account": normalize_address(account),
            "operation": "sign",
            "proposal": proposal_to_dict(proposal),
            "fields": fields_json(envelope_fields_from_proposal(proposal)),
        }
    )
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(5.0)
        conn.connect(str(endpoint))
        conn.sendall(frame + b"\n")
        reply = read_frame(conn, max_bytes=65_536)
    decoded = json.loads(reply.decode("utf-8"))
    if not isinstance(decoded, dict):
        raise ValueError("authority reply must be a JSON object")
    return decoded


def _assert_no_secrets_leaked(
    text: str,
    *,
    key_hex: str,
    signed_hexes: Sequence[str],
) -> None:
    stripped_key = key_hex.lower().removeprefix("0x")
    lowered = text.lower()
    if stripped_key in lowered:
        raise AssertionError("ephemeral private key leaked in output")
    for raw_hex in signed_hexes:
        if raw_hex.lower() in lowered:
            raise AssertionError("raw signed transaction hex leaked in output")


def _sign_proposal_via_socket(
    client: ProfitAuthoritySocketClient,
    policy: AtomicExecutionPolicy,
    proposal: AtomicOrIntentProposal,
) -> tuple[str, str]:
    admission = admit_atomic_or_intent_proposal(
        policy=policy,
        proposal=proposal.envelope,
        simulation=proposal.simulation,
        now_s=proposal.now_s,
        daily_loss_usd_micros=proposal.daily_loss_usd_micros,
        cumulative_loss_usd_micros=proposal.cumulative_loss_usd_micros,
        revert_budget_status=proposal.revert_budget_status,
        has_unresolved_pending=proposal.has_unresolved_pending,
        rehearsal_loopback_anvil=True,
    )
    if not admission.admitted:
        raise AssertionError(f"expected proposal admission, got {admission.reasons}")
    if admission.execution_eligible is not False:
        raise AssertionError("rehearsal admission must retain execution_eligible=False")
    raw_signed = client.sign_admitted_proposal(proposal, envelope_fields_from_proposal(proposal))
    submitted = verify_signed_bytes_match_admission(raw_signed, decision=admission)
    if submitted.account != normalize_address(policy.operator_account):
        raise AssertionError("signed transaction recovered wrong operator account")
    return submitted.transaction_hash, raw_signed.hex()


def _assert_over_daily_loss_refused(
    *,
    client: ProfitAuthoritySocketClient,
    endpoint: Path,
    account: str,
    proposal: AtomicOrIntentProposal,
    expected_reason: str = "daily_loss_limit_reached",
) -> str:
    client_rejected = False
    try:
        client.sign_admitted_proposal(proposal, envelope_fields_from_proposal(proposal))
    except ValueError as exc:
        if expected_reason not in str(exc):
            raise AssertionError(f"unexpected client refusal reason: {exc}") from exc
        client_rejected = True
    if not client_rejected:
        raise AssertionError("client failed to reject over-daily-loss proposal")

    server_reply = _raw_sign_over_socket(endpoint, account, proposal)
    if server_reply.get("refused") is not True:
        raise AssertionError("server failed to refuse raw over-daily-loss request")
    reason_code = str(server_reply.get("reason_code", ""))
    if reason_code != expected_reason:
        raise AssertionError(f"unexpected server refusal reason_code: {reason_code!r}")
    return reason_code


def _rehearse_charter_default() -> tuple[dict[str, Any], dict[str, Any]]:
    env = _subprocess_env()
    signed_hexes: list[str] = []
    acct = Account.create()
    key_hex = acct.key.hex()
    dir_path: Path | None = None

    with _short_ephemeral_dir() as tmp_dir:
        root = Path(tmp_dir)
        dir_path = root
        os.chmod(root, 0o700)
        state_dir = root / "st"
        sock_dir = root / "sk"
        key_dir = root / "ky"
        for sub in (state_dir, sock_dir, key_dir):
            sub.mkdir(mode=0o700)
            os.chmod(sub, 0o700)

        policy_path = root / "policy.json"
        policy_dict = _build_policy_dict(
            operator_account=acct.address,
            max_capital_usd_micros=500_000_000,
            max_daily_loss_usd_micros=200_000_000,
            max_cumulative_loss_usd_micros=500_000_000,
        )
        policy_path.write_text(json.dumps(policy_dict, sort_keys=True), encoding="utf-8")
        loaded = load_atomic_execution_policy(policy_path)

        key_path = key_dir / "e.key"
        _write_private_key_file(key_path, key_hex)

        # 1. --status-only --out-proof Gate 8 verification without touching any key.
        status_sock = sock_dir / "st.sock"
        proof_path = root / "infra_proof.json"
        status_proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "scripts.run_profit_authority",
                "--policy-config",
                str(policy_path),
                "--state-dir",
                str(state_dir),
                "--socket",
                str(status_sock),
                "--status-only",
                "--max-status-requests",
                "1",
                "--ttl-seconds",
                "15",
                "--out-proof",
                str(proof_path),
            ],
            cwd=REPO_ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        _wait_for_socket_and_optional_proof(status_proc, status_sock, proof_path=proof_path)
        status_client = ProfitAuthoritySocketClient(
            status_sock, acct.address, loaded.policy, timeout=5.0
        )
        status_payload = status_client.status()
        if status_payload["signing_enabled"] is not False:
            raise AssertionError("status-only authority unexpectedly enabled signing")
        st_out, st_err = _finish_subprocess(status_proc, expected_rc=0)
        _assert_no_secrets_leaked(st_out + st_err, key_hex=key_hex, signed_hexes=signed_hexes)

        proof = json.loads(proof_path.read_text(encoding="utf-8"))
        if proof.get("schema") != PROFIT_AUTHORITY_INFRA_SCHEMA:
            raise AssertionError("status proof schema mismatch")
        if proof.get("execution_eligible") is not False:
            raise AssertionError("status proof must keep execution_eligible=False")
        readiness = evaluate_profit_engine_readiness(
            contracts_out_dir=root / "absent-contracts",
            infra_proof_path=proof_path,
        )
        gates = {g.name: g for g in readiness.gates}
        if not (
            gates["dual_rpc_quorum_verified"].passed
            and gates["isolated_signer_authority_ready"].passed
            and readiness.execution_eligible is False
        ):
            raise AssertionError("Gate 7/8 readiness failed for --status-only proof")

        # 2. Signing rehearsal: in-limit signs cleanly (1), over daily loss refused (stays 1).
        sign_sock = sock_dir / "sg.sock"
        sign_proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "scripts.run_profit_authority",
                "--allow-rehearsal-loopback-anvil",
                "--policy-config",
                str(policy_path),
                "--state-dir",
                str(state_dir),
                "--socket",
                str(sign_sock),
                "--key-file",
                str(key_path),
                "--max-signatures",
                "12",
                "--max-status-requests",
                "2",
                "--ttl-seconds",
                "30",
            ],
            cwd=REPO_ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        _wait_for_socket_and_optional_proof(sign_proc, sign_sock)
        client = ProfitAuthoritySocketClient(sign_sock, acct.address, loaded.policy, timeout=5.0)
        now_s = int(time.time())

        p1 = _build_proposal(
            account=acct.address,
            nonce=1,
            now_s=now_s,
            initial_amount_usdc=150_000_000,
            min_net_profit_raw=150_000_000,
            daily_loss_usd_micros=0,
            cumulative_loss_usd_micros=0,
        )
        tx1_hash, tx1_hex = _sign_proposal_via_socket(client, loaded.policy, p1)
        signed_hexes.append(tx1_hex)

        p_over_loss = _build_proposal(
            account=acct.address,
            nonce=2,
            now_s=now_s,
            initial_amount_usdc=150_000_000,
            min_net_profit_raw=150_000_000,
            daily_loss_usd_micros=250_000_000,
            cumulative_loss_usd_micros=250_000_000,
        )
        over_loss_reason = _assert_over_daily_loss_refused(
            client=client,
            endpoint=sign_sock,
            account=acct.address,
            proposal=p_over_loss,
        )

        mid_status = client.status()
        if mid_status["signatures_used"] != 1:
            raise AssertionError(
                f"expected signatures_used==1, got {mid_status['signatures_used']}"
            )

        # Sign a second valid proposal so 2 consecutive failure reconciliations can run via CLI.
        p2 = _build_proposal(
            account=acct.address,
            nonce=2,
            now_s=now_s,
            initial_amount_usdc=150_000_000,
            min_net_profit_raw=150_000_000,
            daily_loss_usd_micros=0,
            cumulative_loss_usd_micros=0,
        )
        tx2_hash, tx2_hex = _sign_proposal_via_socket(client, loaded.policy, p2)
        signed_hexes.append(tx2_hex)
        end_status = client.status()
        if end_status["signatures_used"] != 2:
            raise AssertionError(
                f"expected signatures_used==2, got {end_status['signatures_used']}"
            )
        sg_out, sg_err = _finish_subprocess(sign_proc, expected_rc=0)
        _assert_no_secrets_leaked(sg_out + sg_err, key_hex=key_hex, signed_hexes=signed_hexes)

        # 3. Reconcile two consecutive settled failures via --reconcile-only.
        settle1_path = root / "s1.json"
        settle2_path = root / "s2.json"
        settle1_path.write_text(
            json.dumps(
                _early_abort_settlement_payload(
                    tx_hash=tx1_hash, timestamp_s=now_s + 5, gas_cost_usd_micros=3_000_000
                )
            ),
            encoding="utf-8",
        )
        settle2_path.write_text(
            json.dumps(
                _early_abort_settlement_payload(
                    tx_hash=tx2_hash, timestamp_s=now_s + 10, gas_cost_usd_micros=3_000_000
                )
            ),
            encoding="utf-8",
        )

        rec1 = subprocess.run(
            [
                sys.executable,
                "-m",
                "scripts.run_profit_authority",
                "--policy-config",
                str(policy_path),
                "--state-dir",
                str(state_dir),
                "--reconcile-only",
                "--settlement-input",
                str(settle1_path),
            ],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if rec1.returncode != 0:
            raise RuntimeError(
                f"first reconcile expected rc=0, got {rec1.returncode}: {rec1.stderr}"
            )
        rec1_json = json.loads(rec1.stdout)
        if rec1_json["halted"] is not False:
            raise AssertionError("single bounded early-abort probe should not latch halt")

        rec2 = subprocess.run(
            [
                sys.executable,
                "-m",
                "scripts.run_profit_authority",
                "--policy-config",
                str(policy_path),
                "--state-dir",
                str(state_dir),
                "--reconcile-only",
                "--settlement-input",
                str(settle2_path),
            ],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if rec2.returncode != 3:
            raise RuntimeError(f"second consecutive failure expected rc=3, got {rec2.returncode}")
        rec2_json = json.loads(rec2.stdout)
        if rec2_json["halted"] is not True or rec2_json["halt_latch"] is not True:
            raise AssertionError("two consecutive failures failed to latch halt")

        # 4. Reload persisted state offline (--reconcile-only) and via restarted signing daemon.
        reload_proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "scripts.run_profit_authority",
                "--policy-config",
                str(policy_path),
                "--state-dir",
                str(state_dir),
                "--reconcile-only",
            ],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if reload_proc.returncode != 3:
            raise RuntimeError(f"reconcile reload expected rc=3, got {reload_proc.returncode}")
        reload_json = json.loads(reload_proc.stdout)
        if reload_json["halted"] is not True or reload_json["revert_budget_status"] != "halt":
            raise AssertionError("halted flag did not persist across --reconcile-only reload")

        halted_sock = sock_dir / "hl.sock"
        halted_proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "scripts.run_profit_authority",
                "--allow-rehearsal-loopback-anvil",
                "--policy-config",
                str(policy_path),
                "--state-dir",
                str(state_dir),
                "--socket",
                str(halted_sock),
                "--key-file",
                str(key_path),
                "--max-signatures",
                "12",
                "--max-status-requests",
                "1",
                "--ttl-seconds",
                "30",
            ],
            cwd=REPO_ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        _wait_for_socket_and_optional_proof(halted_proc, halted_sock)
        p3 = _build_proposal(
            account=acct.address,
            nonce=3,
            now_s=int(time.time()),
            initial_amount_usdc=150_000_000,
            min_net_profit_raw=150_000_000,
            daily_loss_usd_micros=0,
            cumulative_loss_usd_micros=0,
        )
        halted_reply = _raw_sign_over_socket(halted_sock, acct.address, p3)
        if (
            halted_reply.get("refused") is not True
            or halted_reply.get("reason_code") != "authority_halted"
        ):
            raise AssertionError(f"halted daemon failed to refuse sign: {halted_reply}")
        halted_client = ProfitAuthoritySocketClient(
            halted_sock, acct.address, loaded.policy, timeout=5.0
        )
        halted_status = halted_client.status()
        if halted_status["halted"] is not True:
            raise AssertionError("restarted daemon did not report halted=True")
        hl_out, hl_err = _finish_subprocess(halted_proc, expected_rc=0)
        _assert_no_secrets_leaked(hl_out + hl_err, key_hex=key_hex, signed_hexes=signed_hexes)

    if dir_path is None or dir_path.exists():
        raise AssertionError("ephemeral directory remained on disk after rehearsal exit")

    summary = {
        "max_capital_usd_micros": 500_000_000,
        "max_daily_loss_usd_micros": 200_000_000,
        "max_cumulative_loss_usd_micros": 500_000_000,
        "gate8_status_proof_verified": True,
        "gate7_dual_rpc_verified": True,
        "in_limit_signatures_used": mid_status["signatures_used"],
        "over_daily_loss_refused": True,
        "over_daily_loss_reason": over_loss_reason,
        "first_failure_halted": rec1_json["halted"],
        "two_failure_reconcile_halt_latched": rec2_json["halted"],
        "halt_persisted_across_reload": reload_json["halted"],
        "restarted_daemon_refused_sign": True,
        "ephemeral_dir_removed": True,
    }
    leak_guard = {"key_hex": key_hex, "signed_hexes": signed_hexes}
    return summary, leak_guard


def _rehearse_scaled_policy() -> tuple[dict[str, Any], dict[str, Any]]:
    env = _subprocess_env()
    signed_hexes: list[str] = []
    acct = Account.create()
    key_hex = acct.key.hex()
    dir_path: Path | None = None

    with _short_ephemeral_dir() as tmp_dir:
        root = Path(tmp_dir)
        dir_path = root
        os.chmod(root, 0o700)
        state_dir = root / "st"
        sock_dir = root / "sk"
        key_dir = root / "ky"
        for sub in (state_dir, sock_dir, key_dir):
            sub.mkdir(mode=0o700)
            os.chmod(sub, 0o700)

        policy_path = root / "policy_scaled.json"
        policy_dict = _build_policy_dict(
            operator_account=acct.address,
            max_capital_usd_micros=5_000_000_000,
            max_daily_loss_usd_micros=750_000_000,
            max_cumulative_loss_usd_micros=2_500_000_000,
        )
        policy_path.write_text(json.dumps(policy_dict, sort_keys=True), encoding="utf-8")
        loaded = load_atomic_execution_policy(policy_path)

        charter_policy = AtomicExecutionPolicy(
            chain_id=BASE_CHAIN_ID,
            operator_account=acct.address,
            executor_address=EXECUTOR,
            executor_code_hash=EXECUTOR_CODE_HASH,
            intent_vault_address=INTENT_VAULT,
            intent_vault_code_hash=INTENT_VAULT_CODE_HASH,
            allowed_pools=frozenset({POOL_V3, POOL_V2}),
            allowed_routers=frozenset({ROUTER}),
            allowed_reactors=frozenset({REACTOR}),
            allowed_profit_tokens=frozenset({TOKEN_USDC}),
            max_capital_usd_micros=500_000_000,
            max_daily_loss_usd_micros=200_000_000,
            max_cumulative_loss_usd_micros=500_000_000,
            release_gate_open=False,
        )

        now_s = int(time.time())
        scaled_in_limit = _build_proposal(
            account=acct.address,
            nonce=1,
            now_s=now_s,
            initial_amount_usdc=1_500_000_000,
            min_net_profit_raw=15_000_000,
            daily_loss_usd_micros=250_000_000,
            cumulative_loss_usd_micros=250_000_000,
        )
        charter_decision = admit_atomic_or_intent_proposal(
            policy=charter_policy,
            proposal=scaled_in_limit.envelope,
            simulation=scaled_in_limit.simulation,
            now_s=scaled_in_limit.now_s,
            daily_loss_usd_micros=scaled_in_limit.daily_loss_usd_micros,
            cumulative_loss_usd_micros=scaled_in_limit.cumulative_loss_usd_micros,
            revert_budget_status=scaled_in_limit.revert_budget_status,
            rehearsal_loopback_anvil=True,
        )
        if charter_decision.admitted or "daily_loss_limit_reached" not in charter_decision.reasons:
            raise AssertionError("expected charter default policy to block $250 daily loss")

        key_path = key_dir / "e.key"
        _write_private_key_file(key_path, key_hex)

        sign_sock = sock_dir / "sc.sock"
        sign_proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "scripts.run_profit_authority",
                "--allow-rehearsal-loopback-anvil",
                "--policy-config",
                str(policy_path),
                "--state-dir",
                str(state_dir),
                "--socket",
                str(sign_sock),
                "--key-file",
                str(key_path),
                "--max-signatures",
                "12",
                "--max-status-requests",
                "1",
                "--ttl-seconds",
                "30",
            ],
            cwd=REPO_ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        _wait_for_socket_and_optional_proof(sign_proc, sign_sock)
        client = ProfitAuthoritySocketClient(sign_sock, acct.address, loaded.policy, timeout=5.0)

        _tx_hash, tx_hex = _sign_proposal_via_socket(client, loaded.policy, scaled_in_limit)
        signed_hexes.append(tx_hex)

        p_over_scaled = _build_proposal(
            account=acct.address,
            nonce=2,
            now_s=now_s,
            initial_amount_usdc=1_500_000_000,
            min_net_profit_raw=15_000_000,
            daily_loss_usd_micros=800_000_000,
            cumulative_loss_usd_micros=800_000_000,
        )
        over_reason = _assert_over_daily_loss_refused(
            client=client,
            endpoint=sign_sock,
            account=acct.address,
            proposal=p_over_scaled,
        )

        final_status = client.status()
        if final_status["signatures_used"] != 1:
            used = final_status["signatures_used"]
            raise AssertionError(f"expected signatures_used==1 after scaled refusal, got {used}")
        sg_out, sg_err = _finish_subprocess(sign_proc, expected_rc=0)
        _assert_no_secrets_leaked(sg_out + sg_err, key_hex=key_hex, signed_hexes=signed_hexes)

    if dir_path is None or dir_path.exists():
        raise AssertionError("ephemeral directory remained on disk after scaled rehearsal exit")

    summary = {
        "max_capital_usd_micros": 5_000_000_000,
        "max_daily_loss_usd_micros": 750_000_000,
        "max_cumulative_loss_usd_micros": 2_500_000_000,
        "charter_default_blocked_250_loss": True,
        "scaled_1500_cap_250_loss_signed": True,
        "signatures_used": final_status["signatures_used"],
        "over_800_daily_loss_refused": True,
        "over_800_daily_loss_reason": over_reason,
        "ephemeral_dir_removed": True,
    }
    leak_guard = {"key_hex": key_hex, "signed_hexes": signed_hexes}
    return summary, leak_guard


def run_rehearsal() -> dict[str, Any]:
    """Run keyless ephemeral rehearsal across charter-default and scaled authority policies."""
    charter_summary, charter_secrets = _rehearse_charter_default()
    scaled_summary, scaled_secrets = _rehearse_scaled_policy()

    report: dict[str, Any] = {
        "schema_version": REHEARSAL_SCHEMA_VERSION,
        "passed": True,
        "execution_eligible": False,
        "keyless_ephemeral": True,
        "ephemeral_dirs_cleaned": True,
        "network_calls": 0,
        "policies": {
            "charter_default_500_200": charter_summary,
            "scaled_config_5000_750": scaled_summary,
        },
    }
    report["report_sha256"] = sha256_hex(canonical_json(report))

    rendered = json.dumps(report, sort_keys=True)
    _assert_no_secrets_leaked(
        rendered,
        key_hex=str(charter_secrets["key_hex"]),
        signed_hexes=list(charter_secrets["signed_hexes"]),
    )
    _assert_no_secrets_leaked(
        rendered,
        key_hex=str(scaled_secrets["key_hex"]),
        signed_hexes=list(scaled_secrets["signed_hexes"]),
    )
    return report


def _validate_out_path(path: Path) -> None:
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError(f"refusing symlink output path: {path}")
    if path.exists():
        raise ValueError(f"refusing to overwrite existing output path: {path}")


def build_parser() -> argparse.ArgumentParser:
    """Build CLI parser for configurable profit-authority rehearsal verification."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Optional deterministic JSON report output path (symlinks/existing files refused).",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Execute configurable profit-authority rehearsal and optionally write JSON evidence."""
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.out is not None and (
        args.out.is_symlink() or args.out.parent.is_symlink() or args.out.exists()
    ):
        parser.error("--out already exists or traverses a symlink")
    try:
        report = run_rehearsal()
        rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
        if args.out is not None:
            _validate_out_path(args.out)
            args.out.parent.mkdir(parents=True, exist_ok=True)
            _validate_out_path(args.out)
            with args.out.open("x", encoding="utf-8") as handle:
                handle.write(rendered)
        sys.stdout.write(rendered)
    except (
        OSError,
        ValueError,
        TypeError,
        RuntimeError,
        TimeoutError,
        AssertionError,
        subprocess.SubprocessError,
    ) as exc:
        print(f"Profit authority rehearsal failed: {exc}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
