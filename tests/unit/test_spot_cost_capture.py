"""Frozen RPC -> one replayable cost bundle for one matched spot fill -> report CLI.

Synthetic retained responses only (FrozenRpc answers from a file; no network, wallet
or key). The journal comes from the real LiveExecutor/run_loop path, the proofs from
the real collect_receipt_cost_quorum, and the bundle is replayed by the unchanged
report_spot_shadow CLI.
"""

import hashlib
import json
import sqlite3
import sys
from contextlib import closing
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest

import oasis_agent.launch.spot_cost_capture as capture_module
import scripts.capture_spot_cost as capture_cli
import scripts.report_spot_shadow as report_cli
from oasis_agent.evidence.store import EvidenceStore, ReadOnlyEvidenceStore
from oasis_agent.farming.transaction_costs import persist_transaction_cost
from oasis_agent.launch.spot_cost_binding import SCHEMA
from oasis_agent.launch.spot_cost_capture import (
    DIGEST_CANONICAL,
    DIGEST_FILE_BYTES,
    FROZEN_SCHEMA,
    CostCaptureRefused,
    collect_order_cost,
    frozen_origin,
    frozen_sessions,
    write_cost_bundle,
)
from oasis_agent.models.chain import BlockSnapshot
from oasis_agent.models.common import BlockRef, evidence_ref_from_json

from .test_spot_cost_binding import (
    BLOCK,
    HEADER,
    OPERATOR,
    OTHER_BLOCK,
    PARENT,
    PROVIDERS,
    World,
    cost_capture,
)
from .test_spot_receipt_fees import GAS, L1, PRICE

TOTAL = GAS * PRICE + L1 + OPERATOR


def frozen(
    source: str,
    tx: str,
    header: BlockSnapshot = HEADER,
    *,
    chain_result: int | None = None,
    drop: tuple[str, ...] = (),
    **capture: Any,
) -> dict[str, Any]:
    """Retained responses of one provider, taken from a real capture's own exchanges."""
    responses, seen = [], set()
    for x in cost_capture(source, tx, header, **capture).exchanges:
        request = json.loads(x.request_payload)
        result = json.loads(x.response_payload)["result"]
        key = json.dumps([request["method"], request["params"]])
        if key in seen or request["method"] in drop:
            continue
        seen.add(key)
        if chain_result is not None and request["method"] == "eth_chainId":
            result = hex(chain_result)
        responses.append(
            {"method": request["method"], "params": request["params"], "result": result}
        )
    return {"schema": FROZEN_SCHEMA, "source": source, "chain_id": 8453, "responses": responses}


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def staging_left(parent: Path) -> list[str]:
    return [p.name for p in parent.iterdir() if p.name.startswith(".spot-cost-")]


class Scene:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.world = World(tmp_path)
        self.world.store.close()
        self.out = tmp_path / "bundle"

    def write(self, *files: dict[str, Any]) -> list[str]:
        paths = []
        for i, f in enumerate(files):
            path = self.tmp / f"frozen{i}.json"
            path.write_text(json.dumps(f))
            paths.append(str(path))
        return paths

    def capture(
        self, monkeypatch: pytest.MonkeyPatch, *files: dict[str, Any], intent: str = ""
    ) -> int:
        argv = ["x", "--run", str(self.world.run), "--intent", intent or self.world.intent]
        for path in self.write(*files):
            argv += ["--frozen", path]
        monkeypatch.setattr(sys, "argv", [*argv, "--out", str(self.out)])
        return capture_cli.main()

    def good(self, **capture: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        a, b = PROVIDERS
        return frozen(a, self.world.tx, **capture), frozen(b, self.world.tx, **capture)


def test_frozen_responses_produce_a_bundle_the_report_cli_replays(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    scene = Scene(tmp_path)
    journal = scene.world.run / "journal.db"
    before = digest(journal)
    capsys.readouterr()
    assert scene.capture(monkeypatch, *scene.good()) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["total"] == str(TOTAL)
    assert sorted(p.name for p in scene.out.iterdir()) == ["binding.json", "evidence.db"]
    binding = json.loads((scene.out / "binding.json").read_bytes())
    assert binding["providers"] == PROVIDERS and binding["intent_id"] == scene.world.intent
    assert binding["header"]["block_hash"] == BLOCK
    assert len({c["content_hash"] for c in binding["costs"]}) == 2  # two distinct proofs

    argv = ["x", "--run", str(scene.world.run), "--cost-binding", str(scene.out / "binding.json")]
    monkeypatch.setattr(sys, "argv", [*argv, "--evidence", str(scene.out / "evidence.db")])
    assert report_cli.main() == 0
    replay = json.loads(capsys.readouterr().out)["cost_replay"]
    assert replay["status"] == "replayed"
    assert replay["native_wei"] == {
        "execution": str(GAS * PRICE),
        "l1": str(L1),
        "blob": "0",
        "operator": str(OPERATOR),
        "total": str(TOTAL),
    }
    assert replay["providers"] == PROVIDERS and replay["block"]["block_hash"] == BLOCK
    assert digest(journal) == before and staging_left(tmp_path) == []


def test_a_repeated_run_never_adds_a_second_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    scene = Scene(tmp_path)
    assert scene.capture(monkeypatch, *scene.good()) == 0
    first = {p.name: digest(p) for p in scene.out.iterdir()}
    assert scene.capture(monkeypatch, *scene.good()) == 2
    assert "output_exists" in capsys.readouterr().err
    assert {p.name: digest(p) for p in scene.out.iterdir()} == first
    assert staging_left(tmp_path) == []


@pytest.mark.parametrize(
    ("build", "category"),
    [
        # The collector's pinned block is not the fill's block (same height, other hash).
        (
            lambda s: tuple(
                frozen(
                    p,
                    s.world.tx,
                    BlockSnapshot(BlockRef(8453, 123, OTHER_BLOCK, 1_800_000_000), PARENT),
                )
                for p in PROVIDERS
            ),
            "block_mismatch",
        ),
        (lambda s: s.good(payer="0x" + "11" * 20), "identity_mismatch"),
        (lambda s: s.good(status=0), "status_mismatch"),
        # Chain answered by the frozen provider is not Base: the cost replay refuses it.
        (
            lambda s: tuple(frozen(p, s.world.tx, chain_result=84532) for p in PROVIDERS),
            "quorum_refused",
        ),
        # Providers disagree on one component only (never just the total).
        (
            lambda s: (
                frozen(PROVIDERS[0], s.world.tx),
                frozen(PROVIDERS[1], s.world.tx, operator=OPERATOR + 1),
            ),
            "quorum_refused",
        ),
        # A request the frozen file cannot answer is refused, never fetched live.
        (
            lambda s: tuple(frozen(p, s.world.tx, drop=("eth_call",)) for p in PROVIDERS),
            "quorum_refused",
        ),
        # The same provider twice is not two sources.
        (
            lambda s: (frozen(PROVIDERS[0], s.world.tx), frozen(PROVIDERS[0], s.world.tx)),
            "provider_pair_invalid",
        ),
        # A frozen file for another chain, or of another schema.
        (
            lambda s: tuple(frozen(p, s.world.tx) | {"chain_id": 84532} for p in PROVIDERS),
            "frozen_responses_invalid",
        ),
        (
            lambda s: tuple(frozen(p, s.world.tx) | {"schema": "x"} for p in PROVIDERS),
            "frozen_responses_invalid",
        ),
    ],
)
def test_refusals_publish_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    build: Any,
    category: str,
) -> None:
    scene = Scene(tmp_path)
    assert scene.capture(monkeypatch, *build(scene)) == 2
    assert f"refused: {category}" in capsys.readouterr().err
    assert not scene.out.exists() and staging_left(tmp_path) == []


def test_unmatched_order_and_unpinned_fill_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    scene = Scene(tmp_path)
    assert scene.capture(monkeypatch, *scene.good(), intent="nobody") == 2
    assert "order_not_matched" in capsys.readouterr().err
    sessions = frozen_sessions(dict(zip(PROVIDERS, scene.good(), strict=True)), chain_id=8453)
    pair = (PROVIDERS[0], PROVIDERS[1])
    # A fill journaled before receipt_block existed (height only) never binds.
    legacy = [
        r | {"receipt_fees": {k: v for k, v in r["receipt_fees"].items() if k != "receipt_block"}}
        if r["kind"] == "live_fill"
        else r
        for r in scene.world.records
    ]
    with pytest.raises(CostCaptureRefused, match="receipt_block_unknown"):
        collect_order_cost(legacy, scene.world.intent, sources=pair, sessions=sessions)
    # A duplicated fill is not a matched order.
    fill = next(r for r in scene.world.records if r["kind"] == "live_fill")
    with pytest.raises(CostCaptureRefused, match="order_not_matched"):
        collect_order_cost(
            [*scene.world.records, fill], scene.world.intent, sources=pair, sessions=sessions
        )
    assert not scene.out.exists()


def test_store_failure_mid_write_leaves_no_orphan_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    scene = Scene(tmp_path)
    real = capture_module.persist_transaction_cost
    calls: list[int] = []

    def fail_second(store: Any, capture: Any, **kwargs: Any) -> Any:
        calls.append(1)
        if len(calls) == 2:
            raise OSError("disk full")
        return real(store, capture, **kwargs)

    monkeypatch.setattr(capture_module, "persist_transaction_cost", fail_second)
    assert scene.capture(monkeypatch, *scene.good()) == 2
    assert len(calls) == 2  # the first proof was written, then the run failed
    assert not scene.out.exists() and staging_left(tmp_path) == []


def test_a_bundle_that_does_not_replay_is_never_published(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    scene = Scene(tmp_path)

    def refuses(records: Any, binding: Any, store: Any) -> dict[str, Any]:
        return {"status": "unbound", "reason": "evidence_invalid_0", "native_wei": None}

    monkeypatch.setattr(capture_module, "bind_order_cost", refuses)
    assert scene.capture(monkeypatch, *scene.good()) == 2
    assert "verification_evidence_invalid_0" in capsys.readouterr().err
    assert not scene.out.exists() and staging_left(tmp_path) == []


def test_frozen_rpc_refuses_duplicate_or_malformed_entries() -> None:
    good = frozen(PROVIDERS[0], "0x" + "cd" * 32)
    for bad in (
        good | {"responses": [*good["responses"], good["responses"][0]]},
        good | {"responses": [{"method": "eth_chainId", "params": {}, "result": "0x2105"}]},
        good | {"extra": 1},
        [],
    ):
        with pytest.raises(CostCaptureRefused, match="frozen_responses_invalid"):
            frozen_sessions({PROVIDERS[0]: bad}, chain_id=8453)


def run_capture(scene: Scene, monkeypatch: pytest.MonkeyPatch, files: tuple[Any, ...]) -> list[str]:
    """Run the producer and return the frozen file paths it actually read."""
    paths = scene.write(*files)
    argv = ["x", "--run", str(scene.world.run), "--intent", scene.world.intent]
    for path in paths:
        argv += ["--frozen", path]
    monkeypatch.setattr(sys, "argv", [*argv, "--out", str(scene.out)])
    assert capture_cli.main() == 0
    return paths


def report(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    run: Path,
    bundle: Path,
) -> dict[str, Any]:
    capsys.readouterr()
    argv = ["x", "--run", str(run), "--cost-binding", str(bundle / "binding.json")]
    monkeypatch.setattr(sys, "argv", [*argv, "--evidence", str(bundle / "evidence.db")])
    assert report_cli.main() == 0
    out: dict[str, Any] = json.loads(capsys.readouterr().out)["cost_replay"]
    return out


def test_moved_bundle_still_states_frozen_origin_and_input_digests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    scene = Scene(tmp_path)
    paths = run_capture(scene, monkeypatch, scene.good())
    expected = [hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in paths]
    # Hand the bundle over: move it elsewhere and delete the frozen inputs.
    moved = tmp_path / "elsewhere" / "handover"
    moved.parent.mkdir()
    scene.out.rename(moved)
    for p in paths:
        Path(p).unlink()
    replay = report(monkeypatch, capsys, scene.world.run, moved)
    assert replay["status"] == "replayed"
    assert replay["basis"] == "frozen_input_reconstruction_not_provider_observation"
    assert replay["provider_independence"] == "not_established"
    origin = replay["origin"]
    assert origin["kind"] == "frozen_input_reconstruction"
    assert origin["schema"] == FROZEN_SCHEMA
    assert origin["digest_basis"] == DIGEST_FILE_BYTES
    assert origin["inputs"] == [
        {"source": PROVIDERS[0], "sha256": expected[0]},
        {"source": PROVIDERS[1], "sha256": expected[1]},
    ]
    assert replay["native_wei"]["total"] == str(TOTAL)  # numbers unchanged


def test_two_aliases_over_the_same_answers_are_still_a_reconstruction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    scene = Scene(tmp_path)
    one = frozen(PROVIDERS[0], scene.world.tx)
    two = one | {"source": PROVIDERS[1]}  # identical responses, only the alias differs
    run_capture(scene, monkeypatch, (one, two))
    replay = report(monkeypatch, capsys, scene.world.run, scene.out)
    assert replay["status"] == "replayed" and len(set(replay["evidence"])) == 2
    # Distinct references and two names never read as two observed providers.
    assert replay["basis"] == "frozen_input_reconstruction_not_provider_observation"
    assert replay["origin"]["kind"] == "frozen_input_reconstruction"
    assert replay["provider_independence"] == "not_established"


def legacy_bundle(scene: Scene, origins: tuple[Any, Any] = (None, None)) -> Path:
    """A bundle written the historical way (or with given origins), not via the CLI."""
    bundle = scene.tmp / "legacy"
    bundle.mkdir()
    store = EvidenceStore(bundle / "evidence.db")
    refs = [
        persist_transaction_cost(store, cost_capture(p, scene.world.tx), origin=o)
        for p, o in zip(PROVIDERS, origins, strict=True)
    ]
    store.close()
    binding = {
        "schema": SCHEMA,
        "intent_id": scene.world.intent,
        "header": {
            "chain_id": 8453,
            "number": 123,
            "block_hash": BLOCK,
            "timestamp": HEADER.ref.timestamp,
            "parent_hash": PARENT,
        },
        "providers": list(PROVIDERS),
        "costs": [asdict(r) for r in refs],
    }
    (bundle / "binding.json").write_text(json.dumps(binding))
    return bundle


def test_old_bundle_without_origin_replays_with_unknown_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    scene = Scene(tmp_path)
    bundle = legacy_bundle(scene)
    # The historical interpretation bytes are unchanged when no origin is given.
    store = ReadOnlyEvidenceStore(bundle / "evidence.db")
    binding = json.loads((bundle / "binding.json").read_text())
    ref = evidence_ref_from_json(binding["costs"][0])
    interpretation = json.loads(store.get_record(ref).interpretation_payload or b"null")
    store.close()
    assert interpretation == {
        "scope": "whole_transaction_native_gas_cost",
        "execution_eligible": False,
    }
    replay = report(monkeypatch, capsys, scene.world.run, bundle)
    assert replay["status"] == "replayed" and replay["origin"] == {"kind": "unknown"}
    assert replay["basis"] == "two_declared_providers_retained_raw_receipt_origin_unknown"
    assert replay["native_wei"]["total"] == str(TOTAL)


GOOD_DIGEST = "ab" * 32


def origin_of(*inputs: Any, **change: Any) -> dict[str, Any]:
    base = {
        "kind": "frozen_input_reconstruction",
        "schema": FROZEN_SCHEMA,
        "digest_basis": DIGEST_FILE_BYTES,
        "inputs": list(inputs) or [{"source": p, "sha256": GOOD_DIGEST} for p in PROVIDERS],
    }
    return base | change


@pytest.mark.parametrize(
    "origins",
    [
        (origin_of(), None),  # one proof marked, the other not
        (origin_of(), origin_of(digest_basis=DIGEST_CANONICAL)),  # the two disagree
        (origin_of(kind="provider_observation"),) * 2,  # claims to be an observation
        (origin_of(schema="x"),) * 2,
        (origin_of(digest_basis="md5"),) * 2,
        (
            origin_of(
                {"source": PROVIDERS[0], "sha256": "zz"},
                {"source": PROVIDERS[1], "sha256": GOOD_DIGEST},
            ),
        )
        * 2,
        (
            origin_of(
                {"source": PROVIDERS[1], "sha256": GOOD_DIGEST},
                {"source": PROVIDERS[0], "sha256": GOOD_DIGEST},
            ),
        )
        * 2,
        (origin_of({"source": PROVIDERS[0], "sha256": GOOD_DIGEST}),) * 2,  # one input
        (origin_of() | {"extra": 1},) * 2,
        ("frozen",) * 2,
    ],
)
def test_bad_or_inconsistent_origin_is_refused_not_upgraded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    origins: tuple[Any, Any],
) -> None:
    scene = Scene(tmp_path)
    replay = report(monkeypatch, capsys, scene.world.run, legacy_bundle(scene, origins))
    assert (replay["status"], replay["reason"]) == ("unbound", "origin_invalid")
    assert replay["native_wei"] is None and replay["basis"] == "not_replayed"


def test_edited_origin_bytes_fail_the_retained_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    scene = Scene(tmp_path)
    run_capture(scene, monkeypatch, scene.good())
    db = scene.out / "evidence.db"
    with closing(sqlite3.connect(db)) as connection:
        rows = connection.execute(
            "SELECT evidence_key, interpretation_payload FROM evidence"
        ).fetchall()
        key, payload = rows[0]
        edited = json.loads(payload)
        edited["origin"]["kind"] = "provider_observation"  # promote without re-hashing
        connection.execute(
            "UPDATE evidence SET interpretation_payload = ? WHERE evidence_key = ?",
            (json.dumps(edited).encode(), key),
        )
        connection.commit()
    replay = report(monkeypatch, capsys, scene.world.run, scene.out)
    assert replay["status"] == "unbound" and replay["reason"].startswith("evidence_invalid_")


def test_producer_refuses_an_origin_for_other_sources(tmp_path: Path) -> None:
    scene = Scene(tmp_path)
    sessions = frozen_sessions(dict(zip(PROVIDERS, scene.good(), strict=True)), chain_id=8453)
    pair = (PROVIDERS[0], PROVIDERS[1])
    header, captures = collect_order_cost(
        scene.world.records, scene.world.intent, sources=pair, sessions=sessions
    )
    wrong = frozen_origin(
        (PROVIDERS[1], PROVIDERS[0]), (GOOD_DIGEST, GOOD_DIGEST), DIGEST_FILE_BYTES
    )
    with pytest.raises(CostCaptureRefused, match="origin_sources_mismatch"):
        write_cost_bundle(
            scene.out, scene.world.records, scene.world.intent, header, captures, origin=wrong
        )
    assert not scene.out.exists() and staging_left(tmp_path) == []


@pytest.mark.parametrize(
    "field",
    ["digest_basis", "kind", "schema"],
)
@pytest.mark.parametrize("value", [[], {}, ["x"], 1, None, "unknown-label"])
def test_any_json_type_in_an_origin_enum_is_a_stable_refusal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    field: str,
    value: Any,
) -> None:
    scene = Scene(tmp_path)
    bad = origin_of(**{field: value})
    replay = report(monkeypatch, capsys, scene.world.run, legacy_bundle(scene, (bad, bad)))
    # The CLI still returns the base report; the replay is refused, no amounts.
    assert (replay["status"], replay["reason"]) == ("unbound", "origin_invalid")
    assert replay["native_wei"] is None


@pytest.mark.parametrize("source", [[], {}, 1])
def test_any_json_type_in_an_input_source_is_a_stable_refusal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    source: Any,
) -> None:
    scene = Scene(tmp_path)
    bad = origin_of(
        {"source": source, "sha256": GOOD_DIGEST}, {"source": PROVIDERS[1], "sha256": GOOD_DIGEST}
    )
    replay = report(monkeypatch, capsys, scene.world.run, legacy_bundle(scene, (bad, bad)))
    assert (replay["status"], replay["reason"]) == ("unbound", "origin_invalid")


def null_interpretations(db: Path, rows: int) -> None:
    """Remove the stored interpretation of the first `rows` proofs; references unchanged."""
    with closing(sqlite3.connect(db)) as connection:
        keys = [
            k for (k,) in connection.execute("SELECT evidence_key FROM evidence ORDER BY rowid")
        ]
        for key in keys[:rows]:
            connection.execute(
                "UPDATE evidence SET interpretation_payload = NULL WHERE evidence_key = ?", (key,)
            )
        connection.commit()


@pytest.mark.parametrize("rows", [1, 2])
def test_removed_declared_interpretation_is_corruption_not_a_legacy_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], rows: int
) -> None:
    scene = Scene(tmp_path)
    run_capture(scene, monkeypatch, scene.good())
    null_interpretations(scene.out / "evidence.db", rows)
    replay = report(monkeypatch, capsys, scene.world.run, scene.out)
    # Before this fix both cases replayed silently as origin "unknown".
    assert replay["status"] == "unbound" and replay["reason"].startswith("evidence_invalid_")
    assert replay["native_wei"] is None and replay.get("origin") is None


def test_genuine_legacy_and_normal_bundles_are_unaffected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    scene = Scene(tmp_path)
    legacy = report(monkeypatch, capsys, scene.world.run, legacy_bundle(scene))
    assert legacy["status"] == "replayed" and legacy["origin"] == {"kind": "unknown"}
    run_capture(scene, monkeypatch, scene.good())
    normal = report(monkeypatch, capsys, scene.world.run, scene.out)
    assert normal["status"] == "replayed"
    assert normal["origin"]["kind"] == "frozen_input_reconstruction"
    assert legacy["native_wei"] == normal["native_wei"] and normal["native_wei"]["total"] == str(
        TOTAL
    )
