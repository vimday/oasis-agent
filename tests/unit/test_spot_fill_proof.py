"""One retained proof bundle -> /api/v1/spot/fill-proof -> the console's proof panel.

The endpoint runs the report CLI's three per-order checks (cost replay, receipt transfers,
whole-block settlement) over one verified journal snapshot and one read-only evidence
snapshot. Synthetic frozen inputs only: no network, wallet or key.
"""

import ast
import json
import re
import shutil
import sqlite3
import subprocess
import sys
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

import oasis_agent.launch.spot_cost_binding as cost_binding
import oasis_agent.launch.spot_lifecycle as lifecycle
import oasis_agent.launch.spot_settlement_check as settlement_check
import oasis_agent.launch.spot_transfer_check as transfer_check
import scripts.report_spot_shadow as report_cli
from oasis_agent.launch.onchainos_source import BASE_USDC
from oasis_agent.launch.spot_report import build_report
from oasis_agent.server.databases import ReadOnlyError
from oasis_agent.server.read_worker import ReadWorker
from oasis_agent.server.routers_spot import PROOF_SCHEMA
from scripts.check_dashboard import dashboard_sources

from .server_helpers import make_server_config
from .test_block_settlement import log
from .test_server_api import client_for
from .test_spot_agent import NOW
from .test_spot_cost_binding import OTHER_BLOCK, PROVIDERS, World
from .test_spot_report_api import shadow_run
from .test_spot_settlement_capture import Bundle, digest
from .test_spot_settlement_check import FEE, NATIVE, OTHER_TX, Scene
from .test_spot_transfer_check import STRANGER, Order, at

VIEWS = ("cost_replay", "receipt_transfers", "block_settlement")
UNCONFIGURED = {"schema": PROOF_SCHEMA, "configured": False, "execution_eligible": False}
# Console text exactly as spot.js renders it (fullwidth punctuation is the UI text).
OPEN, CLOSE, COMMA, COLON, NONE = "\uff08", "\uff09", "\uff0c", "\uff1a", "\u2013"


def serve(tmp_path: Path, run: Path | None, proof: Path | None = None) -> Any:
    """A server whose spot sources are `run` and `proof`; its own files live apart."""
    root = tmp_path / "server"
    root.mkdir(exist_ok=True)
    config = make_server_config(root)
    databases = replace(config.databases, spot_run=run, spot_proof=proof)
    object.__setattr__(config, "databases", databases)
    return client_for(root, config=config)


def captured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *flags: str, **chain: Any) -> Bundle:
    bundle = Bundle(tmp_path)
    assert bundle.capture(monkeypatch, bundle.files(**chain), *flags) == 0
    return bundle


def fill_proof(tmp_path: Path, bundle: Bundle) -> dict[str, Any]:
    with serve(tmp_path, bundle.world.run, bundle.out) as client:
        response = client.get("/api/v1/spot/fill-proof")
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def test_api_serves_exactly_the_report_cli_views_of_one_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle = captured(tmp_path, monkeypatch, "--with-settlement")
    capsys.readouterr()
    assert bundle.report(monkeypatch) == 0
    cli = json.loads(capsys.readouterr().out)
    files = [bundle.world.run / "journal.db", *sorted(bundle.out.iterdir())]
    before = {path: digest(path) for path in files}
    body = fill_proof(tmp_path, bundle)
    assert {key: body[key] for key in VIEWS} == {key: cli[key] for key in VIEWS}
    assert [body[key]["status"] for key in VIEWS] == ["replayed", "matched", "reconciled"]
    assert (body["schema"], body["configured"], body["mode"]) == (PROOF_SCHEMA, True, "live")
    assert body["execution_eligible"] is False
    assert body["bundle"] == {
        "binding_sha256": digest(bundle.out / "binding.json"),
        "settlement_sha256": digest(bundle.out / "settlement.json"),
    }
    # Read-only: no journal or proof byte changes and no SQLite side file appears.
    assert {path: digest(path) for path in files} == before
    assert sorted(bundle.out.iterdir()) == files[1:]


def test_a_bundle_captured_without_settlement_states_it_was_not_captured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = fill_proof(tmp_path, captured(tmp_path, monkeypatch))
    assert body["block_settlement"] is None and body["bundle"]["settlement_sha256"] is None
    assert [body[key]["status"] for key in VIEWS[:2]] == ["replayed", "matched"]


def test_an_unconfigured_bundle_is_a_state_and_an_unconfigured_run_is_not_found(
    tmp_path: Path,
) -> None:
    run = tmp_path / "run"
    shadow_run(run, [(NOW, (99, 100))])
    with serve(tmp_path, run) as client:
        response = client.get("/api/v1/spot/fill-proof")
        assert response.status_code == 200 and response.json() == UNCONFIGURED
        assert client.post("/api/v1/spot/fill-proof").status_code == 405
    with serve(tmp_path, None) as client:
        assert client.get("/api/v1/spot/fill-proof").status_code == 404


def _replace_with_directory(out: Path) -> None:
    (out / "settlement.json").unlink()
    (out / "settlement.json").mkdir()


def _replace_with_dangling_link(out: Path) -> None:
    (out / "settlement.json").unlink()
    (out / "settlement.json").symlink_to(out / "absent.json")


def _remove(name: str) -> Any:
    return lambda out: (out / name).unlink()


def _write(name: str, data: bytes) -> Any:
    return lambda out: (out / name).write_bytes(data)


MISSING = "spot proof bundle not found or incomplete"
CORRUPT = "spot proof unavailable or corrupt"
DAMAGE = {
    "bundle_missing": (shutil.rmtree, MISSING),
    "binding_missing": (_remove("binding.json"), MISSING),
    "evidence_missing": (_remove("evidence.db"), MISSING),
    "settlement_directory": (_replace_with_directory, MISSING),
    "settlement_dangling_link": (_replace_with_dangling_link, MISSING),
    "binding_not_json": (_write("binding.json", b"{"), CORRUPT),
    "settlement_not_json": (_write("settlement.json", b"\xff"), CORRUPT),
    # Valid JSON exactly one byte over the 1 MiB limit: refused by size, never parsed.
    "binding_too_large": (_write("binding.json", b" " * ((1 << 20) - 1) + b"{}"), CORRUPT),
    "evidence_not_sqlite": (_write("evidence.db", b"not sqlite" * 64), CORRUPT),
}


@pytest.mark.parametrize("damage", sorted(DAMAGE))
def test_a_missing_incomplete_or_unreadable_bundle_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    bundle = captured(tmp_path, monkeypatch, "--with-settlement")
    harm, detail = DAMAGE[damage]
    harm(bundle.out)
    with serve(tmp_path, bundle.world.run, bundle.out) as client:
        response = client.get("/api/v1/spot/fill-proof")
    assert response.status_code == 503 and response.json()["detail"] == detail
    assert str(tmp_path) not in response.text and "Error" not in response.text


def tamper_second_cost_proof(bundle: Bundle) -> None:
    """Rewrite the second provider's cost proof payload: its reference no longer verifies."""
    db = sqlite3.connect(bundle.out / "evidence.db")
    with db:
        changed = db.execute(
            "UPDATE evidence SET payload = ? WHERE kind = 'transaction_cost' AND source = ?",
            (b"{}", bundle.files()[1]["source"]),
        ).rowcount
    db.close()
    assert changed == 1


def test_a_tampered_evidence_record_is_a_refusal_shown_in_every_view(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = captured(tmp_path, monkeypatch, "--with-settlement")
    tamper_second_cost_proof(bundle)
    body = fill_proof(tmp_path, bundle)
    assert (body["cost_replay"]["status"], body["cost_replay"]["reason"]) == (
        "unbound",
        "evidence_invalid_1",
    )
    for key in VIEWS[1:]:
        assert (body[key]["status"], body[key]["reason"]) == (
            "unbound",
            "cost_binding_evidence_invalid_1",
        )
    assert body["cost_replay"]["native_wei"] is None


def test_a_bundle_of_another_journal_never_binds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = captured(tmp_path, monkeypatch, "--with-settlement")
    other = tmp_path / "shadow"
    shadow_run(other, [(NOW, (99, 100))])
    with serve(tmp_path, other, bundle.out) as client:
        body = client.get("/api/v1/spot/fill-proof").json()
    assert body["mode"] == "shadow" and body["cost_replay"]["reason"] == "order_not_matched"
    assert [body[key]["status"] for key in VIEWS] == ["unbound"] * 3
    assert body["block_settlement"]["reason"] == "cost_binding_order_not_matched"


def test_a_corrupt_journal_fails_closed_and_shows_no_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = captured(tmp_path, monkeypatch, "--with-settlement")
    db = sqlite3.connect(bundle.world.run / "journal.db")
    with db:
        raw = db.execute("SELECT payload FROM execution_events WHERE seq=1").fetchone()[0]
        db.execute("UPDATE execution_events SET payload=? WHERE seq=1", (raw + b" ",))
    db.close()
    with serve(tmp_path, bundle.world.run, bundle.out) as client:
        response = client.get("/api/v1/spot/fill-proof")
    assert response.status_code == 503 and "replayed" not in response.text


def test_report_cli_refuses_an_unreadable_evidence_store_with_a_fixed_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle = captured(tmp_path, monkeypatch, "--with-settlement")
    (bundle.out / "evidence.db").write_bytes(b"not sqlite" * 64)
    capsys.readouterr()
    assert bundle.report(monkeypatch) == 1
    out = capsys.readouterr()
    assert out.out == ""
    assert out.err.strip() == "spot shadow report refused: DatabaseError"


# ---------------------------------------------------------------- console panel

PANEL_PROGRAM = r"""
const nodes = new Map(), out = {stats: null, tables: {}, proofStatus: null, danger: null};
globalThis.document = {getElementById: id => {
  if (!nodes.has(id)) nodes.set(id, {
    textContent: '', danger: false,
    classList: {toggle(name, on) { if (name === 'danger') nodes.get(id).danger = on; }},
    replaceChildren() {
      if (id === 'spot-stats') out.stats = null;
      else delete out.tables[id];
    },
  });
  return nodes.get(id);
}};
const esc = value => String(value == null ? '' : value)
  .replace(/[&<>"']/g, c => '&#' + c.charCodeAt(0) + ';');
const SCENE = __SCENE__;
const calls = [];
let round = 0;
async function apiGet(path) {
  calls.push(path);
  const key = Object.keys(SCENE.routes).find(prefix => path.startsWith(prefix));
  const answer = SCENE.routes[key];
  const step = Array.isArray(answer) ? answer[Math.min(round, answer.length - 1)] : answer;
  if (step && step.delay) await new Promise(resolve => setTimeout(resolve, step.delay));
  if (step && step.fail) throw {status: step.fail, code: step.code || ''};
  return step.body;
}
function renderStats(id, stats) { out.stats = stats; }
function renderTable(id, columns, rows, empty) { out.tables[id] = {columns, rows, empty}; }
"""

PANEL_RUN = r"""
(async () => {
  if (SCENE.race) {
    // The first load answers slowly; a second load starts and finishes before it.
    const slow = loadSpot();
    round = 1;
    await loadSpot();
    await slow;
  } else {
    for (round = 0; round < SCENE.rounds; round++) await loadSpot();
  }
  const proof = document.getElementById('spot-proof-status');
  out.proofStatus = proof.textContent;
  out.danger = proof.danger;
  out.status = document.getElementById('spot-status').textContent;
  out.calls = calls;
  console.log(JSON.stringify(out));
})().catch(error => { console.error(error); process.exitCode = 1; });
"""


def panel(
    report: dict[str, Any],
    proof: Any,
    *,
    rounds: int = 1,
    race: bool = False,
    others: Any = None,
) -> dict[str, Any]:
    """Run the real loadSpot() under Node; `proof` and `others` are a body or a step list."""
    node = shutil.which("node")
    assert node, "Node.js is required"
    source = dashboard_sources()["spot.js"]
    head = source.index("\n", source.index("import "))
    empty = {"items": [], "total": 0, "mode": report["mode"], "execution_eligible": False}
    scene = {
        "routes": {
            "/api/v1/spot/report": others if others is not None else {"body": report},
            "/api/v1/spot/decisions": {"body": empty},
            "/api/v1/spot/assessments": {"body": empty},
            "/api/v1/spot/fill-proof": proof,
        },
        "rounds": rounds,
        "race": race,
    }
    program = (
        PANEL_PROGRAM.replace("__SCENE__", json.dumps(scene))
        + source[head + 1 :].replace("export ", "")
        + PANEL_RUN
    )
    result = subprocess.run(
        [node, "-e", program], text=True, capture_output=True, timeout=10, check=False
    )
    assert result.returncode == 0, result.stderr
    rendered: dict[str, Any] = json.loads(result.stdout)
    return rendered


def proof_rows(out: dict[str, Any]) -> list[list[str]]:
    rows: list[list[str]] = out["tables"]["spot-proof"]["rows"]
    return rows


def tag_free(cell: str) -> str:
    return re.sub(r"<[^>]+>", "", cell)


def test_console_explains_a_reconciled_bundle_with_raw_units_and_limits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = captured(tmp_path, monkeypatch, "--with-settlement")
    body = fill_proof(tmp_path, bundle)
    out = panel(build_report(bundle.world.run), {"body": body})
    rows = proof_rows(out)
    assert [tag_free(row[1]) for row in rows] == ["已重放", "与日志一致", "范围内已对账"]
    assert all('class="ok"' in row[1] for row in rows)
    assert f"总费用 {FEE} wei" in rows[0][4]
    assert f"原生币整块净变化 {-FEE} wei" in rows[2][4] and "残差 0" in rows[2][4]
    assert f"由冻结输入重建{COMMA}非提供方实时观测" in out["proofStatus"]
    assert "两个来源的独立性未确立" in out["proofStatus"]
    assert "不授予执行权限" in out["proofStatus"] and out["danger"] is False
    text = json.dumps(out, ensure_ascii=False)
    assert "undefined" not in text and "NaN" not in text and "[object Object]" not in text


def test_console_shows_a_residual_as_a_failure_with_its_reason_and_numbers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = captured(tmp_path, monkeypatch, "--with-settlement", native=(NATIVE, NATIVE - FEE - 1))
    out = panel(build_report(bundle.world.run), {"body": fill_proof(tmp_path, bundle)})
    settled = proof_rows(out)[2]
    assert tag_free(settled[1]) == "有残差" and 'class="bad"' in settled[1]
    reason = "native_balance_not_explained_by_receipt_fee"
    assert settled[2] == f"原生币余额变化不等于收据费用{OPEN}{reason}{CLOSE}"
    assert f"原生币整块净变化 {-FEE - 1} wei" in settled[4] and "残差 -1" in settled[4]
    assert out["danger"] is True


def test_console_states_an_uncaptured_settlement_and_an_unconfigured_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = captured(tmp_path, monkeypatch)
    report = build_report(bundle.world.run)
    out = panel(report, {"body": fill_proof(tmp_path, bundle)})
    assert tag_free(proof_rows(out)[2][1]) == "未提供"
    assert "settlement.json" in proof_rows(out)[2][2] and out["danger"] is False
    out = panel(report, {"body": UNCONFIGURED})
    assert proof_rows(out) == [] and "databases.spot_proof" in out["tables"]["spot-proof"]["empty"]
    assert out["danger"] is False and "databases.spot_proof" in out["proofStatus"]


def test_console_labels_scoped_refusals_and_keeps_unknown_codes_visible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = captured(tmp_path, monkeypatch, "--with-settlement")
    tamper_second_cost_proof(bundle)
    body = fill_proof(tmp_path, bundle)  # the server's own refusal views, unedited
    body["block_settlement"] |= {"status": "brand_new", "reason": "constructor"}
    out = panel(build_report(bundle.world.run), {"body": body})
    rows = proof_rows(out)
    second = "第 2 份证明无法重放"
    assert rows[0][2] == f"{second}{OPEN}evidence_invalid_1{CLOSE}"
    scoped = f"{OPEN}cost_binding_evidence_invalid_1{CLOSE}"
    assert rows[1][2] == f"成本证明{COLON}{second}{scoped}"
    # An unknown status is a failure shown by its code; a prototype name is not a label.
    assert tag_free(rows[2][1]) == "brand_new" and 'class="bad"' in rows[2][1]
    assert rows[2][2] == "constructor"
    # A refusal reached no outcome: it states no providers, scope or values.
    assert all(row[3] == row[4] == NONE for row in rows)
    assert out["proofStatus"].startswith(f"证据记录缺失、过期或被修改{COLON}")
    assert out["danger"] is True


def test_console_escapes_every_proof_string(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = captured(tmp_path, monkeypatch, "--with-settlement")
    body = fill_proof(tmp_path, bundle)
    body["cost_replay"]["reason"] = "<img src=x onerror=alert(1)>"
    body["block_settlement"]["status"] = "<b>x</b>"
    rows = proof_rows(panel(build_report(bundle.world.run), {"body": body}))
    assert "<img" not in rows[0][2] and "<b>" not in rows[2][1]


@pytest.mark.parametrize(
    "change",
    [
        {"execution_eligible": True},
        {"schema": "oasis.spot-fill-proof.v0"},
        {"configured": "yes"},
        {"mode": "shadow"},
        {"cost_replay": None},
        {"receipt_transfers": {"status": "matched", "execution_eligible": True}},
        {"block_settlement": "reconciled"},
    ],
)
def test_console_refuses_a_proof_response_outside_the_read_only_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: dict[str, Any]
) -> None:
    bundle = captured(tmp_path, monkeypatch, "--with-settlement")
    body = fill_proof(tmp_path, bundle) | change
    out = panel(build_report(bundle.world.run), {"body": body})
    assert "spot-proof" not in out["tables"] and out["danger"] is True
    assert "不符合只读约定" in out["proofStatus"]
    assert out["stats"] is not None  # the journal panels are a separate source


def test_a_failed_proof_refresh_clears_only_the_proof_panel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = captured(tmp_path, monkeypatch, "--with-settlement")
    body = fill_proof(tmp_path, bundle)
    steps = [{"body": body}, {"fail": 503, "code": "corrupt"}]
    out = panel(build_report(bundle.world.run), steps, rounds=2)
    assert "spot-proof" not in out["tables"] and out["danger"] is True
    assert "证据包或日志无法读取或校验失败" in out["proofStatus"]
    assert out["stats"] is not None and len(out["tables"]) == 4


def test_a_failed_journal_refresh_clears_the_proof_panel_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = captured(tmp_path, monkeypatch, "--with-settlement")
    report = build_report(bundle.world.run)
    out = panel(
        report,
        {"body": fill_proof(tmp_path, bundle)},
        rounds=2,
        others=[{"body": report}, {"fail": 503}],
    )
    assert out["stats"] is None and out["tables"] == {}
    assert "现货日志未配置或不可用" in out["proofStatus"] and out["danger"] is True


def test_a_slower_earlier_load_never_overwrites_a_newer_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = captured(tmp_path, monkeypatch, "--with-settlement")
    body = fill_proof(tmp_path, bundle)
    stale = json.loads(json.dumps(body))
    stale["block_settlement"] |= {"status": "residual", "reason": "stale_answer"}
    out = panel(
        build_report(bundle.world.run), [{"body": stale, "delay": 50}, {"body": body}], race=True
    )
    assert [tag_free(row[1]) for row in proof_rows(out)] == ["已重放", "与日志一致", "范围内已对账"]
    assert out["calls"].count("/api/v1/spot/fill-proof") == 2


def test_a_slower_earlier_failure_never_clears_a_newer_render(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = captured(tmp_path, monkeypatch, "--with-settlement")
    report = build_report(bundle.world.run)
    out = panel(
        report,
        {"body": fill_proof(tmp_path, bundle)},
        race=True,
        others=[{"fail": 503, "delay": 50}, {"body": report}],
    )
    assert out["stats"] is not None and len(out["tables"]) == 5
    assert "不可用" not in out["status"]
    assert out["danger"] is False and "不授予执行权限" in out["proofStatus"]


# ---------------------------------------------------------------- label coverage


def _js_keys(source: str, name: str) -> set[str]:
    start = source.index(f"const {name} = {{")
    body = source[start : source.index("\n};", start)]
    return set(re.findall(r"^\s*([a-z0-9_]+):", body, re.MULTILINE)) | set(
        re.findall(r"[{,]\s*([a-z0-9_]+):\s*\[", body)
    )


def _module_strings(module: Any) -> set[str]:
    tree = ast.parse(Path(module.__file__).read_text())
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }


def _refusals(module: Any, *factories: str) -> set[str]:
    """Every literal reason passed to a refusal in `module`, by call shape."""
    tree = ast.parse(Path(module.__file__).read_text())
    out: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id not in factories or not node.args:
            continue
        # The reason is the last argument; a computed one ("cost_binding_" + ...) is
        # labelled by its prefix, never mistaken for the status before it.
        reason = node.args[-1]
        if isinstance(reason, ast.Constant) and isinstance(reason.value, str):
            out.add(reason.value)
    return out


def _outcome_reasons(module: Any) -> set[str]:
    """Reasons assigned with a status: `status, reason = "x", "y"` tuples."""
    tree = ast.parse(Path(module.__file__).read_text())
    out: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Tuple):
            continue
        if len(node.value.elts) != 2:
            continue
        status, reason = node.value.elts
        if (
            isinstance(status, ast.Constant)
            and isinstance(reason, ast.Constant)
            and isinstance(reason.value, str)
        ):
            out.add(reason.value)
    return out


def test_every_proof_reason_and_status_has_a_console_label() -> None:
    source = dashboard_sources()["spot.js"]
    labels = _js_keys(source, "PROOF_REASON")
    reasons = (
        _refusals(cost_binding, "_unbound", "_conflict")
        | _refusals(transfer_check, "_Refused")
        | _refusals(settlement_check, "_Refused")
        | _outcome_reasons(transfer_check)
        | _outcome_reasons(settlement_check)
        | {"receipt_unreadable", "settlement_unreadable"}
    )
    # Prefixed reasons are labelled by scope; evidence_invalid_<n> is labelled by pattern.
    dynamic = {"evidence_invalid_"}
    assert reasons, "no refusal reasons found"
    assert sorted(reasons - labels - dynamic) == []
    statuses = {"replayed", "matched", "reconciled", "unbound", "unverified", "conflict"}
    statuses |= {"journal_mismatch", "residual"}
    assert statuses <= _module_strings(cost_binding) | _module_strings(settlement_check) | (
        _module_strings(transfer_check)
    )
    assert statuses <= _js_keys(source, "PROOF_STATUS")
    endings = ("_not_provider_observation", "_origin_unknown")
    basis = {
        value
        for value in _module_strings(cost_binding)
        if value.endswith(endings) or value == "not_replayed"
    }
    assert basis and basis <= _js_keys(source, "PROOF_BASIS")


def test_every_lifecycle_blocking_reason_has_a_console_label() -> None:
    source = dashboard_sources()["spot.js"]
    labels = _js_keys(source, "BLOCK_LABEL")
    tree = ast.parse(Path(lifecycle.__file__).read_text())
    reasons: set[str] = set()
    for node in ast.walk(tree):
        # problems.add("x"), defects.add("x"), blocking.add("x"), {"x"} problem sets
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "add"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in {"problems", "defects", "blocking"}
        ):
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    reasons.add(arg.value)
                if isinstance(arg, ast.IfExp):
                    reasons |= {
                        e.value
                        for e in (arg.body, arg.orelse)
                        if isinstance(e, ast.Constant) and isinstance(e.value, str)
                    }
        if isinstance(node, ast.Set):
            reasons |= {
                e.value
                for e in node.elts
                if isinstance(e, ast.Constant)
                and isinstance(e.value, str)
                and e.value.endswith(("_invalid", "_mismatch"))
            }
    # f"{kind}_without_intent" for every lifecycle and booking kind.
    reasons |= {
        f"{kind}_without_intent"
        for kind in ("live_fill", "live_unresolved", *lifecycle.BOOKING_KIND.values())
    }
    assert {"fill_amount_mismatch", "spot_live_execution_unreleased"} <= reasons
    assert sorted(reasons - labels) == []


# ---------------------------------------------------------------- CLI / API / UI parity
#
# One synthetic frozen bundle per case, published by the real capture CLI. The report CLI
# prints the three views, the real server serves the same bundle over HTTP, and the real
# core.js client parses those exact bytes for spot.js to render. All three must agree.

BIG_UNITS = 10**30 + 7  # token base units: far beyond Number.MAX_SAFE_INTEGER (2**53 - 1)
# A transactionIndex beyond 2**53 that a float cannot hold (synthetic, not a real block
# size): only an exact integer parse renders it, and a rounded value would differ.
BIG_POSITION = 2**60 + 1
CONSOLE_READS = (
    "/api/v1/spot/report",
    "/api/v1/spot/decisions?limit=50",
    "/api/v1/spot/assessments?limit=30&action=flip",
    "/api/v1/spot/fill-proof",
)
STATUS_LABEL = {
    "replayed": "已重放",
    "matched": "与日志一致",
    "reconciled": "范围内已对账",
    "unbound": "未绑定",
    "unverified": "未验证",
    "conflict": "冲突",
    "journal_mismatch": "与日志不符",
    "residual": "有残差",
}
NATIVE_NET_ONLY = f"原生币只比较整块净余额{COMMA}无逐交易追踪"

CONSOLE_HARNESS = r"""
const nodes = new Map();
class Element {
  constructor(tag) {
    this.tag = tag; this.children = []; this.text = ''; this.markup = '';
    this.className = ''; this.danger = false;
  }
  get textContent() { return this.text; }
  set textContent(value) { this.text = String(value); this.children = []; }
  get innerHTML() { return this.markup; }
  set innerHTML(value) { this.markup = String(value); }
  get classList() {
    const element = this;
    return {toggle(name, on) { if (name === 'danger') element.danger = Boolean(on); }};
  }
  appendChild(node) { this.children.push(node); return node; }
  append(...more) { this.children.push(...more); }
  replaceChildren(...more) { this.children = more; }
}
globalThis.document = {
  getElementById(id) {
    if (!nodes.has(id)) nodes.set(id, new Element('#' + id));
    return nodes.get(id);
  },
  createElement: tag => new Element(tag),
  createDocumentFragment: () => new Element('#fragment'),
};
// Read retries and toasts sleep; run those timers at once so a 503 settles promptly.
globalThis.setTimeout = fn => { Promise.resolve().then(fn); return 0; };
globalThis.clearTimeout = () => {};
const RESPONSES = __RESPONSES__;
globalThis.fetch = async path => {
  const answer = RESPONSES[path];
  if (!answer) throw new Error('unexpected request ' + path);
  return {status: answer.status, ok: answer.status >= 200 && answer.status < 300,
    text: async () => answer.body, json: async () => JSON.parse(answer.body)};
};
const {loadSpot} = await import('./spot.js');
await loadSpot();
const table = id => {
  const fragment = nodes.get(id)?.children[0];
  if (!fragment) return null;
  const rows = fragment.children[1].children.map(tr => tr.children);
  if (rows.length === 1 && rows[0].length === 1 && rows[0][0].className === 'empty') {
    return {rows: [], empty: rows[0][0].textContent};
  }
  return {rows: rows.map(cells => cells.map(td => td.innerHTML)), empty: null};
};
const proof = nodes.get('spot-proof-status');
console.log(JSON.stringify({proof: table('spot-proof'), status: proof.textContent,
  danger: proof.danger, spot: nodes.get('spot-status').textContent}));
"""


def render_console(tmp_path: Path, client: Any) -> dict[str, Any]:
    """Render the spot tab under Node from the exact bytes the real server returned."""
    node = shutil.which("node")
    assert node, "Node.js is required"
    work = tmp_path / "console"
    work.mkdir(exist_ok=True)
    sources = dashboard_sources()
    for name in ("core.js", "spot.js"):
        (work / name).write_text(sources[name])
    (work / "package.json").write_text('{"type":"module"}')
    responses = {}
    for path in CONSOLE_READS:
        response = client.get(path)
        responses[path] = {"status": response.status_code, "body": response.text}
    program = CONSOLE_HARNESS.replace("__RESPONSES__", json.dumps(responses))
    result = subprocess.run(
        [node, "--input-type=module"],
        input=program,
        cwd=work,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    rendered: dict[str, Any] = json.loads(result.stdout)
    rendered["responses"] = responses
    return rendered


def _word(amount: int) -> str:
    return "0x" + amount.to_bytes(32, "big").hex()


def _rewrite(path: Path, change: Callable[[dict[str, Any]], dict[str, Any]]) -> None:
    path.write_text(json.dumps(change(json.loads(path.read_text()))))


class Parity:
    """A matched buy of BIG_UNITS at BIG_POSITION, captured from two frozen files."""

    def __init__(self, tmp_path: Path) -> None:
        self.scene = Scene(tmp_path, Order(World(tmp_path, fill_out=BIG_UNITS), "buy"))
        self.bundle = Bundle(tmp_path, self.scene)
        self.logs = at(self.scene.receipt_logs(), hex(BIG_POSITION), hex(BIG_POSITION))
        token = self.scene.order.token
        self.chain: dict[str, Any] = {
            "receipt_logs": self.logs,
            "position": hex(BIG_POSITION),
            "logs": self.logs,
            "supply": {BASE_USDC: (10**15, 10**15), token: (10**33, 10**33)},
        }

    def capture(
        self,
        monkeypatch: pytest.MonkeyPatch,
        files: list[dict[str, Any]] | None = None,
        **change: Any,
    ) -> None:
        retained = self.bundle.files(**(self.chain | change)) if files is None else files
        assert self.bundle.capture(monkeypatch, retained, "--with-settlement") == 0


def _reconciled(p: Parity, monkeypatch: pytest.MonkeyPatch) -> None:
    p.capture(monkeypatch)


def _residual(p: Parity, monkeypatch: pytest.MonkeyPatch) -> None:
    p.capture(monkeypatch, native=(NATIVE, NATIVE - FEE - 1))  # one wei more left the wallet


def _other_transaction(p: Parity, monkeypatch: pytest.MonkeyPatch) -> None:
    wallet = p.scene.wallet
    gift = log(BASE_USDC, STRANGER, wallet, 9, 7, tx=OTHER_TX, pos=BIG_POSITION + 1)
    balances = p.scene.chain().balances
    before, after = balances[BASE_USDC]
    moved = balances | {BASE_USDC: (before, after + 9)}
    p.capture(monkeypatch, logs=[*p.logs, gift], balances=moved)


def _provider_conflict(p: Parity, monkeypatch: pytest.MonkeyPatch) -> None:
    # Same fee, same net, but the second provider's receipt lists the token leg as two rows.
    files = p.bundle.files(**p.chain)
    usdc, token = p.logs
    units = p.scene.units
    split = [
        usdc,
        token | {"data": _word(units - 1)},
        token | {"logIndex": "0x2", "data": _word(1)},
    ]
    (receipt,) = [r for r in files[1]["responses"] if r["method"] == "eth_getTransactionReceipt"]
    receipt["result"] = receipt["result"] | {"logs": split}
    p.capture(monkeypatch, files)


def _wrong_intent(p: Parity, monkeypatch: pytest.MonkeyPatch) -> None:
    p.capture(monkeypatch)
    _rewrite(p.bundle.out / "binding.json", lambda b: b | {"intent_id": "another-intent"})


def _wrong_block(p: Parity, monkeypatch: pytest.MonkeyPatch) -> None:
    p.capture(monkeypatch)
    _rewrite(
        p.bundle.out / "binding.json",
        lambda b: b | {"header": b["header"] | {"block_hash": OTHER_BLOCK}},
    )


def _missing_cost_record(p: Parity, monkeypatch: pytest.MonkeyPatch) -> None:
    p.capture(monkeypatch)
    db = sqlite3.connect(p.bundle.out / "evidence.db")
    with db:
        deleted = db.execute(
            "DELETE FROM evidence WHERE kind = 'transaction_cost' AND source = ?",
            (PROVIDERS[1],),
        ).rowcount
    db.close()
    assert deleted == 1


def _stale_settlement_reference(p: Parity, monkeypatch: pytest.MonkeyPatch) -> None:
    p.capture(monkeypatch)

    def stale(declared: dict[str, Any]) -> dict[str, Any]:
        first, second = declared["settlements"]
        return declared | {"settlements": [first, second | {"content_hash": "0" * 64}]}

    _rewrite(p.bundle.out / "settlement.json", stale)


RECEIPTS_OK = (("replayed", None), ("matched", None))
MISBOUND_GAP = f"证据包与这笔成交不匹配{OPEN}错绑{CLOSE}"
EVIDENCE_GAP = "证据记录缺失、过期或被修改"
PARITY: dict[str, tuple[Any, tuple[tuple[str, str | None], ...], str]] = {
    "reconciled": (_reconciled, (*RECEIPTS_OK, ("reconciled", None)), "范围内已对账"),
    "residual": (
        _residual,
        (*RECEIPTS_OK, ("residual", "native_balance_not_explained_by_receipt_fee")),
        "有未解释的残差",
    ),
    "other_transaction_in_block": (
        _other_transaction,
        (*RECEIPTS_OK, ("unverified", "other_transactions_moved_wallet_in_block")),
        "证据不足以核对",
    ),
    "provider_conflict": (
        _provider_conflict,
        (
            ("replayed", None),
            ("conflict", "providers_disagree_on_transfer_logs"),
            ("conflict", "receipt_transfers_providers_disagree_on_transfer_logs"),
        ),
        "证据之间矛盾",
    ),
    "wrong_intent": (
        _wrong_intent,
        (
            ("unbound", "order_not_matched"),
            ("unbound", "cost_binding_order_not_matched"),
            ("unbound", "cost_binding_order_not_matched"),
        ),
        MISBOUND_GAP,
    ),
    "wrong_block": (
        _wrong_block,
        (
            ("unbound", "block_mismatch"),
            ("unbound", "cost_binding_block_mismatch"),
            ("unbound", "cost_binding_block_mismatch"),
        ),
        MISBOUND_GAP,
    ),
    "missing_cost_record": (
        _missing_cost_record,
        (
            ("unbound", "evidence_invalid_1"),
            ("unbound", "cost_binding_evidence_invalid_1"),
            ("unbound", "cost_binding_evidence_invalid_1"),
        ),
        EVIDENCE_GAP,
    ),
    "stale_settlement_reference": (
        _stale_settlement_reference,
        (*RECEIPTS_OK, ("unbound", "settlement_evidence_invalid")),
        EVIDENCE_GAP,
    ),
}


def raw_values(key: str, view: dict[str, Any]) -> list[str]:
    """Raw integers the console must show verbatim for one CLI view (none for a refusal)."""
    if key == "cost_replay" and view.get("native_wei"):
        w = view["native_wei"]
        return [f"总费用 {w['total']} wei", f"执行 {w['execution']}", f"L1 {w['l1']}"]
    if key == "receipt_transfers" and view.get("assets"):
        a = view["assets"]
        return [
            f"收据 USDC 净额 {a['usdc']['net']}",
            f"收据代币净额 {a['token']['net']}",
            f"交易位置 {view['transaction_index']}",
        ]
    if key == "block_settlement" and view.get("tokens"):
        n, t = view["native_wei"], view["tokens"]
        return [
            f"原生币整块净变化 {n['delta']} wei",
            f"收据费 {n['receipt_fee_total']}",
            f"残差 {n['residual']}",
            f"USDC 余额变化 {t['usdc']['balance_delta']}",
            f"代币余额变化 {t['token']['balance_delta']}",
        ]
    return []


def snapshot(*directories: Path) -> dict[Path, str]:
    """Every file in the journal and bundle directories, by content digest."""
    return {p: digest(p) for d in directories for p in sorted(d.iterdir()) if p.is_file()}


@pytest.mark.parametrize("case", sorted(PARITY))
def test_cli_api_and_console_agree_on_one_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    case: str,
) -> None:
    build, expected, gap = PARITY[case]
    parity = Parity(tmp_path)
    build(parity, monkeypatch)
    bundle = parity.bundle
    before = snapshot(bundle.world.run, bundle.out)
    capsys.readouterr()
    assert bundle.report(monkeypatch) == 0
    cli = json.loads(capsys.readouterr().out)
    # A replayed cost view states no reason at all; every other view states one or None.
    assert tuple((cli[key]["status"], cli[key].get("reason")) for key in VIEWS) == expected
    with serve(tmp_path, bundle.world.run, bundle.out) as client:
        out = render_console(tmp_path, client)
    body = json.loads(out["responses"]["/api/v1/spot/fill-proof"]["body"])
    assert {key: body[key] for key in VIEWS} == {key: cli[key] for key in VIEWS}
    rows = out["proof"]["rows"]
    for row, key in zip(rows, VIEWS, strict=True):
        view = cli[key]
        assert tag_free(row[1]) == STATUS_LABEL[view["status"]]
        reason = view.get("reason")
        assert row[2] == NONE if reason is None else row[2].endswith(f"{OPEN}{reason}{CLOSE}")
        values = raw_values(key, view)
        assert all(value in row[4] for value in values) if values else row[4] == NONE
        if "providers" in view:
            assert all(provider in row[3] for provider in view["providers"])
        else:
            assert row[3] == NONE
    if "providers" in cli["block_settlement"]:
        # The scope names this block, wallet and both tokens, and native is a net only.
        assert NATIVE_NET_ONLY in rows[2][3]
        assert cli["block_settlement"]["block"]["block_hash"] in rows[2][3]
    assert out["status"].startswith(gap) and out["danger"] is (case != "reconciled")
    if case != "reconciled":
        assert "范围内已对账" not in out["status"] and "范围内已对账" not in str(rows)
    assert "不可用" not in out["spot"]  # the journal panels are a separate source
    # Evidence store, journal and bundle files are only read.
    assert snapshot(bundle.world.run, bundle.out) == before


def test_integers_beyond_two_to_the_53_render_exactly_through_the_real_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parity = Parity(tmp_path)
    _reconciled(parity, monkeypatch)
    bundle = parity.bundle
    with serve(tmp_path, bundle.world.run, bundle.out) as client:
        out = render_console(tmp_path, client)
    raw = out["responses"]["/api/v1/spot/fill-proof"]["body"]
    # The server sends the position as a bare JSON integer a float would round.
    assert f'"transaction_index":{BIG_POSITION}' in raw
    assert float(BIG_POSITION) != BIG_POSITION
    rows = out["proof"]["rows"]
    assert f"交易位置 {BIG_POSITION}" in rows[1][4]
    assert f"收据代币净额 {BIG_UNITS}" in rows[1][4]
    assert f"代币余额变化 {BIG_UNITS}" in rows[2][4]
    assert str(int(float(BIG_POSITION))) not in str(rows)


async def _busy(self: ReadWorker, operation: Any) -> Any:
    raise ReadOnlyError("observation reader unavailable or busy", code="busy")


@pytest.mark.parametrize(
    ("harm", "code", "text"),
    [
        (_remove("binding.json"), "missing", "证据包缺失或不完整"),
        (_write("binding.json", b"{"), "corrupt", "证据包或日志无法读取或校验失败"),
        (None, "busy", "服务器读取队列已满"),
    ],
)
def test_console_tells_missing_corrupt_and_busy_apart_over_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, harm: Any, code: str, text: str
) -> None:
    bundle = captured(tmp_path, monkeypatch, "--with-settlement")
    if harm is not None:
        harm(bundle.out)
    with serve(tmp_path, bundle.world.run, bundle.out) as client:
        if harm is None:
            # The replay runs on the bounded reader: a full reader refuses it.
            monkeypatch.setattr(ReadWorker, "run", _busy)
        out = render_console(tmp_path, client)
    proof = json.loads(out["responses"]["/api/v1/spot/fill-proof"]["body"])
    assert out["responses"]["/api/v1/spot/fill-proof"]["status"] == 503
    assert proof["code"] == code
    assert out["proof"] is None and out["danger"] is True and text in out["status"]
    assert "不可用" not in out["spot"]  # the report still renders without the proof


def test_console_without_a_bundle_keeps_the_default_report_over_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = captured(tmp_path, monkeypatch, "--with-settlement")
    with serve(tmp_path, bundle.world.run) as client:
        out = render_console(tmp_path, client)
    assert json.loads(out["responses"]["/api/v1/spot/fill-proof"]["body"]) == UNCONFIGURED
    assert out["proof"]["rows"] == [] and "databases.spot_proof" in out["proof"]["empty"]
    assert out["danger"] is False and "不可用" not in out["spot"]


def _js_set(source: str, name: str) -> set[str]:
    start = source.index(f"const {name} = new Set([")
    return set(re.findall(r'"([a-z0-9_]+)"', source[start : source.index("]);", start)]))


def _unbound_reasons() -> set[str]:
    """Every literal reason refused with status `unbound` by the three checks."""
    out = _refusals(cost_binding, "_unbound")
    for module in (transfer_check, settlement_check):
        for node in ast.walk(ast.parse(Path(module.__file__).read_text())):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
                continue
            if node.func.id != "_Refused" or len(node.args) != 2:
                continue
            status, reason = node.args
            if (
                isinstance(status, ast.Constant)
                and status.value == "unbound"
                and isinstance(reason, ast.Constant)
                and isinstance(reason.value, str)
            ):
                out.add(reason.value)
    return out


def test_every_unbound_reason_names_its_kind_of_gap() -> None:
    source = dashboard_sources()["spot.js"]
    kinds = [_js_set(source, name) for name in ("MISBOUND", "MALFORMED", "JOURNAL_SIDE")]
    assert not (kinds[0] & kinds[1] or kinds[0] & kinds[2] or kinds[1] & kinds[2])
    reasons = _unbound_reasons()
    assert {"order_not_matched", "settlement_evidence_invalid"} <= reasons
    # evidence_invalid_<n> and settlement_evidence_invalid are named by the evidence rule.
    assert sorted(reasons - set().union(*kinds) - {"settlement_evidence_invalid"}) == []


# ---------------------------------------------------------------- settlement file presence
#
# Only an absent settlement.json is "not provided". A present file is a declared binding:
# whatever it parses to (null, an array, an empty object, a partial object) is checked by
# the report's own settlement check, and unparseable bytes keep the existing refusal.

SCHEMA_ONLY = json.dumps({"schema": "oasis.spot-settlement-binding.v1"}).encode()
SETTLEMENT_FILES: dict[str, bytes | None] = {
    "absent": None,
    "json_null": b"null",
    "json_array": b"[]",
    "json_empty_object": b"{}",
    "json_string": b'"settlement"',
    "json_number": b"0",
    "json_schema_only": SCHEMA_ONLY,
}
UNPARSEABLE = {
    "syntax_error": (b"{", "JSONDecodeError"),
    "not_utf8": (b"\xff", "UnicodeDecodeError"),
}


def report_views(
    bundle: Bundle,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *,
    settlement: bool,
) -> tuple[int, Any, str]:
    """The report CLI over this bundle, naming settlement.json only when asked."""
    argv = ["report", "--run", str(bundle.world.run)]
    argv += ["--cost-binding", str(bundle.out / "binding.json")]
    argv += ["--evidence", str(bundle.out / "evidence.db")]
    if settlement:
        argv += ["--settlement-binding", str(bundle.out / "settlement.json")]
    capsys.readouterr()
    monkeypatch.setattr(sys, "argv", argv)
    code = report_cli.main()
    out = capsys.readouterr()
    return code, json.loads(out.out) if out.out.strip() else None, out.err.strip()


@pytest.mark.parametrize("case", sorted(SETTLEMENT_FILES))
def test_only_an_absent_settlement_file_is_not_provided(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    case: str,
) -> None:
    bundle = captured(tmp_path, monkeypatch, "--with-settlement")
    content = SETTLEMENT_FILES[case]
    settlement = bundle.out / "settlement.json"
    if content is None:
        settlement.unlink()
    else:
        settlement.write_bytes(content)
    before = snapshot(bundle.world.run, bundle.out)
    code, cli, err = report_views(bundle, monkeypatch, capsys, settlement=content is not None)
    assert (code, err) == (0, "")
    with serve(tmp_path, bundle.world.run, bundle.out) as client:
        out = render_console(tmp_path, client)
    answer = out["responses"]["/api/v1/spot/fill-proof"]
    assert answer["status"] == 200
    body = json.loads(answer["body"])
    assert {key: body[key] for key in VIEWS[:2]} == {key: cli[key] for key in VIEWS[:2]}
    rows = out["proof"]["rows"]
    if content is None:
        # Absent: the CLI was not asked, the API states null, the console says not provided.
        assert "block_settlement" not in cli
        assert body["block_settlement"] is None and body["bundle"]["settlement_sha256"] is None
        assert tag_free(rows[2][1]) == "未提供" and out["danger"] is False
        assert out["status"].startswith("收据核对通过")
    else:
        # Present: the same binding check as the CLI, never "not provided".
        view = cli["block_settlement"]
        assert (view["status"], view["reason"]) == ("unbound", "settlement_binding_malformed")
        assert body["block_settlement"] == view
        assert body["bundle"]["settlement_sha256"] == digest(settlement)
        assert tag_free(rows[2][1]) == "未绑定" and 'class="warn"' in rows[2][1]
        assert rows[2][2].endswith(f"{OPEN}settlement_binding_malformed{CLOSE}")
        assert out["status"].startswith(f"证据包声明无效{COLON}") and out["danger"] is True
        assert "未提供" not in json.dumps(out["proof"], ensure_ascii=False)
    assert snapshot(bundle.world.run, bundle.out) == before


@pytest.mark.parametrize("case", sorted(UNPARSEABLE))
def test_an_unparseable_settlement_file_keeps_the_existing_refusal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    case: str,
) -> None:
    bundle = captured(tmp_path, monkeypatch, "--with-settlement")
    content, error = UNPARSEABLE[case]
    (bundle.out / "settlement.json").write_bytes(content)
    before = snapshot(bundle.world.run, bundle.out)
    code, cli, err = report_views(bundle, monkeypatch, capsys, settlement=True)
    assert (code, cli, err) == (1, None, f"spot shadow report refused: {error}")
    with serve(tmp_path, bundle.world.run, bundle.out) as client:
        out = render_console(tmp_path, client)
    answer = out["responses"]["/api/v1/spot/fill-proof"]
    assert answer["status"] == 503 and json.loads(answer["body"])["code"] == "corrupt"
    assert out["proof"] is None and "证据包或日志无法读取或校验失败" in out["status"]
    assert snapshot(bundle.world.run, bundle.out) == before


def test_a_valid_settlement_file_still_reconciles_on_all_three_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle = captured(tmp_path, monkeypatch, "--with-settlement")
    before = snapshot(bundle.world.run, bundle.out)
    code, cli, _ = report_views(bundle, monkeypatch, capsys, settlement=True)
    assert code == 0 and cli["block_settlement"]["status"] == "reconciled"
    with serve(tmp_path, bundle.world.run, bundle.out) as client:
        out = render_console(tmp_path, client)
    body = json.loads(out["responses"]["/api/v1/spot/fill-proof"]["body"])
    assert {key: body[key] for key in VIEWS} == {key: cli[key] for key in VIEWS}
    assert tag_free(out["proof"]["rows"][2][1]) == "范围内已对账" and out["danger"] is False
    assert snapshot(bundle.world.run, bundle.out) == before


@pytest.mark.parametrize("settlement", ["read_but_null", "view_without_a_read"])
def test_console_refuses_a_settlement_view_that_contradicts_its_file_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, settlement: str
) -> None:
    bundle = captured(tmp_path, monkeypatch, "--with-settlement")
    body = fill_proof(tmp_path, bundle)
    if settlement == "read_but_null":
        # The server read a file (digest set) but sent no view: never "not provided".
        body["block_settlement"] = None
    else:
        body["bundle"] = body["bundle"] | {"settlement_sha256": None}
    out = panel(build_report(bundle.world.run), {"body": body})
    assert "spot-proof" not in out["tables"] and out["danger"] is True
    assert "不符合只读约定" in out["proofStatus"]
