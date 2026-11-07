"""Execute actual presentation code: missing state, precision and stale-result removal."""

import json
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

from oasis_agent.models.common import canonical_json, sha256_hex
from oasis_agent.reporting.live_pair import read_pair_status
from oasis_agent.reporting.live_status import read_live_status
from scripts.inspect_live_state import export_pair_status
from tests.unit.server_helpers import make_server_config
from tests.unit.test_live_lp import CONFIG
from tests.unit.test_live_offline_inspection import pair
from tests.unit.test_live_status import NOW, observed_state, write_state
from tests.unit.test_server_api import client_for


def test_pair_dashboard_shows_projection_and_configuration_conflicts(tmp_path, monkeypatch):
    node = shutil.which("node")
    assert node is not None
    root = Path(__file__).resolve().parents[2] / "src/oasis_agent/server/static"
    for name in ("core.js", "live-status.js"):
        shutil.copy(root / name, tmp_path / name)
    (tmp_path / "package.json").write_text('{"type":"module"}')
    monkeypatch.setattr("scripts.inspect_live_state.time.time", lambda: NOW)
    a, b = pair(tmp_path)
    clean = export_pair_status(a.path, b.path, CONFIG)
    artifact = tmp_path / "inspection.json"
    artifact.write_text(json.dumps(clean))
    aligned = read_pair_status(
        artifact, now=NOW, startup_config_hash=clean["expected_config_hash"]
    ).model_dump(by_alias=True)
    (tmp_path / "STOP").touch()
    stopped = export_pair_status(a.path, b.path, CONFIG)
    artifact.write_text(json.dumps(stopped))
    blocked = read_pair_status(
        artifact, now=NOW, startup_config_hash=stopped["expected_config_hash"]
    ).model_dump(by_alias=True)
    old = a.path.read_bytes()
    state = a.load("live")
    state.halted = "private diagnostic"
    a.save(state)
    a.path.write_bytes(old)
    report = export_pair_status(a.path, b.path, CONFIG)
    artifact.write_text(json.dumps(report))
    data = read_live_status(write_state(tmp_path, observed_state()), now=NOW).model_dump(
        by_alias=True
    )
    data["startup"].update(
        configuration_hash=report["expected_config_hash"],
        configuration_status="declared_execution_policy",
        configured_runtime_roles=8,
        local_guards_passed=True,
        missing_runtime_roles=[],
        blockers=[],
    )
    cases = {
        name: read_pair_status(artifact, now=NOW + age, startup_config_hash=target).model_dump(
            by_alias=True
        )
        for name, age, target in (
            ("projection", 0, report["expected_config_hash"]),
            ("mismatch", 0, "ab" * 32),
            ("stale", 301, report["expected_config_hash"]),
        )
    }
    cases.update(aligned=aligned, blocked=blocked)
    program = (
        "import {liveStatusModel, renderLiveStatus} from './live-status.js';\n"
        "const data = "
        + json.dumps(data)
        + ";\nconst cases = "
        + json.dumps(cases)
        + ";\n"
        + r"""
import assert from 'node:assert/strict';
const nodes = new Map();
class Element {
  constructor() { this.innerHTML = ''; this.children = []; }
  set textContent(value) { this.innerHTML = ''; this.text = value; }
  get textContent() { return this.text || ''; }
  append(...nodes) { this.children.push(...nodes); }
  appendChild(node) { this.children.push(node); }
  replaceChildren(...nodes) { this.children = nodes; }
}
globalThis.document = {
  getElementById(id) { if (!nodes.has(id)) nodes.set(id, new Element()); return nodes.get(id); },
  createElement() { return new Element(); }, createDocumentFragment() { return new Element(); }
};
const model = {...data, pair: cases.projection};
renderLiveStatus({...data, pair: cases.aligned});
assert.match(nodes.get('live-release').innerHTML, /当前执行资格未核验/);
renderLiveStatus({...data, pair: cases.blocked});
assert.match(nodes.get('live-pair').innerHTML, /签名服务.*检查时存在 STOP 文件/);
assert.match(nodes.get('live-release').innerHTML, /当前运行条件未满足/);
assert.match(nodes.get('live-title').textContent, /已取得近期观测/);
const legacy = structuredClone(cases.aligned);
for (const role of ['strategy', 'authority']) {
  delete legacy.inspection.services[role].stop_present;
  legacy.blockers.push(`${role}_stop_unverified`);
}
renderLiveStatus({...data, pair: legacy});
assert.match(nodes.get('live-pair').innerHTML, /摘要未记录 STOP 文件状态/);
assert.match(nodes.get('live-release').innerHTML, /当前运行条件未满足/);
assert.equal(liveStatusModel(model).pair.status, 'projection_incomplete');
renderLiveStatus(model);
assert.match(nodes.get('live-pair').innerHTML, /日志已验证.*JSON 投影尚未同步/);
assert.match(nodes.get('live-pair').innerHTML, /策略.*仍保留停机标记/);
assert.match(nodes.get('live-pair').innerHTML, /服务器未重放日志或链上证据/);
assert.match(nodes.get('live-pair').innerHTML, /不能更新 USD 喂价或 NAV 时效/);
assert.doesNotMatch(nodes.get('live-pair').innerHTML, /private diagnostic|可以实盘|交易已启动/);
renderLiveStatus({...data, pair: cases.stale});
assert.match(nodes.get('live-pair').innerHTML, /摘要已过期/);
assert.match(nodes.get('live-pair').innerHTML, /页面刷新不会更新检查时间/);
renderLiveStatus({...data, startup: {...data.startup, configuration_hash: 'ab'.repeat(32)},
  pair: cases.mismatch});
assert.match(nodes.get('live-pair').innerHTML, /待启动配置不一致/);
for (const mutate of [
  p => p.execution_eligible = true, p => p.evidence_replayed = true,
  p => p.rpc_requests = false, p => p.status = 'aligned', p => p.blockers = [],
  p => p.inspection.services.strategy.projection_current = true,
  p => p.inspection.services.strategy.config_hash = '<img src=x>',
  p => p.inspection.inspected_at = -1, p => p.inspection_age_seconds = 301,
  p => p.inspection.services.strategy.runtime_epoch_present = 'yes',
  p => p.inspection.services.strategy.stop_present = 1,
  p => p.inspection.services.authority.stop_present = false,
]) {
  const p = structuredClone(cases.projection); mutate(p);
  assert.equal(liveStatusModel({...data, pair: p}), null);
  renderLiveStatus(model); renderLiveStatus({...data, pair: p});
  assert.equal(nodes.get('live-pair').innerHTML, '');
  assert.equal(nodes.get('live-pending').textContent, '');
}
"""
    )
    completed = subprocess.run(
        [node, "--input-type=module"],
        input=program,
        cwd=tmp_path,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def test_live_dashboard_rejects_malformed_data_and_clears_old_results(
    tmp_path, monkeypatch
) -> None:
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
    monkeypatch.setattr("scripts.inspect_live_state.time.time", lambda: NOW)
    strategy, authority = pair(tmp_path)
    inspection = export_pair_status(strategy.path, authority.path, CONFIG)
    artifact = tmp_path / "inspection.json"
    artifact.write_text(json.dumps(inspection))
    inspected = read_pair_status(
        artifact, now=NOW + 298, startup_config_hash=inspection["expected_config_hash"]
    ).model_dump(by_alias=True)
    program = (
        "import {liveStatusModel, renderLiveStatus, formatNative, loadLiveStatus} from "
        + json.dumps((tmp_path / "live-status.js").as_uri())
        + ";\nconst data = "
        + json.dumps(model)
        + ";\nconst inspected = "
        + json.dumps(inspected)
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
// A recorded release must never be presented as current permission to start.
assert.equal(data.release.new_actions_allowed, true);
assert.match(nodes.get('live-release').innerHTML, /当前运行条件未满足/);
assert.match(nodes.get('live-release').innerHTML, /USD 500 金丝雀发布授权已记录/);
assert.doesNotMatch(nodes.get('live-release').innerHTML, /已放行/);
assert.match(nodes.get('live-release').innerHTML, /0xe0363189/);
const blocker = {code: 'release_closed', title: '实盘发布已关闭', required: '<b>x</b>'};
const closed = {...data, release: {...data.release, new_actions_allowed: false,
  release_evidence: null, blockers: [blocker]}, startup: {...data.startup,
    release_gate_passed: false, local_guards_passed: false, configuration_status: 'not_checked',
    configuration_hash: null, configured_runtime_roles: null, missing_runtime_roles: [],
    blockers: ['release_gate_closed']}};
assert.equal(liveStatusModel(closed).status, 'pending');
renderLiveStatus(closed);
assert.match(nodes.get('live-release').innerHTML, /新交易启动已阻断 · 1 项/);
assert.doesNotMatch(nodes.get('live-release').innerHTML, /<b>/);
assert.match(nodes.get('live-startup').innerHTML, /发布门禁未通过/);
const legacyStartup = {...data.startup, release_gate_passed: true, local_guards_passed: false,
  configuration_status: 'legacy_runtime_policy', configuration_hash: 'ab'.repeat(32),
  configured_runtime_roles: 5,
  missing_runtime_roles: ['gas_oracle_proxy', 'gas_oracle_implementation',
    'gas_oracle_implementation_code'],
  blockers: ['gas_oracle_runtime_pins_missing']};
renderLiveStatus({...data, startup: legacyStartup});
assert.match(nodes.get('live-startup').innerHTML, /本地静态检查未通过/);
assert.match(nodes.get('live-startup').innerHTML, /本地待启动配置指纹 5 \/ 8/);
assert.match(nodes.get('live-startup').innerHTML, /Gas 预言机代理代码/);
assert.match(nodes.get('live-startup').innerHTML, /Gas 预言机实现地址/);
assert.match(nodes.get('live-startup').innerHTML, /Gas 预言机实现代码/);
assert.match(nodes.get('live-startup').innerHTML, /STOP 与停机控制状态/);
assert.match(nodes.get('live-startup').innerHTML, /新鲜 USD 喂价及完整净值/);
assert.match(nodes.get('live-startup').innerHTML, /不代表生产镜像、链上状态或交易许可/);
const fullStartup = {...legacyStartup, local_guards_passed: true,
  configuration_status: 'declared_execution_policy', configured_runtime_roles: 8,
  missing_runtime_roles: [], blockers: []};
renderLiveStatus({...data, startup: fullStartup});
assert.match(nodes.get('live-startup').innerHTML, /本地静态检查通过 · 执行资格未核验/);
assert.match(nodes.get('live-startup').innerHTML, /仍未核验的运行条件/);
assert.doesNotMatch(nodes.get('live-startup').innerHTML, /已放行|可以实盘|交易已启动/);
for (const blocker of ['live_rpc_profile_missing', 'live_rpc_profile_invalid']) {
  const blockedProfile = {...fullStartup, local_guards_passed: false, blockers: [blocker]};
  renderLiveStatus({...data, startup: blockedProfile});
  assert.match(nodes.get('live-startup').innerHTML, /本地静态检查未通过/);
  assert.match(nodes.get('live-startup').innerHTML, /本地 RPC 额度与间隔配置(不存在|无法校验)/);
  const falsePass = {...blockedProfile, local_guards_passed: true};
  assert.equal(liveStatusModel({...data, startup: falsePass}), null);
}
const light = {block: data.observation.block + 10,
  block_timestamp: data.observation.observed_at + 590,
  observed_at: data.observation.observed_at + 600, tick: -199100};
const held = {...data, pending: null, status: 'stale', blockers: ['observation_stale'],
  observation_age_seconds: 900, light_check: light};
assert.equal(liveStatusModel(held).status, 'stale');
renderLiveStatus(held);
assert.match(nodes.get('live-release').innerHTML, /当前运行条件未满足/);
assert.match(nodes.get('live-title').textContent, /观测需要更新/);
assert.doesNotMatch(nodes.get('live-release').innerHTML, /已放行/);
assert.match(nodes.get('live-observation').textContent, /区间检查于 .* tick -199100/);
assert.match(nodes.get('live-observation').textContent, /仅确认仍在区间/);
assert.match(nodes.get('live-observation').textContent, /不更新净值或喂价时效/);
const recent = {...data, pending: null, status: 'observed', blockers: [],
  observation_age_seconds: 0};
assert.equal(liveStatusModel(recent).status, 'observed');
renderLiveStatus(recent);
assert.match(nodes.get('live-release').innerHTML, /当前运行条件未满足/);
assert.doesNotMatch(nodes.get('live-release').innerHTML, /已放行/);
assert.match(nodes.get('live-observation').textContent, /记录 NAV 有效至/);
assert.match(nodes.get('live-observation').textContent, /ETH \/ USDC 喂价上限 1800 \/ 1800 秒/);
const expired = {...recent, status: 'stale', blockers: ['nav_validity_expired'],
  observation_age_seconds: 2, observation: {...recent.observation,
    nav_valid_until: recent.observation.observed_at + 1}};
assert.equal(liveStatusModel(expired).status, 'stale');
renderLiveStatus(expired);
assert.match(nodes.get('live-blockers').innerHTML, /完整 NAV 已到期/);
assert.match(nodes.get('live-release').innerHTML, /发布授权已记录/);
const historical = {...recent, status: 'stale', blockers: ['nav_validity_unverified'],
  observation: {...recent.observation, nav_valid_until: null,
    eth_feed_max_age_seconds: null, usdc_feed_max_age_seconds: null}};
assert.equal(liveStatusModel(historical).status, 'stale');
renderLiveStatus(historical);
assert.match(nodes.get('live-observation').textContent, /记录 NAV 有效至 未记录/);
const oldPolicy = {...recent, status: 'stale', blockers: ['usd_feed_policy_mismatch'],
  observation: {...recent.observation, usdc_feed_max_age_seconds: 90000}};
assert.equal(liveStatusModel(oldPolicy).status, 'stale');
renderLiveStatus(oldPolicy);
assert.match(nodes.get('live-blockers').innerHTML, /配置迁移/);
assert.match(nodes.get('live-observation').textContent, /1800 \/ 90000 秒/);
assert.equal(liveStatusModel({...recent, observation: {...recent.observation,
  eth_feed_max_age_seconds: 900, usdc_feed_max_age_seconds: 60}}).status, 'observed');
const signing = {...data, pending: null, status: 'signing_unknown', signing_unresolved: true,
  blockers: ['signing_outcome_unresolved']};
assert.equal(liveStatusModel(signing).status, 'signing_unknown');
renderLiveStatus(signing);
assert.match(nodes.get('live-release').innerHTML, /当前运行条件未满足/);
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
for (const [status, blockers] of [
  ['halted', ['state_halted']], ['stale', ['observation_stale']],
  ['missing', ['live_state_missing']], ['invalid', ['live_state_invalid']],
  ['unconfigured', ['live_state_unconfigured']]]) {
  renderLiveStatus({...data, pending: null, status, blockers});
  assert.match(nodes.get('live-release').innerHTML, /当前运行条件未满足/);
  assert.doesNotMatch(nodes.get('live-release').innerHTML, /已放行/);
  assert.match(nodes.get('live-release').innerHTML, /日亏损上限 USD 200 · 累计 USD 500/);
}
for (const invalid of [null, {}, {...data, execution_eligible: true},
  {...data, startup: null}, {...data, startup: {...legacyStartup, local_guards_passed: true}},
  {...data, startup: {...fullStartup, execution_eligible: true}},
  {...data, startup: {...fullStartup, startup_prerequisites_passed: true}},
  {...data, startup: {...fullStartup, evidence_replayed: true}},
  {...data, startup: {...fullStartup, unverified_checks: []}},
  {...data, startup: {...fullStartup, rpc_requests: 1}},
  {...data, startup: {...legacyStartup, missing_runtime_roles: ['gas_oracle_proxy']}},
  {...data, startup: {...legacyStartup,
    missing_runtime_roles: ['gas_oracle_proxy', 'gas_oracle_proxy', 'gas_oracle_proxy']}},
  {...data, startup: {...legacyStartup, configuration_hash: '<script>'}},
  {...data, startup: {...fullStartup, configured_runtime_roles: 5}},
  {...data, startup: {...fullStartup, release_gate_passed: false}},
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
  {...held, status: 'observed', blockers: []},
  {...held, status: 'observed', blockers: [], observation_age_seconds: 4021},
  {...held, light_check: {...light, block: data.observation.block}},
  {...held, light_check: {...light, observed_at: data.observation.observed_at - 1}},
  {...held, light_check: {...light, tick: '-199100'}},
  {...expired, status: 'observed', blockers: []},
  {...historical, status: 'observed', blockers: []},
  {...oldPolicy, status: 'observed', blockers: []},
  {...recent, observation: {...recent.observation, nav_valid_until: Infinity}},
  {...recent, observation: {...recent.observation,
    nav_valid_until: recent.observation.block_timestamp + 121}},
  {...recent, observation: {...recent.observation, eth_feed_max_age_seconds: true}},
  {...held, status: 'observed', blockers: [], light_check: null, observation_age_seconds: 900}]) {
  assert.equal(liveStatusModel(invalid), null);
  renderLiveStatus(data);
  renderLiveStatus(invalid);
  assert.match(nodes.get('live-title').textContent, /暂不可用/);
  assert.equal(nodes.get('live-pending').textContent, '');
  assert.equal(nodes.get('live-observation').textContent, '');
  assert.equal(nodes.get('live-stats').textContent, '');
  assert.equal(nodes.get('live-startup').textContent, '');
}

// Model time advances independently of the next polling response.
let clock = 0, nextTimer = 0, requests = 0, respond;
const timers = new Map();
Object.defineProperty(globalThis, 'performance', {value: {now: () => clock}});
globalThis.setTimeout = (callback, delay) => {
  const id = ++nextTimer;
  timers.set(id, {callback, at: clock + delay});
  return id;
};
globalThis.clearTimeout = id => timers.delete(id);
globalThis.fetch = (path) => {
  assert.equal(path, '/api/v1/live/status');
  requests++;
  return new Promise(resolve => {
    respond = payload => resolve({
      status: 200, ok: true, text: async () => JSON.stringify(payload),
    });
  });
};
function advanceTo(ms) {
  clock = ms;
  for (const [id, timer] of timers) {
    if (timer.at > clock) continue;
    timers.delete(id);
    timer.callback();
  }
}
const nearExpiry = {...recent, observation: {...recent.observation,
  nav_valid_until: recent.observation.observed_at + 2}};
const firstRead = loadLiveStatus();
advanceTo(250);
respond(nearExpiry);
await firstRead;
assert.match(nodes.get('live-title').textContent, /已取得近期观测/);
advanceTo(1999);
assert.match(nodes.get('live-title').textContent, /已取得近期观测/);
const waiting = loadLiveStatus();
const concurrent = loadLiveStatus();
assert.equal(requests, 2);
advanceTo(2000);
assert.match(nodes.get('live-title').textContent, /观测需要更新/);
assert.match(nodes.get('live-blockers').innerHTML, /完整 NAV 已到期/);
assert.match(nodes.get('live-release').innerHTML, /当前运行条件未满足/);
assert.match(nodes.get('live-observation').textContent, /记录 NAV 有效至/);
assert.equal(requests, 2);
advanceTo(4100);
respond(nearExpiry);
await Promise.all([waiting, concurrent]);
assert.match(nodes.get('live-title').textContent, /观测需要更新/);

const replacement = loadLiveStatus();
respond(nearExpiry);
await replacement;
const newer = loadLiveStatus();
advanceTo(4200);
respond(recent);
await newer;
advanceTo(6100);
assert.match(nodes.get('live-title').textContent, /已取得近期观测/);

const startConfig = {...fullStartup, configuration_hash: inspected.inspection.expected_config_hash};
const withPair = {...nearExpiry, startup: startConfig, pair: inspected};
const both = loadLiveStatus();
advanceTo(6200);
respond(withPair);
await both;
const readsBeforeExpiry = requests;
assert.match(nodes.get('live-pair').innerHTML, /检查时双服务配置与投影一致/);
advanceTo(8100);
assert.match(nodes.get('live-title').textContent, /观测需要更新/);
assert.match(nodes.get('live-pair').innerHTML, /检查时双服务配置与投影一致/);
advanceTo(9099);
assert.match(nodes.get('live-pair').innerHTML, /检查时双服务配置与投影一致/);
advanceTo(9100);
assert.match(nodes.get('live-pair').innerHTML, /摘要已过期/);
assert.match(nodes.get('live-pair').innerHTML, /检查年龄 301 秒/);
assert.match(nodes.get('live-blockers').innerHTML, /完整 NAV 已到期/);
assert.equal(requests, readsBeforeExpiry);

const pairFirst = {...withPair, pair: {...inspected, inspection_age_seconds: 300}};
const refresh = loadLiveStatus();
respond(pairFirst);
await refresh;
const pendingRefresh = loadLiveStatus();
advanceTo(10100);
assert.match(nodes.get('live-pair').innerHTML, /摘要已过期/);
assert.match(nodes.get('live-title').textContent, /已取得近期观测/);
assert.match(nodes.get('live-release').innerHTML, /当前运行条件未满足/);
advanceTo(11100);
assert.match(nodes.get('live-title').textContent, /观测需要更新/);
respond({...data, startup: startConfig, pair: {...inspected, inspection_age_seconds: 300}});
await pendingRefresh;
assert.match(nodes.get('live-pair').innerHTML, /摘要已过期/);
assert.match(nodes.get('live-title').textContent, /等待对账/);
assert.match(nodes.get('live-pending').textContent, /nonce 3/);
assert.equal(timers.size, 0);

const pendingSnapshot = loadLiveStatus();
respond({...data, startup: startConfig, pair: {...inspected, inspection_age_seconds: 300}});
await pendingSnapshot;
assert.match(nodes.get('live-pair').innerHTML, /检查时双服务配置与投影一致/);
advanceTo(12100);
assert.match(nodes.get('live-pair').innerHTML, /摘要已过期/);
assert.match(nodes.get('live-pending').textContent, /nonce 3/);
assert.equal(timers.size, 0);
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


def test_bounded_run_status_cannot_claim_stopped_without_two_stop_markers(tmp_path):
    node = shutil.which("node")
    assert node is not None
    root = Path(__file__).resolve().parents[2] / "src/oasis_agent/server/static"
    for name in ("core.js", "live-status.js"):
        shutil.copy(root / name, tmp_path / name)
    (tmp_path / "package.json").write_text('{"type":"module"}')
    program = """
import assert from 'node:assert/strict';
import {boundedRunLabel} from './live-status.js';
const row = {attempt_id:"a".repeat(32), as_of:120, execution_eligible:false,
             status:"launch_requested", result:null, stop_present:null};
assert.match(boundedRunLabel(row), /启动已发起.*等待停机证据/);
row.status = "cleanup_unverified";
assert.match(boundedRunLabel(row), /停机或双 STOP 恢复待核对/);
row.result = "timed_out";
row.stop_present = {strategy:true,authority:false};
assert.match(boundedRunLabel(row), /超时/);
row.status = "stopped";
assert.equal(boundedRunLabel(row), null);
row.stop_present.authority = true;
assert.match(boundedRunLabel(row), /已停机.*双 STOP 已恢复.*超时/);
row.result = "completed";
assert.match(boundedRunLabel(row), /双 STOP 已恢复.*单轮完成/);
"""
    result = subprocess.run(
        [node, "--input-type=module"],
        cwd=tmp_path,
        input=program,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_bounded_run_endpoint_and_label_distinguish_safe_blockers(tmp_path):
    node = shutil.which("node")
    assert node is not None
    root = Path(__file__).resolve().parents[2] / "src/oasis_agent/server/static"
    for name in ("core.js", "live-status.js"):
        shutil.copy(root / name, tmp_path / name)
    (tmp_path / "package.json").write_text('{"type":"module"}')

    run = tmp_path / "run"
    run.mkdir()
    config = make_server_config(tmp_path)
    config = replace(config, databases=replace(config.databases, live_bounded_run=run))
    terminal = {
        "schema": "oasis.base-bounded-run.v1",
        "execution_eligible": False,
        "attempt_id": "a" * 32,
        "finished_at": 120,
        "result": "failed",
        "guard": "released",
        "rpc_ceiling_micros": 100_000,
        "cleanup": {
            "verified": True,
            "services_stopped": True,
            "stop_present": {"strategy": True, "authority": True},
        },
        "commands": {"diagnostic": "private-provider-diagnostic"},
    }
    budget = {
        "ceiling_micros": 100_000,
        "cumulative_reserved_upper_micros": 91_155,
        "cumulative_scope_upper_micros": 95_155,
        "enough_for_capture": False,
    }
    replay = {
        "schema": "oasis.live-startup-state.v1",
        "startup_prerequisites_passed": False,
        "execution_eligible": False,
        "historical_only": True,
        "blockers": ["halt_or_reconciliation_unresolved"],
        "blocked_service": "authority",
    }
    cases = {
        "insufficient": {
            "failure_stage": "cumulative_rpc_budget",
            "start_attempted": False,
            "rpc_budget": budget,
        },
        "unverified": {
            "failure_stage": "cumulative_rpc_budget",
            "start_attempted": False,
            "rpc_budget": {**budget, "ceiling_micros": True},
        },
        "budget_missing": {"failure_stage": "cumulative_rpc_budget", "start_attempted": False},
        "reconcile": {
            "failure_stage": "post_stop_replay",
            "start_attempted": True,
            "post_stop_replay": replay,
        },
        "unrecognized": {
            "failure_stage": "post_stop_replay",
            "start_attempted": True,
            "post_stop_replay": {**replay, "blocked_service": "private-role"},
        },
        "timed_out": {
            "result": "timed_out",
            "failure_stage": "one_tick_runtime",
            "start_attempted": True,
            "post_stop_replay": replay,
        },
        "contradictory": {
            "result": "completed",
            "start_attempted": True,
            "post_stop_replay": replay,
        },
        "legacy": {},
    }
    rows = {}
    with client_for(tmp_path, config=config) as client:
        for name, fields in cases.items():
            report = terminal | fields
            report["evidence_hash"] = sha256_hex(canonical_json(report))
            (run / "report.json").write_bytes(canonical_json(report))
            response = client.get("/api/v1/live/status")
            assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
            assert "private-provider-diagnostic" not in response.text
            assert "private-role" not in response.text and str(run) not in response.text
            rows[name] = response.json()["bounded_run"]
        assert client.post("/api/v1/live/status").status_code == 405

    assert rows["insufficient"]["blocker"] == "rpc_budget_insufficient"
    assert rows["unverified"]["blocker"] == "rpc_budget_unverified"
    assert rows["budget_missing"]["blocker"] == "rpc_budget_unverified"
    assert rows["reconcile"]["blocked_service"] == "authority"
    assert rows["unrecognized"]["blocked_service"] is None
    assert rows["timed_out"]["blocker"] == "post_stop_reconciliation_required"
    assert rows["contradictory"]["status"] == "invalid"
    assert rows["legacy"]["blocker"] is None

    program = (
        "const rows = "
        + json.dumps(rows)
        + ";\n"
        + r"""
import assert from 'node:assert/strict';
import {boundedRunLabel} from './live-status.js';
assert.match(boundedRunLabel(rows.insufficient), /未发起启动.*累计 RPC 额度不足/);
assert.match(boundedRunLabel(rows.unverified), /累计 RPC 预算未核验/);
assert.match(boundedRunLabel(rows.reconcile), /双 STOP 已恢复.*签名服务.*待核查停机或对账/);
assert.match(boundedRunLabel(rows.unrecognized), /停机后对账未核验/);
assert.match(boundedRunLabel(rows.timed_out), /超时.*待核查停机或对账/);
assert.match(boundedRunLabel(rows.contradictory), /本轮记录无法校验/);
assert.match(boundedRunLabel(rows.legacy), /失败$/);
assert.equal(boundedRunLabel({...rows.insufficient, blocker:"<img>"}), null);
assert.equal(boundedRunLabel({...rows.reconcile, blocked_service:"<img>"}), null);
"""
    )
    result = subprocess.run(
        [node, "--input-type=module"],
        cwd=tmp_path,
        input=program,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
