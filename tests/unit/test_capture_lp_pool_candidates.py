import json
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts.capture_lp_pool_candidates import run, select_tokens


def test_corrupt_source_refused_before_network_or_output(tmp_path: Path) -> None:
    with patch("scripts.capture_lp_pool_candidates.okx_query") as query:
        with pytest.raises((ValueError, FileNotFoundError)):
            run(tmp_path, tmp_path / "out", tmp_path / "absent", 5)
        query.assert_not_called()
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("limit", [0, 11, True])
def test_selection_is_bounded(tmp_path: Path, limit: int) -> None:
    with pytest.raises(ValueError, match="tokens per chain"):
        select_tokens(tmp_path, limit)


@pytest.mark.parametrize("reflected_secret", [False, True])
def test_failure_halts_without_retaining_secret_or_retry(
    tmp_path: Path, reflected_secret: bool
) -> None:
    (tmp_path / "result.json").write_text("{}")
    response = {"code": "0" if reflected_secret else "429", "data": []}
    if reflected_secret:
        response["msg"] = "fixture-secret"
    with (
        patch("scripts.capture_lp_pool_candidates.select_tokens", return_value=((8453, "x"),)),
        patch(
            "scripts.capture_lp_pool_candidates.credentials", return_value={"key": "fixture-secret"}
        ),
        patch(
            "scripts.capture_lp_pool_candidates.okx_query",
            return_value=json.dumps(response).encode(),
        ) as query,
    ):
        run(tmp_path, tmp_path / "out", tmp_path / "unused", 1)
    assert query.call_count == 1
    assert not list((tmp_path / "out").glob("*.raw.json"))
    raw = (tmp_path / "out/report.json").read_bytes()
    assert b"fixture-secret" not in raw
    report = json.loads(raw)
    assert report["captures"][0]["status"] == "failed"
    assert report["execution_eligible"] is False
