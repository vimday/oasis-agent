"""One retained-input workflow: accounting, decisions and simulation preparation.

Fixed in-process adapters only. Source stores are read-only; derived evidence and
journals live in the run directory. No acquisition, signing or broadcast adapter.
"""

import fcntl
import hashlib
import html
import json
import os
import sqlite3
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, model_validator

from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.execution.lp_preparation import LpPlanBounds, build_lp_simulation_plan
from oasis_agent.execution.lp_reference_price import currency_feed
from oasis_agent.execution.lp_signer_policy import LpSignerPolicy
from oasis_agent.farming.lp_dataset_coverage import assess_dataset_coverage
from oasis_agent.farming.position_capture import replay_position_snapshot_evidence
from oasis_agent.farming.position_quorum import PositionQuorum
from oasis_agent.launch.models import StrictModel
from oasis_agent.models.common import EvidenceRef, canonical_json, sha256_hex
from oasis_agent.risk.account_risk import PortfolioFrame, RiskState, reconcile
from oasis_agent.shadow.journal import ShadowJournal
from oasis_agent.shadow.market_window import build_market_window
from oasis_agent.shadow.service import LpCycle, LpShadowService, _unique_object, parse_policy
from scripts.assemble_lp_cycle import AssemblyRequest, assemble
from scripts.build_lp_market_window import Request as MarketRequest
from scripts.replay_account_risk_gas import Request as AccountRequest
from scripts.replay_account_risk_gas import run as account_run
from scripts.replay_position_account_gas import Request as PositionRequest
from scripts.replay_position_account_gas import run as position_run
from scripts.report_lp_market_economics import summarize

ROOT = Path(__file__).resolve().parents[1]
STAGES = ("market", "coverage", "account", "position", "cycle", "shadow", "preparation")
DEPENDENCIES = {
    "market": (),
    "coverage": ("market",),
    "account": (),
    "position": ("account",),
    "cycle": ("market", "account", "position"),
    "shadow": ("cycle",),
    "preparation": ("shadow",),
}
INPUTS = {
    "market": "market_windows: retained request/evidence pairs",
    "account": "account: raw opening/closing manifest inventory and complete gas request",
    "position": "position: raw position cash/valuation and the account's identical gas proof",
    "cycle": "cycle: assembly request and economic declaration at the market/account close",
    "shadow": "shadow: fixed strategy policy and explicit observation time",
    "preparation": "preparation: retained position quorum, signer policy and bounded intent",
}


class Asset(StrictModel):
    path: Annotated[str, Field(min_length=1, max_length=512)]
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None = None
    checkpoint: Annotated[str, Field(min_length=1, max_length=512)] | None = None

    @model_validator(mode="after")
    def pinned(self):
        if (self.sha256 is None) == (self.checkpoint is None):
            raise ValueError("exactly one explicit digest or sealed checkpoint required")
        return self


class ReplayInput(StrictModel):
    request: Asset
    evidence: Asset


class CycleInput(ReplayInput):
    declaration: Asset


class ShadowInput(StrictModel):
    policy: Asset
    now: Annotated[int, Field(strict=True, ge=0)]


class PreparationInput(StrictModel):
    policy: LpSignerPolicy
    bounds: LpPlanBounds
    positions: tuple[EvidenceRef, EvidenceRef]
    now: Annotated[int, Field(strict=True, ge=0)]


class Workflow(StrictModel):
    schema_version: Literal["oasis.lp-workflow.v1"] = Field(alias="schema")
    market_windows: Annotated[tuple[ReplayInput, ...], Field(max_length=1000)]
    account: ReplayInput | None
    position: ReplayInput | None
    cycle: CycleInput | None
    shadow: ShadowInput | None
    preparation: Asset | None


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def expected(asset: Asset, root: Path) -> str:
    if asset.sha256 is not None:
        return asset.sha256
    if asset.checkpoint is None:
        raise ValueError("checkpoint missing")
    path = root / asset.checkpoint
    if (
        Path(asset.checkpoint).is_absolute()
        or ".." in Path(asset.checkpoint).parts
        or path.is_symlink()
        or not path.resolve().is_relative_to(root.resolve())
        or path.suffix != ".json"
        or path.stat().st_size > 10_000_000
    ):
        raise ValueError("checkpoint path refused")
    manifest = json.loads(path.read_bytes(), object_pairs_hook=_unique_object)
    value = manifest[asset.path]
    # Apply the identical digest constraint to the checkpoint's actual value.
    return Asset(path=asset.path, sha256=value).sha256


def source(asset: Asset, root: Path) -> Path:
    path = root / asset.path
    resolved = path.resolve(strict=True)
    if (
        Path(asset.path).is_absolute()
        or ".." in Path(asset.path).parts
        or path.is_symlink()
        or not resolved.is_relative_to(root.resolve())
        or resolved.suffix not in {".json", ".db"}
        or not resolved.is_file()
        or resolved.stat().st_size > (256_000_000 if resolved.suffix == ".db" else 10_000_000)
        or digest(resolved) != expected(asset, root)
    ):
        raise ValueError("input pin or path refused")
    if resolved.suffix == ".db" and any(
        Path(str(resolved) + suffix).exists() and Path(str(resolved) + suffix).stat().st_size
        for suffix in ("-wal", "-journal")
    ):
        raise ValueError("active source database refused")
    return resolved


def assets(workflow: Workflow) -> tuple[Asset, ...]:
    replays = (*workflow.market_windows, workflow.account, workflow.position, workflow.cycle)
    values = [a for item in replays if item for a in (item.request, item.evidence)]
    if workflow.cycle:
        values.append(workflow.cycle.declaration)
    if workflow.shadow:
        values.append(workflow.shadow.policy)
    if workflow.preparation:
        values.append(workflow.preparation)
    return tuple(values)


def code_pins() -> dict[str, str]:
    paths = sorted([*(ROOT / "src").rglob("*.py"), *(ROOT / "scripts").glob("*.py")])
    return {
        **{str(p.relative_to(ROOT)): digest(p) for p in paths},
        "Makefile": digest(ROOT / "Makefile"),
        "Oasis.command": digest(ROOT / "Oasis.command"),
        "requirements.lock": digest(ROOT / "requirements.lock"),
        "requirements-dev.lock": digest(ROOT / "requirements-dev.lock"),
        "uv.lock": digest(ROOT / "uv.lock"),
        "python": sha256_hex(sys.version.encode()),
    }


def write(path: Path, value: object) -> None:
    with path.open("xb") as stream:
        stream.write(canonical_json(value) + b"\n")
        stream.flush()
        os.fsync(stream.fileno())


def snapshot(asset: Asset, root: Path, output: Path) -> Path:
    target = output / "inputs" / (expected(asset, root) + Path(asset.path).suffix)
    if target.exists():
        return target
    original = source(asset, root)
    if original.suffix == ".db":
        src = sqlite3.connect(original.as_uri() + "?mode=ro", uri=True)
        dst = sqlite3.connect(target)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
    else:
        with target.open("xb") as stream:
            stream.write(original.read_bytes())
    if digest(original) != expected(asset, root):
        raise ValueError("source changed during snapshot")
    return target


def _window(store: EvidenceStore, request: MarketRequest):
    return build_market_window(
        store, **{name: getattr(request, name) for name in type(request).model_fields}
    )


def perform(stage: str, workflow: Workflow, output: Path, inputs: dict[str, Path]) -> None:
    def path(asset: Asset) -> Path:
        return inputs[asset.path]

    def db(item: ReplayInput) -> Path:
        # Only cycle assembly writes derived records; other adapters see a private copy too.
        return path(item.evidence)

    destination = output / stage
    destination.mkdir()
    if stage == "market":
        summaries = []
        for i, item in enumerate(workflow.market_windows):
            store = EvidenceStore(db(item))
            try:
                request = MarketRequest.from_json(path(item.request).read_bytes())
                window = _window(store, request)
                summaries.append({**summarize(window), "pool": asdict(request.pool)})
                write(destination / f"window-{i:04d}.json", json.loads(window.payload))
            finally:
                store.close()
        write(destination / "report.json", {"windows": summaries})
    elif stage == "coverage":
        from_windows = []
        for item in workflow.market_windows:
            store = EvidenceStore(db(item))
            try:
                from_windows.append(
                    _window(store, MarketRequest.from_json(path(item.request).read_bytes()))
                )
            finally:
                store.close()
        write(
            destination / "report.json",
            json.loads(assess_dataset_coverage(tuple(from_windows)).payload),
        )
    elif stage == "account":
        if workflow.account is None:
            raise ValueError("account input missing")
        request = AccountRequest.from_json(path(workflow.account.request).read_bytes())
        policy = request.manifest.policy
        if (
            policy.total_capital_usd_micros > 500_000_000
            or policy.daily_loss_usd_micros > 200_000_000
            or policy.cumulative_loss_usd_micros > 500_000_000
        ):
            raise ValueError("owner limits exceeded")
        account_run(
            path(workflow.account.request), db(workflow.account), destination / "report.json"
        )
        report = json.loads((destination / "report.json").read_bytes())
        opening = PortfolioFrame.model_validate_json(canonical_json(report["opening"]))
        closing = PortfolioFrame.model_validate_json(canonical_json(report["closing"]))
        first = reconcile(
            policy, RiskState(), opening, now=max(a.timestamp for a in opening.accounts)
        )
        state = reconcile(policy, first, closing, now=max(a.timestamp for a in closing.accounts))
        deployed = sum(
            max(0, a.value) for f in closing.accounts for a in f.assets if a.role == "investment"
        )
        write(
            destination / "risk.json",
            {
                "scope": "this opening/closing window only",
                "state": state.model_dump(mode="json"),
                "deployed_usd_micros": deployed,
                "capital_passed": deployed <= policy.total_capital_usd_micros,
                "execution_eligible": False,
            },
        )
        if state.halted or deployed > policy.total_capital_usd_micros:
            raise ValueError("loss latch or capital ceiling refused downstream stages")
    elif stage == "position":
        if workflow.position is None or workflow.account is None:
            raise ValueError("position/account input missing")
        account = AccountRequest.from_json(path(workflow.account.request).read_bytes())
        position = PositionRequest.from_json(path(workflow.position.request).read_bytes())
        gas = position.request.account_gas
        entry = next(a for a in account.manifest.chains if a.account == gas.account)
        if account.closing[entry.key].native_gas_evidence != gas:
            raise ValueError("position and account must reuse the identical full gas request")
        retained = position.request.position.retained
        declared = next(
            p
            for p in entry.positions
            if (p.position_manager, p.token_id, p.pool_id)
            == (retained.position_manager, retained.token_id, retained.pool_id)
        )
        for observation, snapshots, states in (
            (
                account.opening[entry.key],
                position.request.position.cash_window.opening,
                position.request.position.opening_pool_state,
            ),
            (
                account.closing[entry.key],
                position.request.position.cash_window.closing,
                position.request.position.closing_pool_state,
            ),
        ):
            proof = next(p for p in observation.positions if p.asset_id == declared.asset_id)
            if proof.snapshots != snapshots or proof.pool_state != states:
                raise ValueError("account and position valuation must share exact retained proofs")
        for name, policy in (
            (declared.currency0_id, position.request.position.currency0_policy),
            (declared.currency1_id, position.request.position.currency1_policy),
        ):
            if entry.asset(name).usd_feed(entry.chain_id) != policy:
                raise ValueError("account and position USD policies differ")
        position_run(
            path(workflow.position.request), db(workflow.position), destination / "report.json"
        )
        account_report = json.loads((output / "account/report.json").read_bytes())
        position_report = json.loads((destination / "report.json").read_bytes())
        for endpoint in ("opening", "closing"):
            frame = next(
                a
                for a in account_report[endpoint]["accounts"]
                if a["account"] == gas.account and a["chain_id"] == gas.closing.ref.chain_id
            )
            asset = next(a for a in frame["assets"] if a["asset_id"] == declared.asset_id)
            valued = position_report["position"][endpoint]
            if asset["price_usd_micros_numerator"] != valued["usd_micros"]:
                raise ValueError("account and position USD valuation differs")
    elif stage == "cycle":
        if workflow.cycle is None or workflow.account is None or workflow.position is None:
            raise ValueError("cycle inputs missing")
        assembly = AssemblyRequest.from_json(path(workflow.cycle.request).read_bytes())
        market = MarketRequest.from_json(path(workflow.market_windows[-1].request).read_bytes())
        position = PositionRequest.from_json(path(workflow.position.request).read_bytes()).request
        gas = position.account_gas
        if (
            assembly.window.model_dump() != market.model_dump()
            or assembly.position.scope.owner != gas.account
            or assembly.position.scope.pool_id != position.position.retained.pool_id
            or assembly.position.scope.token_id != position.position.retained.token_id
            or assembly.position.references != position.position.cash_window.closing
            or assembly.position.scope.position_manager
            != position.position.retained.position_manager
            or assembly.position.scope.chain_id != gas.closing.ref.chain_id
            or gas.closing.ref != assembly.position.references[0].block
            or currency_feed(gas.closing.ref.chain_id, assembly.reference.currency0).policy
            != position.position.currency0_policy
            or currency_feed(gas.closing.ref.chain_id, assembly.reference.currency1).policy
            != position.position.currency1_policy
        ):
            raise ValueError("cycle must bind this market, account close and position")
        assemble(
            path(workflow.cycle.request),
            path(workflow.cycle.declaration),
            db(workflow.cycle),
            destination / "cycle.jsonl",
        )
        write(
            destination / "report.json",
            {"cycle": "cycle.jsonl", "estimates": "operator declarations"},
        )
    elif stage == "shadow":
        if workflow.shadow is None or workflow.cycle is None:
            raise ValueError("shadow inputs missing")
        policy = parse_policy(path(workflow.shadow.policy).read_bytes())
        cycle = LpCycle.from_json((output / "cycle/cycle.jsonl").read_bytes())
        journal = ShadowJournal(
            destination / "journal.db",
            chain_id=policy.pop("chain_id"),
            max_age_seconds=policy.pop("max_age_seconds"),
        )
        store = EvidenceStore(db(workflow.cycle))
        try:
            result = LpShadowService(store, journal, **policy).consume_once(
                cycle, now=workflow.shadow.now
            )
            write(destination / "report.json", {"decision": result.record, "halted": result.halted})
            if result.halted:
                raise ValueError("shadow halted")
        finally:
            store.close()
            journal.close()
    elif stage == "preparation":
        if workflow.preparation is None or workflow.cycle is None or workflow.shadow is None:
            raise ValueError("preparation inputs missing")
        request = PreparationInput.from_json(path(workflow.preparation).read_bytes())
        if request.now != workflow.shadow.now:
            raise ValueError("preparation and shadow clocks must agree")
        cycle = LpCycle.from_json((output / "cycle/cycle.jsonl").read_bytes())
        store = EvidenceStore(db(workflow.cycle))
        journal = ShadowJournal(
            output / "shadow/journal.db",
            chain_id=request.policy.chain_id,
            max_age_seconds=parse_policy(path(workflow.shadow.policy).read_bytes())[
                "max_age_seconds"
            ],
            read_only=True,
        )
        try:
            captures = tuple(
                replay_position_snapshot_evidence(store, ref) for ref in request.positions
            )
            plan = build_lp_simulation_plan(
                store,
                journal,
                cycle,
                PositionQuorum(*captures),
                request.policy,
                request.bounds,
                now=request.now,
            )
            write(destination / "report.json", json.loads(plan.payload))
        finally:
            store.close()
            journal.close()


def _summary(output: Path, records: list[dict[str, object]]) -> dict[str, object]:
    blocked = [r["stage"] for r in records if r["status"] != "completed"]
    coverage = output / "coverage/report.json"
    payload = {
        "schema": "oasis.lp-workflow-report.v1",
        "status": "blocked" if blocked else "completed",
        "engineering_stages_completed": len(records) - len(blocked),
        "stages": records,
        "coverage_passed": coverage.exists()
        and json.loads(coverage.read_bytes())["coverage_passed"],
        "live_blockers": [
            "complete source/token and access/router/depth admission",
            "full cost allocation and fair baseline economic producer",
            "locked multi-week economics and untouched OOS",
            "funded account lifecycle, finality and verified exit",
            "operator release",
        ],
        "net_pnl": None,
        "target_admitted": False,
        "out_of_sample_accepted": False,
        "canary_accepted": False,
        "execution_eligible": False,
        "rpc_requests": 0,
        "http_requests": 0,
        "signatures": 0,
        "broadcasts": 0,
    }
    write(output / "report.json", payload)
    rows = "".join(
        f"<tr><td>{r['stage']}</td><td>{r['status']}</td>"
        f"<td>{html.escape(str(r.get('reason', '')))}</td></tr>"
        for r in records
    )
    (output / "report.html").write_text(
        '<!doctype html><meta charset="utf-8"><title>Oasis workflow</title>'
        "<style>body{font:16px system-ui;max-width:1000px;margin:40px auto;"
        "background:#101722;color:#eee}"
        "td,th{padding:14px;text-align:left;border-bottom:1px solid #354254}a{color:#8ad}</style>"
        "<h1>Oasis 工程运行结果</h1><p>离线证据 → 市场 → 账户费用 → 仓位 → Shadow → 模拟准备</p>"
        f"<p>状态: {payload['status']}; 已完成 {payload['engineering_stages_completed']}/7 步。</p>"
        "<table><tr><th>步骤</th><th>状态</th><th>待补输入 / 原因</th></tr>" + rows + "</table>"
        "<p>准入、OOS、资金 canary 和执行权限仍关闭; 净收益未知。</p>"
        '<p><a href="report.json">完整机器报告</a></p>',
        encoding="utf-8",
    )
    return payload


def run(
    config: Path, output: Path, *, resume: bool = False, root: Path = ROOT
) -> dict[str, object]:
    raw = config.read_bytes()
    workflow = Workflow.from_json(raw)
    # Validate every input and freeze scope before creating any run state.
    pins = {a.path: digest(source(a, root)) for a in assets(workflow)}
    checkpoint_pins = {
        a.checkpoint: digest(root / a.checkpoint)
        for a in assets(workflow)
        if a.checkpoint is not None
    }
    lock = {
        "config_sha256": sha256_hex(raw),
        "inputs": pins,
        "checkpoints": checkpoint_pins,
        "code": code_pins(),
    }
    resolved = output.resolve()
    sources = [source(a, root) for a in assets(workflow)]
    if any(s.is_relative_to(resolved) or resolved.is_relative_to(s.parent) for s in sources):
        raise ValueError("run output overlaps retained inputs")
    if output.is_symlink():
        raise ValueError("symlink output refused")
    if output.exists() and not resume:
        raise ValueError("existing run requires explicit resume")
    output.mkdir(parents=True, exist_ok=True)
    with (output / "owner.lock").open("a+b") as owner:
        fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        frozen = output / "lock.json"
        if frozen.exists():
            if frozen.read_bytes() != canonical_json(lock) + b"\n":
                raise ValueError("run code/config/input changed")
        else:
            if any(p.name != "owner.lock" for p in output.iterdir()):
                raise ValueError("unowned nonempty output refused")
            write(frozen, lock)
            (output / "inputs").mkdir()
        if (output / "seal.json").exists():
            sealed = json.loads((output / "seal.json").read_bytes())
            actual = {
                str(p.relative_to(output))
                for p in output.rglob("*")
                if p.is_file() and p.name not in {"owner.lock", "seal.json"}
            }
            if set(sealed) != actual:
                raise ValueError("completed run file set changed")
            for name, expected in sealed.items():
                if (output / name).is_symlink():
                    raise ValueError("symlink artifact refused")
                if digest(output / name) != expected:
                    raise ValueError("completed run artifact changed")
            return json.loads((output / "report.json").read_bytes())
        # Interrupted stage state is never interpreted as successful or blindly retried.
        if (output / "active.json").exists() or any(
            (output / (stage + ".json")).exists() for stage in STAGES
        ):
            raise ValueError("interrupted stage retained; inspect before a new reviewed run")
        if any((output / "inputs").iterdir()):
            raise ValueError("partial input snapshot retained; no blind restart")
        inputs = {a.path: snapshot(a, root, output) for a in assets(workflow)}
        records = []
        for stage in STAGES:
            record_path = output / (stage + ".json")
            supplied = (
                bool(workflow.market_windows)
                if stage in {"market", "coverage"}
                else getattr(workflow, stage) is not None
            )
            failed_dependencies = [
                d
                for d in DEPENDENCIES[stage]
                if next(r for r in records if r["stage"] == d)["status"] != "completed"
            ]
            record: dict[str, object] = {"stage": stage, "status": "blocked"}
            if not supplied:
                record["reason"] = INPUTS.get(stage, "market replay required")
            elif failed_dependencies:
                record["reason"] = "requires completed: " + ", ".join(failed_dependencies)
            else:
                write(output / "active.json", {"stage": stage})
                try:
                    perform(stage, workflow, output, inputs)
                    record["status"] = "completed"
                except KeyboardInterrupt:
                    record.update(
                        status="stopped", reason="operator stop; partial artifacts retained"
                    )
                    write(record_path, record)
                    _summary(
                        output,
                        [
                            *records,
                            record,
                            *[
                                {"stage": later, "status": "blocked", "reason": "operator stop"}
                                for later in STAGES[STAGES.index(stage) + 1 :]
                            ],
                        ],
                    )
                    raise
                except Exception:  # noqa: BLE001 - hostile retained values never enter logs
                    record.update(
                        status="failed", reason="raw replay or cross-stage binding refused"
                    )
                write(record_path, record)
                (output / "active.json").unlink()
                records.append(record)
                continue
            write(record_path, record)
            records.append(record)
        for asset in assets(workflow):
            source(asset, root)
        result = _summary(output, records)
        write(
            output / "seal.json",
            {
                str(p.relative_to(output)): digest(p)
                for p in sorted(output.rglob("*"))
                if p.is_file() and p.name not in {"owner.lock", "seal.json"}
            },
        )
        return result
