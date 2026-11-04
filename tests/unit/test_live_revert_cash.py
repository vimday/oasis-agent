"""Synthetic raw two-provider reverted EOA cash conservation and restart tests."""

import json
from contextlib import closing
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import pytest
from eth_abi.abi import encode
from eth_hash.auto import keccak

from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.live import runner
from oasis_agent.live.chain import ChainError, SubmissionUnknown
from oasis_agent.live.intent import SubmittedTransaction
from oasis_agent.live.revert_cash import replay_revert_cash
from oasis_agent.live.wallet import capture_wallet, replay_wallet
from oasis_agent.models.common import BlockRef, canonical_json, evidence_ref_from_json
from tests.unit.gas_oracle_fixtures import synthetic_gas_pins
from tests.unit.test_live_lp import CONFIG, bind_pending, market
from tests.unit.test_live_receipt_costs import LiveCostFixture, loop_for
from tests.unit.test_lp_receipts import PRIMARY, SECONDARY, sessions


@dataclass
class WalletFixture(LiveCostFixture):
    status: int = 0
    native_before: int = 100_000
    native_after: int = 99_782
    token_before: int = 1_000
    token_after: int = 1_000
    nonce_before: int = 3
    nonce_after: int = 4
    account_code: str = "0x"
    token_code_after: str = "0x6000"
    decimals: int = 6
    implementation: str = CONFIG.expected_pins["usdc_implementation"]
    transfer_logs: list[Any] = field(default_factory=list)
    wrong_parent: bool = False
    interval_blocks: int = 1

    def block_hash_at(self, height: int) -> str:
        if height == self.block_number:
            return self.block_hash
        if height == self.block_number - 1:
            return self.parent_hash
        return "0x" + f"{height:064x}"

    @property
    def opening(self) -> BlockRef:
        height = self.block_number - self.interval_blocks
        return BlockRef(
            self.chain_id,
            height,
            self.block_hash_at(height),
            self.timestamp - 2 * self.interval_blocks,
        )

    def post(self, payload: bytes) -> bytes:
        request = json.loads(payload)
        method, params = request["method"], request["params"]
        before = bool(
            params
            and isinstance(params[-1], dict)
            and params[-1].get("blockHash") == self.opening.block_hash
        )
        if method == "eth_getBlockByNumber" and int(params[0], 16) < self.block_number:
            height = int(params[0], 16)
            value: object = {
                "number": hex(height),
                "hash": self.block_hash_at(height),
                "parentHash": self.block_hash_at(height - 1),
                "timestamp": hex(self.timestamp - 2 * (self.block_number - height)),
            }
        elif method == "eth_getBlockByNumber" and self.wrong_parent:
            value = dict(self._result(method), parentHash="0x" + "dd" * 32)
        elif method == "eth_getCode" and params[0] == CONFIG.account:
            value = self.account_code
        elif method == "eth_getCode" and params[0] == CONFIG.pool.currency1:
            value = "0x6000" if before else self.token_code_after
        elif method == "eth_getTransactionCount":
            value = hex(self.nonce_before if before else self.nonce_after)
        elif method == "eth_getBalance":
            value = hex(self.native_before if before else self.native_after)
        elif method == "eth_getStorageAt":
            value = self.implementation
        elif method == "eth_call" and params[0]["to"] == CONFIG.pool.currency1:
            data = params[0]["data"]
            number = (
                self.decimals
                if data == "0x313ce567"
                else self.token_before
                if before
                else self.token_after
            )
            value = "0x" + encode(["uint256"], [number]).hex()
        elif method == "eth_getLogs":
            value = self.transfer_logs
        else:
            return super().post(payload)
        self.methods.append(method)
        return canonical_json({"jsonrpc": "2.0", "id": request["id"], "result": value})


def attach_opening(loop: Any, fixture: WalletFixture, other: WalletFixture | None = None) -> None:
    assert loop.state.pending is not None
    reader = loop.receipt_costs
    pair = sessions({PRIMARY: fixture, SECONDARY: other or replace(fixture)})
    loop.state.pending.wallet_evidence = capture_wallet(
        reader.store,
        loop.c,
        fixture.opening,
        sources=(PRIMARY, SECONDARY),
        sessions=pair,
    )
    bind_pending(loop.state.pending, loop.state.position, loop.c)
    loop.store.save(loop.state)


def test_revert_replays_cash_and_preserves_receipt_balance_as_next_baseline(tmp_path: Path) -> None:
    with closing(EvidenceStore(tmp_path / "evidence.db")) as store:
        fixture = WalletFixture()
        loop = loop_for(tmp_path, store, fixture)
        attach_opening(loop, fixture)
        before = loop.state.pending.wallet_evidence
        for ref in before:
            snapshot = replay_wallet(store, ref).reconstruct()
            assert snapshot.nonce == 3 and snapshot.native == 100_000
        assert "REVERTED" in loop.tick()
        state = loop.store.load("live")
        assert state.pending is None and state.gas_spent_wei == 218
        assert state.last_units == [99_782, 1_000, 0]
        refs = [evidence_ref_from_json(x) for x in state.history[-1]["cash_evidence"]]
        assert len(refs) == 2
        for ref in refs:
            proof = replay_revert_cash(store, ref, CONFIG)
            assert proof.reconstruct(store, CONFIG).native == 99_782
        # A later change cannot hide behind the old unconditional baseline reset.
        loop.observe = lambda: market(eth=99_783, usdc=1_000)
        assert "unexplained wallet change" in loop.tick()


@pytest.mark.parametrize(
    "change",
    [
        {"native_after": 99_783},
        {"native_after": 99_781},
        {"token_after": 1_001},
        {"nonce_after": 5},
        {"nonce_after": 3},
        {"account_code": "0xef0100"},
        {"token_code_after": "0x6001"},
        {"decimals": 18},
        {"implementation": "0x" + "12" * 32},
        {"transfer_logs": [{"offsetting": True}]},
        {"wrong_parent": True},
    ],
)
def test_revert_cash_disagreement_never_clears_pending_or_resets_baseline(
    tmp_path: Path, change: dict[str, Any]
) -> None:
    with closing(EvidenceStore(tmp_path / "evidence.db")) as store:
        fixture, peer = WalletFixture(), WalletFixture()
        loop = loop_for(tmp_path, store, fixture, peer)
        attach_opening(loop, fixture, peer)
        # Mutate only post-publication responses; the opening remains immutable.
        for name, value in change.items():
            setattr(peer, name, value)
        prior = loop.store.path.read_bytes()
        result = loop.tick()
        assert "unresolved" in result and loop.state.pending is not None
        assert loop.store.path.read_bytes() == prior
        assert loop.state.gas_spent_wei == 0 and loop.state.actions_today == 0


def test_legacy_revert_without_wallet_proof_is_not_a_permissive_baseline(tmp_path: Path) -> None:
    with closing(EvidenceStore(tmp_path / "evidence.db")) as store:
        loop = loop_for(tmp_path, store, WalletFixture())
        assert "reverted wallet cash unresolved" in loop.tick()
        assert loop.state.pending is not None and loop.state.last_units is None


def test_revert_cash_restart_after_state_save_failure_counts_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with closing(EvidenceStore(tmp_path / "evidence.db")) as store:
        fixture = WalletFixture()
        loop = loop_for(tmp_path, store, fixture)
        attach_opening(loop, fixture)

        def fail(*_: Any) -> None:
            raise OSError("state publication failure")

        with monkeypatch.context() as patch:
            patch.setattr(loop.store, "save", fail)
            with pytest.raises(OSError):
                loop.tick()
        loop.state = loop.store.load("live")
        assert loop.state.pending is not None
        assert "REVERTED" in loop.tick()
        assert loop.state.gas_spent_wei == 218 and loop.state.actions_today == 1


def test_wallet_capture_failure_blocks_before_any_signing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Exercise the inner capture boundary; the real release gate has its own refusal tests.
    monkeypatch.setattr(runner, "require_live_action", lambda *_: None)
    with closing(EvidenceStore(tmp_path / "evidence.db")) as store:
        fixture = WalletFixture(account_code="0x6000")
        config = replace(CONFIG, expected_pins=CONFIG.expected_pins | synthetic_gas_pins())
        loop = loop_for(tmp_path, store, fixture, config=config)
        loop.state.pending = None

        class ReadChain:
            def uint(self, *_: Any) -> int:
                return CONFIG.chain_id

        loop.rpc = ReadChain()
        loop.secondary = ReadChain()
        with pytest.raises(ChainError, match="pre-sign wallet evidence unresolved"):
            loop._act(
                market(block=100, block_hash=fixture.block_hash, timestamp=fixture.timestamp),
                ("approve_usdc", {"amount": 1}),
            )
        assert loop.state.pending is None


@pytest.mark.parametrize("nonce", [3, 4])
def test_action_persists_wallet_before_submission_and_binds_its_nonce(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, nonce: int
) -> None:
    # Synthetic inner signing test only, never production release evidence.
    monkeypatch.setattr(runner, "require_live_action", lambda *_: None)
    with closing(EvidenceStore(tmp_path / "evidence.db")) as store:
        fixture = WalletFixture()
        config = replace(CONFIG, expected_pins=CONFIG.expected_pins | synthetic_gas_pins())
        loop = loop_for(tmp_path, store, fixture, config=config)
        loop.state.pending = None

        class ReadChain:
            def uint(self, *_: Any) -> int:
                return CONFIG.chain_id

        loop.rpc = ReadChain()
        loop.secondary = ReadChain()

        def submit(*args: Any, **kwargs: Any) -> None:
            # Synthetic metadata; this never calls a key provider or broadcasts.
            tx = SubmittedTransaction(
                fixture.transaction_hash,
                CONFIG.chain_id,
                CONFIG.account,
                nonce,
                kwargs["to"],
                kwargs["value"],
                "0x" + keccak(kwargs["data"]).hex(),
                100_000,
                CONFIG.max_fee_per_gas_wei,
                CONFIG.priority_fee_wei,
            )
            kwargs["on_signed"](tx)
            pending = loop.store.load("live").pending
            assert pending is not None and len(pending.wallet_evidence) == 2
            for ref in pending.wallet_evidence:
                assert replay_wallet(store, ref).reconstruct().nonce == nonce
            raise SubmissionUnknown("synthetic submission")

        monkeypatch.setattr(runner, "send_transaction", submit)
        observed = market(
            block=fixture.opening.number,
            block_hash=fixture.opening.block_hash,
            timestamp=fixture.opening.timestamp,
            eth=fixture.native_before,
            usdc=fixture.token_before,
        )
        if nonce == 3:
            assert "awaiting verified receipt" in loop._act(
                observed, ("approve_usdc", {"amount": 1})
            )
            pending = loop.store.load("live").pending
            assert pending is not None and pending.wallet_evidence
            loop.state = loop.store.load("live")
            loop.state.pending.wallet_evidence = ()
            with pytest.raises(ChainError, match="commitment"):
                loop._verified_receipt(loop.state.pending)
        else:
            with pytest.raises(ChainError, match="signed transaction differs"):
                loop._act(observed, ("approve_usdc", {"amount": 1}))
            assert loop.state.pending is None


def test_wallet_replay_refuses_interpretation_that_raw_balances_contradict(tmp_path: Path) -> None:
    with closing(EvidenceStore(tmp_path / "evidence.db")) as store:
        fixture = WalletFixture()
        refs = capture_wallet(
            store,
            CONFIG,
            fixture.opening,
            sources=(PRIMARY, SECONDARY),
            sessions=sessions({PRIMARY: fixture, SECONDARY: replace(fixture)}),
        )
        record = store.get_record(refs[0])
        payload = json.loads(record.payload)
        payload["balance"]["native"] += 1
        forged = store.put(
            "live_wallet",
            canonical_json(payload),
            refs[0].block,
            refs[0].source,
            request=json.loads(record.request_payload),
            raw_response=json.loads(record.raw_response_payload),
            interpretation={"execution_eligible": False},
        )
        with pytest.raises(ValueError, match="raw proof"):
            replay_wallet(store, forged)


@pytest.mark.parametrize("blocks", [3, 128, 129])
def test_revert_interval_covers_every_block_and_refuses_excessive_distance(
    tmp_path: Path, blocks: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    with closing(EvidenceStore(tmp_path / "evidence.db")) as store:
        fixture = WalletFixture(block_number=200, interval_blocks=blocks)
        peer = WalletFixture(block_number=200, interval_blocks=blocks)
        loop = loop_for(tmp_path, store, fixture, peer)
        for rpc in (loop.rpc, loop.secondary):
            monkeypatch.setattr(rpc, "uint", lambda *_: 210)
        attach_opening(loop, fixture, peer)
        result = loop.tick()
        if blocks > 128:
            assert "unresolved" in result and loop.state.pending is not None
            assert "eth_getLogs" not in fixture.methods
        else:
            assert "REVERTED" in result
            ref = evidence_ref_from_json(loop.state.history[-1]["cash_evidence"][0])
            proof = replay_revert_cash(store, ref, CONFIG)
            assert len(proof.headers) == blocks
            assert fixture.methods.count("eth_getLogs") == 2 * blocks
            # Omitting even an apparently empty intermediate block is not complete proof.
            with pytest.raises(ValueError, match="gap"):
                replace(proof, headers=proof.headers[1:]).reconstruct(store, CONFIG)
