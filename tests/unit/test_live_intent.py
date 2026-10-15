"""Public commitments come from actual synthetic signed bytes before submission."""

import json
import os
import stat
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest
from eth_hash.auto import keccak
from eth_keys import keys

from oasis_agent.live import runner
from oasis_agent.live.chain import ChainError, FeePolicy, send_transaction
from oasis_agent.live.intent import SubmittedTransaction
from oasis_agent.live.state import LoopState, Pending, Position, StateStore
from tests.unit.test_live_lp import CONFIG, ScriptedRpc, loop_with, market


class EphemeralSigner:
    def __init__(self) -> None:
        self._key = keys.PrivateKey(os.urandom(32))
        self.account = self._key.public_key.to_checksum_address().lower()

    def sign(self, digest: bytes) -> tuple[int, int, int]:
        signature = self._key.sign_msg_hash(digest)
        return signature.r, signature.s, signature.v


@pytest.mark.parametrize("save_fails", [False, True])
def test_actual_signed_envelope_is_published_before_broadcast(save_fails: bool) -> None:
    events: list[str] = []
    saved: list[SubmittedTransaction] = []
    signer = EphemeralSigner()
    calldata = bytes.fromhex("12345678")

    class Rpc:
        def uint(self, method: str, params: list[Any]) -> int:
            assert method == "eth_estimateGas"
            return 21000

        def block(self) -> dict[str, Any]:
            return {"baseFeePerGas": "0xa"}

        def nonce(self, account: str, block: str) -> int:
            assert account == signer.account
            return 3

        def send_raw(self, raw: bytes) -> None:
            events.append("broadcast")
            assert events == ["persist", "broadcast"]
            assert saved == [SubmittedTransaction.from_signed(raw)]

        def receipt(self, tx_hash: str) -> dict[str, Any]:
            assert tx_hash == saved[0].transaction_hash
            return {"status": "0x1"}

    def persist(commitment: SubmittedTransaction) -> None:
        events.append("persist")
        if save_fails:
            raise OSError("synthetic disk failure")
        # The persisted API has neither signature scalars nor broadcastable bytes.
        encoded = json.dumps(asdict(commitment))
        assert signer._key.to_hex() not in encoded
        assert "raw" not in encoded and "signature" not in encoded
        saved.append(SubmittedTransaction(**json.loads(encoded)))

    def run() -> None:
        send_transaction(
            Rpc(),
            signer,
            chain_id=8453,
            to=CONFIG.position_manager,
            data=calldata,
            value=17,
            fees=FeePolicy(100, 2),
            on_signed=persist,
        )

    if save_fails:
        with pytest.raises(OSError, match="disk failure"):
            run()
        assert events == ["persist"]
    else:
        run()
        tx = saved[0]
        assert (tx.chain_id, tx.nonce, tx.value, tx.gas_limit) == (8453, 3, 17, 27300)
        assert (tx.max_fee_per_gas, tx.max_priority_fee_per_gas) == (22, 2)
        assert tx.calldata_hash == "0x" + keccak(calldata).hex()
        assert tx.account == signer.account and tx.target == CONFIG.position_manager


@pytest.mark.parametrize("liquidity", [0, 76, 78])
def test_even_in_range_position_changes_halt_without_erasing_position(
    tmp_path: Path, liquidity: int
) -> None:
    position = Position(123, -100, 100, 77, 0)
    loop = loop_with(tmp_path, ScriptedRpc(), position=position)
    loop.mode = "live"
    assert loop._decide(market(tick=0, position_liquidity=liquidity)) is None
    assert loop.state.halted is not None and loop.state.position is position


@pytest.mark.parametrize("fault", [None, "stale", "future", "reorg", "timestamp", "oracle"])
def test_observation_requires_fresh_head_and_matching_closing_headers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str | None
) -> None:
    now = 1_800_000_000
    timestamp = now - 121 if fault == "stale" else now + 1 if fault == "future" else now
    header = {
        "number": "0x64",
        "hash": "0x" + "77" * 32,
        "timestamp": hex(timestamp),
        "parentHash": "0x" + "66" * 32,
    }

    class Rpc:
        def __init__(self, closing: dict[str, Any]) -> None:
            self.closing = closing

        def block(self) -> dict[str, Any]:
            return header

        def call(self, method: str, params: list[Any]) -> dict[str, Any]:
            assert (method, params) == ("eth_getBlockByNumber", ["0x64", False])
            return self.closing

    closing = dict(header)
    if fault == "reorg":
        closing["hash"] = "0x" + "88" * 32
    if fault == "timestamp":
        closing["timestamp"] = hex(timestamp - 1)
    loop = loop_with(tmp_path, Rpc(header))
    loop.mode = "live"
    loop.secondary = Rpc(closing)
    monkeypatch.setattr(runner.time, "time", lambda: now)
    monkeypatch.setattr(
        loop,
        "_reads",
        lambda *_: (
            market().sqrt_price,
            market().tick,
            2500 * 10**8,
            now + 1 if fault == "oracle" else now,
            1,
            1,
            0,
        ),
    )
    if fault:
        with pytest.raises(ChainError):
            loop.observe()
    else:
        assert loop.observe().block_hash == header["hash"]


def test_state_publication_syncs_file_then_parent_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = StateStore(tmp_path / "state.json")
    state = LoopState("live", pending=Pending("swap", "0x" + "ab" * 32, 3, 0))
    observed: list[str] = []
    fsync = os.fsync

    def track(fd: int) -> None:
        is_directory = stat.S_ISDIR(os.fstat(fd).st_mode)
        observed.append("directory" if is_directory else "file")
        if is_directory:
            assert store.load("live").pending == state.pending
        fsync(fd)

    monkeypatch.setattr(os, "fsync", track)
    store.save(state)
    assert observed == ["file", "directory"]
