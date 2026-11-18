"""Integration and security tests for profit-engine Unix socket authority & Compose."""

import ast
import copy
import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any

import pytest
from eth_abi.abi import encode
from eth_account import Account
from eth_hash.auto import keccak

from oasis_agent.execution.atomic_bundle import (
    AtomicCycleIntent,
    PrecheckGuardSpec,
    SwapLegSpec,
    encode_execute_cycle_calldata,
)
from oasis_agent.live.atomic_authority import (
    CYCLE_EXECUTED_TOPIC0,
    EXECUTE_REACTOR_FILL_PARAM_TYPES,
    EXECUTE_REACTOR_FILL_SELECTOR,
    EXECUTE_WITH_CALLBACK_SELECTOR,
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
    ProfitSigningAuthorityServer,
    build_authority_infra_proof,
    envelope_fields_from_proposal,
    make_local_key_signer,
    proposal_to_dict,
    read_frame,
    validate_socket_endpoint,
)
from oasis_agent.live.authority_socket import fields_json
from oasis_agent.live.canary_readiness import evaluate_profit_engine_readiness
from oasis_agent.live.intent import SubmittedTransaction
from oasis_agent.live.profit_strategy_daemon import (
    STRATEGY_DAEMON_SCHEMA,
    ProfitStrategyBridge,
    ProfitStrategyDaemonConfig,
)
from oasis_agent.models.common import canonical_json, sha256_hex
from oasis_agent.risk.capital_policy import CapitalRiskConfig, load_capital_risk_config
from oasis_agent.risk.revert_budget import (
    RollingRevertBudgetPolicy,
    RollingRevertBudgetTracker,
)
from scripts import run_profit_authority as authority_cli
from scripts import run_profit_strategy as strategy_cli
from scripts import verify_profit_compose as compose_cli
from scripts import verify_profit_docker_confinement as docker_confinement_cli

REPO_ROOT = Path(__file__).resolve().parents[2]
EXECUTOR = f"0x{0x11:040x}"
EXECUTOR_CODE_HASH = f"0x{0xAA:064x}"
INTENT_VAULT = f"0x{0x12:040x}"
INTENT_VAULT_CODE_HASH = f"0x{0xBB:064x}"
TOKEN_A = f"0x{0x22:040x}"
TOKEN_B = f"0x{0x33:040x}"
POOL_V3 = f"0x{0x55:040x}"
POOL_V2 = f"0x{0x66:040x}"
ROUTER = f"0x{0x77:040x}"
REACTOR = f"0x{0x88:040x}"
SIM_BLOCK_HASH = f"0x{0xDE:064x}"
RECEIPT_BLOCK_HASH = f"0x{0xFA:064x}"
UINT160_SQRT_1 = 1 << 96
NOW_S = 1_750_000_000


def _short_socket_dir() -> tempfile.TemporaryDirectory[str]:
    base = Path(tempfile.gettempdir()).resolve()
    return tempfile.TemporaryDirectory(prefix="oa-p-", dir=base)


def _make_policy(operator_account: str, *, release_gate_open: bool = True) -> AtomicExecutionPolicy:
    return AtomicExecutionPolicy(
        chain_id=8453,
        operator_account=operator_account,
        executor_address=EXECUTOR,
        executor_code_hash=EXECUTOR_CODE_HASH,
        intent_vault_address=INTENT_VAULT,
        intent_vault_code_hash=INTENT_VAULT_CODE_HASH,
        allowed_pools=frozenset({POOL_V3, POOL_V2}),
        allowed_routers=frozenset({ROUTER}),
        allowed_reactors=frozenset({REACTOR}),
        allowed_profit_tokens=frozenset({TOKEN_A}),
        max_gas_limit=650_000,
        max_fee_per_gas_wei=5_000_000_000,
        max_priority_fee_wei=100_000_000,
        max_deadline_horizon_s=24,
        release_gate_open=release_gate_open,
    )


def _policy_json_dict(policy: AtomicExecutionPolicy) -> dict[str, Any]:
    return {
        "chain_id": policy.chain_id,
        "operator_account": policy.operator_account,
        "executor_address": policy.executor_address,
        "executor_code_hash": policy.executor_code_hash,
        "intent_vault_address": policy.intent_vault_address,
        "intent_vault_code_hash": policy.intent_vault_code_hash,
        "allowed_pools": sorted(policy.allowed_pools),
        "allowed_routers": sorted(policy.allowed_routers),
        "allowed_reactors": sorted(policy.allowed_reactors),
        "allowed_profit_tokens": sorted(policy.allowed_profit_tokens),
        "max_capital_usd_micros": policy.max_capital_usd_micros,
        "max_daily_loss_usd_micros": policy.max_daily_loss_usd_micros,
        "max_cumulative_loss_usd_micros": policy.max_cumulative_loss_usd_micros,
        "max_gas_limit": policy.max_gas_limit,
        "max_fee_per_gas_wei": policy.max_fee_per_gas_wei,
        "max_priority_fee_wei": policy.max_priority_fee_wei,
        "max_deadline_horizon_s": policy.max_deadline_horizon_s,
        "release_gate_open": policy.release_gate_open,
        "isolated_key_volume": "oasis-canary-keys",
        "primary_rpc_provider": "base-publicnode-archive",
        "secondary_rpc_provider": "alchemy-base-archive",
        "secondary_rpc_verified": True,
    }


def _sample_simulation(
    *,
    primary_profit_delta_raw: int = 2_000_000,
    secondary_profit_delta_raw: int = 2_000_000,
    simulated_profit_value_wei: int = 600_000_000_000_000,
) -> DualSimulationProof:
    return DualSimulationProof(
        block_number=28_000_001,
        block_hash=SIM_BLOCK_HASH,
        primary_source="base-publicnode-archive",
        secondary_source="alchemy-base-archive",
        primary_reverted=False,
        secondary_reverted=False,
        primary_early_aborted=False,
        secondary_early_aborted=False,
        primary_gas_used=195_000,
        secondary_gas_used=196_000,
        primary_profit_delta_raw=primary_profit_delta_raw,
        secondary_profit_delta_raw=secondary_profit_delta_raw,
        l2_gas_fee_wei=200_000_000_000_000,
        l1_data_fee_wei=50_000_000_000_000,
        simulated_profit_value_wei=simulated_profit_value_wei,
    )


def _sample_executor_proposal(
    *,
    account: str,
    nonce: int = 1,
    min_net_profit_raw: int = 10**6,
    value_wei: int = 0,
    code_hash: str = EXECUTOR_CODE_HASH,
    simulated_profit_value_wei: int = 600_000_000_000_000,
    primary_profit_delta_raw: int = 2_000_000,
    secondary_profit_delta_raw: int = 2_000_000,
) -> AtomicOrIntentProposal:
    deadline_s = NOW_S + 12
    intent = AtomicCycleIntent(
        chain_id=8453,
        executor_address=EXECUTOR,
        executor_code_hash=EXECUTOR_CODE_HASH,
        nonce=7,
        deadline=deadline_s,
        profit_token=TOKEN_A,
        initial_amount=10**8,
        min_net_profit=max(0, min_net_profit_raw),
        guard=PrecheckGuardSpec(
            pool=POOL_V3,
            min_sqrt_price_x96=UINT160_SQRT_1 - 100,
            max_sqrt_price_x96=UINT160_SQRT_1 + 100,
        ),
        legs=(
            SwapLegSpec(
                pool=POOL_V3,
                token_in=TOKEN_A,
                token_out=TOKEN_B,
                zero_for_one=True,
                sqrt_price_limit_x96=UINT160_SQRT_1 - 1,
                v2_fee_bps=0,
            ),
            SwapLegSpec(
                pool=POOL_V2,
                token_in=TOKEN_B,
                token_out=TOKEN_A,
                zero_for_one=False,
                sqrt_price_limit_x96=0,
                v2_fee_bps=30,
            ),
        ),
    )
    calldata, _ = encode_execute_cycle_calldata(intent)
    envelope = EnvelopeProposal(
        chain_id=8453,
        account=account,
        nonce=nonce,
        target=EXECUTOR,
        target_observed_code_hash=code_hash,
        value_wei=value_wei,
        calldata=calldata,
        gas_limit=350_000,
        max_fee_per_gas_wei=1_000_000_000,
        max_priority_fee_wei=50_000_000,
        profit_token=TOKEN_A,
        min_net_profit_raw=min_net_profit_raw,
        referenced_pools=(POOL_V3, POOL_V2),
        referenced_routers=(),
        referenced_reactor=None,
        deadline_s=deadline_s,
    )
    return AtomicOrIntentProposal(
        envelope=envelope,
        simulation=_sample_simulation(
            primary_profit_delta_raw=primary_profit_delta_raw,
            secondary_profit_delta_raw=secondary_profit_delta_raw,
            simulated_profit_value_wei=simulated_profit_value_wei,
        ),
        now_s=NOW_S,
    )


def _sample_vault_proposal(*, account: str, nonce: int = 2) -> AtomicOrIntentProposal:
    router_sel = keccak(
        b"exactInputSingle((address,address,uint24,address,uint256,uint256,uint160))"
    )[:4]
    step_calldata = router_sel + encode(["uint256"], [12345])
    precheck = (POOL_V3, UINT160_SQRT_1 - 50, UINT160_SQRT_1 + 50)
    steps = [(ROUTER, POOL_V3, TOKEN_A, TOKEN_B, 50_000_000, 49_000_000, step_calldata)]
    plan = (REACTOR, TOKEN_A, 10**6, precheck, steps)
    reactor_calldata = EXECUTE_WITH_CALLBACK_SELECTOR + encode(
        ["bytes", "bytes"], [b"order", b"callback"]
    )
    calldata = EXECUTE_REACTOR_FILL_SELECTOR + encode(
        list(EXECUTE_REACTOR_FILL_PARAM_TYPES), [plan, reactor_calldata]
    )
    envelope = EnvelopeProposal(
        chain_id=8453,
        account=account,
        nonce=nonce,
        target=INTENT_VAULT,
        target_observed_code_hash=INTENT_VAULT_CODE_HASH,
        value_wei=0,
        calldata=calldata,
        gas_limit=420_000,
        max_fee_per_gas_wei=1_200_000_000,
        max_priority_fee_wei=40_000_000,
        profit_token=TOKEN_A,
        min_net_profit_raw=10**6,
        referenced_pools=(POOL_V3,),
        referenced_routers=(ROUTER,),
        referenced_reactor=REACTOR,
        deadline_s=NOW_S + 12,
    )
    return AtomicOrIntentProposal(
        envelope=envelope,
        simulation=_sample_simulation(),
        now_s=NOW_S,
    )


def _raw_socket_exchange(endpoint: Path, payload: dict[str, Any]) -> dict[str, Any]:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(5.0)
        conn.connect(str(endpoint))
        conn.sendall(canonical_json(payload) + b"\n")
        reply = read_frame(conn)
    decoded: dict[str, Any] = json.loads(reply)
    return decoded


def _make_receipt(
    *,
    source: str,
    tx_hash: str,
    target: str = EXECUTOR,
    status: int = 1,
    gas_used: int = 200_000,
    effective_gas_price_wei: int = 1_000_000_000,
    l1_fee_wei: int = 25_000_000_000_000,
) -> dict[str, Any]:
    logs = (
        [{"address": target, "topics": [CYCLE_EXECUTED_TOPIC0], "data": "0x"}]
        if status == 1
        else []
    )
    return {
        "source": source,
        "tx_hash": tx_hash,
        "block_number": 28_000_005,
        "block_hash": RECEIPT_BLOCK_HASH,
        "status": status,
        "gas_used": gas_used,
        "effective_gas_price_wei": effective_gas_price_wei,
        "l1_fee_wei": l1_fee_wei,
        "logs": logs,
    }


def _reconciliation_payload(
    *,
    tx_hash: str,
    status: int = 1,
    native_residual_wei: int = 0,
    erc20_profit_delta_raw: int = 2_000_000,
) -> dict[str, Any]:
    gas_used = 200_000
    gas_price = 1_000_000_000
    l1_fee = 25_000_000_000_000
    expected_spend = gas_used * gas_price + l1_fee
    native_before = 1_000_000_000_000_000_000
    native_after = native_before - expected_spend - native_residual_wei
    return {
        "tx_hash": tx_hash,
        "primary_receipt": _make_receipt(
            source="base-publicnode-archive",
            tx_hash=tx_hash,
            status=status,
            gas_used=gas_used,
            effective_gas_price_wei=gas_price,
            l1_fee_wei=l1_fee,
        ),
        "secondary_receipt": _make_receipt(
            source="alchemy-base-archive",
            tx_hash=tx_hash,
            status=status,
            gas_used=gas_used,
            effective_gas_price_wei=gas_price,
            l1_fee_wei=l1_fee,
        ),
        "native_balance_before_wei": native_before,
        "native_balance_after_wei": native_after,
        "profit_token_balance_before_raw": 10_000_000,
        "profit_token_balance_after_raw": 10_000_000 + erc20_profit_delta_raw,
        "gas_cost_usd_micros": 50_000,
        "realized_profit_usd_micros": 500_000 if status == 1 else 0,
        "timestamp_s": NOW_S + 5,
    }


def test_af_unix_happy_path_executor_and_intent_vault_plus_persistence() -> None:
    acct = Account.create()
    policy = _make_policy(acct.address)
    signer_cb = make_local_key_signer(bytes(acct.key), acct.address)

    with _short_socket_dir() as td:
        root = Path(td)
        os.chmod(root, 0o700)
        state_dir = root / "st"
        state_dir.mkdir(mode=0o700)
        endpoint = root / "a.sock"

        with ProfitSigningAuthorityServer(
            policy=policy,
            state_dir=state_dir,
            endpoint=endpoint,
            key_signer=signer_cb,
            max_signatures=5,
            ttl_seconds=30,
            now_fn=lambda: NOW_S,
        ) as server:
            worker = threading.Thread(target=lambda: server.serve(max_requests=4))
            worker.start()
            try:
                client = ProfitAuthoritySocketClient(endpoint, acct.address, policy, timeout=5.0)
                st0 = client.status()
                assert st0["schema"] == PROFIT_AUTHORITY_SCHEMA
                assert st0["account"] == acct.address.lower()
                assert st0["signatures_used"] == 0
                assert st0["max_signatures"] == 5
                assert st0["last_nonce"] is None
                assert st0["halted"] is False
                assert st0["stop_present"] is False
                assert st0["signing_enabled"] is True

                exec_prop = _sample_executor_proposal(account=acct.address, nonce=1)
                exec_fields = envelope_fields_from_proposal(exec_prop)
                raw_exec = client.sign_admitted_proposal(exec_prop, exec_fields)
                exec_admission = admit_atomic_or_intent_proposal(
                    policy=policy,
                    proposal=exec_prop.envelope,
                    simulation=exec_prop.simulation,
                    now_s=NOW_S,
                    daily_loss_usd_micros=0,
                    cumulative_loss_usd_micros=0,
                    revert_budget_status=exec_prop.revert_budget_status,
                )
                sub_exec = verify_signed_bytes_match_admission(raw_exec, decision=exec_admission)
                assert sub_exec.account == acct.address.lower()
                assert sub_exec.nonce == 1

                vault_prop = _sample_vault_proposal(account=acct.address, nonce=2)
                vault_fields = envelope_fields_from_proposal(vault_prop)
                raw_vault = client.sign_admitted_proposal(vault_prop, vault_fields)
                vault_admission = admit_atomic_or_intent_proposal(
                    policy=policy,
                    proposal=vault_prop.envelope,
                    simulation=vault_prop.simulation,
                    now_s=NOW_S,
                    daily_loss_usd_micros=0,
                    cumulative_loss_usd_micros=0,
                    revert_budget_status=vault_prop.revert_budget_status,
                )
                sub_vault = verify_signed_bytes_match_admission(raw_vault, decision=vault_admission)
                assert sub_vault.account == acct.address.lower()
                assert sub_vault.nonce == 2

                st1 = client.status()
                assert st1["signatures_used"] == 2
                assert st1["last_nonce"] == 2
            finally:
                worker.join(timeout=6.0)
            assert not worker.is_alive()

        state_info = (state_dir / "profit_authority_state.json").lstat()
        assert stat.S_IMODE(state_info.st_mode) == 0o600

        # Restarting server reloads persisted nonce and signature count cleanly.
        restarted = ProfitSigningAuthorityServer(
            policy=policy,
            state_dir=state_dir,
            endpoint=endpoint,
            key_signer=signer_cb,
            max_signatures=5,
            ttl_seconds=30,
            require_existing_state=True,
            now_fn=lambda: NOW_S,
        )
        assert restarted.current_status()["signatures_used"] == 2
        assert restarted.current_status()["last_nonce"] == 2


def test_fail_closed_refusals_negative_profit_codehash_nonzero_value_stop_budget_nonce_status() -> (
    None
):
    acct = Account.create()
    policy = _make_policy(acct.address)
    signer_cb = make_local_key_signer(bytes(acct.key), acct.address)

    with _short_socket_dir() as td:
        root = Path(td)
        os.chmod(root, 0o700)
        state_dir = root / "st"
        state_dir.mkdir(mode=0o700)
        endpoint = root / "b.sock"

        # 1. Client refuses invalid/unprofitable proposals BEFORE touching the socket.
        client = ProfitAuthoritySocketClient(endpoint, acct.address, policy, timeout=5.0)
        bad_profit = _sample_executor_proposal(account=acct.address, nonce=1, min_net_profit_raw=-1)
        with pytest.raises(ValueError, match="nonpositive_min_net_profit"):
            client.sign_admitted_proposal(bad_profit, envelope_fields_from_proposal(bad_profit))

        bad_gas_profit = _sample_executor_proposal(
            account=acct.address,
            nonce=1,
            simulated_profit_value_wei=100_000_000_000_000,  # <= 250_000_000_000_000 L1+L2 gas
        )
        with pytest.raises(ValueError, match="simulated_profit_below_l1_l2_gas_cost"):
            client.sign_admitted_proposal(
                bad_gas_profit, envelope_fields_from_proposal(bad_gas_profit)
            )

        bad_codehash = _sample_executor_proposal(
            account=acct.address, nonce=1, code_hash=f"0x{0xCC:064x}"
        )
        with pytest.raises(ValueError, match="executor_code_hash_mismatch"):
            client.sign_admitted_proposal(bad_codehash, envelope_fields_from_proposal(bad_codehash))

        nonzero_val = _sample_executor_proposal(account=acct.address, nonce=1, value_wei=1)
        with pytest.raises(ValueError, match="nonzero_transaction_value"):
            client.sign_admitted_proposal(nonzero_val, envelope_fields_from_proposal(nonzero_val))

        # 2. Direct socket checks against server for those same violations + STOP, budget, nonce.
        with ProfitSigningAuthorityServer(
            policy=policy,
            state_dir=state_dir,
            endpoint=endpoint,
            key_signer=signer_cb,
            max_signatures=1,
            ttl_seconds=30,
            now_fn=lambda: NOW_S,
        ) as server:
            worker = threading.Thread(target=lambda: server.serve(max_requests=8))
            worker.start()
            try:
                for bad_prop, expected_code in (
                    (bad_profit, "nonpositive_min_net_profit"),
                    (bad_codehash, "executor_code_hash_mismatch"),
                    (nonzero_val, "nonzero_transaction_value"),
                ):
                    reply = _raw_socket_exchange(
                        endpoint,
                        {
                            "schema": PROFIT_AUTHORITY_SCHEMA,
                            "account": acct.address.lower(),
                            "operation": "sign",
                            "proposal": proposal_to_dict(bad_prop),
                            "fields": fields_json(envelope_fields_from_proposal(bad_prop)),
                        },
                    )
                    assert reply == {
                        "schema": PROFIT_AUTHORITY_SCHEMA,
                        "refused": True,
                        "reason_code": expected_code,
                    }

                # STOP file present blocks signing and reports stop_present=True in status().
                stop_file = state_dir / "STOP"
                stop_file.touch()
                st_stop = client.status()
                assert st_stop["stop_present"] is True

                good_n5 = _sample_executor_proposal(account=acct.address, nonce=5)
                with pytest.raises(ValueError, match="stop_file_present"):
                    client.sign_admitted_proposal(good_n5, envelope_fields_from_proposal(good_n5))
                stop_file.unlink()

                # Valid sign with nonce=5 consumes the 1-signature budget.
                raw_ok = client.sign_admitted_proposal(
                    good_n5, envelope_fields_from_proposal(good_n5)
                )
                assert SubmittedTransaction.from_signed(raw_ok).nonce == 5

                # Exhausted max_signatures refuses next sign.
                good_n6 = _sample_executor_proposal(account=acct.address, nonce=6)
                with pytest.raises(ValueError, match="signature_budget_exhausted"):
                    client.sign_admitted_proposal(good_n6, envelope_fields_from_proposal(good_n6))

                # Status remains available after signature budget exhaustion.
                assert client.status()["signatures_used"] == 1
            finally:
                worker.join(timeout=6.0)
            assert not worker.is_alive()

        # 3. Stale nonce refused when max_signatures allows more signatures.
        with ProfitSigningAuthorityServer(
            policy=policy,
            state_dir=state_dir,
            endpoint=endpoint,
            key_signer=signer_cb,
            max_signatures=5,
            ttl_seconds=30,
            require_existing_state=True,
            now_fn=lambda: NOW_S,
        ) as server:
            worker = threading.Thread(target=lambda: server.serve(max_requests=2))
            worker.start()
            try:
                stale_n5 = _sample_executor_proposal(account=acct.address, nonce=5)
                with pytest.raises(ValueError, match="stale_nonce"):
                    client.sign_admitted_proposal(stale_n5, envelope_fields_from_proposal(stale_n5))
                stale_n4 = _sample_executor_proposal(account=acct.address, nonce=4)
                with pytest.raises(ValueError, match="stale_nonce"):
                    client.sign_admitted_proposal(stale_n4, envelope_fields_from_proposal(stale_n4))
            finally:
                worker.join(timeout=6.0)
            assert not worker.is_alive()

        # 4. Status-only mode holds no key_signer and refuses sign & settle.
        status_endpoint = root / "s.sock"
        with ProfitSigningAuthorityServer(
            policy=policy,
            state_dir=state_dir,
            endpoint=status_endpoint,
            key_signer=None,
            max_signatures=5,
            ttl_seconds=30,
            status_only=True,
            now_fn=lambda: NOW_S,
        ) as status_srv:
            worker = threading.Thread(target=lambda: status_srv.serve(max_status_requests=3))
            worker.start()
            try:
                status_client = ProfitAuthoritySocketClient(
                    status_endpoint, acct.address, policy, timeout=5.0
                )
                st = status_client.status()
                assert st["signing_enabled"] is False
                assert st["signatures_used"] == 1

                prop_n6 = _sample_executor_proposal(account=acct.address, nonce=6)
                with pytest.raises(ValueError, match="signing_disabled"):
                    status_client.sign_admitted_proposal(
                        prop_n6, envelope_fields_from_proposal(prop_n6)
                    )
                with pytest.raises(ValueError, match="signing_disabled"):
                    status_client.record_settlement(_reconciliation_payload(tx_hash=f"0x{1:064x}"))
            finally:
                worker.join(timeout=6.0)
            assert not worker.is_alive()


def test_settlement_reconciliation_latches_halt_on_residual_and_deep_revert() -> None:
    acct = Account.create()
    policy = _make_policy(acct.address)
    signer_cb = make_local_key_signer(bytes(acct.key), acct.address)

    with _short_socket_dir() as td:
        root = Path(td)
        os.chmod(root, 0o700)
        state_dir = root / "st"
        state_dir.mkdir(mode=0o700)
        endpoint = root / "c.sock"

        with ProfitSigningAuthorityServer(
            policy=policy,
            state_dir=state_dir,
            endpoint=endpoint,
            key_signer=signer_cb,
            revert_tracker=RollingRevertBudgetTracker(
                RollingRevertBudgetPolicy(
                    max_hourly_gas_burn_usd_micros=5_000_000,
                    max_deep_reverts_per_hour=2,
                )
            ),
            max_signatures=5,
            ttl_seconds=30,
            now_fn=lambda: NOW_S,
        ) as server:
            worker = threading.Thread(target=lambda: server.serve(max_requests=6))
            worker.start()
            try:
                client = ProfitAuthoritySocketClient(endpoint, acct.address, policy, timeout=5.0)
                p1 = _sample_executor_proposal(account=acct.address, nonce=1)
                raw1 = client.sign_admitted_proposal(p1, envelope_fields_from_proposal(p1))
                tx1 = SubmittedTransaction.from_signed(raw1).transaction_hash

                # Clean reconciliation leaves halted=False.
                res1 = client.record_settlement(
                    _reconciliation_payload(tx_hash=tx1, status=1, native_residual_wei=0)
                )
                assert res1["reconciled"] is True
                assert res1["halted"] is False
                assert res1["native_residual_wei"] == 0

                # Second transaction reconciles with 1 wei residual -> latches halted=True.
                p2 = _sample_executor_proposal(account=acct.address, nonce=2)
                raw2 = client.sign_admitted_proposal(p2, envelope_fields_from_proposal(p2))
                tx2 = SubmittedTransaction.from_signed(raw2).transaction_hash
                res2 = client.record_settlement(
                    _reconciliation_payload(tx_hash=tx2, status=1, native_residual_wei=1)
                )
                assert res2["reconciled"] is False
                assert res2["halt_latch"] is True
                assert res2["halted"] is True
                assert res2["native_residual_wei"] == 1

                assert client.status()["halted"] is True

                # New signing attempt must be refused once halted.
                p3 = _sample_executor_proposal(account=acct.address, nonce=3)
                with pytest.raises(ValueError, match="authority_halted"):
                    client.sign_admitted_proposal(p3, envelope_fields_from_proposal(p3))
            finally:
                worker.join(timeout=6.0)
            assert not worker.is_alive()

        # Deep revert also latches halted=True even on a fresh server with max_deep_reverts=2.
        state_dir2 = root / "s2"
        state_dir2.mkdir(mode=0o700)
        ep2 = root / "d.sock"
        with ProfitSigningAuthorityServer(
            policy=policy,
            state_dir=state_dir2,
            endpoint=ep2,
            key_signer=signer_cb,
            max_signatures=5,
            ttl_seconds=30,
            now_fn=lambda: NOW_S,
        ) as srv2:
            worker2 = threading.Thread(target=lambda: srv2.serve(max_requests=3))
            worker2.start()
            try:
                c2 = ProfitAuthoritySocketClient(ep2, acct.address, policy, timeout=5.0)
                p = _sample_executor_proposal(account=acct.address, nonce=1)
                raw = c2.sign_admitted_proposal(p, envelope_fields_from_proposal(p))
                tx_h = SubmittedTransaction.from_signed(raw).transaction_hash
                res = c2.record_settlement(
                    _reconciliation_payload(
                        tx_hash=tx_h, status=0, native_residual_wei=0, erc20_profit_delta_raw=0
                    )
                )
                assert res["outcome"] == "deep_execution_revert"
                assert res["halted"] is True
                p_next = _sample_executor_proposal(account=acct.address, nonce=2)
                with pytest.raises(ValueError, match="authority_halted"):
                    c2.sign_admitted_proposal(p_next, envelope_fields_from_proposal(p_next))
            finally:
                worker2.join(timeout=6.0)
            assert not worker2.is_alive()


def test_wrong_signer_rejected_and_socket_permissions_enforced() -> None:
    acct = Account.create()
    rogue = Account.create()
    policy = _make_policy(acct.address)
    rogue_signer = make_local_key_signer(bytes(rogue.key), rogue.address)

    with _short_socket_dir() as td:
        root = Path(td)
        os.chmod(root, 0o700)
        state_dir = root / "st"
        state_dir.mkdir(mode=0o700)
        endpoint = root / "e.sock"

        with ProfitSigningAuthorityServer(
            policy=policy,
            state_dir=state_dir,
            endpoint=endpoint,
            key_signer=rogue_signer,
            max_signatures=3,
            ttl_seconds=30,
            now_fn=lambda: NOW_S,
        ) as server:
            worker = threading.Thread(target=lambda: server.serve(max_requests=2))
            worker.start()
            try:
                client = ProfitAuthoritySocketClient(endpoint, acct.address, policy, timeout=5.0)
                prop = _sample_executor_proposal(account=acct.address, nonce=1)
                with pytest.raises(ValueError, match="signed_bytes_verification_failed"):
                    client.sign_admitted_proposal(prop, envelope_fields_from_proposal(prop))
                assert client.status()["signatures_used"] == 0
            finally:
                worker.join(timeout=6.0)
            assert not worker.is_alive()

        # Insecure socket permissions (0644 instead of 0600) must be rejected.
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            bad_ep = root / "bad.sock"
            s.bind(str(bad_ep))
            bad_ep.chmod(0o644)
            with pytest.raises(ValueError, match="private and owned"):
                validate_socket_endpoint(bad_ep)


def test_infra_proof_gate8_and_cli_least_privilege() -> None:
    acct = Account.create()
    policy = _make_policy(acct.address)

    with _short_socket_dir() as td:
        root = Path(td)
        os.chmod(root, 0o700)
        state_dir = root / "st"
        state_dir.mkdir(mode=0o700)
        policy_path = root / "policy.json"
        policy_path.write_text(json.dumps(_policy_json_dict(policy)), encoding="utf-8")
        key_path = root / "canary.key"
        key_path.write_text(acct.key.hex(), encoding="ascii")
        key_path.chmod(0o600)
        proof_path = root / "infra_proof.json"
        endpoint = root / "p.sock"

        # CLI refuses --key-file combined with --status-only or --reconcile-only.
        for mode_flag in ("--status-only", "--reconcile-only"):
            with pytest.raises(SystemExit) as exc_info:
                authority_cli.main(
                    [
                        "--policy-config",
                        str(policy_path),
                        "--state-dir",
                        str(state_dir),
                        "--socket",
                        str(endpoint),
                        "--key-file",
                        str(key_path),
                        mode_flag,
                    ]
                )
            assert exc_info.value.code == 2

        # Run status-only CLI in subprocess and emit --out-proof without loading any key.
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO_ROOT / "src")
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "scripts.run_profit_authority",
                "--policy-config",
                str(policy_path),
                "--state-dir",
                str(state_dir),
                "--socket",
                str(endpoint),
                "--status-only",
                "--max-status-requests",
                "1",
                "--ttl-seconds",
                "1",
                "--out-proof",
                str(proof_path),
            ],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr
        assert acct.key.hex() not in proc.stdout
        assert acct.key.hex() not in proc.stderr

        proof = json.loads(proof_path.read_text(encoding="utf-8"))
        assert proof["schema"] == PROFIT_AUTHORITY_INFRA_SCHEMA
        assert proof["execution_eligible"] is False
        assert proof["isolated_signer_verified"] is True
        assert proof["isolated_key_volume"] == "oasis-canary-keys"
        assert proof["authority_socket_path"] == str(endpoint)

        # Compatibility check against canary_readiness.py Gates 7 and 8.
        readiness = evaluate_profit_engine_readiness(
            contracts_out_dir=root / "absent-contracts",
            infra_proof_path=proof_path,
        )
        gate_map = {gate.name: gate for gate in readiness.gates}
        assert gate_map["dual_rpc_quorum_verified"].passed is True
        assert gate_map["isolated_signer_authority_ready"].passed is True
        assert readiness.execution_eligible is False

        # Direct builder refuses halted status or placeholder RPC metadata.
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as bound_sock:
            bound_sock.bind(str(endpoint))
            endpoint.chmod(0o600)
            halted_status = {
                "schema": PROFIT_AUTHORITY_SCHEMA,
                "account": acct.address.lower(),
                "signatures_used": 0,
                "max_signatures": 12,
                "last_nonce": None,
                "halted": True,
                "stop_present": False,
            }
            with pytest.raises(ValueError, match="halted"):
                build_authority_infra_proof(
                    socket_path=endpoint,
                    account=acct.address,
                    isolated_key_volume="oasis-canary-keys",
                    primary_rpc_provider="base-publicnode-archive",
                    secondary_rpc_provider="alchemy-base-archive",
                    secondary_rpc_verified=True,
                    authority_status=halted_status,
                )
            ok_status = {**halted_status, "halted": False}
            with pytest.raises(ValueError, match="distinct"):
                build_authority_infra_proof(
                    socket_path=endpoint,
                    account=acct.address,
                    isolated_key_volume="oasis-canary-keys",
                    primary_rpc_provider="base-publicnode-archive",
                    secondary_rpc_provider="base-publicnode-archive",
                    secondary_rpc_verified=True,
                    authority_status=ok_status,
                )


def test_cli_reconcile_only_without_key_latches_halt_on_residual() -> None:
    acct = Account.create()
    policy = _make_policy(acct.address)
    signer_cb = make_local_key_signer(bytes(acct.key), acct.address)

    with _short_socket_dir() as td:
        root = Path(td)
        os.chmod(root, 0o700)
        state_dir = root / "st"
        state_dir.mkdir(mode=0o700)
        policy_path = root / "policy.json"
        policy_path.write_text(json.dumps(_policy_json_dict(policy)), encoding="utf-8")
        endpoint = root / "r.sock"

        # Sign one proposal cleanly in-process to populate persistent state.
        with ProfitSigningAuthorityServer(
            policy=policy,
            state_dir=state_dir,
            endpoint=endpoint,
            key_signer=signer_cb,
            max_signatures=5,
            ttl_seconds=30,
            now_fn=lambda: NOW_S,
        ) as srv:
            worker = threading.Thread(target=lambda: srv.serve(max_requests=1))
            worker.start()
            try:
                client = ProfitAuthoritySocketClient(endpoint, acct.address, policy, timeout=5.0)
                prop = _sample_executor_proposal(account=acct.address, nonce=1)
                raw = client.sign_admitted_proposal(prop, envelope_fields_from_proposal(prop))
                tx_hash = SubmittedTransaction.from_signed(raw).transaction_hash
            finally:
                worker.join(timeout=6.0)
            assert not worker.is_alive()

        # Reconcile offline via CLI --reconcile-only with a 1-wei native residual -> exit 3 & halt.
        settle_path = root / "settle.json"
        settle_path.write_text(
            json.dumps(_reconciliation_payload(tx_hash=tx_hash, status=1, native_residual_wei=1)),
            encoding="utf-8",
        )
        rc = authority_cli.main(
            [
                "--policy-config",
                str(policy_path),
                "--state-dir",
                str(state_dir),
                "--reconcile-only",
                "--settlement-input",
                str(settle_path),
            ]
        )
        assert rc == 3
        rc_status = authority_cli.main(
            [
                "--policy-config",
                str(policy_path),
                "--state-dir",
                str(state_dir),
                "--reconcile-only",
            ]
        )
        assert rc_status == 3


def test_verify_profit_compose_inspect_and_confinement_regressions() -> None:
    report = compose_cli.inspect(compose_file=REPO_ROOT / "deploy/profit-engine/compose.yaml")
    assert report["schema"] == "oasis.profit-compose-policy-verification.v1"
    assert report["passed"] is True
    assert report["execution_eligible"] is False
    assert report["network_calls"] == 0
    assert set(report["services"]) == {
        "initialize",
        "profit-authority",
        "profit-strategy",
        "profit-authority-status",
        "profit-authority-recovery",
        "profit-strategy-recovery",
    }

    risk_src = str((REPO_ROOT / compose_cli.RISK_CONFIG_SOURCE_REL).resolve())
    # Build a synthetic expanded document and ensure every confinement regression fails closed.
    services: dict[str, Any] = {}
    for name, mounts in compose_cli.MOUNTS.items():
        setup = name == "initialize"
        offline = setup or name == "profit-authority-status"
        vol_entries: list[dict[str, Any]] = []
        for target, source in mounts.items():
            if target == compose_cli.RISK_CONFIG_TARGET:
                vol_entries.append(
                    {
                        "type": "bind",
                        "source": risk_src,
                        "target": target,
                        "read_only": True,
                    }
                )
            else:
                vol_entries.append(
                    {
                        "type": "volume",
                        "source": source,
                        "target": target,
                        "read_only": target == compose_cli.READONLY_KEYS_TARGET,
                    }
                )
        service: dict[str, Any] = {
            "user": "0:0" if setup else "10001:10001",
            "read_only": True,
            "cap_drop": ["ALL"],
            "cap_add": ["CHOWN", "FOWNER", "DAC_OVERRIDE"] if setup else [],
            "security_opt": ["no-new-privileges:true"],
            "restart": "no",
            "pids_limit": 128,
            "mem_limit": "536870912",
            "tmpfs": [f"/{'tmp'}{compose_cli.TMPFS_OPT_SUFFIX}"],
            "environment": {} if offline else dict.fromkeys(compose_cli.RPC_NAMES),
            "volumes": vol_entries,
            "command": list(compose_cli.COMMANDS[name]),
        }
        if setup:
            service["profiles"] = ["setup"]
        elif name not in {"profit-authority", "profit-strategy"}:
            service["profiles"] = ["recovery"]
        if offline:
            service["network_mode"] = "none"
        services[name] = service

    doc: dict[str, Any] = {
        "services": services,
        "volumes": {"canary-keys": {"external": True, "name": "oasis-canary-keys"}},
    }
    compose_cli.verify_services(
        doc,
        dict.fromkeys(compose_cli.RPC_NAMES),
        expected_risk_config=REPO_ROOT / compose_cli.RISK_CONFIG_SOURCE_REL,
    )

    for fault in (
        "strategy_key_mount",
        "strategy_authority_state",
        "recovery_authority_state",
        "writable_secrets",
        "writable_risk_config",
        "rogue_bind_source",
        "strategy_live_mode",
        "root_strategy",
        "networked_status",
        "secret_env",
        "recovery_key_flag",
        "non_external_keys",
    ):
        broken = copy.deepcopy(doc)
        if fault == "strategy_key_mount":
            broken["services"]["profit-strategy"]["volumes"].append(
                copy.deepcopy(broken["services"]["profit-authority"]["volumes"][-1])
            )
        elif fault == "strategy_authority_state":
            broken["services"]["profit-strategy"]["volumes"][0]["source"] = "profit-authority-state"
        elif fault == "recovery_authority_state":
            broken["services"]["profit-strategy-recovery"]["volumes"][0]["source"] = (
                "profit-authority-state"
            )
        elif fault == "writable_secrets":
            broken["services"]["profit-authority"]["volumes"][-1]["read_only"] = False
        elif fault == "writable_risk_config":
            broken["services"]["profit-strategy"]["volumes"][-1]["read_only"] = False
        elif fault == "rogue_bind_source":
            broken["services"]["profit-strategy"]["volumes"][-1]["source"] = str(
                REPO_ROOT / ".env.example"
            )
        elif fault == "strategy_live_mode":
            cmd = broken["services"]["profit-strategy"]["command"]
            cmd[cmd.index("shadow")] = "live"
        elif fault == "root_strategy":
            broken["services"]["profit-strategy"]["user"] = "0:0"
        elif fault == "networked_status":
            broken["services"]["profit-authority-status"].pop("network_mode")
        elif fault == "secret_env":
            leaked_env = "OASIS_PRIVATE_" + "KEY"
            broken["services"]["profit-strategy"]["environment"][leaked_env] = "synthetic-marker"
        elif fault == "recovery_key_flag":
            broken["services"]["profit-authority-recovery"]["command"] += ["--key-file", "/k"]
        else:
            broken["volumes"]["canary-keys"]["external"] = False
        with pytest.raises(ValueError):
            compose_cli.verify_services(
                broken,
                dict.fromkeys(compose_cli.RPC_NAMES),
                expected_risk_config=REPO_ROOT / compose_cli.RISK_CONFIG_SOURCE_REL,
            )

    dockerfile = (REPO_ROOT / "deploy/profit-engine/Dockerfile").read_text(encoding="utf-8")
    assert "rust:1-slim-bookworm" in dockerfile
    assert "rust:1.85-slim-bookworm" not in dockerfile
    assert "cargo build --locked --manifest-path crates/Cargo.toml --release" in dockerfile
    assert "OASIS_FASTPATH_LIB=/app/crates/target/release/liboasis_core.so" in dockerfile
    risk_copy = (
        "COPY configs/profit-engine-risk.example.json /app/configs/profit-engine-risk.example.json"
    )
    assert risk_copy in dockerfile
    assert dockerfile.index(risk_copy) < dockerfile.index("USER 10001:10001")

    dockerignore = (REPO_ROOT / "deploy/profit-engine/Dockerfile.dockerignore").read_text(
        encoding="utf-8"
    )
    dockerignore_lines = dockerignore.splitlines()
    for expected_entry in (
        "**",
        "!crates/",
        "!crates/**",
        "crates/target/",
        "crates/**/target/",
        "!src/",
        "!src/**",
        "!scripts/",
        "!scripts/**",
        "!configs/",
        "!configs/live.rpc.canary.json",
        "!configs/profit-engine-risk.example.json",
        "!requirements.lock",
        "!pyproject.toml",
        ".git",
        ".venv",
        "contracts",
        "reports",
        ".env*",
        "**/keys*",
        "**/__pycache__",
        "**/*.pyc",
        "**/.DS_Store",
    ):
        assert expected_entry in dockerignore_lines


def test_profit_strategy_modules_never_import_private_keys_or_eth_account() -> None:
    forbidden_tokens = {
        "eth_account",
        "load_private_key",
        "make_local_key_signer",
        "sign_digest",
        "sign_transaction",
        "dotenv",
    }
    tmp_marker = "/" + "tmp"
    for rel_path in (
        "src/oasis_agent/live/profit_strategy_daemon.py",
        "scripts/run_profit_strategy.py",
    ):
        source = (REPO_ROOT / rel_path).read_text(encoding="utf-8")
        assert tmp_marker not in source
        tree = ast.parse(source)
        for node in ast.walk(tree):
            assert not isinstance(node, ast.Assert)
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not any(tok in alias.name for tok in forbidden_tokens)
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                assert not any(tok in mod for tok in forbidden_tokens)
                for alias in node.names:
                    assert alias.name not in forbidden_tokens


class _SyntheticAuthorityDouble:
    def __init__(self, status_payload: dict[str, Any] | Exception) -> None:
        self._payload = status_payload
        self.sign_calls = 0
        self.settle_calls = 0

    def status(self) -> dict[str, Any]:
        if isinstance(self._payload, Exception):
            raise self._payload
        return dict(self._payload)

    def sign_admitted_proposal(
        self,
        proposal: AtomicOrIntentProposal,
        envelope_fields: list[object],
    ) -> bytes:
        del proposal, envelope_fields
        self.sign_calls += 1
        raise AssertionError("sign_admitted_proposal must not be called")

    def record_settlement(self, reconciliation_input: dict[str, Any]) -> dict[str, Any]:
        del reconciliation_input
        self.settle_calls += 1
        raise AssertionError("record_settlement must not be called")


def test_profit_strategy_bridge_shadow_status_headroom_and_fail_closed_gates() -> None:
    acct = Account.create()
    policy = _make_policy(acct.address)
    risk_path = REPO_ROOT / "configs/profit-engine-risk.example.json"
    risk_cfg = load_capital_risk_config(risk_path)

    with _short_socket_dir() as td:
        root = Path(td)
        os.chmod(root, 0o700)
        auth_state_dir = root / "auth"
        strat_state_dir = root / "strat"
        auth_state_dir.mkdir(mode=0o700)
        strat_state_dir.mkdir(mode=0o700)
        endpoint = root / "p.sock"

        # Missing socket fails closed immediately.
        cfg_missing = ProfitStrategyDaemonConfig(
            authority_socket=endpoint,
            mode="shadow",
            risk_config_path=risk_path,
            policy=policy,
            state_dir=strat_state_dir,
        )
        bridge_missing = ProfitStrategyBridge(cfg_missing)
        missing_snap = bridge_missing.sync_status()
        assert missing_snap.authority_reachable is False
        assert missing_snap.can_deploy is False
        assert missing_snap.deployable_notional_usd_micros == 0
        assert missing_snap.blocked_reason == "authority_status_unavailable"
        assert missing_snap.execution_eligible is False
        assert missing_snap.release_permitted is False

        with ProfitSigningAuthorityServer(
            policy=policy,
            state_dir=auth_state_dir,
            endpoint=endpoint,
            key_signer=make_local_key_signer(bytes(acct.key), acct.address),
            max_signatures=5,
            ttl_seconds=30,
            now_fn=lambda: NOW_S,
        ) as server:
            worker = threading.Thread(target=lambda: server.serve(max_requests=4))
            worker.start()
            try:
                snap_ok = bridge_missing.sync_status(tick=1)
                assert snap_ok.schema == STRATEGY_DAEMON_SCHEMA
                assert snap_ok.authority_reachable is True
                assert snap_ok.can_deploy is True
                assert snap_ok.blocked_reason is None
                assert snap_ok.deployable_notional_usd_micros == 225_000_000
                assert snap_ok.headroom.remaining_daily_loss_usd_micros == (
                    risk_cfg.max_daily_loss_usd_micros
                )
                assert snap_ok.execution_eligible is False
                assert snap_ok.release_permitted is False

                # Shadow mode NEVER calls sign_admitted_proposal or broadcast.
                prop = _sample_executor_proposal(account=acct.address, nonce=1)
                broadcast_called = False

                def _never_broadcast(_raw: bytes, _sub: SubmittedTransaction) -> str:
                    nonlocal broadcast_called
                    broadcast_called = True
                    return f"0x{0x11:064x}"

                shadow_res = bridge_missing.submit_dual_simulated_intent(
                    proposal=prop,
                    broadcast_fn=_never_broadcast,
                )
                assert shadow_res.status == "shadow_blocked"
                assert shadow_res.signed is False
                assert shadow_res.broadcast is False
                assert shadow_res.execution_eligible is False
                assert shadow_res.release_permitted is False
                assert broadcast_called is False

                # STOP file active blocks new exposure fail-closed.
                stop_file = auth_state_dir / "STOP"
                stop_file.touch()
                snap_stop = bridge_missing.sync_status(tick=2)
                assert snap_stop.authority_reachable is True
                assert snap_stop.stop_active is True
                assert snap_stop.halted is True
                assert snap_stop.can_deploy is False
                assert snap_stop.deployable_notional_usd_micros == 0
                assert snap_stop.blocked_reason == "stop_active"
                assert snap_stop.execution_eligible is False
                stop_file.unlink()

                # Authority still has 0 signatures used after shadow probes.
                snap_after = bridge_missing.sync_status(tick=3)
                assert snap_after.authority_status.signatures_used == 0
                assert snap_after.can_deploy is True
            finally:
                worker.join(timeout=6.0)
            assert not worker.is_alive()

    # Check ledger translation & fail-closed gates with injected doubles.
    base_cfg = ProfitStrategyDaemonConfig(
        authority_socket=Path("/nonexistent/authority.sock"),
        mode="shadow",
        risk_config=CapitalRiskConfig(),
        policy=policy,
    )
    partial_loss_bridge = ProfitStrategyBridge(
        base_cfg,
        client=_SyntheticAuthorityDouble(
            {
                "schema": PROFIT_AUTHORITY_SCHEMA,
                "account": acct.address.lower(),
                "daily_loss_usd": 195,
                "cumulative_loss_usd": 195,
                "stop_active": False,
                "pending_count": 0,
                "halted": False,
            }
        ),
    )
    partial_snap = partial_loss_bridge.sync_status()
    assert partial_snap.can_deploy is True
    # Remaining daily loss = $5M micros -> 5M * 10000 / 250 = 200M (< 225M single-clip cap)
    assert partial_snap.deployable_notional_usd_micros == 200_000_000

    for bad_payload, expected_reason in (
        (
            {
                "schema": PROFIT_AUTHORITY_SCHEMA,
                "account": acct.address.lower(),
                "stop_active": True,
                "pending_count": 0,
                "halted": False,
            },
            "stop_active",
        ),
        (
            {
                "schema": PROFIT_AUTHORITY_SCHEMA,
                "account": acct.address.lower(),
                "stop_active": False,
                "pending_count": 2,
                "halted": False,
            },
            "unresolved_residuals",
        ),
        (
            {
                "schema": PROFIT_AUTHORITY_SCHEMA,
                "account": acct.address.lower(),
                "daily_loss_usd_micros": 200_000_000,
                "stop_active": False,
                "pending_count": 0,
                "halted": False,
            },
            "daily_loss_exhausted",
        ),
        (
            {
                "schema": PROFIT_AUTHORITY_SCHEMA,
                "account": acct.address.lower(),
                "policy_digest": sha256_hex(b"wrong-policy"),
                "halted": False,
            },
            "authority_status_unavailable",
        ),
        (
            TimeoutError("socket timed out"),
            "authority_status_timeout",
        ),
    ):
        dbl = _SyntheticAuthorityDouble(bad_payload)
        br = ProfitStrategyBridge(base_cfg, client=dbl)
        snap = br.sync_status()
        assert snap.can_deploy is False
        assert snap.deployable_notional_usd_micros == 0
        assert snap.blocked_reason == expected_reason
        assert snap.execution_eligible is False
        assert snap.release_permitted is False

    zero_cash_bridge = ProfitStrategyBridge(
        ProfitStrategyDaemonConfig(
            authority_socket=Path("/nonexistent/authority.sock"),
            mode="shadow",
            risk_config=CapitalRiskConfig(),
            policy=policy,
            cash_available_usd_micros=0,
        ),
        client=_SyntheticAuthorityDouble(
            {
                "schema": PROFIT_AUTHORITY_SCHEMA,
                "account": acct.address.lower(),
                "halted": False,
                "stop_present": False,
            }
        ),
    )
    zero_snap = zero_cash_bridge.sync_status()
    assert zero_snap.can_deploy is False
    assert zero_snap.deployable_notional_usd_micros == 0
    assert zero_snap.blocked_reason == "insufficient_deployable_headroom"


def test_profit_strategy_bridge_dry_run_live_ambiguous_stop_and_reconcile() -> None:
    acct = Account.create()
    policy = _make_policy(acct.address, release_gate_open=False)
    risk_path = REPO_ROOT / "configs/profit-engine-risk.example.json"

    with _short_socket_dir() as td:
        root = Path(td)
        os.chmod(root, 0o700)
        auth_state_dir = root / "auth"
        strat_state_dir = root / "strat"
        auth_state_dir.mkdir(mode=0o700)
        strat_state_dir.mkdir(mode=0o700)
        endpoint = root / "l.sock"

        with ProfitSigningAuthorityServer(
            policy=policy,
            state_dir=auth_state_dir,
            endpoint=endpoint,
            key_signer=make_local_key_signer(bytes(acct.key), acct.address),
            max_signatures=6,
            ttl_seconds=30,
            allow_rehearsal_loopback_anvil=True,
            now_fn=lambda: NOW_S,
        ) as server:
            worker = threading.Thread(target=lambda: server.serve(max_requests=10))
            worker.start()
            try:
                # 1. dry_run simulates + admits without consuming nonce or broadcasting.
                dry_cfg = ProfitStrategyDaemonConfig(
                    authority_socket=endpoint,
                    mode="dry_run",
                    risk_config_path=risk_path,
                    policy=policy,
                    state_dir=strat_state_dir,
                    rehearsal_loopback_anvil=True,
                )
                dry_bridge = ProfitStrategyBridge(dry_cfg, now_fn=lambda: NOW_S)
                base_prop = _sample_executor_proposal(account=acct.address, nonce=1)
                rehearsal_prop_1 = AtomicOrIntentProposal(
                    envelope=base_prop.envelope,
                    simulation=base_prop.simulation,
                    now_s=base_prop.now_s,
                    rehearsal_loopback_anvil=True,
                )
                dry_res = dry_bridge.submit_dual_simulated_intent(
                    proposal=rehearsal_prop_1,
                    notional_usd_micros=50_000_000,
                )
                assert dry_res.status == "dry_run_admitted"
                assert dry_res.admitted is True
                assert dry_res.signed is False
                assert dry_res.broadcast is False
                assert dry_res.execution_eligible is False
                assert dry_res.release_permitted is False

                # 2. live rehearsal signs, broadcasts, and dual-reconciles cleanly.
                live_cfg = ProfitStrategyDaemonConfig(
                    authority_socket=endpoint,
                    mode="live",
                    risk_config_path=risk_path,
                    policy=policy,
                    state_dir=strat_state_dir,
                    rehearsal_loopback_anvil=True,
                )
                live_bridge = ProfitStrategyBridge(live_cfg, now_fn=lambda: NOW_S)
                settled_res = live_bridge.submit_dual_simulated_intent(
                    envelope=base_prop.envelope,
                    simulate_fn=lambda _env: _sample_simulation(),
                    notional_usd_micros=50_000_000,
                    broadcast_fn=lambda _raw, sub: sub.transaction_hash,
                    receipt_fn=lambda tx_h: _reconciliation_payload(
                        tx_hash=tx_h, status=1, native_residual_wei=0
                    ),
                )
                assert settled_res.status == "settled"
                assert settled_res.admitted is True
                assert settled_res.signed is True
                assert settled_res.broadcast is True
                assert settled_res.reconciled is True
                assert settled_res.halted is False
                assert settled_res.tx_hash is not None

                # 3. Ambiguous broadcast failure latches HALT and requires explicit recovery.
                prop2 = _sample_executor_proposal(account=acct.address, nonce=2)
                rehearsal_prop_2 = AtomicOrIntentProposal(
                    envelope=prop2.envelope,
                    simulation=prop2.simulation,
                    now_s=prop2.now_s,
                    rehearsal_loopback_anvil=True,
                )

                def _ambiguous_broadcast(_raw: bytes, _sub: SubmittedTransaction) -> str:
                    raise TimeoutError("upstream rpc broadcast timeout")

                ambig_res = live_bridge.submit_dual_simulated_intent(
                    proposal=rehearsal_prop_2,
                    notional_usd_micros=50_000_000,
                    broadcast_fn=_ambiguous_broadcast,
                )
                assert ambig_res.status == "ambiguous_halted"
                assert ambig_res.signed is True
                assert ambig_res.broadcast is False
                assert ambig_res.halted is True
                assert ambig_res.blocked_reason == "ambiguous_broadcast_stop"
                assert ambig_res.tx_hash is not None

                # Restarting bridge from state_dir preserves halted + pending across restarts.
                restarted_bridge = ProfitStrategyBridge(live_cfg, now_fn=lambda: NOW_S)
                assert restarted_bridge.halted is True
                blocked_snap = restarted_bridge.sync_status()
                assert blocked_snap.can_deploy is False
                assert blocked_snap.halted is True
                assert blocked_snap.pending_count >= 1

                # Recovery without receipts stays unresolved and halted.
                unresolved_rec = restarted_bridge.reconcile_pending()
                assert unresolved_rec.status == "unresolved_or_halted"
                assert unresolved_rec.halted is True
                assert ambig_res.tx_hash in unresolved_rec.unresolved_hashes

                # Recovery with dual receipts settles pending tx on authority.
                resolved_rec = restarted_bridge.reconcile_pending(
                    receipt_fn=lambda tx_h: _reconciliation_payload(
                        tx_hash=tx_h, status=1, native_residual_wei=0
                    )
                )
                assert resolved_rec.settled_hashes == (ambig_res.tx_hash,)
                assert resolved_rec.unresolved_hashes == ()
            finally:
                worker.join(timeout=6.0)
            assert not worker.is_alive()


def test_run_profit_strategy_cli_once_and_reconcile_only() -> None:
    acct = Account.create()
    policy = _make_policy(acct.address)
    risk_path = REPO_ROOT / "configs/profit-engine-risk.example.json"

    with _short_socket_dir() as td:
        root = Path(td)
        os.chmod(root, 0o700)
        auth_state_dir = root / "auth"
        strat_state_dir = root / "strat"
        auth_state_dir.mkdir(mode=0o700)
        strat_state_dir.mkdir(mode=0o700)
        policy_path = root / "policy.json"
        policy_path.write_text(json.dumps(_policy_json_dict(policy)), encoding="utf-8")
        endpoint = root / "cli.sock"
        out_status = root / "status.json"
        out_recovery = root / "recovery.json"

        with ProfitSigningAuthorityServer(
            policy=policy,
            state_dir=auth_state_dir,
            endpoint=endpoint,
            key_signer=make_local_key_signer(bytes(acct.key), acct.address),
            max_signatures=3,
            ttl_seconds=30,
            now_fn=lambda: NOW_S,
        ) as server:
            worker = threading.Thread(target=lambda: server.serve(max_requests=3))
            worker.start()
            try:
                env = dict(os.environ)
                env["PYTHONPATH"] = str(REPO_ROOT / "src")
                proc = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "scripts.run_profit_strategy",
                        "--mode",
                        "shadow",
                        "--authority-socket",
                        str(endpoint),
                        "--risk-config",
                        str(risk_path),
                        "--policy-config",
                        str(policy_path),
                        "--state-dir",
                        str(strat_state_dir),
                        "--out-json",
                        str(out_status),
                        "--once",
                    ],
                    cwd=REPO_ROOT,
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
                assert proc.returncode == 0, proc.stderr
                payload = json.loads(out_status.read_text(encoding="utf-8"))
                assert payload["schema"] == STRATEGY_DAEMON_SCHEMA
                assert payload["mode"] == "shadow"
                assert payload["authority_reachable"] is True
                assert payload["can_deploy"] is True
                assert payload["deployable_notional_usd_micros"] == 225_000_000
                assert payload["execution_eligible"] is False
                assert payload["release_permitted"] is False

                # Reconcile-only exits 0 when clean, and exits 3 when STOP is present.
                rc_clean = strategy_cli.main(
                    [
                        "--mode",
                        "shadow",
                        "--reconcile-only",
                        "--authority-socket",
                        str(endpoint),
                        "--risk-config",
                        str(risk_path),
                        "--policy-config",
                        str(policy_path),
                        "--state-dir",
                        str(strat_state_dir),
                        "--out-json",
                        str(out_recovery),
                    ]
                )
                assert rc_clean == 0
                clean_json = json.loads(out_recovery.read_text(encoding="utf-8"))
                assert clean_json["status"] == "clean"
                assert clean_json["execution_eligible"] is False

                (auth_state_dir / "STOP").touch()
                rc_stop = strategy_cli.main(
                    [
                        "--mode",
                        "shadow",
                        "--reconcile-only",
                        "--authority-socket",
                        str(endpoint),
                        "--risk-config",
                        str(risk_path),
                        "--policy-config",
                        str(policy_path),
                        "--state-dir",
                        str(strat_state_dir),
                        "--out-json",
                        str(out_recovery),
                    ]
                )
                assert rc_stop == 3
                stop_json = json.loads(out_recovery.read_text(encoding="utf-8"))
                assert stop_json["stop_active"] is True
                assert stop_json["halted"] is True
                assert stop_json["blocked_reason"] == "stop_active"
            finally:
                worker.join(timeout=6.0)
            assert not worker.is_alive()


def _extract_flag_pair(args: list[str], flag: str, value: str) -> bool:
    return any(
        args[idx] == flag and idx + 1 < len(args) and args[idx + 1] == value
        for idx in range(len(args))
    )


def _extract_mounts(args: list[str]) -> list[str]:
    return [args[idx + 1] for idx in range(len(args) - 1) if args[idx] == "-v"]


class _MockDockerHarness:
    def __init__(self, *, fail_strategy: bool = False) -> None:
        self.fail_strategy = fail_strategy
        self.account = Account.create().address.lower()
        self.calls: list[list[str]] = []
        self.created_volumes: list[str] = []
        self.removed_containers: list[str] = []
        self.removed_volumes: list[str] = []
        self.authority_container: str | None = None
        self.key_volume: str | None = None
        self.run_records: dict[str, list[str]] = {}
        self.authority_proof: dict[str, Any] = {}

    def __call__(
        self,
        cmd: list[str],
        **_kwargs: Any,
    ) -> subprocess.CompletedProcess[str]:
        args = list(cmd[1:])
        self.calls.append(args)
        verb = args[0]

        if verb == "build":
            self.run_records["build"] = args
            return subprocess.CompletedProcess(cmd, 0, stdout="built\n", stderr="")

        if verb == "volume" and len(args) >= 3 and args[1] == "create":
            self.created_volumes.append(args[2])
            return subprocess.CompletedProcess(cmd, 0, stdout=args[2] + "\n", stderr="")

        if verb == "volume" and len(args) >= 4 and args[1] == "rm" and args[2] == "-f":
            self.removed_volumes.extend(args[3:])
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        if verb == "rm" and len(args) >= 3 and args[1] == "-f":
            self.removed_containers.extend(args[2:])
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        if verb == "inspect":
            inspect_payload = [
                {
                    "State": {"Running": True, "Status": "running"},
                    "Config": {"User": "10001:10001"},
                    "HostConfig": {
                        "ReadonlyRootfs": True,
                        "CapDrop": ["ALL"],
                        "SecurityOpt": ["no-new-privileges:true"],
                        "NetworkMode": "none",
                        "PidsLimit": 128,
                    },
                    "Mounts": [
                        {
                            "Type": "volume",
                            "Name": self.key_volume,
                            "Destination": "/secrets",
                            "RW": False,
                        }
                    ],
                }
            ]
            return subprocess.CompletedProcess(
                cmd, 0, stdout=json.dumps(inspect_payload), stderr=""
            )

        if verb != "run":
            raise AssertionError(f"unexpected docker command: {cmd}")

        mounts = _extract_mounts(args)
        if any(m.endswith(":/authority-state") for m in mounts):
            self.run_records["initialize"] = args
            for mount in mounts:
                if mount.endswith(":/secrets"):
                    self.key_volume = mount.split(":", 1)[0]
            return subprocess.CompletedProcess(
                cmd, 0, stdout=json.dumps({"account": self.account}) + "\n", stderr=""
            )

        if "--status-only" in args:
            self.run_records["profit-authority-status"] = args
            kv_idx = args.index("--key-volume")
            key_vol = args[kv_idx + 1]
            proof: dict[str, Any] = {
                "schema": PROFIT_AUTHORITY_INFRA_SCHEMA,
                "execution_eligible": False,
                "account": self.account,
                "isolated_signer_verified": True,
                "isolated_key_volume": key_vol,
                "authority_socket_path": "/run/oasis/profit_authority.sock",
                "primary_rpc_provider": "publicnode_base",
                "secondary_rpc_provider": "base_official_mainnet",
                "secondary_rpc_verified": True,
                "isolated_signer": {
                    "isolated_signer_verified": True,
                    "isolated_key_volume": key_vol,
                    "authority_socket_path": "/run/oasis/profit_authority.sock",
                },
                "dual_rpc_quorum": {
                    "primary_rpc_provider": "publicnode_base",
                    "secondary_rpc_provider": "base_official_mainnet",
                    "secondary_rpc_verified": True,
                },
                "authority_status": {
                    "schema": PROFIT_AUTHORITY_SCHEMA,
                    "account": self.account,
                    "signatures_used": 0,
                    "max_signatures": 12,
                    "last_nonce": None,
                    "halted": False,
                    "stop_present": False,
                },
            }
            proof["proof_sha256"] = sha256_hex(canonical_json(proof))
            self.authority_proof = proof
            return subprocess.CompletedProcess(cmd, 0, stdout="ready\n", stderr="")

        if args[-2:] == ["cat", "/state/authority_status_proof.json"]:
            self.run_records["proof_reader"] = args
            return subprocess.CompletedProcess(
                cmd, 0, stdout=json.dumps(self.authority_proof), stderr=""
            )

        if "-d" in args and "--key-file" in args:
            self.run_records["profit-authority"] = args
            self.authority_container = args[args.index("--name") + 1]
            return subprocess.CompletedProcess(cmd, 0, stdout="ctr-id\n", stderr="")

        if "validate_socket_endpoint" in args[-1]:
            self.run_records["socket_probe"] = args
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        if "scripts.run_profit_strategy" in args:
            self.run_records["profit-strategy"] = args
            if self.fail_strategy:
                return subprocess.CompletedProcess(
                    cmd, 3, stdout="", stderr="simulated strategy failure"
                )
            return subprocess.CompletedProcess(cmd, 0, stdout="{}\n", stderr="")

        if args[-2:] == ["cat", "/state/strategy_status.json"]:
            self.run_records["strategy_reader"] = args
            strategy_payload = {
                "schema": STRATEGY_DAEMON_SCHEMA,
                "mode": "shadow",
                "authority_reachable": True,
                "can_deploy": True,
                "deployable_notional_usd_micros": 225_000_000,
                "execution_eligible": False,
                "release_permitted": False,
                "authority_status": {
                    "schema": PROFIT_AUTHORITY_SCHEMA,
                    "account": self.account,
                    "signing_enabled": True,
                    "signatures_used": 0,
                },
            }
            return subprocess.CompletedProcess(
                cmd, 0, stdout=json.dumps(strategy_payload), stderr=""
            )

        if "cap_eff_zero" in args[-1]:
            self.run_records["negative_probe"] = args
            neg_payload = {
                "uid": 10001,
                "gid": 10001,
                "cap_eff_zero": True,
                "no_new_privs": True,
                "secrets_absent": True,
                "authority_state_absent": True,
                "image_risk_config_present": True,
                "rootfs_readonly": True,
                "risk_config_bind_readonly": True,
            }
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(neg_payload), stderr="")

        raise AssertionError(f"unhandled docker run command: {args}")


def test_verify_profit_docker_confinement_happy_path_and_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _MockDockerHarness()
    monkeypatch.setattr(docker_confinement_cli.subprocess, "run", harness)

    source = (REPO_ROOT / "scripts/verify_profit_docker_confinement.py").read_text(encoding="utf-8")
    tmp_marker = "/" + "tmp"
    assert tmp_marker not in source
    for node in ast.walk(ast.parse(source)):
        assert not isinstance(node, ast.Assert)

    out_report = tmp_path / "confinement_report.json"
    out_proof = tmp_path / "signer_proof.json"
    rc = docker_confinement_cli.main(
        [
            "--out",
            str(out_report),
            "--out-signer-proof",
            str(out_proof),
            "--build-network",
            "host",
        ]
    )
    assert rc == 0

    report = json.loads(out_report.read_text(encoding="utf-8"))
    assert report["schema"] == "oasis.profit-docker-confinement.v1"
    assert report["passed"] is True
    assert report["execution_eligible"] is False
    assert report["rehearsal_cleaned"] is True
    assert report["authority_account"] == harness.account
    assert report["strategy_status_summary"]["can_deploy"] is True
    assert report["strategy_status_summary"]["execution_eligible"] is False
    expected_sha = sha256_hex(
        canonical_json({k: v for k, v in report.items() if k != "report_sha256"})
    )
    assert report["report_sha256"] == expected_sha

    # Extracted Gate 8 proof passes canary_readiness Gates 7 & 8.
    proof = json.loads(out_proof.read_text(encoding="utf-8"))
    assert proof["schema"] == PROFIT_AUTHORITY_INFRA_SCHEMA
    assert proof["execution_eligible"] is False
    readiness = evaluate_profit_engine_readiness(
        contracts_out_dir=tmp_path / "absent-contracts",
        infra_proof_path=out_proof,
    )
    gate_map = {g.name: g for g in readiness.gates}
    assert gate_map["dual_rpc_quorum_verified"].passed is True
    assert gate_map["isolated_signer_authority_ready"].passed is True

    # Build network passed cleanly.
    assert "--network" in harness.run_records["build"]
    assert "host" in harness.run_records["build"]

    # Hardening flags & key/authority-state isolation across runtime containers.
    for role in (
        "profit-authority",
        "profit-authority-status",
        "profit-strategy",
        "negative_probe",
    ):
        args = harness.run_records[role]
        assert "--read-only" in args
        assert _extract_flag_pair(args, "--cap-drop", "ALL")
        assert _extract_flag_pair(args, "--security-opt", "no-new-privileges:true")
        assert _extract_flag_pair(args, "--user", "10001:10001")
        assert _extract_flag_pair(args, "--network", "none")
        assert _extract_flag_pair(args, "--pids-limit", "128")
        assert _extract_flag_pair(args, "--memory", "512m")

    auth_mounts = _extract_mounts(harness.run_records["profit-authority"])
    assert f"{harness.key_volume}:/secrets:ro" in auth_mounts

    for keyless_role in ("profit-authority-status", "profit-strategy", "negative_probe"):
        mounts = _extract_mounts(harness.run_records[keyless_role])
        assert not any(str(harness.key_volume) in m or ":/secrets" in m for m in mounts)

    for strat_role in ("profit-strategy", "negative_probe"):
        mounts = _extract_mounts(harness.run_records[strat_role])
        assert not any("authority-state" in m for m in mounts)

    # Complete cleanup of authority container and all 4 temporary volumes.
    assert len(harness.created_volumes) == 4
    assert harness.authority_container in harness.removed_containers
    assert set(harness.removed_volumes) == set(harness.created_volumes)

    # Refuse overwriting existing files or writing through symlinks.
    assert docker_confinement_cli.main(["--skip-build", "--out", str(out_report)]) == 3
    sym_out = tmp_path / "sym.json"
    sym_out.symlink_to(tmp_path / "target.json")
    assert docker_confinement_cli.main(["--skip-build", "--out", str(sym_out)]) == 3


def test_verify_profit_docker_confinement_cleanup_on_strategy_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _MockDockerHarness(fail_strategy=True)
    monkeypatch.setattr(docker_confinement_cli.subprocess, "run", harness)

    out_report = tmp_path / "failed_report.json"
    out_proof = tmp_path / "failed_proof.json"
    rc = docker_confinement_cli.main(
        [
            "--skip-build",
            "--out",
            str(out_report),
            "--out-signer-proof",
            str(out_proof),
        ]
    )
    assert rc == 3
    assert "build" not in harness.run_records
    assert not out_report.exists()
    assert not out_proof.exists()
    assert len(harness.created_volumes) == 4
    assert harness.authority_container in harness.removed_containers
    assert set(harness.removed_volumes) == set(harness.created_volumes)
