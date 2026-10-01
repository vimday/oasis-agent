"""Run the real presentation model against missing and expired release evidence."""

import json
import shutil
import subprocess
from pathlib import Path

from oasis_agent.reporting.lp_readiness import LpGate


def test_readiness_path_fails_closed_and_routes_to_evidence(tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node is not None, "Node.js is required for the frontend behavior gate"
    root = Path(__file__).resolve().parents[2] / "src/oasis_agent/server/static"
    for name in ("core.js", "readiness.js"):
        shutil.copy(root / name, tmp_path / name)
    (tmp_path / "package.json").write_text('{"type":"module"}')
    program = (
        "import {readinessModel, renderReadinessPath, RELEASE_PATH} from "
        + json.dumps((tmp_path / "readiness.js").as_uri())
        + ";\nconst expectedGates = "
        + json.dumps([gate.value for gate in LpGate])
        + ";\n"
        + r"""
import assert from 'node:assert/strict';
assert.deepEqual(RELEASE_PATH.map(([id]) => id), expectedGates);
const nodes = new Map();
globalThis.document = {getElementById(id) {
  if (!nodes.has(id)) nodes.set(id, {textContent: '', innerHTML: ''});
  return nodes.get(id);
}};
const assessment = {
  as_of: '2026-09-13T00:00:00Z',
  gates: RELEASE_PATH.map(([gate]) => ({gate, status: 'pass', expires_at: '2026-09-14T00:00:00Z'}))
};
assert.equal(readinessModel(assessment).passed, 9);
assert.equal(readinessModel(assessment).next, null);
renderReadinessPath(assessment);
assert.match(nodes.get('readiness-next').innerHTML, /尚未获得执行资格/);
assert.match(nodes.get('readiness-summary').textContent, /原始证据与执行授权未验证/);
assessment.gates[2].expires_at = assessment.as_of;
assert.equal(readinessModel(assessment).passed, 8);
assert.equal(readinessModel(assessment).next.id, 'wp2_replay_accounting');
assert.equal(readinessModel(assessment).next.status, 'expired');
renderReadinessPath(assessment);
assert.match(nodes.get('readiness-next').innerHTML, /data-view="evidence"/);
assessment.gates[2].expires_at = 'invalid';
assert.equal(readinessModel(assessment).next.status, 'unresolved');
assessment.gates[0].status = '<script>';
assert.equal(readinessModel(assessment).next.status, 'unresolved');
for (const invalid of [null, {}, {gates: []}, {...assessment, as_of: 'bad'},
  {...assessment, gates: assessment.gates.slice(1)},
  {...assessment, gates: [...assessment.gates.slice(1), assessment.gates[1]]}]) {
  assert.equal(readinessModel(invalid).valid, false);
  assert.equal(readinessModel(invalid).passed, 0);
  renderReadinessPath(invalid);
  assert.match(nodes.get('readiness-summary').textContent, /资格未验证/);
  assert.match(nodes.get('readiness-next').innerHTML, /data-view="system"/);
  assert.ok(!nodes.get('readiness-path').innerHTML.includes('path-step pass'));
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
