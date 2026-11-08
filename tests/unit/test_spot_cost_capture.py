"""Frozen RPC -> one replayable cost bundle for one matched spot fill -> report CLI.

Synthetic retained responses only (FrozenRpc answers from a file; no network, wallet
or key). The journal comes from the real LiveExecutor/run_loop path, the proofs from
the real collect_receipt_cost_quorum, and the bundle is replayed by the unchanged
report_spot_shadow CLI.
"""

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import pytest

import oasis_agent.launch.spot_cost_capture as capture_module
import scripts.capture_spot_cost as capture_cli
import scripts.report_spot_shadow as report_cli
from oasis_agent.launch.spot_cost_capture import (
    FROZEN_SCHEMA,
    CostCaptureRefused,
    collect_order_cost,
    frozen_sessions,
)
from oasis_agent.models.chain import BlockSnapshot
from oasis_agent.models.common import BlockRef

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

    def fail_second(store: Any, capture: Any) -> Any:
        calls.append(1)
        if len(calls) == 2:
            raise OSError("disk full")
        return real(store, capture)

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
