"""Unit tests for atomic/intent policy admission, byte verification, and reconciliation."""

import json
import os
import shutil
import subprocess
from dataclasses import FrozenInstanceError, replace
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
    EARLY_ABORTED_TOPIC0,
    EXECUTE_BATCH_WITH_CALLBACK_SELECTOR,
    EXECUTE_CYCLE_SELECTOR,
    EXECUTE_REACTOR_FILL_PARAM_TYPES,
    EXECUTE_REACTOR_FILL_PLAN_TYPE,
    EXECUTE_REACTOR_FILL_SELECTOR,
    EXECUTE_REACTOR_FILL_SIGNATURE,
    EXECUTE_WITH_CALLBACK_SELECTOR,
    INTENT_VAULT_EARLY_ABORTED_TOPIC0,
    REACTOR_FILL_EXECUTED_TOPIC0,
    AdmissionDecision,
    AtomicExecutionPolicy,
    DualReceiptObservation,
    DualSimulationProof,
    EnvelopeProposal,
    admit_atomic_or_intent_proposal,
    reconcile_atomic_or_intent_receipt,
    verify_signed_bytes_match_admission,
)
from oasis_agent.models.common import sha256_hex
from oasis_agent.risk.revert_budget import (
    AttemptOutcome,
    ReceiptLog,
    RevertBudgetStatus,
    RollingRevertBudgetPolicy,
    RollingRevertBudgetTracker,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
EXECUTOR_REL_ARTIFACT = Path("contracts/out/OasisAtomicExecutor.sol/OasisAtomicExecutor.json")
VAULT_REL_ARTIFACT = Path("contracts/out/OasisIntentVault.sol/OasisIntentVault.json")

EXECUTOR = "0x" + "11" * 20
EXECUTOR_CODE_HASH = "0x" + "aa" * 32
INTENT_VAULT = "0x" + "12" * 20
INTENT_VAULT_CODE_HASH = "0x" + "bb" * 32
TOKEN_A = "0x" + "22" * 20
TOKEN_B = "0x" + "33" * 20
POOL_V3 = "0x" + "55" * 20
POOL_V2 = "0x" + "66" * 20
ROUTER = "0x" + "77" * 20
REACTOR = "0x" + "88" * 20
UNLISTED_ADDR = "0x" + "99" * 20
ALT_CODE_HASH = "0x" + "cc" * 32
SIM_BLOCK_HASH = "0x" + "de" * 32
TX_HASH = "0x" + "ef" * 32
RECEIPT_BLOCK_HASH = "0x" + "fa" * 32
UINT160_SQRT_1 = 1 << 96
NOW_S = 1_750_000_000


def _canonical_param_type(param: dict[str, Any]) -> str:
    raw_type: str = param["type"]
    if not raw_type.startswith("tuple"):
        return raw_type
    suffix = raw_type[len("tuple") :]
    inner = ",".join(_canonical_param_type(component) for component in param["components"])
    return f"({inner}){suffix}"


def _worktree_common_root() -> Path | None:
    dot_git = REPO_ROOT / ".git"
    if not dot_git.is_file():
        return None
    first_line = dot_git.read_text(encoding="utf-8").strip()
    if not first_line.startswith("gitdir:"):
        return None
    gitdir = Path(first_line.split(":", 1)[1].strip())
    if not gitdir.is_absolute():
        gitdir = (REPO_ROOT / gitdir).resolve()
    commondir_file = gitdir / "commondir"
    if not commondir_file.is_file():
        return None
    common_rel = commondir_file.read_text(encoding="utf-8").strip()
    common_git_dir = (gitdir / common_rel).resolve()
    return common_git_dir.parent


def _load_compiled_abi_or_build(relative_artifact: Path) -> dict[str, Any]:
    artifact_path = REPO_ROOT / relative_artifact
    if not artifact_path.is_file():
        if os.environ.get("OASIS_REQUIRE_FOUNDRY_ARTIFACTS") == "1":
            raise FileNotFoundError(artifact_path)
        common_root = _worktree_common_root()
        if common_root is not None and (common_root / relative_artifact).is_file():
            artifact_path = common_root / relative_artifact
        else:
            forge_bin = shutil.which("forge")
            if forge_bin is None or not (REPO_ROOT / "contracts" / "lib").exists():
                pytest.skip("Foundry artifact unavailable in worktree/Python-only container")
            subprocess.run(
                [forge_bin, "build", "--root", str(REPO_ROOT / "contracts")],
                check=True,
            )
    loaded: dict[str, Any] = json.loads(artifact_path.read_text(encoding="utf-8"))
    return loaded


def _make_policy(
    *,
    operator_account: str,
    release_gate_open: bool = True,
    max_fee_per_gas_wei: int = 5_000_000_000,
    max_priority_fee_wei: int = 100_000_000,
    max_gas_limit: int = 650_000,
    max_daily_loss_usd_micros: int = 200_000_000,
    max_cumulative_loss_usd_micros: int = 500_000_000,
) -> AtomicExecutionPolicy:
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
        max_daily_loss_usd_micros=max_daily_loss_usd_micros,
        max_cumulative_loss_usd_micros=max_cumulative_loss_usd_micros,
        max_gas_limit=max_gas_limit,
        max_fee_per_gas_wei=max_fee_per_gas_wei,
        max_priority_fee_wei=max_priority_fee_wei,
        max_deadline_horizon_s=24,
        release_gate_open=release_gate_open,
    )


def _sample_executor_calldata(
    *, deadline_s: int = NOW_S + 12, min_net_profit: int = 10**6
) -> bytes:
    intent = AtomicCycleIntent(
        chain_id=8453,
        executor_address=EXECUTOR,
        executor_code_hash=EXECUTOR_CODE_HASH,
        nonce=7,
        deadline=deadline_s,
        profit_token=TOKEN_A,
        initial_amount=10**8,
        min_net_profit=min_net_profit,
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
    raw, _ = encode_execute_cycle_calldata(intent)
    return raw


def _sample_vault_calldata(
    *,
    reactor: str = REACTOR,
    profit_token: str = TOKEN_A,
    min_net_profit: int = 10**6,
    precheck_pool: str = POOL_V3,
    step_router: str = ROUTER,
    step_pool: str = POOL_V3,
    step_selector: bytes | None = None,
    reactor_selector: bytes = EXECUTE_WITH_CALLBACK_SELECTOR,
) -> bytes:
    router_sel = (
        step_selector
        if step_selector is not None
        else keccak(b"exactInputSingle((address,address,uint24,address,uint256,uint256,uint160))")[
            :4
        ]
    )
    step_calldata = router_sel + encode(["uint256"], [12345])
    precheck = (precheck_pool, UINT160_SQRT_1 - 50, UINT160_SQRT_1 + 50)
    steps = [
        (step_router, step_pool, TOKEN_A, TOKEN_B, 50_000_000, 49_000_000, step_calldata),
    ]
    plan = (reactor, profit_token, min_net_profit, precheck, steps)
    reactor_calldata = reactor_selector + encode(["bytes", "bytes"], [b"order", b"callback"])
    return EXECUTE_REACTOR_FILL_SELECTOR + encode(
        list(EXECUTE_REACTOR_FILL_PARAM_TYPES), [plan, reactor_calldata]
    )


def _sample_executor_proposal(
    *,
    account: str,
    nonce: int = 3,
    deadline_s: int = NOW_S + 12,
    calldata: bytes | None = None,
) -> EnvelopeProposal:
    resolved_calldata = (
        calldata if calldata is not None else _sample_executor_calldata(deadline_s=deadline_s)
    )
    return EnvelopeProposal(
        chain_id=8453,
        account=account,
        nonce=nonce,
        target=EXECUTOR,
        target_observed_code_hash=EXECUTOR_CODE_HASH,
        value_wei=0,
        calldata=resolved_calldata,
        gas_limit=350_000,
        max_fee_per_gas_wei=1_000_000_000,
        max_priority_fee_wei=50_000_000,
        profit_token=TOKEN_A,
        min_net_profit_raw=10**6,
        referenced_pools=(POOL_V3, POOL_V2),
        referenced_routers=(),
        referenced_reactor=None,
        deadline_s=deadline_s,
    )


def _sample_vault_proposal(
    *,
    account: str,
    nonce: int = 4,
    deadline_s: int = NOW_S + 12,
    calldata: bytes | None = None,
) -> EnvelopeProposal:
    return EnvelopeProposal(
        chain_id=8453,
        account=account,
        nonce=nonce,
        target=INTENT_VAULT,
        target_observed_code_hash=INTENT_VAULT_CODE_HASH,
        value_wei=0,
        calldata=calldata if calldata is not None else _sample_vault_calldata(),
        gas_limit=420_000,
        max_fee_per_gas_wei=1_200_000_000,
        max_priority_fee_wei=40_000_000,
        profit_token=TOKEN_A,
        min_net_profit_raw=10**6,
        referenced_pools=(POOL_V3,),
        referenced_routers=(ROUTER,),
        referenced_reactor=REACTOR,
        deadline_s=deadline_s,
    )


def _sample_simulation(
    *,
    primary_source: str = "alchemy",
    secondary_source: str = "quicknode",
    primary_reverted: bool = False,
    secondary_reverted: bool = False,
    primary_early_aborted: bool = False,
    secondary_early_aborted: bool = False,
    primary_profit_delta_raw: int = 2_000_000,
    secondary_profit_delta_raw: int = 2_000_000,
    l2_gas_fee_wei: int = 200_000_000_000_000,
    l1_data_fee_wei: int = 50_000_000_000_000,
    simulated_profit_value_wei: int = 600_000_000_000_000,
) -> DualSimulationProof:
    return DualSimulationProof(
        block_number=28_000_001,
        block_hash=SIM_BLOCK_HASH,
        primary_source=primary_source,
        secondary_source=secondary_source,
        primary_reverted=primary_reverted,
        secondary_reverted=secondary_reverted,
        primary_early_aborted=primary_early_aborted,
        secondary_early_aborted=secondary_early_aborted,
        primary_gas_used=195_000,
        secondary_gas_used=196_000,
        primary_profit_delta_raw=primary_profit_delta_raw,
        secondary_profit_delta_raw=secondary_profit_delta_raw,
        l2_gas_fee_wei=l2_gas_fee_wei,
        l1_data_fee_wei=l1_data_fee_wei,
        simulated_profit_value_wei=simulated_profit_value_wei,
    )


def _sample_tracker() -> RollingRevertBudgetTracker:
    return RollingRevertBudgetTracker(
        RollingRevertBudgetPolicy(
            max_hourly_gas_burn_usd_micros=5_000_000,
            max_deep_reverts_per_hour=2,
            max_early_abort_gas=65_000,
            window_seconds=3_600,
        )
    )


def _sample_receipt(
    *,
    source: str,
    target: str,
    status: int = 1,
    gas_used: int = 200_000,
    effective_gas_price_wei: int = 1_000_000_000,
    l1_fee_wei: int = 25_000_000_000_000,
    logs: tuple[ReceiptLog, ...] = (),
    tx_hash: str = TX_HASH,
    block_number: int = 28_000_002,
    block_hash: str = RECEIPT_BLOCK_HASH,
) -> DualReceiptObservation:
    return DualReceiptObservation(
        source=source,
        tx_hash=tx_hash,
        block_number=block_number,
        block_hash=block_hash,
        status=status,
        gas_used=gas_used,
        effective_gas_price_wei=effective_gas_price_wei,
        l1_fee_wei=l1_fee_wei,
        logs=logs,
    )


def test_compiled_intent_vault_and_executor_abi_match_selectors_and_topics() -> None:
    vault_artifact = _load_compiled_abi_or_build(VAULT_REL_ARTIFACT)
    vault_abi = vault_artifact["abi"]
    vault_ids: dict[str, str] = vault_artifact["methodIdentifiers"]

    fn_entry = next(
        item
        for item in vault_abi
        if item["type"] == "function" and item["name"] == "executeReactorFill"
    )
    compiled_sig = (
        f"executeReactorFill({','.join(_canonical_param_type(p) for p in fn_entry['inputs'])})"
    )
    assert compiled_sig == EXECUTE_REACTOR_FILL_SIGNATURE
    assert keccak(compiled_sig.encode())[:4] == EXECUTE_REACTOR_FILL_SELECTOR
    assert vault_ids[compiled_sig] == EXECUTE_REACTOR_FILL_SELECTOR.hex()
    assert EXECUTE_REACTOR_FILL_PLAN_TYPE in compiled_sig

    fill_event = next(
        item
        for item in vault_abi
        if item["type"] == "event" and item["name"] == "ReactorFillExecuted"
    )
    fill_types = ",".join(_canonical_param_type(p) for p in fill_event["inputs"])
    fill_sig = f"ReactorFillExecuted({fill_types})"
    assert "0x" + keccak(fill_sig.encode()).hex() == REACTOR_FILL_EXECUTED_TOPIC0

    vault_early_event = next(
        item for item in vault_abi if item["type"] == "event" and item["name"] == "EarlyAborted"
    )
    vault_early_sig = (
        f"EarlyAborted({','.join(_canonical_param_type(p) for p in vault_early_event['inputs'])})"
    )
    assert "0x" + keccak(vault_early_sig.encode()).hex() == INTENT_VAULT_EARLY_ABORTED_TOPIC0

    exec_artifact = _load_compiled_abi_or_build(EXECUTOR_REL_ARTIFACT)
    exec_abi = exec_artifact["abi"]
    cycle_fn = next(
        item for item in exec_abi if item["type"] == "function" and item["name"] == "executeCycle"
    )
    cycle_sig = f"executeCycle({','.join(_canonical_param_type(p) for p in cycle_fn['inputs'])})"
    assert keccak(cycle_sig.encode())[:4] == EXECUTE_CYCLE_SELECTOR


def test_admit_happy_paths_for_atomic_executor_and_intent_vault() -> None:
    acct = Account.create()
    policy = _make_policy(operator_account=acct.address, release_gate_open=True)
    sim = _sample_simulation()

    exec_prop = _sample_executor_proposal(account=acct.address)
    exec_decision = admit_atomic_or_intent_proposal(
        policy=policy,
        proposal=exec_prop,
        simulation=sim,
        now_s=NOW_S,
        daily_loss_usd_micros=0,
        cumulative_loss_usd_micros=0,
        revert_budget_status=RevertBudgetStatus.ACTIVE,
    )
    assert isinstance(exec_decision, AdmissionDecision)
    assert exec_decision.admitted is True
    assert exec_decision.target_kind == "atomic_executor"
    assert exec_decision.execution_eligible is True
    assert exec_decision.reasons == ()
    assert exec_decision.calldata_sha256 == sha256_hex(exec_prop.calldata)
    assert len(exec_decision.proposal_digest) == 64

    vault_cd = _sample_vault_calldata(reactor_selector=EXECUTE_BATCH_WITH_CALLBACK_SELECTOR)
    vault_prop = _sample_vault_proposal(account=acct.address, calldata=vault_cd)
    vault_decision = admit_atomic_or_intent_proposal(
        policy=policy,
        proposal=vault_prop,
        simulation=sim,
        now_s=NOW_S,
        daily_loss_usd_micros=199_999_999,
        cumulative_loss_usd_micros=499_999_999,
        revert_budget_status=RevertBudgetStatus.ACTIVE,
    )
    assert vault_decision.admitted is True
    assert vault_decision.target_kind == "intent_vault"
    assert vault_decision.execution_eligible is True
    assert vault_decision.reasons == ()
    assert vault_decision.calldata_sha256 == sha256_hex(vault_cd)

    with pytest.raises(FrozenInstanceError):
        vault_decision.admitted = False  # type: ignore[misc]


def test_loopback_anvil_rehearsal_flag_and_release_gate_enforcement() -> None:
    acct = Account.create()
    closed_policy = _make_policy(operator_account=acct.address, release_gate_open=False)
    open_policy = _make_policy(operator_account=acct.address, release_gate_open=True)
    prop = _sample_executor_proposal(account=acct.address)
    sim = _sample_simulation()

    # Closed release gate without rehearsal flag is denied fail-closed.
    denied = admit_atomic_or_intent_proposal(
        policy=closed_policy,
        proposal=prop,
        simulation=sim,
        now_s=NOW_S,
        daily_loss_usd_micros=0,
        cumulative_loss_usd_micros=0,
        revert_budget_status=RevertBudgetStatus.ACTIVE,
        rehearsal_loopback_anvil=False,
    )
    assert denied.admitted is False
    assert denied.target_kind == "rejected"
    assert denied.execution_eligible is False
    assert "release_gate_closed" in denied.reasons

    # Closed release gate WITH loopback Anvil rehearsal flag admits for local rehearsal,
    # while keeping execution_eligible strictly False.
    rehearsal_closed = admit_atomic_or_intent_proposal(
        policy=closed_policy,
        proposal=prop,
        simulation=sim,
        now_s=NOW_S,
        daily_loss_usd_micros=0,
        cumulative_loss_usd_micros=0,
        revert_budget_status=RevertBudgetStatus.ACTIVE,
        rehearsal_loopback_anvil=True,
    )
    assert rehearsal_closed.admitted is True
    assert rehearsal_closed.target_kind == "atomic_executor"
    assert rehearsal_closed.execution_eligible is False

    # Even with open release gate, loopback Anvil rehearsal keeps execution_eligible=False.
    rehearsal_open = admit_atomic_or_intent_proposal(
        policy=open_policy,
        proposal=prop,
        simulation=sim,
        now_s=NOW_S,
        daily_loss_usd_micros=0,
        cumulative_loss_usd_micros=0,
        revert_budget_status=RevertBudgetStatus.ACTIVE,
        rehearsal_loopback_anvil=True,
    )
    assert rehearsal_open.admitted is True
    assert rehearsal_open.execution_eligible is False


def test_admission_rejects_every_risk_policy_calldata_and_simulation_violation() -> None:
    acct = Account.create()
    policy = _make_policy(operator_account=acct.address, release_gate_open=True)
    base_exec = _sample_executor_proposal(account=acct.address)
    base_vault = _sample_vault_proposal(account=acct.address)
    base_sim = _sample_simulation()

    def check_denied(
        *,
        pol: AtomicExecutionPolicy = policy,
        prop: EnvelopeProposal = base_exec,
        sim: DualSimulationProof = base_sim,
        daily_loss: int = 0,
        cum_loss: int = 0,
        budget: RevertBudgetStatus = RevertBudgetStatus.ACTIVE,
        pending: bool = False,
        expected_reason: str,
    ) -> AdmissionDecision:
        res = admit_atomic_or_intent_proposal(
            policy=pol,
            proposal=prop,
            simulation=sim,
            now_s=NOW_S,
            daily_loss_usd_micros=daily_loss,
            cumulative_loss_usd_micros=cum_loss,
            revert_budget_status=budget,
            has_unresolved_pending=pending,
        )
        assert res.admitted is False
        assert res.target_kind == "rejected"
        assert res.execution_eligible is False
        assert expected_reason in res.reasons
        return res

    # Risk & governance gates
    check_denied(budget=RevertBudgetStatus.HALT, expected_reason="revert_budget_not_active")
    check_denied(pending=True, expected_reason="unresolved_pending_transaction")
    check_denied(daily_loss=200_000_000, expected_reason="daily_loss_limit_reached")
    check_denied(cum_loss=500_000_000, expected_reason="cumulative_loss_limit_reached")

    # Envelope identity, value, gas, fees, deadline, profit_token
    check_denied(
        prop=replace(base_exec, chain_id=1),
        expected_reason="chain_id_mismatch",
    )
    check_denied(
        prop=replace(base_exec, account=UNLISTED_ADDR),
        expected_reason="operator_account_mismatch",
    )
    check_denied(
        prop=replace(base_exec, nonce=-1),
        expected_reason="invalid_nonce",
    )
    check_denied(
        prop=replace(base_exec, value_wei=1),
        expected_reason="nonzero_transaction_value",
    )
    check_denied(
        prop=replace(base_exec, gas_limit=650_001),
        expected_reason="gas_limit_out_of_bounds",
    )
    check_denied(
        prop=replace(base_exec, max_fee_per_gas_wei=5_000_000_001),
        expected_reason="max_fee_per_gas_exceeds_policy",
    )
    check_denied(
        prop=replace(base_exec, max_priority_fee_wei=100_000_001),
        expected_reason="max_priority_fee_exceeds_policy",
    )
    check_denied(
        prop=replace(
            base_exec,
            max_fee_per_gas_wei=50_000_000,
            max_priority_fee_wei=60_000_000,
        ),
        expected_reason="priority_fee_exceeds_max_fee",
    )
    check_denied(
        prop=replace(base_exec, profit_token=TOKEN_B),
        expected_reason="profit_token_not_allowlisted",
    )
    check_denied(
        prop=replace(base_exec, min_net_profit_raw=0),
        expected_reason="nonpositive_min_net_profit",
    )
    check_denied(
        prop=replace(base_exec, deadline_s=NOW_S),
        expected_reason="deadline_out_of_horizon",
    )
    check_denied(
        prop=replace(base_exec, deadline_s=NOW_S + 25),
        expected_reason="deadline_out_of_horizon",
    )

    # Target & code hash & allowlists
    check_denied(
        prop=replace(base_exec, target=UNLISTED_ADDR),
        expected_reason="unallowlisted_target",
    )
    check_denied(
        prop=replace(base_exec, target_observed_code_hash=ALT_CODE_HASH),
        expected_reason="executor_code_hash_mismatch",
    )
    check_denied(
        prop=replace(base_vault, target_observed_code_hash=ALT_CODE_HASH),
        expected_reason="intent_vault_code_hash_mismatch",
    )
    check_denied(
        prop=replace(base_exec, referenced_pools=()),
        expected_reason="empty_referenced_pools",
    )
    check_denied(
        prop=replace(base_exec, referenced_pools=(POOL_V3, UNLISTED_ADDR)),
        expected_reason="pool_not_allowlisted",
    )
    check_denied(
        prop=replace(base_vault, referenced_routers=(UNLISTED_ADDR,)),
        expected_reason="router_not_allowlisted",
    )
    check_denied(
        prop=replace(base_vault, referenced_reactor=UNLISTED_ADDR),
        expected_reason="reactor_not_allowlisted",
    )
    check_denied(
        prop=replace(base_vault, referenced_reactor=None),
        expected_reason="reactor_not_allowlisted",
    )

    # Inner calldata validation & anti-smuggling checks
    check_denied(
        prop=replace(base_exec, calldata=base_exec.calldata + b"\x00"),
        expected_reason="noncanonical_execute_cycle_calldata",
    )
    check_denied(
        prop=replace(base_exec, calldata=base_vault.calldata),
        expected_reason="invalid_executor_selector",
    )
    check_denied(
        prop=replace(base_exec, calldata=_sample_executor_calldata(min_net_profit=1)),
        expected_reason="calldata_min_net_profit_mismatch",
    )
    check_denied(
        prop=replace(base_exec, calldata=_sample_executor_calldata(deadline_s=NOW_S + 20)),
        expected_reason="calldata_deadline_mismatch",
    )
    check_denied(
        prop=replace(base_vault, calldata=base_exec.calldata),
        expected_reason="invalid_intent_vault_selector",
    )
    smuggled_vault_cd = _sample_vault_calldata(step_pool=UNLISTED_ADDR)
    check_denied(
        prop=replace(base_vault, calldata=smuggled_vault_cd),
        expected_reason="calldata_pool_not_allowlisted",
    )
    bad_reactor_cd = _sample_vault_calldata(reactor_selector=b"\x12\x34\x56\x78")
    check_denied(
        prop=replace(base_vault, calldata=bad_reactor_cd),
        expected_reason="invalid_reactor_calldata_selector",
    )
    forbidden_router_cd = _sample_vault_calldata(
        step_selector=keccak(b"approve(address,uint256)")[:4]
    )
    check_denied(
        prop=replace(base_vault, calldata=forbidden_router_cd),
        expected_reason="forbidden_router_selector",
    )

    # Dual simulation checks
    check_denied(
        sim=_sample_simulation(primary_source="alchemy", secondary_source="alchemy"),
        expected_reason="simulation_sources_not_distinct",
    )
    check_denied(
        sim=_sample_simulation(primary_reverted=True),
        expected_reason="simulation_reverted",
    )
    check_denied(
        sim=_sample_simulation(secondary_early_aborted=True),
        expected_reason="simulation_early_aborted",
    )
    check_denied(
        sim=_sample_simulation(
            primary_profit_delta_raw=2_000_000,
            secondary_profit_delta_raw=1_900_000,
        ),
        expected_reason="simulation_profit_disagreement",
    )
    check_denied(
        sim=_sample_simulation(
            primary_profit_delta_raw=999_999,
            secondary_profit_delta_raw=999_999,
        ),
        expected_reason="simulation_profit_below_min_net_profit",
    )
    check_denied(
        sim=_sample_simulation(
            l2_gas_fee_wei=400_000_000_000_000,
            l1_data_fee_wei=200_000_000_000_000,
            simulated_profit_value_wei=600_000_000_000_000,
        ),
        expected_reason="simulated_profit_below_l1_l2_gas_cost",
    )

    # Deterministic multi-reason accumulation in a single call
    multi = admit_atomic_or_intent_proposal(
        policy=_make_policy(operator_account=acct.address, release_gate_open=False),
        proposal=replace(base_exec, value_wei=10, profit_token=TOKEN_B),
        simulation=_sample_simulation(primary_reverted=True),
        now_s=NOW_S,
        daily_loss_usd_micros=250_000_000,
        cumulative_loss_usd_micros=0,
        revert_budget_status=RevertBudgetStatus.HALT,
        has_unresolved_pending=True,
    )
    assert multi.admitted is False
    assert set(
        {
            "release_gate_closed",
            "revert_budget_not_active",
            "unresolved_pending_transaction",
            "daily_loss_limit_reached",
            "nonzero_transaction_value",
            "profit_token_not_allowlisted",
            "simulation_reverted",
        }
    ).issubset(multi.reasons)


def test_plain_int_and_bool_enforced_on_models() -> None:
    acct = Account.create()
    with pytest.raises(TypeError, match="plain bool"):
        _make_policy(operator_account=acct.address, release_gate_open=1)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="plain int"):
        AtomicExecutionPolicy(
            chain_id=True,
            operator_account=acct.address,
            executor_address=EXECUTOR,
            executor_code_hash=EXECUTOR_CODE_HASH,
            intent_vault_address=INTENT_VAULT,
            intent_vault_code_hash=INTENT_VAULT_CODE_HASH,
            allowed_pools=frozenset({POOL_V3}),
            allowed_routers=frozenset({ROUTER}),
            allowed_reactors=frozenset({REACTOR}),
            allowed_profit_tokens=frozenset({TOKEN_A}),
        )


def test_verify_signed_bytes_match_admission_and_rejects_any_tamper() -> None:
    acct = Account.create()
    other_acct = Account.create()
    policy = _make_policy(operator_account=acct.address, release_gate_open=False)
    proposal = _sample_vault_proposal(account=acct.address)
    decision = admit_atomic_or_intent_proposal(
        policy=policy,
        proposal=proposal,
        simulation=_sample_simulation(),
        now_s=NOW_S,
        daily_loss_usd_micros=0,
        cumulative_loss_usd_micros=0,
        revert_budget_status=RevertBudgetStatus.ACTIVE,
        rehearsal_loopback_anvil=True,
    )
    assert decision.admitted is True

    tx_fields: dict[str, Any] = {
        "type": 2,
        "chainId": decision.chain_id,
        "nonce": decision.nonce,
        "to": decision.target,
        "value": decision.value_wei,
        "data": proposal.calldata,
        "gas": decision.gas_limit,
        "maxFeePerGas": decision.max_fee_per_gas_wei,
        "maxPriorityFeePerGas": decision.max_priority_fee_wei,
    }
    raw_signed = bytes(acct.sign_transaction(tx_fields).raw_transaction)
    submitted = verify_signed_bytes_match_admission(raw_signed, decision)
    assert submitted.account == acct.address.lower()
    assert submitted.nonce == decision.nonce
    assert submitted.target == decision.target
    assert submitted.calldata_hash == "0x" + keccak(proposal.calldata).hex()

    # Refuses rejected decisions
    rejected = admit_atomic_or_intent_proposal(
        policy=policy,
        proposal=proposal,
        simulation=_sample_simulation(),
        now_s=NOW_S,
        daily_loss_usd_micros=0,
        cumulative_loss_usd_micros=0,
        revert_budget_status=RevertBudgetStatus.ACTIVE,
        rehearsal_loopback_anvil=False,
    )
    with pytest.raises(ValueError, match="rejected"):
        verify_signed_bytes_match_admission(raw_signed, rejected)

    # Wrong signer rejected
    wrong_signer_raw = bytes(other_acct.sign_transaction(tx_fields).raw_transaction)
    with pytest.raises(ValueError, match="account"):
        verify_signed_bytes_match_admission(wrong_signer_raw, decision)

    # Calldata tamper rejected
    tampered_cd = bytearray(proposal.calldata)
    tampered_cd[-1] ^= 0x01
    wrong_cd_raw = bytes(
        acct.sign_transaction({**tx_fields, "data": bytes(tampered_cd)}).raw_transaction
    )
    with pytest.raises(ValueError, match="calldata"):
        verify_signed_bytes_match_admission(wrong_cd_raw, decision)

    # Nonce tamper rejected
    wrong_nonce_raw = bytes(
        acct.sign_transaction({**tx_fields, "nonce": decision.nonce + 1}).raw_transaction
    )
    with pytest.raises(ValueError, match="nonce"):
        verify_signed_bytes_match_admission(wrong_nonce_raw, decision)

    # Gas, fee, value, target, chainId tampers rejected
    for field_key, bad_val, match_str in (
        ("gas", decision.gas_limit + 1, "gas_limit"),
        ("maxFeePerGas", decision.max_fee_per_gas_wei + 1, "max_fee_per_gas"),
        ("maxPriorityFeePerGas", decision.max_priority_fee_wei + 1, "max_priority_fee_per_gas"),
        ("value", 1, "value"),
        ("to", EXECUTOR, "target"),
        ("chainId", 1, "chain_id"),
    ):
        bad_raw = bytes(acct.sign_transaction({**tx_fields, field_key: bad_val}).raw_transaction)
        with pytest.raises(ValueError, match=match_str):
            verify_signed_bytes_match_admission(bad_raw, decision)


def test_reconcile_receipt_across_fill_early_abort_revert_and_halt_conditions() -> None:
    acct = Account.create()
    policy = _make_policy(operator_account=acct.address, release_gate_open=True)
    sim = _sample_simulation()
    exec_decision = admit_atomic_or_intent_proposal(
        policy=policy,
        proposal=_sample_executor_proposal(account=acct.address),
        simulation=sim,
        now_s=NOW_S,
        daily_loss_usd_micros=0,
        cumulative_loss_usd_micros=0,
        revert_budget_status=RevertBudgetStatus.ACTIVE,
    )
    vault_decision = admit_atomic_or_intent_proposal(
        policy=policy,
        proposal=_sample_vault_proposal(account=acct.address),
        simulation=sim,
        now_s=NOW_S,
        daily_loss_usd_micros=0,
        cumulative_loss_usd_micros=0,
        revert_budget_status=RevertBudgetStatus.ACTIVE,
    )

    tracker = _sample_tracker()
    native_before = 10**18
    gas_used = 200_000
    eff_price = 1_000_000_000
    l1_fee = 25_000_000_000_000
    expected_spend = gas_used * eff_price + l1_fee

    # 1a. Full fill for OasisAtomicExecutor
    cycle_log = ReceiptLog(
        address=EXECUTOR,
        topics=(
            CYCLE_EXECUTED_TOPIC0,
            "0x" + "00" * 31 + "07",
            "0x" + "00" * 12 + "22" * 20,
            "0x" + "00" * 12 + acct.address[2:].lower(),
        ),
    )
    exec_res = reconcile_atomic_or_intent_receipt(
        decision=exec_decision,
        primary_receipt=_sample_receipt(
            source="alchemy", target=EXECUTOR, gas_used=gas_used, logs=(cycle_log,)
        ),
        secondary_receipt=_sample_receipt(
            source="quicknode", target=EXECUTOR, gas_used=gas_used, logs=(cycle_log,)
        ),
        native_balance_before_wei=native_before,
        native_balance_after_wei=native_before - expected_spend,
        profit_token_balance_before_raw=10_000_000,
        profit_token_balance_after_raw=12_500_000,
        gas_cost_usd_micros=400_000,
        realized_profit_usd_micros=2_500_000,
        tracker=tracker,
        timestamp_s=NOW_S + 1,
    )
    assert exec_res.reconciled is True
    assert exec_res.halt_latch is False
    assert exec_res.outcome is AttemptOutcome.SUCCESS
    assert exec_res.native_residual_wei == 0
    assert exec_res.erc20_profit_delta_raw == 2_500_000
    assert exec_res.revert_budget_status is RevertBudgetStatus.ACTIVE
    assert exec_res.reasons == ()

    # 1b. Full fill for OasisIntentVault with auxiliary Transfer log
    vault_fill_log = ReceiptLog(
        address=INTENT_VAULT,
        topics=(
            REACTOR_FILL_EXECUTED_TOPIC0,
            "0x" + "00" * 12 + "88" * 20,
            "0x" + "00" * 12 + "22" * 20,
            "0x" + "00" * 12 + acct.address[2:].lower(),
        ),
    )
    erc20_transfer_log = ReceiptLog(
        address=TOKEN_A,
        topics=(
            "0x" + keccak(b"Transfer(address,address,uint256)").hex(),
            "0x" + "00" * 12 + "88" * 20,
            "0x" + "00" * 12 + "12" * 20,
        ),
    )
    vault_res = reconcile_atomic_or_intent_receipt(
        decision=vault_decision,
        primary_receipt=_sample_receipt(
            source="alchemy",
            target=INTENT_VAULT,
            gas_used=gas_used,
            logs=(erc20_transfer_log, vault_fill_log),
        ),
        secondary_receipt=_sample_receipt(
            source="quicknode",
            target=INTENT_VAULT,
            gas_used=gas_used,
            logs=(erc20_transfer_log, vault_fill_log),
        ),
        native_balance_before_wei=native_before,
        native_balance_after_wei=native_before - expected_spend,
        profit_token_balance_before_raw=10_000_000,
        profit_token_balance_after_raw=11_000_000,
        gas_cost_usd_micros=400_000,
        realized_profit_usd_micros=1_000_000,
        tracker=tracker,
        timestamp_s=NOW_S + 2,
    )
    assert vault_res.reconciled is True
    assert vault_res.halt_latch is False
    assert vault_res.outcome is AttemptOutcome.SUCCESS
    assert vault_res.erc20_profit_delta_raw == 1_000_000

    # 2. Cheap <65k gas early abort probe for BOTH contracts stays ACTIVE
    abort_gas = 55_000
    abort_spend = abort_gas * eff_price + l1_fee
    exec_abort_log = ReceiptLog(
        address=EXECUTOR,
        topics=(EARLY_ABORTED_TOPIC0, "0x" + "00" * 31 + "07", "0x" + "00" * 12 + "55" * 20),
    )
    abort_exec = reconcile_atomic_or_intent_receipt(
        decision=exec_decision,
        primary_receipt=_sample_receipt(
            source="alchemy", target=EXECUTOR, gas_used=abort_gas, logs=(exec_abort_log,)
        ),
        secondary_receipt=_sample_receipt(
            source="quicknode", target=EXECUTOR, gas_used=abort_gas, logs=(exec_abort_log,)
        ),
        native_balance_before_wei=native_before,
        native_balance_after_wei=native_before - abort_spend,
        profit_token_balance_before_raw=10_000_000,
        profit_token_balance_after_raw=10_000_000,
        gas_cost_usd_micros=100_000,
        realized_profit_usd_micros=0,
        tracker=tracker,
        timestamp_s=NOW_S + 3,
    )
    assert abort_exec.reconciled is True
    assert abort_exec.halt_latch is False
    assert abort_exec.outcome is AttemptOutcome.EARLY_ABORT_PROBE

    vault_abort_log = ReceiptLog(
        address=INTENT_VAULT,
        topics=(
            INTENT_VAULT_EARLY_ABORTED_TOPIC0,
            "0x" + "00" * 12 + "88" * 20,
            "0x" + "00" * 12 + "55" * 20,
        ),
    )
    abort_vault = reconcile_atomic_or_intent_receipt(
        decision=vault_decision,
        primary_receipt=_sample_receipt(
            source="alchemy", target=INTENT_VAULT, gas_used=abort_gas, logs=(vault_abort_log,)
        ),
        secondary_receipt=_sample_receipt(
            source="quicknode", target=INTENT_VAULT, gas_used=abort_gas, logs=(vault_abort_log,)
        ),
        native_balance_before_wei=native_before,
        native_balance_after_wei=native_before - abort_spend,
        profit_token_balance_before_raw=10_000_000,
        profit_token_balance_after_raw=10_000_000,
        gas_cost_usd_micros=100_000,
        realized_profit_usd_micros=0,
        tracker=tracker,
        timestamp_s=NOW_S + 4,
    )
    assert abort_vault.reconciled is True
    assert abort_vault.halt_latch is False
    assert abort_vault.outcome is AttemptOutcome.EARLY_ABORT_PROBE

    # 3. Deep reverts stay ACTIVE up to cap, then latch HALT when cap exceeded
    revert_gas = 120_000
    revert_spend = revert_gas * eff_price + l1_fee
    for step in range(3):
        rev_res = reconcile_atomic_or_intent_receipt(
            decision=exec_decision,
            primary_receipt=_sample_receipt(
                source="alchemy", target=EXECUTOR, status=0, gas_used=revert_gas
            ),
            secondary_receipt=_sample_receipt(
                source="quicknode", target=EXECUTOR, status=0, gas_used=revert_gas
            ),
            native_balance_before_wei=native_before,
            native_balance_after_wei=native_before - revert_spend,
            profit_token_balance_before_raw=10_000_000,
            profit_token_balance_after_raw=10_000_000,
            gas_cost_usd_micros=100_000,
            realized_profit_usd_micros=0,
            tracker=tracker,
            timestamp_s=NOW_S + 10 + step,
        )
        assert rev_res.reconciled is True
        assert rev_res.outcome is AttemptOutcome.DEEP_EXECUTION_REVERT
        if step < 2:
            assert rev_res.halt_latch is False
            assert rev_res.revert_budget_status is RevertBudgetStatus.ACTIVE
        else:
            assert rev_res.halt_latch is True
            assert rev_res.revert_budget_status is RevertBudgetStatus.HALT

    # 4. Provider disagreement -> reconciled=False, halt_latch=True
    disagree_tracker = _sample_tracker()
    disagree_res = reconcile_atomic_or_intent_receipt(
        decision=exec_decision,
        primary_receipt=_sample_receipt(
            source="alchemy", target=EXECUTOR, gas_used=gas_used, logs=(cycle_log,)
        ),
        secondary_receipt=_sample_receipt(
            source="quicknode", target=EXECUTOR, gas_used=gas_used + 1, logs=(cycle_log,)
        ),
        native_balance_before_wei=native_before,
        native_balance_after_wei=native_before - expected_spend,
        profit_token_balance_before_raw=10_000_000,
        profit_token_balance_after_raw=12_500_000,
        gas_cost_usd_micros=400_000,
        realized_profit_usd_micros=2_500_000,
        tracker=disagree_tracker,
        timestamp_s=NOW_S + 20,
    )
    assert disagree_res.reconciled is False
    assert disagree_res.halt_latch is True
    assert disagree_res.outcome is AttemptOutcome.AMBIGUOUS_HALT
    assert disagree_res.revert_budget_status is RevertBudgetStatus.HALT
    assert "receipt_provider_disagreement" in disagree_res.reasons

    # 5. Identical sources -> reconciled=False, halt_latch=True
    same_src_res = reconcile_atomic_or_intent_receipt(
        decision=exec_decision,
        primary_receipt=_sample_receipt(
            source="alchemy", target=EXECUTOR, gas_used=gas_used, logs=(cycle_log,)
        ),
        secondary_receipt=_sample_receipt(
            source="alchemy", target=EXECUTOR, gas_used=gas_used, logs=(cycle_log,)
        ),
        native_balance_before_wei=native_before,
        native_balance_after_wei=native_before - expected_spend,
        profit_token_balance_before_raw=10_000_000,
        profit_token_balance_after_raw=12_500_000,
        gas_cost_usd_micros=400_000,
        realized_profit_usd_micros=2_500_000,
        tracker=_sample_tracker(),
        timestamp_s=NOW_S + 21,
    )
    assert same_src_res.reconciled is False
    assert same_src_res.halt_latch is True
    assert "receipt_sources_not_distinct" in same_src_res.reasons

    # 6. Native residual nonzero -> reconciled=False, halt_latch=True
    native_res = reconcile_atomic_or_intent_receipt(
        decision=exec_decision,
        primary_receipt=_sample_receipt(
            source="alchemy", target=EXECUTOR, gas_used=gas_used, logs=(cycle_log,)
        ),
        secondary_receipt=_sample_receipt(
            source="quicknode", target=EXECUTOR, gas_used=gas_used, logs=(cycle_log,)
        ),
        native_balance_before_wei=native_before,
        native_balance_after_wei=native_before - expected_spend - 1,
        profit_token_balance_before_raw=10_000_000,
        profit_token_balance_after_raw=12_500_000,
        gas_cost_usd_micros=400_000,
        realized_profit_usd_micros=2_500_000,
        tracker=_sample_tracker(),
        timestamp_s=NOW_S + 22,
    )
    assert native_res.reconciled is False
    assert native_res.halt_latch is True
    assert native_res.native_residual_wei == 1
    assert native_res.revert_budget_status is RevertBudgetStatus.HALT
    assert "native_balance_residual_nonzero" in native_res.reasons

    # 7. ERC-20 profit shortfall on full fill -> reconciled=False, halt_latch=True
    erc20_short_res = reconcile_atomic_or_intent_receipt(
        decision=vault_decision,
        primary_receipt=_sample_receipt(
            source="alchemy", target=INTENT_VAULT, gas_used=gas_used, logs=(vault_fill_log,)
        ),
        secondary_receipt=_sample_receipt(
            source="quicknode", target=INTENT_VAULT, gas_used=gas_used, logs=(vault_fill_log,)
        ),
        native_balance_before_wei=native_before,
        native_balance_after_wei=native_before - expected_spend,
        profit_token_balance_before_raw=10_000_000,
        profit_token_balance_after_raw=10_999_999,
        gas_cost_usd_micros=400_000,
        realized_profit_usd_micros=999_999,
        tracker=_sample_tracker(),
        timestamp_s=NOW_S + 23,
    )
    assert erc20_short_res.reconciled is False
    assert erc20_short_res.halt_latch is True
    assert erc20_short_res.erc20_profit_delta_raw == 999_999
    assert "erc20_profit_below_min_net_profit" in erc20_short_res.reasons

    # 8. Unexpected ERC-20 delta on early abort -> reconciled=False, halt_latch=True
    erc20_abort_res = reconcile_atomic_or_intent_receipt(
        decision=vault_decision,
        primary_receipt=_sample_receipt(
            source="alchemy", target=INTENT_VAULT, gas_used=abort_gas, logs=(vault_abort_log,)
        ),
        secondary_receipt=_sample_receipt(
            source="quicknode", target=INTENT_VAULT, gas_used=abort_gas, logs=(vault_abort_log,)
        ),
        native_balance_before_wei=native_before,
        native_balance_after_wei=native_before - abort_spend,
        profit_token_balance_before_raw=10_000_000,
        profit_token_balance_after_raw=9_999_000,
        gas_cost_usd_micros=100_000,
        realized_profit_usd_micros=0,
        tracker=_sample_tracker(),
        timestamp_s=NOW_S + 24,
    )
    assert erc20_abort_res.reconciled is False
    assert erc20_abort_res.halt_latch is True
    assert erc20_abort_res.erc20_profit_delta_raw == -1_000
    assert "unexpected_erc20_balance_change_on_nonfill" in erc20_abort_res.reasons

    # 9. Cross-contract event signature mismatch (executor event on intent vault) -> halt
    cross_res = reconcile_atomic_or_intent_receipt(
        decision=vault_decision,
        primary_receipt=_sample_receipt(
            source="alchemy",
            target=INTENT_VAULT,
            gas_used=gas_used,
            logs=(
                ReceiptLog(
                    address=INTENT_VAULT,
                    topics=cycle_log.topics,
                ),
            ),
        ),
        secondary_receipt=_sample_receipt(
            source="quicknode",
            target=INTENT_VAULT,
            gas_used=gas_used,
            logs=(
                ReceiptLog(
                    address=INTENT_VAULT,
                    topics=cycle_log.topics,
                ),
            ),
        ),
        native_balance_before_wei=native_before,
        native_balance_after_wei=native_before - expected_spend,
        profit_token_balance_before_raw=10_000_000,
        profit_token_balance_after_raw=12_000_000,
        gas_cost_usd_micros=400_000,
        realized_profit_usd_micros=2_000_000,
        tracker=_sample_tracker(),
        timestamp_s=NOW_S + 25,
    )
    assert cross_res.reconciled is False
    assert cross_res.halt_latch is True
    assert cross_res.outcome is AttemptOutcome.AMBIGUOUS_HALT

    # 10. Oversized (>65k gas) early abort classifies as DEEP_EXECUTION_REVERT
    oversized_gas = 65_001
    oversized_spend = oversized_gas * eff_price + l1_fee
    oversized_res = reconcile_atomic_or_intent_receipt(
        decision=vault_decision,
        primary_receipt=_sample_receipt(
            source="alchemy", target=INTENT_VAULT, gas_used=oversized_gas, logs=(vault_abort_log,)
        ),
        secondary_receipt=_sample_receipt(
            source="quicknode",
            target=INTENT_VAULT,
            gas_used=oversized_gas,
            logs=(vault_abort_log,),
        ),
        native_balance_before_wei=native_before,
        native_balance_after_wei=native_before - oversized_spend,
        profit_token_balance_before_raw=10_000_000,
        profit_token_balance_after_raw=10_000_000,
        gas_cost_usd_micros=100_000,
        realized_profit_usd_micros=0,
        tracker=_sample_tracker(),
        timestamp_s=NOW_S + 26,
    )
    assert oversized_res.reconciled is True
    assert oversized_res.outcome is AttemptOutcome.DEEP_EXECUTION_REVERT

    # 11. Nonzero realized_profit_usd_micros on an early abort fails closed
    bad_profit_res = reconcile_atomic_or_intent_receipt(
        decision=exec_decision,
        primary_receipt=_sample_receipt(
            source="alchemy", target=EXECUTOR, gas_used=abort_gas, logs=(exec_abort_log,)
        ),
        secondary_receipt=_sample_receipt(
            source="quicknode", target=EXECUTOR, gas_used=abort_gas, logs=(exec_abort_log,)
        ),
        native_balance_before_wei=native_before,
        native_balance_after_wei=native_before - abort_spend,
        profit_token_balance_before_raw=10_000_000,
        profit_token_balance_after_raw=10_000_000,
        gas_cost_usd_micros=100_000,
        realized_profit_usd_micros=500,
        tracker=_sample_tracker(),
        timestamp_s=NOW_S + 27,
    )
    assert bad_profit_res.reconciled is False
    assert bad_profit_res.halt_latch is True
    assert "nonzero_realized_profit_on_nonfill" in bad_profit_res.reasons

    # 12. Rejected AdmissionDecision cannot be reconciled
    rejected = admit_atomic_or_intent_proposal(
        policy=_make_policy(operator_account=acct.address, release_gate_open=False),
        proposal=_sample_executor_proposal(account=acct.address),
        simulation=sim,
        now_s=NOW_S,
        daily_loss_usd_micros=0,
        cumulative_loss_usd_micros=0,
        revert_budget_status=RevertBudgetStatus.ACTIVE,
    )
    with pytest.raises(ValueError, match="rejected"):
        reconcile_atomic_or_intent_receipt(
            decision=rejected,
            primary_receipt=_sample_receipt(
                source="alchemy", target=EXECUTOR, gas_used=gas_used, logs=(cycle_log,)
            ),
            secondary_receipt=_sample_receipt(
                source="quicknode", target=EXECUTOR, gas_used=gas_used, logs=(cycle_log,)
            ),
            native_balance_before_wei=native_before,
            native_balance_after_wei=native_before - expected_spend,
            profit_token_balance_before_raw=10_000_000,
            profit_token_balance_after_raw=12_500_000,
            gas_cost_usd_micros=400_000,
            realized_profit_usd_micros=2_500_000,
            tracker=_sample_tracker(),
            timestamp_s=NOW_S + 28,
        )


def test_empty_steps_vault_fill_and_legacy_or_access_list_tx_rejection() -> None:
    acct = Account.create()
    policy = _make_policy(operator_account=acct.address, release_gate_open=True)
    # Inventory-backed vault fill (0 swap steps + active precheck pool) admits cleanly.
    empty_steps: list[tuple[str, str, str, str, int, int, bytes]] = []
    plan = (
        REACTOR,
        TOKEN_A,
        10**6,
        (POOL_V3, UINT160_SQRT_1 - 10, UINT160_SQRT_1 + 10),
        empty_steps,
    )
    reactor_cd = EXECUTE_WITH_CALLBACK_SELECTOR + encode(["bytes", "bytes"], [b"order", b"cb"])
    vault_cd = EXECUTE_REACTOR_FILL_SELECTOR + encode(
        list(EXECUTE_REACTOR_FILL_PARAM_TYPES), [plan, reactor_cd]
    )
    prop = EnvelopeProposal(
        chain_id=8453,
        account=acct.address,
        nonce=9,
        target=INTENT_VAULT,
        target_observed_code_hash=INTENT_VAULT_CODE_HASH,
        value_wei=0,
        calldata=vault_cd,
        gas_limit=300_000,
        max_fee_per_gas_wei=1_000_000_000,
        max_priority_fee_wei=50_000_000,
        profit_token=TOKEN_A,
        min_net_profit_raw=10**6,
        referenced_pools=(POOL_V3,),
        referenced_routers=(),
        referenced_reactor=REACTOR,
        deadline_s=NOW_S + 10,
    )
    decision = admit_atomic_or_intent_proposal(
        policy=policy,
        proposal=prop,
        simulation=_sample_simulation(),
        now_s=NOW_S,
        daily_loss_usd_micros=0,
        cumulative_loss_usd_micros=0,
        revert_budget_status=RevertBudgetStatus.ACTIVE,
    )
    assert decision.admitted is True
    assert decision.target_kind == "intent_vault"

    # Legacy (non-type-2) and EIP-1559 accessList transactions are rejected.
    legacy_raw = bytes(
        acct.sign_transaction(
            {
                "chainId": decision.chain_id,
                "nonce": decision.nonce,
                "to": decision.target,
                "value": decision.value_wei,
                "data": prop.calldata,
                "gas": decision.gas_limit,
                "gasPrice": decision.max_fee_per_gas_wei,
            }
        ).raw_transaction
    )
    with pytest.raises(ValueError, match="EIP-1559"):
        verify_signed_bytes_match_admission(legacy_raw, decision)

    access_list_raw = bytes(
        acct.sign_transaction(
            {
                "type": 2,
                "chainId": decision.chain_id,
                "nonce": decision.nonce,
                "to": decision.target,
                "value": decision.value_wei,
                "data": prop.calldata,
                "gas": decision.gas_limit,
                "maxFeePerGas": decision.max_fee_per_gas_wei,
                "maxPriorityFeePerGas": decision.max_priority_fee_wei,
                "accessList": [{"address": TOKEN_A, "storageKeys": []}],
            }
        ).raw_transaction
    )
    with pytest.raises(ValueError, match="access list"):
        verify_signed_bytes_match_admission(access_list_raw, decision)
