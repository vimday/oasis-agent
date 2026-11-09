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
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

import oasis_agent.launch.spot_cost_binding as cost_binding
import oasis_agent.launch.spot_lifecycle as lifecycle
import oasis_agent.launch.spot_settlement_check as settlement_check
import oasis_agent.launch.spot_transfer_check as transfer_check
from oasis_agent.launch.spot_report import build_report
from oasis_agent.server.routers_spot import PROOF_SCHEMA
from scripts.check_dashboard import dashboard_sources

from .server_helpers import make_server_config
from .test_server_api import client_for
from .test_spot_agent import NOW
from .test_spot_report_api import shadow_run
from .test_spot_settlement_capture import Bundle, digest
from .test_spot_settlement_check import FEE, NATIVE

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


def test_a_tampered_evidence_record_is_a_refusal_shown_in_every_view(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = captured(tmp_path, monkeypatch, "--with-settlement")
    db = sqlite3.connect(bundle.out / "evidence.db")
    with db:
        changed = db.execute(
            "UPDATE evidence SET payload = ? WHERE kind = 'transaction_cost' AND source = ?",
            (b"{}", bundle.files()[1]["source"]),
        ).rowcount
    db.close()
    assert changed == 1
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
  if (step && step.fail) throw {status: step.fail};
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
    assert [tag_free(row[1]) for row in rows] == ["已重放", "一致", "已对账"]
    assert all('class="ok"' in row[1] for row in rows)
    assert f"总费用 {FEE} wei" in rows[0][3]
    assert f"原生币变化 {-FEE} wei" in rows[2][3] and "残差 0" in rows[2][3]
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
    assert f"原生币变化 {-FEE - 1} wei" in settled[3] and "残差 -1" in settled[3]
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
    body = fill_proof(tmp_path, bundle)
    unbound = {"status": "unbound", "native_wei": None, "assets": None, "tokens": None}
    body["cost_replay"] |= unbound | {"reason": "evidence_invalid_1"}
    body["receipt_transfers"] |= unbound | {"reason": "cost_binding_evidence_invalid_1"}
    body["block_settlement"] |= {"status": "brand_new", "reason": "constructor", "tokens": None}
    rows = proof_rows(panel(build_report(bundle.world.run), {"body": body}))
    second = f"第 2 份证明无法重放{OPEN}evidence_invalid_1{CLOSE}"
    assert rows[0][2] == second
    assert rows[1][2] == f"成本证明{COLON}{second}"
    # An unknown status is a failure shown by its code; a prototype name is not a label.
    assert tag_free(rows[2][1]) == "brand_new" and 'class="bad"' in rows[2][1]
    assert rows[2][2] == "constructor"
    assert all(row[3] == NONE for row in rows)


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
    out = panel(build_report(bundle.world.run), [{"body": body}, {"fail": 503}], rounds=2)
    assert "spot-proof" not in out["tables"] and out["danger"] is True
    assert "单笔成交证明不可用或校验失败" in out["proofStatus"]
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
    assert [tag_free(row[1]) for row in proof_rows(out)] == ["已重放", "一致", "已对账"]
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
