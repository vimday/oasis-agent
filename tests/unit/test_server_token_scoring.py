import json
import shutil
import subprocess
from urllib.parse import quote

from oasis_agent.launch.scoring import ScorePolicy, score_candidate
from scripts.check_dashboard import dashboard_sources
from tests.unit.test_server_api import client_for
from tests.unit.test_token_scoring import candidate


def test_api_and_core_use_identical_score_and_never_execute(tmp_path):
    value, policy = candidate(), ScorePolicy()
    with client_for(tmp_path) as client:
        payload = {
            "candidate": value.model_dump(mode="json"),
            "policy": policy.model_dump(mode="json"),
        }
        response = client.post("/api/v1/research/token-score", json=payload)
        assert response.status_code == 200
        assert response.json() == score_candidate(value, policy).model_dump(mode="json")
        payload["candidate"]["execute"] = True
        assert client.post("/api/v1/research/token-score", json=payload).status_code == 422


def test_console_preserves_large_integers_and_clears_failed_results():
    sources = dashboard_sources()
    core_url = "data:text/javascript," + quote(sources["core.js"], safe="")
    source = sources["research.js"].replace('"./core.js"', json.dumps(core_url))
    program = r"""

const assert = (await import('node:assert/strict')).default;
const nodes = new Map();
globalThis.document = {getElementById: id => nodes.get(id)};
for (const id of ['score-form','score-submit','score-result',
  'score-status','score-candidate','score-policy']) {
  nodes.set(id, {textContent:'old', addEventListener(name, fn){this.handler=fn;}});
}
const module = await import('data:text/javascript,' + encodeURIComponent(SOURCE));
const raw = '{"large":123456789012345678901234567890}';
assert.equal(module.scoreRequestBody(raw, '{}'), '{"candidate":'+raw+',"policy":{}}');
assert.throws(() => module.scoreRequestBody('null', '{}'));
assert.throws(() => module.scoreRequestBody('[]', '{}'));
assert.throws(() => module.scoreRequestBody('x'.repeat(1000001), '{}'));
nodes.get('score-candidate').files = [{size:raw.length, text:async()=>raw}];
nodes.get('score-policy').files = [{size:2, text:async()=>'{}'}];
globalThis.fetch = async (url, options) => {
  assert.equal(options.body, '{"candidate":'+raw+',"policy":{}}');
  return {ok:true,text:async()=>JSON.stringify({score_bps:9000,action:'simulate_entry',
    execution_eligible:false,evidence_level:'normalized_import_not_raw_attestation'})};
};
module.wireResearch();
await nodes.get('score-form').handler({preventDefault(){}});
assert.match(nodes.get('score-result').textContent, /simulate_entry/);
globalThis.fetch = async () => ({ok:false});
await nodes.get('score-form').handler({preventDefault(){}});
assert.equal(nodes.get('score-result').textContent, '');
assert.equal(nodes.get('score-submit').disabled, false);
assert.match(nodes.get('score-status').textContent, /评分失败/);
""".replace("SOURCE", json.dumps(source))
    node = shutil.which("node")
    assert node
    result = subprocess.run(
        [node, "--input-type=module", "-e", program],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
