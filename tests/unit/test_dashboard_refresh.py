"""Execute dashboard refresh behavior in Node, without network or browser timing."""

import shutil
import subprocess

from scripts.check_dashboard import dashboard_source


def test_dashboard_inflight_reads_and_failed_status():
    node = shutil.which("node")
    assert node is not None, "Node.js is required for the frontend behavior gate"
    script = dashboard_source()
    client = script[script.index("const pendingReads") : script.index("const apiPost")]
    status = script[
        script.index("function decisionExplanation") : script.index("async function loadGates")
    ]
    escaping = script[script.index("const ESCAPES") : script.index("function short")]
    tick = script[script.index("function tick()") : script.index("function startPolling()")]
    program = (
        r"""
const assert = require('node:assert/strict');
let calls = 0, settle, reject;
function api() {
  calls++;
  return new Promise((resolve, fail) => { settle = resolve; reject = fail; });
}
const nodes = new Map();
const $ = id => {
  if (!nodes.has(id)) nodes.set(id, {textContent: '', innerHTML: ''});
  return nodes.get(id);
};
function setBadge(node, label) { node.textContent = label; }
const fmtInt = String;
let stats;
function renderStats(id, value) { stats = value; }
const document = {hidden: false};
let tab = 'overview', loads = 0;
const currentTab = () => tab;
const views = new Map([['overview', {poll: true, load() { loads++; }}]]);
"""
        + client
        + escaping
        + status
        + tick
        + r"""
(async () => {
  const first = apiGet('/status');
  assert.equal(apiGet('/status'), first);
  assert.equal(calls, 1);
  settle({verified: true});
  await first;
  const second = apiGet('/status');
  assert.notEqual(second, first);
  assert.equal(calls, 2);
  reject(new Error('offline'));
  await assert.rejects(second);
  assert.equal(pendingReads.size, 0);
  // Failure must clear earlier optimistic status instead of just showing a toast.
  $('st-halt').textContent = 'halt clear';
  const loading = loadStatus();
  reject(new Error('offline'));
  await loading;
  assert.equal($('st-halt').textContent, 'halt 未知');
  assert.equal($('st-eligible').textContent, 'eligible 未验证');
  assert.match($('latest-decision').textContent, /暂不可用/);
  const recovered = loadStatus();
  settle({execution_eligible: null, latest_health: null, halt_latched: false});
  await recovered;
  assert.equal($('st-halt').textContent, 'halt 未知');
  assert.equal($('st-eligible').textContent, 'eligible 未验证');
  assert.equal(stats[3].value, '未知');
  assert.match(decisionExplanation({}), /记录未提供原因/);
  const explanation = decisionExplanation({
    reasons: ['stale', '<script>'], rebalance_reasons: ['stale'],
    strategy_qualification: '<unsafe>'
  });
  assert.equal(explanation.split('stale').length, 2);
  assert.ok(!explanation.includes('<script>'));
  assert.ok(explanation.includes('&lt;unsafe&gt;'));
  const before = calls;
  tick();
  assert.equal(loads, 1);
  assert.equal(calls, before); // overview owns its status refresh
  document.hidden = true;
  tick();
  assert.equal(loads, 1);
  tab = 'decisions';
  views.set('decisions', {poll: false, load() { loads++; }});
  refreshVisibleViews();
  assert.equal(loads, 1);
  document.hidden = false;
  refreshVisibleViews();
  assert.equal(loads, 2); // recover notifications missed while the tab was hidden
  settle({execution_eligible: null, latest_health: null});
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    )
    result = subprocess.run(
        [node, "-e", program], capture_output=True, text=True, timeout=10, check=False
    )
    assert result.returncode == 0, result.stderr
