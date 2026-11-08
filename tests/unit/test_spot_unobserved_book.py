"""A live run stopped before reading the wallet shows an unobserved book, never paper cash."""

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from oasis_agent.launch.spot_report import build_report
from oasis_agent.sniping.journal import ExecutionJournal
from scripts.check_dashboard import dashboard_sources

from .test_spot_budget_ledger import CountingWallet, kinds, start_live
from .test_spot_report_api import configured, shadow_run
from .test_spot_runner import SubmittingExecutor, open_live, run_live

# Console labels, exactly as spot.js renders them (fullwidth parentheses are the UI text).
EQUITY = "策略权益 USD\uff08非全账户净值\uff09"
HELD = "策略账簿持仓\uff08非钱包盘点\uff09"
UNOBSERVED = "未观测\uff08尚无持久化的钱包余额观测\uff09"


def assert_unobserved(report: dict[str, Any]) -> None:
    book, accounting = report["paper_book"], report["accounting"]
    assert accounting["book_observed"] is False
    assert accounting["cash_basis"] == "unobserved_no_live_start"
    assert accounting["positions_scope"] == "strategy_book_not_wallet_inventory"
    assert book["initial_cash_usd_micros"] is None
    assert book["final_nav_usd_micros"] is None
    assert book["max_drawdown_usd_micros"] is None


def test_zero_budget_live_run_reports_unobserved_book(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = tmp_path / "run"
    assert start_live(run, monkeypatch, CountingWallet(), 0) is None
    report = build_report(run)
    assert_unobserved(report)
    assert report["operations"]["budget_state"]["open_positions"] is None


def test_failed_initialization_reports_unobserved_book(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = tmp_path / "run"
    with pytest.raises(OSError):
        start_live(run, monkeypatch, CountingWallet(fail=True), 1)
    assert kinds(run) == ["budget_reserve"]
    assert_unobserved(build_report(run))


def test_interrupted_between_settle_and_live_start_stays_unobserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = tmp_path / "run"
    original = ExecutionJournal.append

    def faulty(self: ExecutionJournal, kind: str, payload: dict[str, Any]) -> Any:
        if kind == "live_start":
            raise OSError("killed")
        return original(self, kind, payload)

    monkeypatch.setattr(ExecutionJournal, "append", faulty)
    with pytest.raises(OSError):
        start_live(run, monkeypatch, CountingWallet(), 1)
    monkeypatch.setattr(ExecutionJournal, "append", original)
    # The balance was read, but the book never recorded it: still not an observation.
    assert kinds(run) == ["budget_reserve", "budget_settle"]
    assert_unobserved(build_report(run))


def test_initialized_live_run_without_ticks_shows_the_observed_wallet_cash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = tmp_path / "run"
    assert start_live(run, monkeypatch, CountingWallet(), 1) is not None
    report = build_report(run)
    assert report["accounting"]["book_observed"] is True
    assert report["accounting"]["cash_basis"] == "wallet_usdc_observed"
    assert report["paper_book"]["initial_cash_usd_micros"] == 1_000_000_000
    assert report["paper_book"]["final_nav_usd_micros"] == 1_000_000_000


def test_live_and_shadow_controls_keep_observed_values(tmp_path: Path) -> None:
    live = tmp_path / "live"
    open_live(live).close()
    assert run_live(live, SubmittingExecutor(RuntimeError("unused")), ticks=1) in (0, 4)
    report = build_report(live)
    assert report["accounting"]["book_observed"] is True
    assert isinstance(report["paper_book"]["final_nav_usd_micros"], int)
    shadow = tmp_path / "shadow"
    shadow_run(shadow, [(1_800_000_000, (99, 100))])
    report = build_report(shadow)
    assert report["accounting"]["cash_basis"] == "paper"
    assert report["accounting"]["book_observed"] is True
    assert report["paper_book"]["initial_cash_usd_micros"] == 1_000_000_000


def test_api_serves_the_unobserved_book_as_nulls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = tmp_path / "run"
    assert start_live(run, monkeypatch, CountingWallet(), 0) is None
    with configured(tmp_path, run) as client:
        response = client.get("/api/v1/spot/report")
        assert response.status_code == 200
        assert_unobserved(response.json())


def render(report: dict[str, Any]) -> dict[str, Any]:
    """Run the real loadSpot() under Node with stub DOM/API; return what it rendered."""
    node = shutil.which("node")
    assert node, "Node.js is required"
    source = dashboard_sources()["spot.js"]
    head = source.index("\n", source.index("import "))
    program = (
        r"""
const nodes = new Map(), out = {stats: null, tables: {}};
globalThis.document = {getElementById: id => {
  if (!nodes.has(id)) nodes.set(id, {textContent: '', classList: {toggle(){}}});
  return nodes.get(id);
}};
const esc = String;
const REPORT = """
        + json.dumps(report)
        + r""";
async function apiGet(path) {
  return path.startsWith('/api/v1/spot/report') ? REPORT : {items: [], total: 0};
}
function renderStats(id, stats) { out.stats = stats; }
function renderTable(id, columns, rows, empty) { out.tables[id] = {rows, empty}; }
"""
        + source[head + 1 :].replace("export ", "")
        + r"""
loadSpot().then(() => {
  out.status = document.getElementById('spot-status').textContent;
  console.log(JSON.stringify(out));
}).catch(error => { console.error(error); process.exitCode = 1; });
"""
    )
    result = subprocess.run(
        [node, "-e", program], text=True, capture_output=True, timeout=10, check=False
    )
    assert result.returncode == 0, result.stderr
    rendered: dict[str, Any] = json.loads(result.stdout)
    return rendered


def test_console_never_shows_paper_cash_or_empty_wallet_for_unobserved_book(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = tmp_path / "run"
    assert start_live(run, monkeypatch, CountingWallet(), 0) is None
    out = render(build_report(run))
    stats = {s["label"]: str(s["value"]) for s in out["stats"]}
    text = json.dumps(out, ensure_ascii=False)
    # A read may have happened (killed before live_start): never claim it did not.
    assert stats[EQUITY] == stats[HELD] == UNOBSERVED
    assert "未读取" not in json.dumps(out["stats"], ensure_ascii=False)
    assert "1000.00" not in text and "null" not in text and "undefined" not in text
    assert "启动前未能读取钱包余额" in out["status"] and "停止时持仓未知" in out["status"]
    assert "未观测" in out["tables"]["spot-positions"]["empty"]


def test_console_keeps_observed_values_for_an_initialized_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = tmp_path / "run"
    assert start_live(run, monkeypatch, CountingWallet(), 1) is not None
    out = render(build_report(run))
    stats = {s["label"]: str(s["value"]) for s in out["stats"]}
    assert stats[EQUITY] == "1000.00"
    assert stats[HELD] == "0"
    assert "不代表钱包无持仓" in out["tables"]["spot-positions"]["empty"]
    assert "未初始化" not in out["status"]


def test_console_wording_holds_after_a_read_without_live_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = tmp_path / "run"
    original = ExecutionJournal.append

    def faulty(self: ExecutionJournal, kind: str, payload: dict[str, Any]) -> Any:
        if kind == "live_start":
            raise OSError("killed")
        return original(self, kind, payload)

    monkeypatch.setattr(ExecutionJournal, "append", faulty)
    with pytest.raises(OSError):
        start_live(run, monkeypatch, CountingWallet(), 1)
    monkeypatch.setattr(ExecutionJournal, "append", original)
    out = render(build_report(run))
    stats = {s["label"]: str(s["value"]) for s in out["stats"]}
    assert stats[EQUITY] == UNOBSERVED
    assert "1000.00" not in json.dumps(out, ensure_ascii=False)
