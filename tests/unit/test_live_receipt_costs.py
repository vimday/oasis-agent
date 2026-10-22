"""Real live pending transitions over synthetic raw two-provider cost proofs.

No RPC endpoint or signing authority is contacted. Costs must include Base L1
and operator fees on success and revert, and refusals must preserve pending.
"""

import json
import sys
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

import pytest
from eth_abi.abi import encode
from eth_hash.auto import keccak

from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.farming.transaction_costs import replay_transaction_cost
from oasis_agent.live.config import LiveConfig
from oasis_agent.live.positions import replay_position_outcome
from oasis_agent.live.receipts import RetainedReceiptCostReader
from oasis_agent.live.rpc_profile import LiveRpcProfile
from oasis_agent.live.runner import TRANSFER_TOPIC, LiveLoop
from oasis_agent.live.state import LoopState, Pending, Position, StateStore
from oasis_agent.models.chain import BlockSnapshot
from oasis_agent.models.common import canonical_json, sha256_hex
from oasis_agent.rpc.budget import BudgetPolicy, RpcBudget
from scripts import run_live_lp as live_cli
from scripts.run_live_lp import receipt_cost_reader
from tests.unit.test_live_lp import BLOCK_HASH, CONFIG, ScriptedRpc, bind_pending
from tests.unit.test_lp_receipts import PRIMARY, SECONDARY, ReceiptFixture, sessions


@dataclass
class LiveCostFixture(ReceiptFixture):
    chain_id: int = 8453
    payer: str = CONFIG.account
    block_number: int = 100
    block_hash: str = BLOCK_HASH
    overrides: dict[str, Any] = field(default_factory=dict)
    position_enabled: bool = False
    position_burned: bool = False
    position_owner: str = CONFIG.account
    position_info: int | None = None
    manager_liquidity: int = 77
    core_liquidity: int | None = None
    position_key: tuple[Any, ...] = CONFIG.pool.tuple()

    def post(self, payload: bytes) -> bytes:
        request = json.loads(payload)
        if self.position_enabled and request["method"] == "eth_call":
            selector = request["params"][0]["data"][:10]
            packed = int(CONFIG.pool_id[:52], 16) << 56 | (100 << 32) | (((-100) & 0xFFFFFF) << 8)
            info = (
                self.position_info
                if self.position_info is not None
                else (0 if self.position_burned else packed)
            )
            core = (
                self.core_liquidity
                if self.core_liquidity is not None
                else (0 if self.position_burned else 77)
            )
            answers = {
                "ownerOf(uint256)": (["address"], [self.position_owner]),
                "getPoolAndPositionInfo(uint256)": (
                    ["(address,address,uint24,int24,address)", "uint256"],
                    [self.position_key, info],
                ),
                "getPositionLiquidity(uint256)": (["uint128"], [self.manager_liquidity]),
                "positionInfo(uint256)": (["uint256"], [info]),
                "getPositionLiquidity(bytes32,bytes32)": (["uint128"], [core]),
            }
            for signature, (types, values) in answers.items():
                if selector == "0x" + keccak(signature.encode())[:4].hex():
                    self.methods.append(request["method"])
                    return canonical_json(
                        {
                            "jsonrpc": "2.0",
                            "id": request["id"],
                            "result": "0x" + encode(types, values).hex(),
                        }
                    )
        return super().post(payload)

    def _receipt(self) -> dict[str, Any]:
        return super()._receipt() | {"to": CONFIG.position_manager, "logs": []} | self.overrides


class NeverSign:
    def sign_envelope(self, *_: Any) -> bytes:
        raise AssertionError("unexpected authority call")

    account = CONFIG.account

    def sign(self, digest: bytes) -> tuple[int, int, int]:
        raise AssertionError("pending resolution must never sign")


def position_config() -> LiveConfig:
    runtime = "0x" + keccak(bytes.fromhex("6000")).hex()
    return replace(
        CONFIG,
        expected_pins=CONFIG.expected_pins
        | {
            "position_manager": runtime,
            "state_view": runtime,
        },
    )


@pytest.fixture
def evidence(tmp_path: Path) -> Iterator[EvidenceStore]:
    store = EvidenceStore(tmp_path / "evidence.db")
    yield store
    store.close()


def loop_for(
    tmp_path: Path,
    evidence: EvidenceStore,
    primary: LiveCostFixture,
    secondary: LiveCostFixture | None = None,
    *,
    receipt: dict[str, Any] | None = None,
    kind: str = "swap",
    config: LiveConfig = CONFIG,
) -> LiveLoop:
    observed = primary._receipt() if receipt is None else receipt
    reader = RetainedReceiptCostReader(
        evidence,
        chain_id=CONFIG.chain_id,
        account=CONFIG.account,
        native_decimals=18,
        sources=(PRIMARY, SECONDARY),
        sessions=sessions({PRIMARY: primary, SECONDARY: secondary or replace(primary)}),
    )
    loop = LiveLoop(
        config,
        ScriptedRpc(receipt=observed),
        StateStore(tmp_path / "live.state.json"),
        mode="live",
        signer=NeverSign(),
        secondary=ScriptedRpc(receipt=observed),
        receipt_costs=reader,
        log=lambda _: None,
    )
    loop.state.pending = Pending(
        kind,
        primary.transaction_hash,
        3,
        10**10,
        {"lower": -100, "upper": 100, "liquidity": 77},
        CONFIG.position_manager,
    )
    bind_pending(loop.state.pending, config=config)
    loop.store.save(loop.state)
    return loop


def test_success_retains_whole_cost_but_cannot_settle_without_cash(
    tmp_path: Path, evidence: EvidenceStore
) -> None:
    fixture = LiveCostFixture(status=1)
    loop = loop_for(tmp_path, evidence, fixture)
    # 100 * 2 execution + 7 L1 + 11 operator; not execution-only 200.
    result = loop.tick()
    assert "cash unresolved" in result
    state = loop.store.load("live")
    assert state.pending is not None and state.gas_spent_wei == 0
    assert state.actions_today == 0 and state.consecutive_reverts == 0
    cost, refs = loop._receipt_cost(state.pending, fixture._receipt())
    assert cost == 218
    assert len(refs) == 2 and {ref.source for ref in refs} == {PRIMARY, SECONDARY}
    for ref in refs:
        assert ref.block is not None
        proof = replay_transaction_cost(
            evidence,
            ref,
            header=BlockSnapshot(ref.block, fixture.parent_hash),
            transaction_hash=fixture.transaction_hash,
        )
        assert proof.reconstruct().total_native == 218


@pytest.mark.parametrize("kind", ["mint", "burn"])
def test_lp_proofs_do_not_clear_pending_without_owner_cash(
    tmp_path: Path, evidence: EvidenceStore, kind: str
) -> None:
    zero, owner = "0x" + "0" * 64, "0x" + "0" * 24 + CONFIG.account[2:]
    sender, recipient = (zero, owner) if kind == "mint" else (owner, zero)
    log = {
        "address": CONFIG.position_manager,
        "topics": [TRANSFER_TOPIC, sender, recipient, "0x" + f"{123:064x}"],
    }
    fixture = LiveCostFixture(
        overrides={"logs": [log]}, position_enabled=True, position_burned=kind == "burn"
    )
    loop = loop_for(tmp_path, evidence, fixture, kind=kind, config=position_config())
    if kind == "burn":
        loop.state.position = Position(123, -100, 100, 77, 0)
        bind_pending(loop.state.pending, loop.state.position, config=loop.c)
    assert "cash unresolved" in loop.tick()
    state = loop.store.load("live")
    assert state.gas_spent_wei == 0 and state.pending is not None
    assert (state.position is not None) == (kind == "burn")
    _, costs = loop._receipt_cost(state.pending, fixture._receipt())
    refs = loop._position_outcome(state.pending, fixture._receipt(), costs)
    assert len(refs) == 2 and {r.source for r in refs} == {PRIMARY, SECONDARY}
    for ref in refs:
        capture = replay_position_outcome(evidence, ref)
        assert capture.scope.token_id == 123 and capture.block.block_hash == BLOCK_HASH
        assert capture.scope.burned == (kind == "burn")
        assert capture.scope.liquidity == (0 if kind == "burn" else 77)
        assert capture.validate().ref == ref.block
    assert state.pending.transaction is not None
    assert state.pending.transaction.transaction_hash == fixture.transaction_hash
    # Offline replay validates the raw calls, not just the retained interpretation.
    capture = replay_position_outcome(evidence, refs[0])
    for index, result in ((0, "0x1"), (2, "0x6001"), (-2, "0x" + encode(["uint128"], [1]).hex())):
        exchanges = list(capture.exchanges)
        response = json.loads(exchanges[index].response_payload)
        response["result"] = result
        exchanges[index] = replace(exchanges[index], response_payload=canonical_json(response))
        with pytest.raises(ValueError):
            replace(capture, exchanges=tuple(exchanges)).validate()


@pytest.mark.parametrize(
    ("kind", "changes"),
    [
        ("mint", {"position_owner": "0x" + "ab" * 20}),
        ("mint", {"position_key": (*CONFIG.pool.tuple()[:2], 3000, 60, CONFIG.pool.hooks)}),
        ("mint", {"position_info": 0}),
        ("mint", {"position_info": int(CONFIG.pool_id[:52], 16) << 56 | 1}),
        ("mint", {"manager_liquidity": 78}),
        ("mint", {"core_liquidity": 78}),
        ("burn", {"position_info": 1}),
        ("burn", {"core_liquidity": 1}),
    ],
)
def test_position_refusal_preserves_pending_until_both_providers_prove_outcome(
    tmp_path: Path, evidence: EvidenceStore, kind: str, changes: dict[str, Any]
) -> None:
    zero, owner = "0x" + "0" * 64, "0x" + "0" * 24 + CONFIG.account[2:]
    log = {
        "address": CONFIG.position_manager,
        "topics": [
            TRANSFER_TOPIC,
            *((zero, owner) if kind == "mint" else (owner, zero)),
            "0x" + f"{123:064x}",
        ],
    }
    primary = LiveCostFixture(
        overrides={"logs": [log]}, position_enabled=True, position_burned=kind == "burn"
    )
    secondary = replace(primary, **changes)
    loop = loop_for(tmp_path, evidence, primary, secondary, kind=kind, config=position_config())
    if kind == "burn":
        loop.state.position = Position(123, -100, 100, 77, 0)
        bind_pending(loop.state.pending, loop.state.position, config=loop.c)
    loop.store.save(loop.state)
    before = loop.store.path.read_bytes()
    assert "position outcome unresolved" in loop.tick()
    assert loop.store.path.read_bytes() == before
    # Agreement on the position still cannot replace complete owner cash accounting.
    loop.state = loop.store.load("live")
    for name in changes:
        setattr(secondary, name, getattr(primary, name))
    assert "cash unresolved" in loop.tick()
    assert loop.state.pending is not None and loop.state.gas_spent_wei == 0
    assert loop.state.actions_today == 0


@pytest.mark.parametrize("change", ["missing", "intent", "config", "position", "nonce", "chain"])
def test_changed_pending_plan_refuses_before_any_rpc_read(
    tmp_path: Path, evidence: EvidenceStore, change: str
) -> None:
    loop = loop_for(tmp_path, evidence, LiveCostFixture())
    pending = loop.state.pending
    assert pending is not None and pending.transaction is not None
    if change == "missing":
        pending.transaction = None
    elif change == "intent":
        pending.intent["liquidity"] += 1
    elif change == "config":
        loop.c = replace(loop.c, half_width_ticks=500)
    elif change == "position":
        loop.state.position = Position(123, -100, 100, 77, 0)
    elif change == "nonce":
        pending.nonce += 1
    else:
        pending.transaction = replace(pending.transaction, chain_id=1)

    class NoRpc:
        def receipt(self, _: str) -> None:
            raise AssertionError("unbound pending must refuse before RPC")

    loop.rpc = NoRpc()
    loop.store.save(loop.state)
    before = loop.store.path.read_bytes()
    assert "pending" in loop.tick()
    assert loop.store.path.read_bytes() == before
    assert loop.state.gas_spent_wei == 0


@pytest.mark.parametrize(
    "secondary",
    [
        LiveCostFixture(operator_fee=12),
        LiveCostFixture(l1_fee=8),
        LiveCostFixture(chain_id=1),
        LiveCostFixture(header_hash="0x" + "bb" * 32),
        LiveCostFixture(has_receipt=False),
    ],
)
def test_cost_disagreement_or_missing_proof_preserves_position_and_pending(
    tmp_path: Path, evidence: EvidenceStore, secondary: LiveCostFixture
) -> None:
    loop = loop_for(tmp_path, evidence, LiveCostFixture(), secondary)
    loop.state.position = Position(123, -100, 100, 77, 0)
    bind_pending(loop.state.pending, loop.state.position)
    loop.store.save(loop.state)
    before = loop.store.path.read_bytes()
    assert "whole native receipt cost unresolved" in loop.tick()
    assert loop.store.path.read_bytes() == before


@pytest.mark.parametrize(
    "overrides",
    [
        {"to": "0x" + "ab" * 20},
        {"logs": [{"address": "0x" + "ab" * 20}]},
        {"effectiveGasPrice": "0x3"},
        {"blockHash": "0x" + "ab" * 32},
        {"type": "0x1"},
    ],
)
def test_cost_proof_cannot_be_attached_to_a_different_live_receipt(
    tmp_path: Path, evidence: EvidenceStore, overrides: dict[str, Any]
) -> None:
    fixture = LiveCostFixture(overrides=overrides)
    loop = loop_for(tmp_path, evidence, fixture, receipt=LiveCostFixture()._receipt())
    assert "unresolved" in loop.tick()
    assert loop.state.pending is not None and loop.state.actions_today == 0
    assert loop.state.gas_spent_wei == 0


def test_persistence_failure_retains_pending_and_redacts_error(
    tmp_path: Path, evidence: EvidenceStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*args: Any, **kwargs: Any) -> None:
        raise OSError("https://secret-provider.invalid/token")

    monkeypatch.setattr(evidence, "put", refuse)
    loop = loop_for(tmp_path, evidence, LiveCostFixture())
    result = loop.tick()
    assert "unresolved" in result and "secret-provider" not in result
    assert loop.store.load("live").pending is not None and loop.state.gas_spent_wei == 0


def test_crash_cannot_turn_cost_only_evidence_into_settlement(
    tmp_path: Path, evidence: EvidenceStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = LiveCostFixture()
    loop = loop_for(tmp_path, evidence, fixture)

    def fail_save(*args: Any) -> None:
        raise OSError("synthetic state publication failure")

    with monkeypatch.context() as patch:
        patch.setattr(loop.store, "save", fail_save)
        with pytest.raises(OSError, match="publication"):
            loop.tick()
    assert loop.store.load("live").pending is not None
    # Same disk state after restart; already retained evidence is safe to reacquire.
    loop.state = loop.store.load("live")
    assert "cash unresolved" in loop.tick()
    assert loop.store.load("live").gas_spent_wei == 0
    assert loop.state.actions_today == 0 and loop.state.pending is not None


def test_live_constructor_refuses_missing_cost_reader(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="retained receipt cost"):
        LiveLoop(
            CONFIG,
            ScriptedRpc(),
            StateStore(tmp_path / "live.json"),
            mode="live",
            signer=NeverSign(),
            secondary=ScriptedRpc(),
        )


def test_cli_receipt_factory_freezes_budget_and_rejects_duplicate_endpoint(
    tmp_path: Path, evidence: EvidenceStore
) -> None:
    with pytest.raises(ValueError, match="distinct endpoints"):
        receipt_cost_reader(
            CONFIG, evidence, tmp_path, "https://same.invalid/", "https://same.invalid"
        )
    reader = receipt_cost_reader(
        CONFIG, evidence, tmp_path, "https://primary.invalid", "https://secondary.invalid"
    )
    assert reader.account == CONFIG.account and reader.chain_id == CONFIG.chain_id
    path = tmp_path / "live.receipt-budget.db"
    policies = tuple(BudgetPolicy(source, 4096, 3000) for source in reader.sources)
    with RpcBudget(path, policies) as budget:
        assert budget.reserve("live_primary", now_ms=1) == 0
    receipt_cost_reader(
        CONFIG, evidence, tmp_path, "https://primary.invalid", "https://secondary.invalid"
    )
    with RpcBudget(path, policies) as budget:
        assert budget.counts()["live_primary"] == 1


def test_legacy_execution_only_gas_is_preserved_and_reported_incomplete(
    tmp_path: Path, evidence: EvidenceStore
) -> None:
    loop = loop_for(tmp_path, evidence, LiveCostFixture())
    old = json.loads(loop.store.path.read_bytes())
    old.pop("legacy_gas_cost_incomplete")
    old["gas_spent_wei"] = 200
    # Legacy migration uses an unbound old file; editing a journal projection is refused.
    old["journal_required"] = False
    loop.store = StateStore(tmp_path / "legacy.state.json")
    loop.store.path.write_text(json.dumps(old))
    loop.state = loop.store.load("live")
    assert loop.state.legacy_gas_cost_incomplete
    assert "cash unresolved" in loop.tick()
    assert loop.state.gas_spent_wei == 200
    assert loop.store.load("live").legacy_gas_cost_incomplete
    assert loop.report()["legacy_gas_cost_incomplete"] is True


def test_missing_base_l1_cost_is_never_zero(tmp_path: Path, evidence: EvidenceStore) -> None:
    loop = loop_for(tmp_path, evidence, LiveCostFixture(overrides={"l1Fee": None}))
    assert "unresolved" in loop.tick()
    assert loop.state.pending is not None and loop.state.gas_spent_wei == 0


def test_live_cli_refuses_missing_opening_evidence_before_cost_acquisition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = LiveCostFixture()
    pending = Pending("swap", fixture.transaction_hash, 3, 10**10, {}, CONFIG.position_manager)
    bind_pending(pending)
    state = StateStore(tmp_path / "live.state.json")
    state.save(LoopState(mode="live", pending=pending))
    state.bind(sha256_hex(canonical_json(asdict(CONFIG))))
    with RpcBudget(
        tmp_path / "live.receipt-budget.db",
        LiveRpcProfile().policies(("live_primary", "live_secondary")),
    ):
        pass

    def forbidden(*args, **kwargs):
        raise AssertionError("missing opening proof must refuse before RPC or signer access")

    for name in ("one_shot_session", "rpc_url", "BudgetedRpc", "TransactionSocketClient"):
        monkeypatch.setattr(live_cli, name, forbidden)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_live_lp",
            "--mode",
            "live",
            "--state-dir",
            str(tmp_path),
            "--reconcile-only",
        ],
    )
    with pytest.raises(SystemExit) as exc:
        live_cli.main()
    assert exc.value.code == 3
    result = state.load("live")
    assert result.pending is not None and result.gas_spent_wei == 0
    assert not (tmp_path / "live.receipts.db").exists()
