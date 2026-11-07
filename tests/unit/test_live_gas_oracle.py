"""Gas-cost code approval must precede online acquisition and key use.

Every provider, account and key used here is synthetic; no chain is contacted.
"""

import json
import sys
from contextlib import closing
from dataclasses import asdict, replace

import pytest
from eth_hash.auto import keccak

from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.live import authority_simulation as simulation
from oasis_agent.live.config import LiveConfig
from oasis_agent.live.gas_oracle import IMPLEMENTATION_SLOT, ORACLE, require_gas_oracle_pins
from oasis_agent.models.common import canonical_json
from scripts import run_live_authority, run_live_lp, start_live_deployment
from tests.unit.gas_oracle_fixtures import synthetic_gas_pins
from tests.unit.test_live_authority import setup_authority
from tests.unit.test_live_lp import CONFIG
from tests.unit.test_live_valuation import synthetic_feed_pin  # noqa: F401


def legacy_config(config):
    return replace(
        config,
        expected_pins={
            k: v for k, v in config.expected_pins.items() if not k.startswith("gas_oracle_")
        },
    )


def forbidden(*args, **kwargs):
    raise AssertionError("missing gas-oracle approval must precede online/key access")


def test_unpinned_gas_code_cannot_approve_an_authority_envelope(tmp_path, monkeypatch):
    with closing(EvidenceStore(tmp_path / "proof.db")) as store:
        authority, key, fields, context = setup_authority(tmp_path, store, monkeypatch)
        market = authority.loop.observe()
        authority.loop.c = legacy_config(authority.loop.c)
        with pytest.raises(ValueError, match="gas oracle runtime pins"):
            authority._envelope(fields, market, context)
        assert key.calls == 0


def test_missing_gas_policy_cannot_begin_new_authority_rpc(tmp_path, monkeypatch):
    with closing(EvidenceStore(tmp_path / "proof.db")) as store:
        authority, key, fields, context = setup_authority(tmp_path, store, monkeypatch)
        authority.loop.c = legacy_config(authority.loop.c)
        monkeypatch.setattr(authority.loop.rpc, "call", forbidden)
        with pytest.raises(ValueError, match="gas oracle runtime pins"):
            authority.sign(fields, context)
        assert key.calls == 0


@pytest.mark.parametrize("role", ["strategy", "authority"])
def test_execution_cli_refuses_legacy_pins_before_rpc_or_key(tmp_path, monkeypatch, role):
    module = run_live_lp if role == "strategy" else run_live_authority
    path = tmp_path / "config.json"
    path.write_bytes(canonical_json(asdict(legacy_config(CONFIG))))
    directory = tmp_path / "state"
    directory.mkdir(mode=0o700)
    options = (
        ["--mode", "live", "--signer-socket", "/unused"]
        if role == "strategy"
        else ["--socket", "/unused"]
    )
    monkeypatch.setattr(
        sys, "argv", ["run", "--config", str(path), "--state-dir", str(directory), *options]
    )
    monkeypatch.setattr(module, "require_live_start", lambda: None)
    monkeypatch.setattr(module, "BudgetedRpc", forbidden)
    if role == "strategy":
        monkeypatch.setattr(module, "rpc_url", forbidden)
    else:
        monkeypatch.setattr(module, "load_private_key", forbidden)
    with pytest.raises(SystemExit) as caught:
        module.main()
    assert caught.value.code == 3 and not list(directory.iterdir())


def test_one_command_start_refuses_unpinned_policy_before_docker(tmp_path, monkeypatch):
    monkeypatch.setattr(start_live_deployment, "require_live_start", lambda: None)
    monkeypatch.setattr(start_live_deployment, "command", forbidden)
    configs = tmp_path / "configs"
    configs.mkdir()
    (configs / "live.base-eth-usdc.json").write_bytes(canonical_json(asdict(CONFIG)))
    monkeypatch.setattr(start_live_deployment.compose, "ROOT", tmp_path)
    output = tmp_path / "attempt"
    with pytest.raises(ValueError, match="gas oracle runtime pins"):
        start_live_deployment.start("unused", output)
    assert not output.exists()


def test_implementation_slot_matches_the_canonical_eip1967_derivation():
    assert (
        "0x"
        + (int.from_bytes(keccak(b"eip1967.proxy.implementation"), "big") - 1)
        .to_bytes(32, "big")
        .hex()
        == IMPLEMENTATION_SLOT
    )


@pytest.mark.parametrize("field", list(synthetic_gas_pins()))
def test_partial_gas_policy_is_not_a_legacy_or_execution_config(field):
    pins = CONFIG.expected_pins | synthetic_gas_pins()
    del pins[field]
    with pytest.raises(ValueError, match="expected_pins"):
        LiveConfig.from_json(canonical_json(asdict(replace(CONFIG, expected_pins=pins))))


@pytest.mark.parametrize(
    "word",
    ["0x" + "00" * 32, "0x" + "11" * 32, "0x1", "0x" + "00" * 12 + "AB" * 20],
)
def test_bad_implementation_slot_refuses_before_any_acquisition(word):
    pins = CONFIG.expected_pins | synthetic_gas_pins() | {"gas_oracle_implementation": word}
    with pytest.raises(ValueError, match="implementation slot"):
        require_gas_oracle_pins(pins)


@pytest.mark.parametrize("field", ["gas_oracle_proxy", "gas_oracle_implementation_code"])
@pytest.mark.parametrize("digest", ["0x" + "00" * 32, "0x" + keccak(b"").hex()])
def test_placeholder_or_empty_runtime_hash_is_not_approval(field, digest):
    pins = CONFIG.expected_pins | synthetic_gas_pins() | {field: digest}
    with pytest.raises(ValueError, match="nonempty code"):
        require_gas_oracle_pins(pins)


@pytest.mark.parametrize("both_sources", [False, True])
@pytest.mark.parametrize(
    "changes",
    [
        {"gas_proxy_code": "0x6001"},
        {"gas_impl_code": "0x6001"},
        {"gas_impl_word": "0x" + "00" * 12 + "cd" * 20},
        {"gas_proxy_code": "0x"},
        {"gas_impl_code": "0x"},
        {"gas_impl_word": "0x" + "11" * 32},
        {"gas_proxy_code": "0x60 00"},
    ],
)
def test_agreement_cannot_replace_approved_proxy_or_implementation(
    tmp_path, monkeypatch, both_sources, changes
):
    with closing(EvidenceStore(tmp_path / "proof.db")) as store:
        authority, key, fields, context = setup_authority(tmp_path, store, monkeypatch)
        market = authority.loop.observe()
        fixtures = [authority.loop.secondary.fixture]
        if both_sources:
            fixtures.append(authority.loop.rpc.fixture)
        for fixture in fixtures:
            for name, value in changes.items():
                setattr(fixture, name, value)
        with pytest.raises(ValueError, match="gas oracle"):
            authority._envelope(fields, market, context)
        assert key.calls == 0 and authority.simulation_evidence is None
        assert (
            store._connection.execute(
                "SELECT COUNT(*) FROM evidence WHERE kind='live_authority_simulation'"
            ).fetchone()[0]
            == 0
        )


def test_retained_simulation_names_the_approved_implementation_and_pinned_slot(
    tmp_path, monkeypatch
):
    with closing(EvidenceStore(tmp_path / "proof.db")) as store:
        authority, key, fields, context = setup_authority(tmp_path, store, monkeypatch)
        market = authority.loop.observe()
        authority._envelope(fields, market, context)
        assert key.calls == 0
        for ref in authority.simulation_evidence:
            record = store.get_record(ref)
            payload = json.loads(record.payload)
            assert payload["schema"] == "oasis.live-authority-simulation.v3"
            assert payload["gas_oracle_pins"] == synthetic_gas_pins()
            requests = json.loads(record.request_payload)["exchanges"]
            slot, code = [json.loads(bytes.fromhex(item["payload"])) for item in requests[11:13]]
            pin = {"blockHash": market.block_hash, "requireCanonical": True}
            assert slot["params"] == [ORACLE, IMPLEMENTATION_SLOT, pin]
            assert code["params"] == ["0x" + "ab" * 20, pin]


def test_rehashed_runtime_summary_cannot_override_wire_proof(tmp_path, monkeypatch):
    with closing(EvidenceStore(tmp_path / "proof.db")) as store:
        authority, key, fields, context = setup_authority(tmp_path, store, monkeypatch)
        market = authority.loop.observe()
        authority._envelope(fields, market, context)
        ref = authority.simulation_evidence[0]
        record = store.get_record(ref)
        payload = json.loads(record.payload)
        payload["gas_oracle_pins"]["gas_oracle_proxy"] = "0x" + "aa" * 32
        forged = store.put(
            ref.kind,
            canonical_json(payload),
            ref.block,
            ref.source,
            request=record.request_payload,
            raw_response=record.raw_response_payload,
            interpretation={"execution_eligible": False},
        )
        with pytest.raises(ValueError, match="interpretation contradicts"):
            simulation.replay_simulation(
                store,
                forged,
                authority.loop.c,
                ref.block,
                payload["transaction"],
                nonce=fields[1],
                gas=fields[4],
                maximum=fields[3],
            )
        assert key.calls == 0
