"""UI validation and CLI parsing must reject the same unsafe policies."""

import json
import sys
import tomllib
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from oasis_agent.launch.spot_agent import AgentConfig
from oasis_agent.server.app import create_app
from oasis_agent.server.config import ServerShadowRunnerConfig
from oasis_agent.shadow.market_archive import ArchiveConfig
from oasis_agent.shadow.observer_config import Config
from oasis_agent.shadow.service import parse_policy
from oasis_agent.sniping.config import ExecutionConfig

from .server_helpers import make_server_config


@pytest.mark.parametrize(
    "patch",
    [
        {"buy_tranches_wei": []},
        {"max_gas": 0},
        {"v3_fees": []},
        {"v3_fees": [500, 500]},
        {"v3_fees": [-1]},
        {"usdc_weth_fee": 3000},
    ],
)
def test_launch_rejects_invalid_or_unbound_policy(patch):
    with pytest.raises(ValueError):
        ExecutionConfig.from_json(json.dumps(patch))


def test_launch_rejects_duplicate_json_keys():
    with pytest.raises(ValueError, match="duplicate"):
        ExecutionConfig.from_json('{"max_gas": 0, "max_gas": 800000}')


@pytest.mark.parametrize("patch", [{"chain_id": True}, {"max_age_seconds": 0}, {"unknown": 1}])
def test_lp_parser_rejects_invalid_envelope_before_opening_journal(patch):
    policy = json.loads(Path("configs/lp-shadow.robinhood-testnet.example.json").read_text())
    with pytest.raises((ValueError, TypeError)):
        parse_policy(json.dumps(policy | patch).encode())


def test_validation_keeps_base_unit_precision_and_never_applies(tmp_path):
    config = make_server_config(tmp_path)
    with TestClient(create_app(config, environ={"OASIS_SERVER_OPS_TOKEN": "test-ops"})) as client:
        payload = {"kind": "launch", "document": '{"buy_tranches_wei":[100000000000000001]}'}
        assert client.post("/api/v1/configuration/validate", json=payload).status_code == 401
        response = client.post(
            "/api/v1/configuration/validate",
            json=payload,
            headers={"Authorization": "Bearer test-ops"},
        )
        assert response.status_code == 200
        value = response.json()
        assert value["applied"] is False
        assert "100000000000000001" in value["document"]
        assert value["execution_eligible"] is False
        assert client.get("/api/v1/runs").json()["items"] == []


def test_all_ui_templates_use_the_same_validators(tmp_path):
    config = make_server_config(tmp_path)
    with TestClient(create_app(config, environ={"OASIS_SERVER_OPS_TOKEN": "test-ops"})) as client:
        templates = client.get("/api/v1/configuration").json()["templates"]
        for kind, document in templates.items():
            response = client.post(
                "/api/v1/configuration/validate",
                json={"kind": kind, "document": document},
                headers={"Authorization": "Bearer test-ops"},
            )
            assert response.status_code == 200
            result = response.json()["document"]
            if kind == "rpc":
                assert tomllib.loads(result)["chains"][0]["rpc_url_env"] == "BASE_RPC_PRIMARY"
            elif kind == "lp":
                assert parse_policy(result.encode())["chain_id"] == 46630
            elif kind == "archive":
                assert ArchiveConfig.from_json(result).start_block == 51907410
            elif kind == "market":
                assert Config.from_json(result).market is not None
            elif kind == "spot":
                assert AgentConfig.model_validate_json(result).chain_id == 8453
            else:
                assert ExecutionConfig.from_json(result).chain_id == 8453


def test_stop_requires_token_and_unknown_runs_fail_closed(tmp_path):
    config = replace(
        make_server_config(tmp_path),
        shadow_runner=ServerShadowRunnerConfig(
            (sys.executable, "-c", "import time; time.sleep(30)"), 60, 1
        ),
    )
    auth = {"Authorization": "Bearer test-ops"}
    with TestClient(create_app(config, environ={"OASIS_SERVER_OPS_TOKEN": "test-ops"})) as client:
        assert client.post("/api/v1/runs/missing/stop").status_code == 401
        assert client.post("/api/v1/runs/missing/stop", headers=auth).status_code == 404
        run = client.post("/api/v1/runs/shadow", headers=auth).json()
        result = client.post(f"/api/v1/runs/{run['run_id']}/stop", headers=auth)
        assert result.status_code == 200 and result.json()["status"] in {"stopping", "stopped"}


def test_launch_v1_encoding_is_stable_and_v2_binds_token_metadata():
    legacy = ExecutionConfig.from_json("{}").model_dump(mode="json", by_alias=True)
    assert "token_decimals" not in legacy
    with pytest.raises(ValueError):
        ExecutionConfig.from_json('{"token_decimals":6}')
    config = ExecutionConfig.from_json(
        '{"schema":"oasis.launch-execution-config.v2",'
        '"token_decimals":6,"reference_supply":1234567}'
    )
    document = config.model_dump_json(by_alias=True)
    assert '"token_decimals":6' in document
    assert ExecutionConfig.from_json(document).reference_supply == 1234567
