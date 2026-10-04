"""A complete synthetic raw workflow, never an admitted target or live account."""

import fcntl
import json
import socket
from dataclasses import asdict, replace
from decimal import Decimal
from pathlib import Path

import pytest
from eth_abi.abi import encode
from eth_hash.auto import keccak

from oasis_agent.evidence.rpc_capture import BoundedExchangeRecorder, persist_block_range_evidence
from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.execution.account_risk_reader import AccountObservation, PositionEvidence
from oasis_agent.execution.lp_preparation import LpPlanBounds
from oasis_agent.execution.lp_reference_price import (
    CurrencyDeclaration,
    FeedPin,
    ReferenceProvider,
    currency_feed,
    persist_currency_feed_evidence,
)
from oasis_agent.execution.lp_signer_policy import LpSignerPolicy
from oasis_agent.execution.lp_state_machine import RuntimePin, RuntimeRole, TickRange
from oasis_agent.farming.account_gas_coverage import replay_account_gas
from oasis_agent.farming.account_inventory import (
    collect_account_inventory_quorum,
    persist_account_inventory_evidence,
)
from oasis_agent.farming.position_capture import replay_position_snapshot_evidence
from oasis_agent.farming.price_evidence import replay_price_observation_evidence
from oasis_agent.models.common import canonical_json
from oasis_agent.risk.portfolio_manifest import (
    ChainInventory,
    ManifestAsset,
    ManifestFeed,
    ManifestPosition,
    PortfolioManifest,
)
from oasis_agent.rpc.collector import BlockRangeDataset
from oasis_agent.rpc.transport import JsonRpcTransport
from oasis_agent.shadow.pool_state_capture import (
    collect_slot0_window,
    persist_slot0_capture,
    replay_slot0_window_evidence,
)
from oasis_agent.shadow.service import serializable
from oasis_agent.uniswap_v4.events import SWAP_TOPIC
from scripts import lp_workflow as workflow
from scripts.replay_account_risk_gas import Request as AccountRequest
from scripts.replay_position_account_gas import Request as PositionRequest
from tests.rpc_evidence_helpers import range_exchanges
from tests.unit.test_account_risk_gas_binding import GasInventory, scenario
from tests.unit.test_account_risk_reader import (
    FeedSpec,
    TokenSpec,
    inventory_session,
    risk_policy,
    runtime_hash,
)
from tests.unit.test_lp_cycle_assembly import candidate, declaration
from tests.unit.test_lp_reference_price import capture as reference_capture
from tests.unit.test_position_account_gas import joined_proofs
from tests.unit.test_position_hold_return import snapshot_units
from tests.unit.test_shadow_lp_runner import band_strategy


def pinned(path, root):
    return {"path": str(path.relative_to(root)), "sha256": workflow.digest(path)}


@pytest.fixture
def complete(tmp_path):
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    generator = joined_proofs(incoming, "hold")
    store, joined = next(generator)
    try:
        gas, position = joined.account_gas, joined.position
        position = replace(
            position,
            cash_window=replace(
                position.cash_window,
                opening=snapshot_units(store, position.cash_window.opening, liquidity=10**18),
                closing=snapshot_units(store, position.cash_window.closing, liquidity=10**18),
            ),
        )
        joined = replace(joined, position=position)
        snapshots = [
            replay_position_snapshot_evidence(store, pair[0])
            for pair in (position.cash_window.opening, position.cash_window.closing)
        ]
        key = snapshots[-1].snapshot.pool_key
        policies = (position.currency0_policy, position.currency1_policy, gas.native_policy)

        def feed(policy):
            return ManifestFeed(
                address=policy.address,
                description=policy.description,
                runtime_keccak=policy.runtime_keccak,
                max_age_seconds=policy.max_age_seconds,
            )

        assets = tuple(
            ManifestAsset(
                asset_id=f"token{i}",
                address=p.base_token,
                decimals=18,
                runtime_keccak=None if i == 2 else runtime_hash("0x6000"),
                role="cash",
                feed=feed(p),
            )
            for i, p in enumerate(policies)
        )
        snap = snapshots[-1].snapshot
        entry = ChainInventory(
            chain_id=gas.closing.ref.chain_id,
            account=gas.account,
            primary_source=gas.providers[0],
            secondary_source=gas.providers[1],
            assets=assets,
            positions=(
                ManifestPosition(
                    asset_id="lp",
                    position_manager=snap.position_manager,
                    position_manager_runtime_keccak=runtime_hash("0x6004"),
                    token_id=snap.token_id,
                    pool_id=snap.pool_id,
                    state_view=snapshots[-1].state_view,
                    state_view_runtime_keccak=position.retained.state_view_runtime_keccak,
                    pool_manager=position.cash_window.pool_manager,
                    pool_manager_runtime_keccak=position.retained.pool_manager_runtime_keccak,
                    currency0_id="token0",
                    currency1_id="token1",
                ),
            ),
        )
        manifest = PortfolioManifest(policy=risk_policy(entry), chains=(entry,))
        spent = replay_account_gas(store, gas).total_native

        class Inventory(GasInventory):
            @property
            def codes(self):
                return super().codes | {snap.position_manager: "0x6004"}

            def _call(self, params):
                if params[0]["to"] == snap.position_manager:
                    return "0x" + encode(["address"], [gas.account]).hex()
                return super()._call(params)

        observations = []
        for n, (capture, mark) in enumerate(
            zip(snapshots, (position.opening_marks, position.closing_marks), strict=True)
        ):
            prices = [
                replay_price_observation_evidence(
                    store, ref[0], header=capture.header, policy=p
                ).reconstruct_observation()
                for ref, p in zip(
                    (mark.currency0_price, mark.currency1_price), policies, strict=False
                )
            ]
            specs = tuple(
                TokenSpec(
                    f"token{i}",
                    p.base_token,
                    "0x6000",
                    18,
                    1000000,
                    FeedSpec(p.address, p.description, prices[i].answer, "0x6000"),
                )
                for i, p in enumerate(policies[:2])
            )
            f = Inventory(
                chain_id=capture.header.ref.chain_id,
                account=gas.account,
                block_number=capture.header.ref.number,
                block_hash=capture.header.ref.block_hash,
                parent_hash=capture.header.parent_hash,
                timestamp=capture.header.ref.timestamp,
                native_units=10**18 - (spent if n else 0),
                native_feed=FeedSpec(
                    gas.native_policy.address, gas.native_policy.description, 200000000000, "0x6000"
                ),
                tokens=specs,
            )
            refs = persist_account_inventory_evidence(
                store,
                collect_account_inventory_quorum(
                    inventory_session(gas.providers[0], f),
                    inventory_session(gas.providers[1], replace(f, methods=[], block_calls=0)),
                    entry=entry,
                    block_number=f.block_number,
                ),
            )
            observations.append(
                AccountObservation(
                    refs,
                    positions=(
                        PositionEvidence(
                            "lp",
                            position.cash_window.closing if n else position.cash_window.opening,
                            position.closing_pool_state if n else position.opening_pool_state,
                        ),
                    ),
                    native_gas_evidence=gas if n else None,
                )
            )
        account = AccountRequest.model_validate(
            {
                "schema": "oasis.account-risk-gas-input.v1",
                "manifest": manifest,
                "opening": {entry.key: observations[0]},
                "closing": {entry.key: observations[1]},
            }
        )
        (incoming / "account.json").write_bytes(account.model_dump_json(by_alias=True).encode())
        (incoming / "position.json").write_bytes(
            PositionRequest.model_validate(
                {"schema": "oasis.position-account-gas-input.v1", "request": joined}
            )
            .model_dump_json(by_alias=True)
            .encode()
        )
        headers = tuple(c.header for c in snapshots)
        states = tuple(
            replay_slot0_window_evidence(store, pair[0]).slot0[0]
            for pair in (position.opening_pool_state, position.closing_pool_state)
        )

        class Market:
            def post(self, raw):
                r = json.loads(raw)
                m, p = r["method"], r["params"]
                if m == "eth_chainId":
                    result = hex(headers[0].ref.chain_id)
                elif m == "eth_getBlockByNumber":
                    h = next(h for h in headers if h.ref.number == int(p[0], 16))
                    result = {
                        "number": hex(h.ref.number),
                        "hash": h.ref.block_hash,
                        "timestamp": hex(h.ref.timestamp),
                        "parentHash": h.parent_hash,
                    }
                elif m == "eth_getCode":
                    result = "0x6000" if p[0] == snapshots[-1].state_view else "0x6001"
                elif p[0]["data"] == "0x" + keccak(b"poolManager()")[:4].hex():
                    result = "0x" + encode(["address"], [position.cash_window.pool_manager]).hex()
                else:
                    st = next(
                        st
                        for h, st in zip(headers, states, strict=True)
                        if h.ref.block_hash == p[1]["blockHash"]
                    )
                    result = (
                        "0x"
                        + encode(
                            ["uint160", "int24", "uint24", "uint24"],
                            [st.sqrt_price_x96, st.tick, st.protocol_fee, st.lp_fee],
                        ).hex()
                    )
                return canonical_json({"jsonrpc": "2.0", "id": r["id"], "result": result})

        swaps = []
        prices = []
        for peer in gas.providers:
            dataset = BlockRangeDataset(
                peer, headers, (), position.cash_window.pool_manager, (SWAP_TOPIC, snap.pool_id)
            )
            swaps.append(persist_block_range_evidence(store, dataset, range_exchanges(dataset)))
            recorder = BoundedExchangeRecorder()
            rpc = JsonRpcTransport("https://fixture.invalid", http=Market(), recorder=recorder)
            cap = collect_slot0_window(
                (rpc, recorder, peer),
                chain_id=gas.closing.ref.chain_id,
                state_view=snapshots[-1].state_view,
                pool_manager=position.cash_window.pool_manager,
                pool_id=snap.pool_id,
                block_numbers=tuple(h.ref.number for h in headers),
                state_view_runtime_keccak=position.retained.state_view_runtime_keccak,
                pool_manager_runtime_keccak=position.retained.pool_manager_runtime_keccak,
            )
            prices.append(
                persist_slot0_capture(
                    store,
                    replace(cap, exchanges=recorder.exchanges),
                    retrieved_at="2026-10-04T00:00:00Z",
                )
            )
        market = {
            "swap_references": [asdict(r) for r in swaps],
            "price_references": [asdict(r) for r in prices],
            "pool": asdict(key),
            "pool_manager": position.cash_window.pool_manager,
            "mature_prefix": False,
            "horizon_seconds": 1,
            "max_lateness_seconds": 1,
        }
        (incoming / "market.json").write_bytes(canonical_json(market))
        providers = []
        declarations = [
            CurrencyDeclaration(
                address=p.base_token,
                decimals=18,
                feed=FeedPin(
                    address=p.address,
                    description=p.description,
                    runtime_keccak=p.runtime_keccak,
                    max_age_seconds=p.max_age_seconds,
                ),
            )
            for p in policies[:2]
        ]
        answers = [
            replay_price_observation_evidence(store, pair[0], header=headers[-1], policy=p)
            .reconstruct_observation()
            .answer
            for pair, p in zip(
                (position.closing_marks.currency0_price, position.closing_marks.currency1_price),
                policies,
                strict=False,
            )
        ]
        for peer in gas.providers:
            refs = [
                persist_currency_feed_evidence(
                    store,
                    reference_capture(
                        peer,
                        currency_feed(gas.closing.ref.chain_id, d),
                        answer=a,
                        block=headers[-1],
                    ),
                )
                for d, a in zip(declarations, answers, strict=True)
            ]
            providers.append(ReferenceProvider(source=peer, currency0=refs[0], currency1=refs[1]))
        scope = {
            "chain_id": gas.closing.ref.chain_id,
            "position_manager": snap.position_manager,
            "token_id": snap.token_id,
            "pool_id": snap.pool_id,
            "owner": gas.account,
        }
        assembly = {
            "window": market,
            "position": {
                "scope": scope,
                "references": [asdict(r) for r in position.cash_window.closing],
            },
            "reference": {
                "pool": asdict(key),
                "currency0": declarations[0].model_dump(mode="json"),
                "currency1": declarations[1].model_dump(mode="json"),
                "providers": [p.model_dump(mode="json") for p in providers],
                "max_reference_deviation": "0.05",
            },
            "retrieved_at": "2026-10-04T00:00:00Z",
        }
        (incoming / "assembly.json").write_bytes(canonical_json(assembly))
        d = replace(
            declaration(),
            chain_id=gas.closing.ref.chain_id,
            pool_id=snap.pool_id,
            block_number=gas.closing.ref.number,
            block_hash=gas.closing.ref.block_hash,
            min_benefit=Decimal(0),
            max_volatility=Decimal(100),
            max_toxicity=Decimal(100),
            candidates=(candidate(target_range=TickRange(-180, 180, key.tick_spacing)),),
        )
        (incoming / "declaration.json").write_bytes(d.payload)
        now = gas.closing.ref.timestamp
        policy = {
            "chain_id": gas.closing.ref.chain_id,
            "max_age_seconds": 3600,
            "strategy": serializable(band_strategy()),
            "min_benefit": "0",
            "canary_limits": None,
        }
        (incoming / "shadow.json").write_bytes(canonical_json(policy))
        executor = "0x" + "88" * 20
        signing = LpSignerPolicy(
            gas.closing.ref.chain_id,
            gas.account,
            executor,
            snap.pool_id,
            "0x" + "99" * 32,
            (
                RuntimePin(RuntimeRole.EXECUTOR, executor, "0x" + "11" * 32),
                RuntimePin(RuntimeRole.POSITION_MANAGER, snap.position_manager, "0x" + "22" * 32),
                RuntimePin(
                    RuntimeRole.POOL_MANAGER, position.cash_window.pool_manager, "0x" + "33" * 32
                ),
            ),
            1000,
            1000,
            1000,
            500000,
            1000000,
            10,
            60,
        )
        (incoming / "preparation.json").write_bytes(
            canonical_json(
                {
                    "policy": asdict(signing),
                    "bounds": asdict(LpPlanBounds(1, now + 50, 100, 1, 1, 1000, 1000, 400000, 60)),
                    "positions": [asdict(r) for r in position.cash_window.closing],
                    "now": now,
                }
            )
        )
        database = next(p for p in incoming.glob("*.db"))
        store.close()

        def replay(name):
            return {
                "request": pinned(incoming / (name + ".json"), tmp_path),
                "evidence": pinned(database, tmp_path),
            }

        w = {
            "schema": "oasis.lp-workflow.v1",
            "market_windows": [replay("market")],
            "account": replay("account"),
            "position": replay("position"),
            "cycle": replay("assembly")
            | {"declaration": pinned(incoming / "declaration.json", tmp_path)},
            "shadow": {"policy": pinned(incoming / "shadow.json", tmp_path), "now": now},
            "preparation": pinned(incoming / "preparation.json", tmp_path),
        }
        config = tmp_path / "workflow.json"
        config.write_bytes(canonical_json(w))
        yield config, w
    finally:
        generator.close()


def test_complete_pipeline_and_idempotent_restart_are_non_broadcasting(
    complete, tmp_path, monkeypatch
):
    config, _ = complete

    def denied(*args, **kwargs):
        raise AssertionError("network forbidden")

    monkeypatch.setattr(socket.socket, "connect", denied)
    output = tmp_path / "run"
    result = workflow.run(config, output, root=tmp_path)
    assert result["status"] == "completed", result
    assert result["engineering_stages_completed"] == 7
    assert result["coverage_passed"] is False
    assert result["net_pnl"] is None and not result["execution_eligible"]
    plan = json.loads((output / "preparation/report.json").read_bytes())
    assert plan["calldata"].startswith("0x") and plan["value"] == 0
    before = {p: workflow.digest(p) for p in output.rglob("*") if p.is_file()}
    assert workflow.run(config, output, resume=True, root=tmp_path) == result
    assert all(workflow.digest(p) == d for p, d in before.items())


def rewrite(config, document):
    config.write_bytes(canonical_json(document))


def change_input(config, document, group, root, mutate):
    asset = (
        document[group]["request"] if group in {"account", "position", "cycle"} else document[group]
    )
    path = root / asset["path"]
    body = json.loads(path.read_bytes())
    mutate(body)
    path.write_bytes(canonical_json(body))
    asset["sha256"] = workflow.digest(path)
    rewrite(config, document)


@pytest.mark.parametrize(
    "bad,failed",
    [
        ("account_cost_gap", "account"),
        ("capital_ceiling", "account"),
        ("owner_limit", "account"),
        ("position_gas_owner", "position"),
        ("undeclared_position", "account"),
        ("cycle_market", "cycle"),
        ("cycle_position", "cycle"),
        ("stale_shadow", "shadow"),
        ("hold_decision", "preparation"),
        ("preparation_account", "preparation"),
        ("preparation_clock", "preparation"),
    ],
)
def test_real_stage_refusals_preserve_upstream_and_block_dependents(
    complete, tmp_path, bad, failed
):
    config, document = complete
    if bad == "account_cost_gap":

        def mutate(body):
            next(iter(body["closing"].values()))["native_gas_evidence"]["blocks"][0]["costs"] = []

        change_input(config, document, "account", tmp_path, mutate)
    elif bad in {"capital_ceiling", "owner_limit"}:
        change_input(
            config,
            document,
            "account",
            tmp_path,
            lambda b: b["manifest"]["policy"].update(
                **(
                    {"total_capital_usd_micros": 1}
                    if bad == "capital_ceiling"
                    else {"daily_loss_usd_micros": 200_000_001}
                )
            ),
        )
    elif bad == "position_gas_owner":
        change_input(
            config,
            document,
            "position",
            tmp_path,
            lambda b: b["request"]["account_gas"].update(account="0x" + "ff" * 20),
        )
    elif bad == "undeclared_position":

        def mutate(body):
            entry = body["manifest"]["chains"][0]
            entry["positions"] = []
            key = next(iter(body["manifest"]["policy"]["inventory"]))
            body["manifest"]["policy"]["inventory"][key].remove("lp")
            body["opening"][key]["positions"] = []
            body["closing"][key]["positions"] = []

        change_input(config, document, "account", tmp_path, mutate)
    elif bad == "cycle_market":
        change_input(
            config, document, "cycle", tmp_path, lambda b: b["window"]["pool"].update(fee=500)
        )
    elif bad == "cycle_position":
        change_input(
            config,
            document,
            "cycle",
            tmp_path,
            lambda b: b["position"]["scope"].update(token_id=99),
        )
    elif bad == "stale_shadow":
        document["shadow"]["now"] += 3601
        rewrite(config, document)
    elif bad == "hold_decision":
        asset = document["shadow"]["policy"]
        body = json.loads((tmp_path / asset["path"]).read_bytes())
        body["min_benefit"] = "100000000000000000"
        (tmp_path / asset["path"]).write_bytes(canonical_json(body))
        asset["sha256"] = workflow.digest(tmp_path / asset["path"])
        rewrite(config, document)
    else:
        change_input(
            config,
            document,
            "preparation",
            tmp_path,
            (lambda b: b["policy"].update(account="0x" + "ff" * 20))
            if bad == "preparation_account"
            else lambda b: b.update(now=b["now"] + 1),
        )
    output = tmp_path / "run"
    originals = {
        a["path"]: workflow.digest(tmp_path / a["path"])
        for a in (document["account"]["evidence"], document["position"]["evidence"])
    }
    result = workflow.run(config, output, root=tmp_path)
    states = {s["stage"]: s["status"] for s in result["stages"]}
    assert result["status"] == "blocked" and states[failed] == "failed"
    for stage in workflow.STAGES:
        if failed in workflow.DEPENDENCIES[stage]:
            assert states[stage] == "blocked"
    assert all(workflow.digest(tmp_path / p) == d for p, d in originals.items())
    assert not result["execution_eligible"] and result["net_pnl"] is None
    assert workflow.run(config, output, resume=True, root=tmp_path) == result


@pytest.mark.parametrize(
    "bad", ["unknown", "bad_pin", "path_escape", "absolute", "symlink", "active_db"]
)
def test_preflight_refuses_before_run_state_or_network(complete, tmp_path, bad):
    config, document = complete
    if bad == "unknown":
        document["capture"] = True
    else:
        asset = document["market_windows"][0]["evidence"]
        if bad == "bad_pin":
            asset["sha256"] = "0" * 64
        elif bad == "path_escape":
            asset["path"] = "../incoming/cash.db"
        elif bad == "absolute":
            asset["path"] = str(tmp_path / asset["path"])
        elif bad == "symlink":
            target = tmp_path / "incoming/link.db"
            target.symlink_to(tmp_path / asset["path"])
            asset["path"] = str(target.relative_to(tmp_path))
        else:
            Path(str(tmp_path / asset["path"]) + "-wal").write_bytes(b"active")
    rewrite(config, document)
    output = tmp_path / "run"
    with pytest.raises((ValueError, OSError)):
        workflow.run(config, output, root=tmp_path)
    assert not output.exists()


def test_missing_inputs_are_visible_without_zero_filling(complete, tmp_path):
    config, document = complete
    document.update(account=None, position=None, cycle=None, shadow=None, preparation=None)
    rewrite(config, document)
    result = workflow.run(config, tmp_path / "run", root=tmp_path)
    assert result["status"] == "blocked" and result["engineering_stages_completed"] == 2
    assert result["net_pnl"] is None and result["coverage_passed"] is False
    assert not (tmp_path / "run/account/report.json").exists()


@pytest.mark.parametrize("bad", ["artifact", "config", "code"])
def test_restart_refuses_changed_result_or_run_identity(complete, tmp_path, monkeypatch, bad):
    config, document = complete
    output = tmp_path / "run"
    workflow.run(config, output, root=tmp_path)
    if bad == "artifact":
        (output / "shadow/report.json").write_bytes(b"{}")
    elif bad == "config":
        document["shadow"]["now"] += 1
        rewrite(config, document)
    else:
        original = workflow.code_pins()
        monkeypatch.setattr(workflow, "code_pins", lambda: original | {"python": "0" * 64})
    with pytest.raises(ValueError):
        workflow.run(config, output, resume=True, root=tmp_path)


def test_output_collision_lock_and_stop_never_replay_blindly(complete, tmp_path, monkeypatch):
    config, _ = complete
    output = tmp_path / "run"
    output.mkdir()
    with (output / "owner.lock").open("a+b") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):
            workflow.run(config, output, resume=True, root=tmp_path)

    def stop(*args):
        raise KeyboardInterrupt

    monkeypatch.setattr(workflow, "perform", stop)
    with pytest.raises(KeyboardInterrupt):
        workflow.run(config, output, resume=True, root=tmp_path)
    assert (output / "active.json").exists()
    assert json.loads((output / "market.json").read_bytes())["status"] == "stopped"
    with pytest.raises(ValueError, match="interrupted"):
        workflow.run(config, output, resume=True, root=tmp_path)
    with pytest.raises(ValueError, match="explicit resume"):
        workflow.run(config, output, root=tmp_path)


def test_account_loss_latch_prevents_any_position_or_decision(tmp_path):
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    store = EvidenceStore(incoming / "account.db")
    try:
        manifest, opening, closing, _ = scenario(store)
        manifest = manifest.model_copy(
            update={"policy": manifest.policy.model_copy(update={"daily_loss_usd_micros": 1})}
        )
        request = AccountRequest.model_validate(
            {
                "schema": "oasis.account-risk-gas-input.v1",
                "manifest": manifest,
                "opening": opening,
                "closing": closing,
            }
        )
        (incoming / "account.json").write_bytes(request.model_dump_json(by_alias=True).encode())
    finally:
        store.close()
    document = {
        "schema": "oasis.lp-workflow.v1",
        "market_windows": [],
        "account": {
            "request": pinned(incoming / "account.json", tmp_path),
            "evidence": pinned(incoming / "account.db", tmp_path),
        },
        "position": None,
        "cycle": None,
        "shadow": None,
        "preparation": None,
    }
    config = tmp_path / "workflow.json"
    rewrite(config, document)
    result = workflow.run(config, tmp_path / "run", root=tmp_path)
    assert next(r for r in result["stages"] if r["stage"] == "account")["status"] == "failed"
    risk = json.loads((tmp_path / "run/account/risk.json").read_bytes())
    assert risk["state"]["halted"] is True and risk["state"]["cumulative_loss"] == 1
    assert not (tmp_path / "run/shadow/journal.db").exists()


@pytest.mark.parametrize("bad", [None, "digest", "file"])
def test_sealed_checkpoint_pins_are_equivalent_and_checked_on_restart(complete, tmp_path, bad):
    config, document = complete
    checkpoint = tmp_path / "incoming/pins.json"
    asset = document["market_windows"][0]["request"]
    pins = {asset["path"]: asset.pop("sha256")}
    asset["checkpoint"] = str(checkpoint.relative_to(tmp_path))
    checkpoint.write_bytes(canonical_json(pins))
    rewrite(config, document)
    if bad == "digest":
        checkpoint.write_bytes(canonical_json({asset["path"]: "0" * 64}))
        with pytest.raises(ValueError):
            workflow.run(config, tmp_path / "run", root=tmp_path)
        assert not (tmp_path / "run").exists()
        return
    result = workflow.run(config, tmp_path / "run", root=tmp_path)
    assert result["status"] == "completed"
    if bad == "file":
        checkpoint.write_bytes(checkpoint.read_bytes() + b"\n")
        with pytest.raises(ValueError, match="changed"):
            workflow.run(config, tmp_path / "run", resume=True, root=tmp_path)
    else:
        assert workflow.run(config, tmp_path / "run", resume=True, root=tmp_path) == result
