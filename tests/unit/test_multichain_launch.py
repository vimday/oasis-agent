"""Cross-chain isolation and explicit policy economics; no network or signing."""

import json
from dataclasses import replace

import pytest

from oasis_agent.evidence.rpc_capture import BoundedExchangeRecorder
from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.launch.models import MultiChainTarget, Snapshot, Target, parse_target
from oasis_agent.launch.quotes import matrix_reasons
from oasis_agent.launch.recon import acquire_identity, identity_report, retain_identity
from oasis_agent.launch.watcher import CandidateWatcher, PoolEvent
from oasis_agent.rpc.transport import JsonRpcTransport
from oasis_agent.shadow.sniper_runner import SniperJournal, evaluate, read_records

from .test_launch import cycle, target
from .test_launch_recon import Poster


def multichain(chain=1):
    return MultiChainTarget.model_validate(
        target().model_dump()
        | {"schema": "oasis.launch-target.v2", "chain_id": chain, "symbol": "EXAMPLE"}
    )


def on_chain(chain):
    value = cycle()
    return value.model_copy(
        update={
            "observations": tuple(
                observation.model_copy(
                    update={"snapshot": observation.snapshot.model_copy(update={"chain_id": chain})}
                )
                for observation in value.observations
            )
        }
    )


@pytest.mark.parametrize("chain", [1, 4663, 8453, 11155111])
def test_explicit_target_roundtrip_and_journal_restart(tmp_path, chain):
    policy = multichain(chain)
    assert parse_target(policy.model_dump_json()) == policy
    path = tmp_path / "launch.db"
    journal = SniperJournal(path, policy)
    first = journal.append(on_chain(chain))
    assert first["chain_id"] == chain
    assert first["execution_eligible"] is False
    journal.close()
    journal = SniperJournal(path, policy)
    assert journal.append(on_chain(chain)) == first
    assert len(read_records(journal.db)) == 1
    journal.close()


def test_two_agreeing_providers_on_wrong_chain_stand_down():
    result = evaluate(on_chain(8453), multichain(1))
    assert result["decision"] == "stand_down"
    assert "chain_mismatch" in result["stand_down_reasons"]


def test_custom_supply_and_cap_are_used():
    policy = multichain().model_copy(update={"max_fdv_usd_micros": 1})
    assert "entry_fdv_at_or_above_cap" in matrix_reasons(on_chain(1).observations, policy)
    policy = policy.model_copy(update={"reference_supply": 1, "max_fdv_usd_micros": 10**18})
    assert "entry_fdv_at_or_above_cap" not in matrix_reasons(on_chain(1).observations, policy)


@pytest.mark.parametrize("chain", [0, -1, True, 2**256, 1.5])
def test_invalid_chain_rejected(chain):
    with pytest.raises(ValueError):
        Snapshot.model_validate(cycle().observations[0].snapshot.model_dump() | {"chain_id": chain})


def test_legacy_serialization_and_explicit_policy_requirements():
    old = Target().model_dump_json()
    assert "schema" not in json.loads(old)
    assert parse_target(old) == Target()
    with pytest.raises(ValueError):
        Target(chain_id=1)
    value = multichain().model_dump(mode="json", by_alias=True)
    for name in ("chain_id", "target_ca", "reference_supply", "principal_limit_usd_micros"):
        with pytest.raises(ValueError):
            parse_target(json.dumps({key: val for key, val in value.items() if key != name}))


def test_recon_binds_custom_chain_and_asset(tmp_path):
    policy = multichain(1)
    poster = Poster(chain="0x1")
    recorder = BoundedExchangeRecorder()
    sample = acquire_identity(
        JsonRpcTransport("https://example.invalid", http=poster, recorder=recorder),
        recorder,
        source="ethereum_primary",
        block_number=100,
        target=policy,
    )
    assert sample.block.chain_id == 1
    assert sample.target_ca == policy.target_ca
    store = EvidenceStore(tmp_path / "evidence.db")
    try:
        first = retain_identity(store, sample)
        other = replace(sample, source="ethereum_secondary", target_ca="0x" + "4" * 40)
        second = retain_identity(store, other)
        assert json.loads(store.get(first))["target_ca"] == policy.target_ca
        assert identity_report((sample, other), (first, second))["identity_agreement"] is False
    finally:
        store.close()


def test_same_event_identifiers_on_different_chains_are_not_duplicates():
    watcher = CandidateWatcher()
    value = {
        "source": "primary",
        "factory": "0x" + "1" * 40,
        "pool": "0x" + "2" * 40,
        "block_hash": "0x" + "3" * 64,
        "transaction_hash": "0x" + "4" * 64,
        "log_index": 0,
        "phase": "final",
        "kind": "created",
        "removed": False,
    }
    assert watcher.ingest(PoolEvent(chain_id=1, **value))
    assert watcher.ingest(PoolEvent(chain_id=8453, **value))
    assert not watcher.ingest(PoolEvent(chain_id=1, **value))
