"""Execute actual presentation code: missing state, precision and stale-result removal."""

import json
import shutil
import subprocess
from pathlib import Path

from oasis_agent.reporting.live_status import read_live_status
from tests.unit.test_live_status import NOW, observed_state, write_state


def test_live_dashboard_rejects_malformed_data_and_clears_old_results(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node is not None, "Node.js is required for frontend behavior tests"
    root = Path(__file__).resolve().parents[2] / "src/oasis_agent/server/static"
    for name in ("core.js", "live-status.js"):
        shutil.copy(root / name, tmp_path / name)
    (tmp_path / "package.json").write_text('{"type":"module"}')
    record = observed_state()
    record["pending"] = {
        "kind": "swap",
        "tx_hash": "0x" + "ab" * 32,
        "nonce": 3,
        "signed_at": NOW,
    }
    model = read_live_status(write_state(tmp_path, record), now=NOW).model_dump(by_alias=True)
    program = (
        "import {liveStatusModel, renderLiveStatus, formatNative} from "
        + json.dumps((tmp_path / "live-status.js").as_uri())
        + ";\nconst data = "
        + json.dumps(model)
        + ";\n"
        + r"""
import assert from 'node:assert/strict';
class Element {
  constructor() { this.textContent = ''; this.innerHTML = ''; this.children = []; }
  append(...nodes) { this.children.push(...nodes); }
  appendChild(node) { this.children.push(node); }
  replaceChildren(...nodes) { this.children = nodes; }
}
const nodes = new Map();
globalThis.document = {
  getElementById(id) { if (!nodes.has(id)) nodes.set(id, new Element()); return nodes.get(id); },
  createElement() { return new Element(); }, createDocumentFragment() { return new Element(); }
};
assert.equal(formatNative('18446744073709551617'), '18.446744073709551617 ETH');
assert.equal(formatNative('1'), '0.000000000000000001 ETH');
assert.equal(formatNative('0'), '0.0 ETH');
for (const value of [null, 1e18, '-1', '<script>', '01']) {
  assert.equal(formatNative(value), '未记录');
}
assert.equal(liveStatusModel(data).status, 'pending');
renderLiveStatus(data);
assert.match(nodes.get('live-title').textContent, /等待对账/);
assert.match(nodes.get('live-pending').textContent, /nonce 3/);
assert.match(nodes.get('live-source').textContent, /未重放/);
// The committed release is open: limits and receipt evidence, no blocker list.
assert.equal(data.release.new_actions_allowed, true);
assert.match(nodes.get('live-release').innerHTML, /USD 500 金丝雀已放行/);
assert.match(nodes.get('live-release').innerHTML, /0xe0363189/);
const blocker = {code: 'release_closed', title: '实盘发布已关闭', required: '<b>x</b>'};
const closed = {...data, release: {...data.release, new_actions_allowed: false,
  release_evidence: null, blockers: [blocker]}};
assert.equal(liveStatusModel(closed).status, 'pending');
renderLiveStatus(closed);
assert.match(nodes.get('live-release').innerHTML, /新交易启动已阻断 · 1 项/);
assert.doesNotMatch(nodes.get('live-release').innerHTML, /<b>/);
const light = {block: data.observation.block + 10,
  block_timestamp: data.observation.observed_at + 590,
  observed_at: data.observation.observed_at + 600, tick: -199100};
const held = {...data, pending: null, status: 'observed', blockers: [],
  observation_age_seconds: 900, light_check: light};
assert.equal(liveStatusModel(held).status, 'observed');
renderLiveStatus(held);
assert.match(nodes.get('live-observation').textContent, /区间检查于 .* tick -199100/);
assert.match(nodes.get('live-observation').textContent, /仅确认仍在区间/);
const signing = {...data, pending: null, status: 'signing_unknown', signing_unresolved: true,
  blockers: ['signing_outcome_unresolved']};
assert.equal(liveStatusModel(signing).status, 'signing_unknown');
renderLiveStatus(signing);
assert.match(nodes.get('live-title').textContent, /签名结果未知/);
assert.match(nodes.get('live-next').textContent, /禁止自动重签或重发/);
assert.match(nodes.get('live-pending').textContent, /不能认定交易未签名或未提交/);
assert.doesNotMatch(nodes.get('live-pending').textContent, /无待处理交易/);
renderLiveStatus(data);
assert.match(nodes.get('live-pending').textContent, /nonce 3/);
assert.doesNotMatch(nodes.get('live-title').textContent, /签名结果未知/);
const unknown = {...data, status: 'missing'};
renderLiveStatus(unknown);
assert.equal(nodes.get('live-pending').textContent, '待处理状态未知');
assert.match(nodes.get('live-observation').textContent, /没有可用/);
for (const invalid of [null, {}, {...data, execution_eligible: true},
  {...data, evidence_replayed: true}, {...data, profitability: 'profitable'},
  {...signing, signing_unresolved: false}, {...signing, blockers: []},
  {...signing, status: 'observed'}, {...data, signing_unresolved: undefined},
  {...data, signing_unresolved: 'false'},
  {...data, status: 'observed', blockers: [] , observation_age_seconds: 121},
  {...data, blockers: ['<script>']}, {...data, recent_actions: [null]},
  {...data, pending: {...data.pending, tx_hash: '<unsafe>'}},
  {...data, observation: {...data.observation, nav_usd: Infinity}},
  {...data, observation: {...data.observation, nav_usd: '500'}},
  {...data, release: {...data.release, new_actions_allowed: 'true'}},
  {...data, release: {...data.release, release_evidence: null}},
  {...data, release: {...data.release, blockers: [blocker]}},
  {...data, release: {...data.release, new_actions_allowed: false}},
  {...data, release: {...data.release, new_actions_allowed: false, blockers: [null]}},
  {...held, observation_age_seconds: 4021},
  {...held, light_check: {...light, block: data.observation.block}},
  {...held, light_check: {...light, observed_at: data.observation.observed_at - 1}},
  {...held, light_check: {...light, tick: '-199100'}},
  {...held, light_check: null, observation_age_seconds: 900}]) {
  assert.equal(liveStatusModel(invalid), null);
  renderLiveStatus(data);
  renderLiveStatus(invalid);
  assert.match(nodes.get('live-title').textContent, /暂不可用/);
  assert.equal(nodes.get('live-pending').textContent, '');
  assert.equal(nodes.get('live-observation').textContent, '');
  assert.equal(nodes.get('live-stats').textContent, '');
}
"""
    )
    result = subprocess.run(
        [node, "--input-type=module", "-e", program],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
