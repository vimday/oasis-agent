"""The batch console shares backend decisions and cannot turn unknown inputs into orders."""

import json
import shutil
import subprocess
from urllib.parse import quote

from oasis_agent.launch.batch_scoring import score_batch
from scripts.check_dashboard import dashboard_sources

from .test_batch_scoring import request
from .test_server_api import client_for


def test_batch_api_matches_core_and_sanitizes_binding_failures(tmp_path):
    value = request()
    with client_for(tmp_path) as client:
        response = client.post("/api/v1/research/batch-score", json=value.model_dump(mode="json"))
        assert response.status_code == 200
        assert response.json() == score_batch(value).model_dump(mode="json")
        payload = value.model_dump(mode="json")
        payload["candidates"] *= 2
        response = client.post("/api/v1/research/batch-score", json=payload)
        assert response.status_code == 422
        assert response.json()["detail"] == "Batch evidence binding failed"
        assert client.get("/api/v1/research/batch-score").status_code == 405


def test_batch_console_exact_units_unknown_scores_and_failed_request_cleanup():
    sources = dashboard_sources()
    source = sources["batch-research.js"].replace(
        '"./core.js"', json.dumps("data:text/javascript," + quote(sources["core.js"], safe=""))
    )
    program = r"""
const assert = (await import('node:assert/strict')).default;
class Element {
  constructor(){ this.textContent=''; this.innerHTML=''; this.children=[]; }
  appendChild(value){this.children.push(value);}
  replaceChildren(...values){this.children=values;}
  addEventListener(name,fn){this[name]=fn;}
}
const nodes = new Map();
globalThis.document = {
  getElementById(id){if(!nodes.has(id))nodes.set(id,new Element());return nodes.get(id);},
  createElement(){return new Element();},createDocumentFragment(){return new Element();},
};
const module = await import('data:text/javascript,'+encodeURIComponent(SOURCE));
const raw = '{"amount":123456789012345678901234567890}';
assert.equal(module.batchRequestBody(raw),raw);
assert.throws(()=>module.batchRequestBody('[]'));
assert.throws(()=>module.batchRequestBody('x'.repeat(10000001)));
const value = VALUE;
let view = module.batchView(value);
assert.equal(view.candidateRows[0][4],'未评分');
value.items[0].discovery.token='<img src=x onerror=alert(1)>';
assert.ok(module.batchView(value).candidateRows[0][1].startsWith('&lt;'));
value.execution_eligible=true;
assert.throws(()=>module.batchView(value));
value.execution_eligible=false;
assert.match(module.failureSummary({execution_eligible:false,error_type:'HTTPError',http_status:403}),
  /403/);
assert.throws(()=>module.failureSummary({execution_eligible:false,error_type:'bad',http_status:999}));
module.wireBatchResearch();
document.getElementById('batch-score-input').files=[{size:raw.length,text:async()=>raw}];
globalThis.fetch=async(url,options)=>{
  assert.equal(url,'/api/v1/research/batch-score');assert.equal(options.body,raw);
  return {ok:true,text:async()=>JSON.stringify(value)};
};
await nodes.get('batch-score-form').submit({preventDefault(){}});
assert.match(nodes.get('batch-score-result').textContent,/oasis.batch-score.v1/);
globalThis.fetch=async()=>({ok:false});
await nodes.get('batch-score-form').submit({preventDefault(){}});
assert.equal(nodes.get('batch-score-result').textContent,'');
assert.equal(nodes.get('batch-score-table').children.length,0);
assert.equal(nodes.get('batch-exit-table').children.length,0);
assert.match(nodes.get('batch-score-status').textContent,/失败/);
assert.equal(nodes.get('batch-score-submit').disabled,false);
""".replace("SOURCE", json.dumps(source)).replace(
        "VALUE", json.dumps(score_batch(request(())).model_dump(mode="json"))
    )
    node = shutil.which("node")
    assert node
    result = subprocess.run(
        [node, "--input-type=module", "-e", program],
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
