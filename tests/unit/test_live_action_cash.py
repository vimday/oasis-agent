"""Full synthetic raw action proofs; never authorize mainnet execution."""

import copy
import json
from contextlib import closing
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

import pytest
from eth_abi.abi import encode
from eth_hash.auto import keccak

from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.live.action_cash import native_delta, replay_action_cash
from oasis_agent.live.action_plan import action_call
from oasis_agent.live.cash_interval import TRANSFER
from oasis_agent.live.intent import pending_plan_hash
from oasis_agent.live.state import Position
from oasis_agent.models.common import canonical_json, evidence_ref_from_json
from tests.unit.test_live_lp import CONFIG
from tests.unit.test_live_receipt_costs import loop_for, position_config
from tests.unit.test_live_revert_cash import WalletFixture, attach_opening

MANAGER = "0x" + "22" * 20
ZERO = "0x" + "00" * 20


def topic(address: str) -> str:
    return "0x" + "0" * 24 + address[2:]


@dataclass
class ActionFixture(WalletFixture):
    status: int = 1
    kind: str = "swap"
    terms: dict[str, Any] = field(
        default_factory=lambda: {"zero_for_one": True, "amount_in": 100, "min_out": 190}
    )
    trace_fault: str | None = None
    native_change: int = -100
    token_change: int = 200

    @property
    def prior_position(self) -> Position | None:
        return Position(123, -100, 100, 77, 0) if self.kind == "burn" else None

    @property
    def planned(self) -> tuple[str, bytes, int]:
        return action_call(
            CONFIG, self.opening.timestamp, self.kind, self.terms, self.prior_position
        )

    def logs(self) -> list[dict[str, Any]]:
        if self.token_change < 0:
            sender, recipient = CONFIG.account, MANAGER
        else:
            sender, recipient = MANAGER, CONFIG.account
        logs = [
            {
                "address": CONFIG.pool.currency1,
                "topics": [TRANSFER, topic(sender), topic(recipient)],
                "data": "0x" + encode(["uint256"], [abs(self.token_change)]).hex(),
            }
        ]
        if self.kind in {"mint", "burn"}:
            sender, recipient = (
                (ZERO, CONFIG.account) if self.kind == "mint" else (CONFIG.account, ZERO)
            )
            logs.append(
                {
                    "address": CONFIG.position_manager,
                    "topics": [TRANSFER, topic(sender), topic(recipient), "0x" + f"{123:064x}"],
                    "data": "0x",
                }
            )
        return [
            log
            | {
                "blockHash": self.block_hash,
                "blockNumber": hex(self.block_number),
                "transactionHash": self.transaction_hash,
                "transactionIndex": "0x0",
                "logIndex": hex(i),
                "removed": False,
            }
            for i, log in enumerate(logs)
        ]

    def trace(self) -> dict[str, Any]:
        target, data, value = self.planned
        root = {
            "type": "CALL",
            "from": CONFIG.account,
            "to": target,
            "value": hex(value),
            "input": "0x" + data.hex(),
            "calls": [],
        }
        refund = value + self.native_change
        if refund:
            root["calls"].append(
                {
                    "type": "CALL",
                    "from": target,
                    "to": CONFIG.account,
                    "value": hex(refund),
                    "input": "0x",
                }
            )
        if self.trace_fault == "input":
            root["input"] = "0xdead"
        if self.trace_fault == "refund":
            root["calls"][0]["value"] = "0x1"
        if self.trace_fault == "payer":
            root["calls"][0]["from"] = MANAGER
        if self.trace_fault == "selfdestruct":
            root["calls"].append(
                {"type": "SELFDESTRUCT", "from": target, "to": CONFIG.account, "value": "0x0"}
            )
        if self.trace_fault == "missing_value":
            root.pop("value")
        return root

    def _receipt(self) -> dict[str, Any]:
        return super()._receipt() | {"to": self.planned[0], "logs": self.logs()}

    def post(self, payload: bytes) -> bytes:
        req = json.loads(payload)
        method, params = req["method"], req["params"]
        if method == "debug_traceTransaction":
            value = self.trace()
        elif (
            method == "eth_call"
            and params[0]["to"] == CONFIG.state_view
            and params[0]["data"] == "0x" + keccak(b"poolManager()")[:4].hex()
        ):
            value = "0x" + encode(["address"], [MANAGER]).hex()
        elif method == "eth_getLogs":
            topics = params[0]["topics"]
            value = [
                log
                for log in self.logs()
                if (topics[1] is None or topics[1] == log["topics"][1])
                and (len(topics) < 3 or topics[2] == log["topics"][2])
            ]
        else:
            return super().post(payload)
        return canonical_json({"jsonrpc": "2.0", "id": req["id"], "result": value})


def fixture_for(kind: str) -> ActionFixture:
    if kind == "swap_native":
        return ActionFixture(native_after=99_682, token_after=1200)
    if kind == "swap_token":
        return ActionFixture(
            terms={"zero_for_one": False, "amount_in": 200, "min_out": 90},
            native_change=100,
            token_change=-200,
            native_after=99_882,
            token_after=800,
        )
    if kind == "mint":
        return ActionFixture(
            kind="mint",
            terms={
                "lower": -100,
                "upper": 100,
                "liquidity": 77,
                "amount0_max": 120,
                "amount1_max": 220,
            },
            native_change=-100,
            token_change=-200,
            native_after=99_682,
            token_after=800,
            position_enabled=True,
        )
    return ActionFixture(
        kind="burn",
        terms={"amount0_min": 90, "amount1_min": 190},
        native_change=100,
        token_change=200,
        native_after=99_882,
        token_after=1200,
        position_enabled=True,
        position_burned=True,
    )


def action_loop(tmp_path: Path, store: EvidenceStore, a: ActionFixture, b: ActionFixture) -> Any:
    loop = loop_for(tmp_path, store, a, b, kind=a.kind, config=position_config())
    p = loop.state.pending
    loop.state.position = a.prior_position
    p.intent = dict(a.terms)
    p.to = a.planned[0]
    attach_opening(loop, a, b)
    p.transaction = replace(
        p.transaction,
        target=p.to,
        value=a.planned[2],
        calldata_hash="0x" + keccak(a.planned[1]).hex(),
    )
    p.plan_hash = pending_plan_hash(
        asdict(loop.c),
        p.kind,
        p.intent,
        p.transaction,
        asdict(loop.state.position) if loop.state.position else None,
        tuple(asdict(r) for r in p.wallet_evidence),
    )
    loop.store.save(loop.state)
    return loop


@pytest.mark.parametrize("kind", ["swap_native", "swap_token", "mint", "burn"])
def test_success_cash_replays_native_refund_and_token_conservation(
    tmp_path: Path, kind: str
) -> None:
    with closing(EvidenceStore(tmp_path / "evidence.db")) as store:
        a = fixture_for(kind)
        loop = action_loop(tmp_path, store, a, copy.deepcopy(a))
        assert "confirmed" in loop.tick()
        assert loop.state.pending is None and loop.state.gas_spent_wei == 218
        assert loop.state.last_units[:2] == [a.native_after, a.token_after]
        refs = [evidence_ref_from_json(r) for r in loop.state.history[-1]["cash_evidence"]]
        for ref in refs:
            proof = replay_action_cash(store, ref, loop.c)
            assert proof.reconstruct(store, loop.c).native == a.native_after


@pytest.mark.parametrize(
    "fault",
    [
        "input",
        "refund",
        "payer",
        "selfdestruct",
        "missing_value",
        "native",
        "token",
        "nonce",
        "foreign_transfer",
        "min_out",
    ],
)
def test_action_cash_fault_never_clears_pending(tmp_path: Path, fault: str) -> None:
    with closing(EvidenceStore(tmp_path / "evidence.db")) as store:
        a = fixture_for("swap_token")
        b = copy.deepcopy(a)
        if fault in {"input", "refund", "payer", "selfdestruct", "missing_value"}:
            b.trace_fault = fault
        if fault == "native":
            b.native_after += 1
        if fault == "token":
            b.token_after += 1
        if fault == "nonce":
            b.nonce_after += 1
        if fault == "foreign_transfer":
            b.transaction_hash = "0x" + "99" * 32
        if fault == "min_out":
            a.terms["min_out"] = 101
            b.terms["min_out"] = 101
        loop = action_loop(tmp_path, store, a, b)
        before = json.loads(loop.store.path.read_bytes())
        assert "unresolved" in loop.tick()
        assert json.loads(loop.store.path.read_bytes()) == before | {"settlement_attempts": 1}


def test_reverted_descendants_cannot_be_counted_as_refunds(tmp_path: Path) -> None:
    with closing(EvidenceStore(tmp_path / "evidence.db")) as store:
        a = fixture_for("swap_token")
        loop = action_loop(tmp_path, store, a, copy.deepcopy(a))
        root = a.trace()
        root["calls"][0]["error"] = "execution reverted"
        assert native_delta(root, loop.state.pending.transaction, MANAGER, a.planned[1]) == 0
