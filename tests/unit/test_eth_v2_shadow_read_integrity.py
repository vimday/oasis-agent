"""Offline transport boundary checks for the Ethereum shadow evidence reader."""

import pytest

from oasis_agent.rpc.budget import RpcBudget
from scripts import run_eth_v2_forward_shadow as capture

CHAIN_ID_CALL = [("eth_chainId", [])]


def retain_batch(directory, response, *, started=200, curl_status=0):
    request = capture.canonical(
        [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "eth_chainId",
                "params": [],
            }
        ]
    )
    metadata = capture.canonical(
        {
            "source": capture.ENDPOINT,
            "request_sha256": capture.digest(request),
            "response_sha256": capture.digest(response),
            "reserved": True,
            "started_ms": started,
            "received_ms": started + 10,
            "latency_ns": 10_000_000,
            "curl_status": curl_status,
            "http_status": "200",
        }
    )
    (directory / "01.request.json").write_bytes(request)
    (directory / "01.response.json").write_bytes(response)
    (directory / "01.meta.json").write_bytes(metadata)


def test_experiment_cannot_reserve_or_replay_a_send_method(tmp_path):
    reads = capture.Reads(tmp_path)
    with pytest.raises(ValueError, match="method outside the read-only experiment"):
        reads.batch([("eth_sendRawTransaction", ["0x00"])])
    assert reads.http == reads.logical == 0
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (b'[{"id":1,"jsonrpc":"2.0","error":null}]', "malformed RPC error envelope"),
        (b'[{"id":1,"jsonrpc":"2.0","error":{"code":false}}]', "malformed RPC error envelope"),
        (b'[{"id":1,"jsonrpc":"2.0","result":NaN}]', "nonstandard JSON number"),
        (b'[["error"]]', "malformed RPC response envelope"),
        (
            b'[{"id":1,"jsonrpc":"2.0","error":{"code":-32601,"message":"method unavailable"}}]',
            "RPC stopped.*-32601",
        ),
    ],
)
def test_invalid_or_refused_read_stops_with_a_controlled_reason(tmp_path, body, message):
    retain_batch(tmp_path, body)
    reads = capture.Reads(tmp_path)
    with pytest.raises(ValueError, match=message):
        reads.batch(CHAIN_ID_CALL)
    assert reads.http == 1
    assert reads.reserved_ms == [200]


def test_interrupted_transport_keeps_the_reserved_attempt(tmp_path):
    retain_batch(tmp_path, b"", curl_status=-15)
    reads = capture.Reads(tmp_path)
    with pytest.raises(ValueError, match=r"transport stopped.*curl -15"):
        reads.batch(CHAIN_ID_CALL)
    assert reads.reserved_ms == [200]


def test_replay_cross_checks_capture_time_against_original_ledger(tmp_path):
    response = b'[{"id":1,"jsonrpc":"2.0","result":"0x1"}]'
    retain_batch(tmp_path, response)
    ledger_path = tmp_path / "budget.sqlite3"
    with RpcBudget(ledger_path, capture.POLICY) as ledger:
        assert ledger.reserve(capture.SOURCE, now_ms=200) == 0
    with RpcBudget(ledger_path, capture.POLICY, read_only=True) as ledger:
        matching = capture.Reads(tmp_path)
        assert matching.batch(CHAIN_ID_CALL)[0] == ["0x1"]
        matching.verify_budget(ledger)

        retain_batch(tmp_path, response, started=201)
        shifted = capture.Reads(tmp_path)
        assert shifted.batch(CHAIN_ID_CALL)[0] == ["0x1"]
        with pytest.raises(ValueError, match="clocks disagree with original reservations"):
            shifted.verify_budget(ledger)


def test_router_validation_checks_intermediates_even_with_expected_final_output():
    directions = [(2_500_000, 5_000_000), (5_000_000, 2_500_000)]
    initial = 10_000
    first_out = capture.bound_and_quote(directions[:1], initial)[0]
    final_out = capture.bound_and_quote(directions, initial)[0]
    raw = "0x" + "".join(map(capture.word, (32, 3, initial, first_out, final_out)))
    assert capture.checked_router_amounts(raw, directions, initial) == [
        initial,
        first_out,
        final_out,
    ]

    altered = "0x" + "".join(map(capture.word, (32, 3, initial, first_out + 1, final_out)))
    with pytest.raises(ValueError, match="model disagree at hop 1"):
        capture.checked_router_amounts(altered, directions, initial)
