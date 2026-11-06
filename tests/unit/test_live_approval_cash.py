"""Synthetic raw approvals: exact allowance outcome, whole gas and wallet conservation."""

import json
from contextlib import closing
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

import pytest
from eth_abi.abi import encode
from eth_hash.auto import keccak

from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.live.approval_cash import replay_approval_cash
from oasis_agent.live.chain import ChainError
from oasis_agent.live.exact_risk import ExactRisk
from oasis_agent.live.intent import pending_plan_hash
from oasis_agent.live.state import day_of
from oasis_agent.live.v4lp import erc20_approve_call, permit2_approve_call
from oasis_agent.models.common import canonical_json, evidence_ref_from_json
from oasis_agent.reporting.live_status import read_live_status
from tests.unit.test_live_lp import CONFIG
from tests.unit.test_live_receipt_costs import loop_for
from tests.unit.test_live_revert_cash import WalletFixture, attach_opening

TEST_CONFIG = replace(
    CONFIG,
    expected_pins={
        **CONFIG.expected_pins,
        "permit2": "0x" + keccak(bytes.fromhex("6000")).hex(),
    },
)


@dataclass
class ApprovalFixture(WalletFixture):
    status: int = 1
    kind: str = "approve_usdc"
    approved_amount: int = CONFIG.approval_usdc
    expiry_delta: int = 0
    permit_nonce_after: int = 7
    permit_code_after: str = "0x6000"

    @property
    def expiration(self) -> int:
        return self.opening.timestamp + CONFIG.approval_days * 86_400

    @property
    def target(self) -> str:
        return CONFIG.pool.currency1 if self.kind == "approve_usdc" else CONFIG.permit2

    def _receipt(self) -> dict[str, Any]:
        return super()._receipt() | {"to": self.target}

    def post(self, payload: bytes) -> bytes:
        request = json.loads(payload)
        method, params = request["method"], request["params"]
        before = bool(
            params
            and isinstance(params[-1], dict)
            and params[-1].get("blockHash") == self.opening.block_hash
        )
        if method == "eth_getCode" and params[0] == CONFIG.permit2:
            value = "0x6000" if before else self.permit_code_after
        elif method == "eth_call" and params[0]["to"] == CONFIG.permit2:
            values = (
                (0, 0, 7)
                if before
                else (
                    self.approved_amount,
                    self.expiration + self.expiry_delta,
                    self.permit_nonce_after,
                )
            )
            value = "0x" + encode(["uint160", "uint48", "uint48"], values).hex()
        elif (
            method == "eth_call"
            and params[0]["to"] == CONFIG.pool.currency1
            and params[0]["data"].startswith("0xdd62ed3e")
        ):
            value = "0x" + encode(["uint256"], [self.approved_amount]).hex()
        else:
            return super().post(payload)
        self.methods.append(method)
        return canonical_json({"jsonrpc": "2.0", "id": request["id"], "result": value})


def bind_approval(loop: Any, fixture: ApprovalFixture) -> None:
    p = loop.state.pending
    data = erc20_approve_call(CONFIG.permit2, p.intent["amount"])
    if p.kind == "approve_permit2":
        data = permit2_approve_call(
            CONFIG.pool.currency1, p.intent["spender"], p.intent["amount"], p.intent["expiration"]
        )
    p.transaction = replace(p.transaction, target=p.to, calldata_hash="0x" + keccak(data).hex())
    rebind(loop)


def rebind(loop: Any) -> None:
    p = loop.state.pending
    p.plan_hash = pending_plan_hash(
        asdict(loop.c),
        p.kind,
        p.intent,
        p.transaction,
        None,
        tuple(asdict(ref) for ref in p.wallet_evidence),
    )
    loop.store.save(loop.state)


def approval_loop(
    tmp_path: Path, store: EvidenceStore, a: ApprovalFixture, b: ApprovalFixture
) -> Any:
    loop = loop_for(tmp_path, store, a, b, kind=a.kind, config=TEST_CONFIG)
    p = loop.state.pending
    p.to = a.target
    p.intent = {"amount": CONFIG.approval_usdc}
    if a.kind == "approve_permit2":
        p.intent.update(spender=CONFIG.position_manager, expiration=a.expiration)
    attach_opening(loop, a, b)
    bind_approval(loop, a)
    return loop


@pytest.mark.parametrize(
    "kind,spender",
    [
        ("approve_usdc", None),
        ("approve_permit2", CONFIG.position_manager),
        ("approve_permit2", CONFIG.universal_router),
    ],
)
def test_approval_advances_only_with_replayable_outcome_and_exact_wallet_cash(
    tmp_path: Path, kind: str, spender: str | None
) -> None:
    with closing(EvidenceStore(tmp_path / "evidence.db")) as store:
        a, b = ApprovalFixture(kind=kind), ApprovalFixture(kind=kind)
        loop = approval_loop(tmp_path, store, a, b)
        if spender is not None:
            loop.state.pending.intent["spender"] = spender
            bind_approval(loop, a)
        loop.state.last_observation = {"pre_action": True}
        assert "confirmed" in loop.tick()
        assert loop.state.pending is None and loop.state.last_units == [99_782, 1_000, 0]
        assert loop.state.gas_spent_wei == 218 and loop.state.actions_today == 1
        assert loop.state.last_observation is None
        report = read_live_status(loop.store.path)
        assert report.recent_actions[0].cash_references == 2
        assert report.observation is None and not report.evidence_replayed
        refs = [evidence_ref_from_json(r) for r in loop.state.history[-1]["cash_evidence"]]
        assert len(refs) == 2
        for ref in refs:
            proof = replay_approval_cash(store, ref, TEST_CONFIG)
            balance, nonce = proof.reconstruct(store, TEST_CONFIG)
            assert balance.native == 99_782 and nonce == (7 if kind == "approve_permit2" else None)


@pytest.mark.parametrize(
    "kind,change",
    [
        ("approve_usdc", {"approved_amount": 1}),
        ("approve_usdc", {"native_after": 99_783}),
        ("approve_usdc", {"native_after": 99_781}),
        ("approve_usdc", {"token_after": 1_001}),
        ("approve_usdc", {"nonce_after": 5}),
        ("approve_usdc", {"transfer_logs": [{"offsetting": True}]}),
        ("approve_usdc", {"account_code": "0xef0100"}),
        ("approve_usdc", {"wrong_parent": True}),
        ("approve_permit2", {"approved_amount": 1}),
        ("approve_permit2", {"expiry_delta": 1}),
        ("approve_permit2", {"permit_nonce_after": 8}),
        ("approve_permit2", {"permit_code_after": "0x6001"}),
    ],
)
def test_approval_outcome_or_cash_disagreement_keeps_pending(
    tmp_path: Path, kind: str, change: dict[str, Any]
) -> None:
    with closing(EvidenceStore(tmp_path / "evidence.db")) as store:
        a, b = ApprovalFixture(kind=kind), ApprovalFixture(kind=kind)
        loop = approval_loop(tmp_path, store, a, b)
        for name, value in change.items():
            setattr(b, name, value)
        original = json.loads(loop.store.path.read_bytes())
        assert "unresolved" in loop.tick()
        assert json.loads(loop.store.path.read_bytes()) == original | {"settlement_attempts": 1}
        assert loop.state.pending is not None and loop.state.gas_spent_wei == 0


@pytest.mark.parametrize(
    "change", ["value", "calldata", "spender", "amount", "expiration", "baseline"]
)
def test_success_receipt_cannot_validate_a_different_or_unbounded_approval(
    tmp_path: Path, change: str
) -> None:
    with closing(EvidenceStore(tmp_path / "evidence.db")) as store:
        a = ApprovalFixture(kind="approve_permit2")
        loop = approval_loop(tmp_path, store, a, ApprovalFixture(kind=a.kind))
        p = loop.state.pending
        if change == "value":
            p.transaction = replace(p.transaction, value=1)
        elif change == "calldata":
            p.transaction = replace(p.transaction, calldata_hash="0x" + "ab" * 32)
        elif change == "spender":
            p.intent["spender"] = "0x" + "cc" * 20
            bind_approval(loop, a)
        elif change == "amount":
            p.intent["amount"] += 1
            bind_approval(loop, a)
        elif change == "expiration":
            p.intent["expiration"] += 1
            bind_approval(loop, a)
        else:
            p.wallet_evidence = ()
        rebind(loop)
        assert "approval cash unresolved" in loop.tick()
        assert loop.state.pending is not None and loop.state.actions_today == 0


def test_approval_restart_after_failed_state_publication_counts_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with closing(EvidenceStore(tmp_path / "evidence.db")) as store:
        loop = approval_loop(tmp_path, store, ApprovalFixture(), ApprovalFixture())

        def fail(*_: Any) -> None:
            raise OSError("publication failed")

        with monkeypatch.context() as patch:
            patch.setattr(loop.store, "save", fail)
            with pytest.raises(OSError):
                loop.tick()
        loop.state = loop.store.load("live")
        assert loop.state.pending is not None
        assert "confirmed" in loop.tick()
        assert loop.state.gas_spent_wei == 218 and loop.state.actions_today == 1


def test_approval_replay_refuses_rewritten_intent_and_foreign_action(tmp_path: Path) -> None:
    with closing(EvidenceStore(tmp_path / "evidence.db")) as store:
        loop = approval_loop(tmp_path, store, ApprovalFixture(), ApprovalFixture())
        assert "confirmed" in loop.tick()
        ref = evidence_ref_from_json(loop.state.history[-1]["cash_evidence"][0])
        proof = replay_approval_cash(store, ref, TEST_CONFIG)
        with pytest.raises(ValueError, match="only bounded approval"):
            replace(proof, kind="swap").reconstruct(store, TEST_CONFIG)
        # Even consistent rewritten metadata cannot override the retained allowance answer.
        record = store.get_record(ref)
        data = json.loads(record.payload)
        data["intent"]["amount"] -= 1
        data["transaction"]["calldata_hash"] = (
            "0x" + keccak(erc20_approve_call(CONFIG.permit2, data["intent"]["amount"])).hex()
        )
        forged = store.put(
            "live_approval_cash",
            canonical_json(data),
            ref.block,
            ref.source,
            request=json.loads(record.request_payload),
            raw_response=json.loads(record.raw_response_payload),
            interpretation={"execution_eligible": False},
        )
        with pytest.raises(ValueError, match="approval outcome"):
            replay_approval_cash(store, forged, TEST_CONFIG)


def settled_with_baseline(tmp_path: Path, store: EvidenceStore) -> Any:
    """Live state after a confirmed approval: risk baseline kept, NAV proofs cleared."""
    a, b = ApprovalFixture(), ApprovalFixture()
    loop = approval_loop(tmp_path, store, a, b)
    risk = ExactRisk()
    at = a.opening.timestamp
    risk.observe(day=day_of(at), at=at, nav=500_000_000)
    loop.state.exact_risk, loop.state.initial_nav_usd = risk, 500.0
    assert "confirmed" in loop.tick()
    assert loop.state.nav_evidence == () and loop.state.pending is None
    return loop


def test_settled_approval_cash_stands_in_for_cleared_nav_checkpoint(tmp_path: Path) -> None:
    # Live regression: the first settled transaction blocked every later tick and signature.
    with closing(EvidenceStore(tmp_path / "evidence.db")) as store:
        loop = settled_with_baseline(tmp_path, store)
        loop.store.save(loop.state)
        loop.state = loop.store.load("live")
        before = asdict(loop.state)
        loop._check_nav_checkpoint()
        assert asdict(loop.state) == before


@pytest.mark.parametrize(
    "fault", ["units", "observed", "checkpoint", "one_proof", "swapped", "kind", "no_history"]
)
def test_settled_cash_stand_in_refuses_any_inconsistency(tmp_path: Path, fault: str) -> None:

    with closing(EvidenceStore(tmp_path / "evidence.db")) as store:
        loop = settled_with_baseline(tmp_path, store)
        s = loop.state
        last = s.history[-1]
        if fault == "units":
            s.last_units = [s.last_units[0] + 1, *s.last_units[1:]]
        elif fault == "observed":
            s.last_observation = {"pre_action": True}
        elif fault == "checkpoint":
            s.checkpoint_at = 1
        elif fault == "one_proof":
            last["cash_evidence"] = last["cash_evidence"][:1]
        elif fault == "swapped":
            last["cash_evidence"] = last["cash_evidence"][::-1]
        elif fault == "kind":
            last["event"] = "approve_permit2"
        elif fault == "no_history":
            s.history = []
        with pytest.raises(ChainError, match=r"NAV checkpoint|settled action cash"):
            loop._check_nav_checkpoint()
