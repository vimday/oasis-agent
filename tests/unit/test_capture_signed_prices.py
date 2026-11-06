"""Gateway limits and publication using authentic historical signer packages."""

import asyncio
import json
import sys

import httpx
import pytest

from oasis_agent.models.common import canonical_json, sha256_hex
from scripts import capture_signed_prices as cli
from tests.unit.test_signed_prices import FIXTURE, TIMESTAMP

CREDENTIAL = "example-redstone-access"


class Body(httpx.AsyncByteStream):
    def __init__(self, raw):
        self.raw = raw

    async def __aiter__(self):
        yield self.raw


def test_only_the_expected_gateway_and_feeds_are_requested():
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, stream=Body(FIXTURE.read_bytes()))

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(respond), follow_redirects=False
        ) as client:
            return await cli.download(client, CREDENTIAL)

    assert asyncio.run(run()) == FIXTURE.read_bytes()
    (request,) = requests
    assert request.method == "GET"
    assert request.url.copy_with(query=None) == httpx.URL(cli.URL)
    assert list(request.url.params.multi_items()) == [("dataFeedIds", "ETH,USDC")]
    assert request.headers["x-api-key"] == CREDENTIAL
    assert request.headers["accept-encoding"] == "identity"


@pytest.mark.parametrize(
    "status,encoding,size",
    [(302, "identity", 0), (200, "gzip", 0), (200, "identity", cli.MAX_RAW_BYTES + 1)],
)
def test_gateway_rejects_redirect_encoding_and_oversize(status, encoding, size):
    def respond(_):
        return httpx.Response(
            status, headers={"Content-Encoding": encoding}, stream=Body(bytes(size))
        )

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            return await cli.download(client, CREDENTIAL)

    with pytest.raises(ValueError):
        asyncio.run(run())


@pytest.mark.parametrize("mode", ["valid", "missing", "stale", "reflected", "http_error"])
def test_cli_retains_only_validated_unexpired_non_reflected_packages(
    tmp_path, monkeypatch, capsys, mode
):
    path = tmp_path / "attempt"
    monkeypatch.setattr(sys, "argv", ["capture", "--out", str(path)])
    monkeypatch.setattr(cli, "now_ms", lambda: TIMESTAMP + (1_800_001 if mode == "stale" else 0))
    monkeypatch.setenv(cli.KEY_ENV, CREDENTIAL)
    if mode == "missing":
        monkeypatch.delenv(cli.KEY_ENV)

    calls = []

    async def fetch(key):
        calls.append(key)
        if mode == "http_error":
            raise cli.GatewayRefusal(403)
        if mode == "reflected":
            data = json.loads(FIXTURE.read_bytes())
            data["ETH"][0]["dataPoints"][0]["metadata"] = {"detail": CREDENTIAL}
            return json.dumps(data).replace(CREDENTIAL, r"\u0065" + CREDENTIAL[1:]).encode()
        return FIXTURE.read_bytes()

    monkeypatch.setattr(cli, "fetch", fetch)
    assert cli.main() == (0 if mode == "valid" else 3)
    assert calls == ([] if mode == "missing" else [CREDENTIAL])
    raw = (path / "report.json").read_bytes()
    report = json.loads(raw)
    assert CREDENTIAL.encode() not in raw and CREDENTIAL not in capsys.readouterr().out
    assert report.pop("evidence_hash") == sha256_hex(canonical_json(report))
    assert report["passed"] is (mode == "valid") and report["execution_eligible"] is False
    assert report["attempts"] == len(calls)
    if mode == "valid":
        assert (path / "prices.json").read_bytes() == FIXTURE.read_bytes()
    else:
        assert not (path / "prices.json").exists()
    if mode == "http_error":
        assert report["http_status"] == 403
