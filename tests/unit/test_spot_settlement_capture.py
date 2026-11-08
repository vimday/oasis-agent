"""Frozen RPC -> cost + whole-block settlement bundle for one spot fill -> report CLI.

One frozen file per provider answers both the receipt cost reads and the block
settlement reads of the fill's wallet. The real capture and report CLIs run in-process.
Synthetic retained responses only: no network, wallet or key.
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
from oasis_agent.farming.block_settlement import settlement_requests
from oasis_agent.launch.onchainos_source import BASE_USDC
from oasis_agent.launch.spot_cost_capture import (
    DIGEST_FILE_BYTES,
    CostCaptureRefused,
    collect_order_cost,
    collect_order_settlement,
    frozen_origin,
    frozen_sessions,
    write_settled_bundle,
)
from oasis_agent.models.chain import BlockSnapshot
from oasis_agent.models.common import BlockRef

from .test_block_settlement import log
from .test_spot_cost_binding import HEADER, PARENT, PROVIDERS
from .test_spot_cost_capture import frozen
from .test_spot_settlement_check import FEE, NATIVE, OTHER_TX, Scene
from .test_spot_transfer_check import STRANGER

PAIR = (PROVIDERS[0], PROVIDERS[1])
# The capture CLI's summary before settlement existed; without the flag it must not change.
COST_SUMMARY_KEYS = {"bundle", "origin", "execution", "l1", "blob", "operator", "total"}


class Bundle:
    """A matched buy whose two frozen files answer both cost and settlement reads."""

    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.scene = Scene(tmp_path)
        self.world = self.scene.world
        self.world.store.close()
        self.out = tmp_path / "bundle"

    def files(self, **chain: Any) -> list[dict[str, Any]]:
        """Each provider's cost responses plus the block settlement answers of `chain`."""
        block = self.scene.chain(**chain)
        specs = settlement_requests(HEADER, self.scene.wallet, self.scene.tokens)
        out = []
        for source in PROVIDERS:
            retained = frozen(source, self.world.tx, logs=self.scene.receipt_logs())
            seen = {json.dumps([r["method"], r["params"]]) for r in retained["responses"]}
            for (method, params), result in zip(specs, block.results(), strict=True):
                key = json.dumps([method, params])
                if key not in seen:  # chain id and the block header are shared reads
                    seen.add(key)
                    retained["responses"].append(
                        {"method": method, "params": params, "result": result}
                    )
            out.append(retained)
        return out

    def sessions(self, files: list[dict[str, Any]]) -> Any:
        return frozen_sessions(dict(zip(PROVIDERS, files, strict=True)), chain_id=8453)

    def capture(
        self, monkeypatch: pytest.MonkeyPatch, files: list[dict[str, Any]], *flags: str
    ) -> int:
        argv = ["x", "--run", str(self.world.run), "--intent", self.world.intent]
        for i, retained in enumerate(files):
            path = self.tmp / f"frozen{i}.json"
            path.write_text(json.dumps(retained))
            argv += ["--frozen", str(path)]
        monkeypatch.setattr(sys, "argv", [*argv, "--out", str(self.out), *flags])
        return capture_cli.main()

    def report(self, monkeypatch: pytest.MonkeyPatch, *flags: str) -> int:
        argv = ["x", "--run", str(self.world.run)]
        argv += ["--cost-binding", str(self.out / "binding.json")]
        argv += ["--evidence", str(self.out / "evidence.db")]
        argv += ["--settlement-binding", str(self.out / "settlement.json")]
        monkeypatch.setattr(sys, "argv", [*argv, *flags])
        return report_cli.main()


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def staging_left(parent: Path) -> list[str]:
    return [p.name for p in parent.iterdir() if p.name.startswith(".spot-cost-")]


def test_frozen_files_produce_a_settled_bundle_the_report_reconciles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle = Bundle(tmp_path)
    journal = bundle.world.run / "journal.db"
    before = digest(journal)
    capsys.readouterr()
    assert bundle.capture(monkeypatch, bundle.files(), "--with-settlement") == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["settlement"] == {"status": "reconciled", "reason": None}
    assert printed["total"] == str(FEE)
    names = sorted(p.name for p in bundle.out.iterdir())
    assert names == ["binding.json", "evidence.db", "settlement.json"]
    assert bundle.report(monkeypatch, "--require-block-settlement") == 0
    view = json.loads(capsys.readouterr().out)["block_settlement"]
    assert view["status"] == "reconciled" and view["native_wei"]["delta"] == str(-FEE)
    assert view["origin"]["kind"] == "frozen_input_reconstruction"
    assert view["tokens"]["usdc"]["token"] == BASE_USDC
    assert digest(journal) == before and staging_left(tmp_path) == []


def test_without_the_flag_the_bundle_and_summary_are_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle = Bundle(tmp_path)
    capsys.readouterr()
    assert bundle.capture(monkeypatch, bundle.files()) == 0
    assert set(json.loads(capsys.readouterr().out)) == COST_SUMMARY_KEYS
    assert sorted(p.name for p in bundle.out.iterdir()) == ["binding.json", "evidence.db"]


def another_payment(scene: Scene) -> dict[str, Any]:
    """Another transaction pays the wallet 9 USDC units in the fill's own block."""
    gift = log(BASE_USDC, STRANGER, scene.wallet, 9, 7, tx=OTHER_TX, pos=3)
    balances = scene.chain().balances
    before, after = balances[BASE_USDC]
    return {
        "logs": [*scene.receipt_logs(), gift],
        "balances": balances | {BASE_USDC: (before, after + 9)},
    }


@pytest.mark.parametrize(
    ("chain", "status"),
    [
        (lambda scene: {"native": (NATIVE, NATIVE - FEE - 1)}, "residual"),
        (another_payment, "unverified"),
    ],
)
def test_an_unreconciled_settlement_is_published_and_the_strict_report_refuses_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    chain: Any,
    status: str,
) -> None:
    bundle = Bundle(tmp_path)
    files = bundle.files(**chain(bundle.scene))
    capsys.readouterr()
    assert bundle.capture(monkeypatch, files, "--with-settlement") == 0
    assert json.loads(capsys.readouterr().out)["settlement"]["status"] == status
    assert bundle.report(monkeypatch) == 0
    assert json.loads(capsys.readouterr().out)["block_settlement"]["status"] == status
    assert bundle.report(monkeypatch, "--require-block-settlement") == 2


def test_frozen_files_without_settlement_answers_publish_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle = Bundle(tmp_path)
    cost_only = [frozen(p, bundle.world.tx, logs=bundle.scene.receipt_logs()) for p in PROVIDERS]
    assert bundle.capture(monkeypatch, cost_only, "--with-settlement") == 2
    assert "settlement_quorum_refused" in capsys.readouterr().err
    assert not bundle.out.exists() and staging_left(tmp_path) == []


def test_providers_disagreeing_on_the_block_publish_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle = Bundle(tmp_path)
    first = bundle.files()[0]
    second = bundle.files(native=(NATIVE, NATIVE - FEE + 1))[1]
    assert bundle.capture(monkeypatch, [first, second], "--with-settlement") == 2
    assert "settlement_quorum_refused" in capsys.readouterr().err
    assert not bundle.out.exists() and staging_left(tmp_path) == []


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        ("unbound", "settlement_evidence_invalid"),
        ("unverified", "settlement_unreadable"),
        # The quorum already made both providers agree; a disagreement on read-back means
        # the written proofs are not what this run collected.
        ("conflict", "providers_disagree_on_block_settlement"),
    ],
)
def test_settlement_proofs_that_do_not_read_back_are_never_published(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    status: str,
    reason: str,
) -> None:
    bundle = Bundle(tmp_path)

    def refuses(*_args: Any) -> dict[str, Any]:
        return {"status": status, "reason": reason, "tokens": None}

    monkeypatch.setattr(capture_module, "check_block_settlement", refuses)
    assert bundle.capture(monkeypatch, bundle.files(), "--with-settlement") == 2
    assert f"verification_settlement_{reason}" in capsys.readouterr().err
    assert not bundle.out.exists() and staging_left(tmp_path) == []


def test_receipts_contradicting_the_block_log_list_are_published_as_a_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Both files' receipts list the token leg once; both block log lists split it in two.
    bundle = Bundle(tmp_path)
    usdc, token = bundle.scene.receipt_logs()
    units = bundle.scene.units
    split = [
        usdc,
        token | {"data": "0x" + (units - 1).to_bytes(32, "big").hex()},
        token | {"logIndex": hex(2), "data": "0x" + (1).to_bytes(32, "big").hex()},
    ]
    capsys.readouterr()
    assert bundle.capture(monkeypatch, bundle.files(logs=split), "--with-settlement") == 0
    assert json.loads(capsys.readouterr().out)["settlement"] == {
        "status": "conflict",
        "reason": "receipt_and_block_transfer_logs_differ",
    }
    assert bundle.report(monkeypatch, "--require-block-settlement") == 2


def test_settlement_of_another_block_or_provider_order_is_refused(tmp_path: Path) -> None:
    bundle = Bundle(tmp_path)
    files, records, intent = bundle.files(), bundle.world.records, bundle.world.intent
    header, costs = collect_order_cost(
        records, intent, sources=PAIR, sessions=bundle.sessions(files)
    )
    other = BlockSnapshot(BlockRef(8453, 123, "0x" + "b2" * 32, 1_800_000_000), PARENT)
    with pytest.raises(CostCaptureRefused, match="block_mismatch"):
        collect_order_settlement(
            records, intent, other, sources=PAIR, sessions=bundle.sessions(files)
        )
    swapped = collect_order_settlement(
        records, intent, header, sources=(PAIR[1], PAIR[0]), sessions=bundle.sessions(files)
    )
    origin = frozen_origin(PAIR, ("ab" * 32, "cd" * 32), DIGEST_FILE_BYTES)
    with pytest.raises(CostCaptureRefused, match="settlement_sources_mismatch"):
        write_settled_bundle(bundle.out, records, intent, header, costs, swapped, origin=origin)
    assert not bundle.out.exists() and staging_left(tmp_path) == []
