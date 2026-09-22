"""The public API, installed assets and navigable console must evolve together."""

import json
import re
import shutil
import subprocess
from pathlib import Path

from scripts.check_dashboard import dashboard_sources

from .test_server_api import client_for


def test_every_api_has_a_real_console_view_and_packaged_assets(tmp_path):
    html = (Path(__file__).parents[2] / "src/oasis_agent/server/static/index.html").read_text()
    tabs = set(re.findall(r'data-tab="([^"]+)"', html))
    with client_for(tmp_path) as client:
        endpoints = client.get("/api/v1/meta").json()["endpoints"]
        assert endpoints
        assert all(endpoint["console_view"] in tabs for endpoint in endpoints)
        for filename in (*dashboard_sources(), "console.css"):
            response = client.get("/static/" + filename)
            assert response.status_code == 200
            assert response.headers["x-content-type-options"] == "nosniff"
        assert "script-src 'self';" in client.get("/").headers["content-security-policy"]
        assert client.get("/static/../config.py").status_code == 404


def test_newest_first_pages_are_bounded_and_execution_history_is_exposed(tmp_path):
    with client_for(tmp_path) as client:
        for path in ("observations", "decisions", "journal/lp/events"):
            result = client.get(f"/api/v1/{path}?limit=1&order=desc").json()
            assert result["total"] == 1
            assert len(result["items"]) == 1
            assert client.get(f"/api/v1/{path}?limit=101").status_code == 422
            assert client.get(f"/api/v1/{path}?order=wrong").status_code == 422
        detail = client.get("/api/v1/decisions/1").json()
        assert json.loads(detail["document"])["decision"] == detail["decision"]
        evidence = client.get("/api/v1/evidence/evidence-1").json()
        assert json.loads(evidence["document"]) == evidence["record"]


def test_console_keeps_evm_integers_exact_and_validates_gate_expiry():
    node = shutil.which("node")
    assert node, "Node.js is required"
    source = dashboard_sources()["core.js"]
    program = (
        "const module = await import('data:text/javascript,' + encodeURIComponent("
        + json.dumps(source)
        + "));\n"
        + r"""
const assert = (await import('node:assert/strict')).default;
const {parseJsonExact, fmtInt, fmtTime, gateState} = module;
const original = '{"amount":' + (2n ** 256n - 1n).toString()
  + ',"text":"12345678901234567890","small":7,"negative":-9007199254740992}';
const value = parseJsonExact(original);
assert.equal(value.amount, (2n ** 256n - 1n).toString());
assert.equal(value.negative, '-9007199254740992');
assert.equal(value.small, 7);
assert.equal(value.text, '12345678901234567890');
assert.equal(fmtInt(value.amount).replaceAll(',', ''), value.amount);
assert.equal(fmtInt(null), '\u2013');
assert.equal(fmtTime(0), fmtTime('1970-01-01T00:00:00Z'));
assert.throws(() => parseJsonExact('{"x":099999999999999999999999999}'));
assert.throws(() => parseJsonExact('{"x":NaN}'));
const instant = '2030-01-01T00:00:00Z';
assert.equal(gateState({status:'pass', expires_at:instant}, instant), 'expired');
assert.equal(gateState({status:'pass', expires_at:'bad'}, '2030-01-01T00:00:00Z'), 'unresolved');
"""
    )
    result = subprocess.run(
        [node, "--input-type=module", "-e", program],
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_paged_view_discards_out_of_order_responses_and_clears_failed_data():
    node = shutil.which("node")
    assert node, "Node.js is required"
    source = dashboard_sources()["tables.js"]
    source = source[source.index("let pageSize") : source.index("let detailGeneration")]
    source = source.replace("export ", "")
    program = (
        r"""
const assert = require('node:assert/strict');
const pending = [], nodes = new Map();
const $ = id => {
  if (!nodes.has(id)) nodes.set(id, {textContent:'', innerHTML:'', setAttribute(){},
    replaceChildren(){this.innerHTML='';}});
  return nodes.get(id);
};
const apiGet = path => new Promise((resolve,reject) => pending.push({path,resolve,reject}));
const esc = String, fmtInt = String;
let rows;
function renderTable(id, columns, value) { rows = value; }
"""
        + source
        + r"""
(async () => {
  setPageLimit(2);
  const view = createPagedView('test','/records',{columns:['id'],row:x=>x});
  const old = view.load();
  const fresh = view.load({reset:true});
  assert.ok(pending[1].path.includes('limit=2'));
  pending[1].resolve({items:['new'],total:3}); await fresh;
  pending[0].resolve({items:['old'],total:1}); await old;
  assert.deepEqual(rows,['new']);
  const next = view.load({delta:1});
  assert.ok(pending[2].path.includes('offset=2'));
  pending[2].reject(new Error('corrupt')); await next;
  assert.deepEqual(rows,[]);
  assert.match($('test-status').textContent,/失败/);
  assert.match($('test-pager').innerHTML,/重试/);
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
    )
    result = subprocess.run(
        [node, "-e", program], text=True, capture_output=True, timeout=10, check=False
    )
    assert result.returncode == 0, result.stderr
