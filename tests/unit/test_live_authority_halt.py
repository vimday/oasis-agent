"""Authority recovery failures latch across requests and process restarts.

All chain responses and transaction commitments are synthetic. No key provider,
network transport, or production state is used.
"""

from contextlib import closing
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest

from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.live.authority import TransactionAuthority
from oasis_agent.live.chain import ChainError
from oasis_agent.live.receipts import RetainedReceiptCostReader
from oasis_agent.live.runner import LiveLoop
from oasis_agent.live.state import Pending, StateStore
from tests.unit.test_live_lp import BLOCK_HASH, CONFIG, bind_pending, mined
from tests.unit.test_lp_receipts import PRIMARY, SECONDARY

TX_HASH = "0x" + "aa" * 32
OTHER_HASH = "0x" + "bb" * 32
ANCHOR = {"number": 100, "hash": BLOCK_HASH, "transaction_hash": TX_HASH}


def forbidden(*_args: Any, **_kwargs: Any) -> Any:
    pytest.fail("halted authority must not use RPC, capture evidence, or call a key provider")


class NoKey:
    account = CONFIG.account
    sign = forbidden


class AnchorRpc:
    """Only the existing settlement and pending receipt checks may use these replies."""

    def __init__(self, fault: str = "") -> None:
        self.fault = fault
        self.blocked = False

    def call(self, method: str, params: list[Any]) -> dict[str, str] | None:
        if self.blocked:
            forbidden()
        assert method == "eth_getBlockByNumber"
        if self.fault == "transport":
            raise ChainError("synthetic unavailable transport")
        if params == ["finalized", False]:
            return {"number": "0x63"}  # The anchor remains subject to canonicality checks.
        assert params == ["0x64", False]
        if self.fault == "header_missing":
            return None
        return {"hash": OTHER_HASH if self.fault == "header_changed" else BLOCK_HASH}

    def receipt(self, tx_hash: str) -> dict[str, Any] | None:
        if self.blocked:
            forbidden()
        assert tx_hash == TX_HASH
        if self.fault == "receipt_missing":
            return None
        overrides = {
            "receipt_block_changed": {"blockHash": OTHER_HASH},
            "receipt_tx_changed": {"transactionHash": OTHER_HASH},
            "receipt_payer_changed": {"from": "0x" + "bb" * 20},
            "receipt_target_changed": {"to": "0x" + "bb" * 20},
        }
        return mined(TX_HASH) | overrides.get(self.fault, {})

    def uint(self, method: str, params: list[Any]) -> int:
        if self.blocked:
            forbidden()
        assert method == "eth_blockNumber" and params == []
        return 110


def make_authority(
    tmp_path: Path, store: EvidenceStore, providers: tuple[AnchorRpc, AnchorRpc]
) -> TransactionAuthority:
    loop = LiveLoop.recovery(CONFIG, providers[0], StateStore(tmp_path / "authority.json"), "live")
    loop.secondary = providers[1]
    loop.receipt_costs = RetainedReceiptCostReader(
        store,
        chain_id=CONFIG.chain_id,
        account=CONFIG.account,
        native_decimals=18,
        sources=(PRIMARY, SECONDARY),
        sessions=forbidden,
    )
    return TransactionAuthority(loop, NoKey())


def assert_durable_halt(
    authority: TransactionAuthority,
    before: dict[str, Any],
    reason: str,
    tmp_path: Path,
    store: EvidenceStore,
    providers: tuple[AnchorRpc, AnchorRpc],
) -> None:
    assert asdict(authority.loop.store.load("live")) == before | {"halted": reason}
    # Even if the provider's transient problem disappears, the next request and
    # a newly constructed authority must stop at the durable latch before RPC.
    for rpc in providers:
        rpc.fault, rpc.blocked = "", True
    with pytest.raises(ValueError, match="authority halted"):
        authority.sign([], {})
    restarted = make_authority(tmp_path, store, providers)
    with pytest.raises(ValueError, match="authority halted"):
        restarted.sign([], {})
    assert asdict(restarted.loop.state) == before | {"halted": reason}


@pytest.mark.parametrize("provider", [0, 1])
@pytest.mark.parametrize(
    "fault",
    [
        "header_missing",
        "header_changed",
        "receipt_missing",
        "receipt_block_changed",
        "receipt_tx_changed",
    ],
)
def test_settlement_halt_survives_next_request_and_restart(tmp_path, monkeypatch, provider, fault):
    monkeypatch.setattr("oasis_agent.live.authority.require_live_action", lambda *_: None)
    with closing(EvidenceStore(tmp_path / "evidence.db")) as store:
        providers = (AnchorRpc(), AnchorRpc())
        providers[provider].fault = fault
        authority = make_authority(tmp_path, store, providers)
        authority.loop.state.settled_blocks = [dict(ANCHOR)]
        authority.loop.store.save(authority.loop.state)
        before = asdict(authority.loop.state)
        reason = (
            "settled transaction block reorged or unavailable"
            if fault.startswith("header")
            else "settled transaction receipt changed or unavailable"
        )
        with pytest.raises(ChainError, match=reason):
            authority.sign([], {})
        assert_durable_halt(authority, before, reason, tmp_path, store, providers)


@pytest.mark.parametrize(
    "fault", ["receipt_tx_changed", "receipt_payer_changed", "receipt_target_changed"]
)
def test_pending_identity_halt_survives_without_releasing_commitment(tmp_path, monkeypatch, fault):
    monkeypatch.setattr("oasis_agent.live.authority.require_live_action", lambda *_: None)
    with closing(EvidenceStore(tmp_path / "evidence.db")) as store:
        providers = (AnchorRpc(fault), AnchorRpc(fault))
        authority = make_authority(tmp_path, store, providers)
        pending = Pending("mint", TX_HASH, 3, 1_791_470_000, {}, CONFIG.position_manager)
        bind_pending(pending)
        authority.loop.state.pending = pending
        authority.loop.store.save(authority.loop.state)
        before = asdict(authority.loop.state)
        reason = f"receipt for {TX_HASH} does not match the signed transaction"
        with pytest.raises(ChainError, match=reason):
            authority.sign([], {})
        assert_durable_halt(
            authority, before | {"settlement_attempts": 1}, reason, tmp_path, store, providers
        )


def test_transport_failure_does_not_invent_or_mutate_halt(tmp_path, monkeypatch):
    monkeypatch.setattr("oasis_agent.live.authority.require_live_action", lambda *_: None)
    with closing(EvidenceStore(tmp_path / "evidence.db")) as store:
        authority = make_authority(tmp_path, store, (AnchorRpc("transport"), AnchorRpc()))
        authority.loop.state.settled_blocks = [dict(ANCHOR)]
        authority.loop.store.save(authority.loop.state)
        before = asdict(authority.loop.state)
        with pytest.raises(ChainError, match="synthetic unavailable transport"):
            authority.sign([], {})
        assert asdict(authority.loop.store.load("live")) == before
