"""Actual urllib processing over in-memory HTTPS responses; no external transport."""

import io
import json
import urllib.request
from email.message import Message
from urllib.response import addinfourl

import pytest
from eth_hash.auto import keccak

from oasis_agent.live.chain import (
    ChainError,
    FeePolicy,
    Rpc,
    RpcRejected,
    SubmissionUnknown,
    send_transaction,
)
from oasis_agent.models.common import canonical_json
from tests.unit.test_live_intent import EphemeralSigner
from tests.unit.test_live_lp import CONFIG

URL = "https://approved.invalid/?key=synthetic-query-credential"
RAW = b"synthetic-non-transaction"
DIGEST = "0x" + keccak(RAW).hex()


def memory_https(monkeypatch, reply):
    calls = []
    original = urllib.request.build_opener

    class Wire(urllib.request.BaseHandler):
        handler_order = 0

        def https_open(self, request):
            calls.append(request)
            code, headers, body = reply(request, len(calls))
            message = Message()
            for key, value in headers.items():
                message[key] = value
            response = addinfourl(io.BytesIO(body), message, request.full_url, code)
            response.msg = "synthetic response"
            return response

    monkeypatch.setattr(
        urllib.request, "build_opener", lambda *handlers: original(Wire(), *handlers)
    )
    # Cover the preceding implementation's urlopen too, without ever opening a
    # socket. Its actual default redirect/error processors remain in this stack.
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda request, timeout, context: original(
            Wire(), urllib.request.HTTPSHandler(context=context)
        ).open(request, timeout=timeout),
    )
    return calls


def success(request, result=DIGEST):
    request_id = json.loads(request.data)["id"] if request.data is not None else 1
    return canonical_json({"jsonrpc": "2.0", "id": request_id, "result": result})


@pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
def test_signed_post_never_follows_a_redirect(monkeypatch, code):
    calls = memory_https(
        monkeypatch,
        lambda request, count: (
            (code, {"Location": "https://unapproved.invalid/"}, b"")
            if count == 1
            else (200, {}, success(request))
        ),
    )
    with pytest.raises(SubmissionUnknown):
        Rpc(URL).send_raw(RAW)
    assert len(calls) == 1 and calls[0].get_method() == "POST"
    assert calls[0].full_url == URL


@pytest.mark.parametrize(
    "fault", ["id", "bool_id", "float_id", "version", "missing", "ambiguous", "duplicate", "array"]
)
def test_signed_post_requires_one_unambiguous_matching_rpc_response(monkeypatch, fault):
    response = {"jsonrpc": "2.0", "id": 1, "result": DIGEST}
    if fault in {"id", "bool_id", "float_id"}:
        response["id"] = {"id": 2, "bool_id": True, "float_id": 1.0}[fault]
    elif fault == "version":
        response["jsonrpc"] = "1.0"
    elif fault == "missing":
        del response["result"]
    elif fault == "ambiguous":
        response["error"] = {"code": -32000, "message": "synthetic-query-credential"}
    raw = (
        b'{"jsonrpc":"2.0","id":2,"id":1,"result":"' + DIGEST.encode() + b'"}'
        if fault == "duplicate"
        else b"[]"
        if fault == "array"
        else canonical_json(response)
    )
    calls = memory_https(monkeypatch, lambda *_: (200, {}, raw))
    with pytest.raises(SubmissionUnknown) as error:
        Rpc(URL).send_raw(RAW)
    assert "credential" not in str(error.value)
    assert len(calls) == 1


def test_direct_rpc_redacts_provider_rejection(monkeypatch):
    raw = canonical_json(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {"code": -32000, "message": URL, "data": "synthetic-query-credential"},
        }
    )
    calls = memory_https(monkeypatch, lambda *_: (200, {}, raw))
    with pytest.raises(RpcRejected) as error:
        Rpc(URL, retries=1).call("eth_chainId", [])
    assert "credential" not in str(error.value) and "approved.invalid" not in str(error.value)
    assert len(calls) == 1


def test_signed_post_has_a_bounded_response(monkeypatch):
    raw = b" " * 1_000_001 + canonical_json({"jsonrpc": "2.0", "id": 1, "result": DIGEST})
    calls = memory_https(monkeypatch, lambda *_: (200, {}, raw))
    with pytest.raises(SubmissionUnknown):
        Rpc(URL).send_raw(RAW)
    assert len(calls) == 1


@pytest.mark.parametrize("result", ["0x" + "11" * 32, "synthetic-query-credential"])
def test_signed_post_acknowledges_only_the_hash_of_its_actual_bytes(monkeypatch, result):
    calls = memory_https(monkeypatch, lambda request, _: (200, {}, success(request, result)))
    with pytest.raises(SubmissionUnknown) as error:
        Rpc(URL).send_raw(RAW)
    assert "credential" not in str(error.value) and len(calls) == 1


def test_signed_post_accepts_the_matching_hash_once(monkeypatch):
    calls = memory_https(monkeypatch, lambda request, _: (200, {}, success(request)))
    assert Rpc(URL).send_raw(RAW) == DIGEST
    assert len(calls) == 1


@pytest.mark.parametrize("fault", ["id", "array", "redirect", "provider_error"])
def test_uncertain_post_preserves_commitment_and_never_posts_or_polls_again(monkeypatch, fault):
    def reply(request, count):
        if fault == "redirect" and count == 1:
            return 302, {"Location": "https://unapproved.invalid/"}, b""
        if fault == "array":
            return 200, {}, b"[]"
        if fault == "provider_error":
            return (
                200,
                {},
                canonical_json(
                    {"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": URL}}
                ),
            )
        request_id = json.loads(request.data)["id"] if request.data is not None else 1
        return 200, {}, canonical_json({"jsonrpc": "2.0", "id": request_id + 1, "result": DIGEST})

    calls = memory_https(monkeypatch, reply)

    class PreparedRpc(Rpc):
        def uint(self, method, params):
            return 21000

        def block(self):
            return {"baseFeePerGas": "0xa"}

        def nonce(self, account, block="pending"):
            return 3

        def receipt(self, transaction_hash):
            pytest.fail("an uncertain post must return to independent pending reconciliation")

    published = []
    with pytest.raises(SubmissionUnknown) as error:
        send_transaction(
            PreparedRpc(URL),
            EphemeralSigner(),
            chain_id=8453,
            to=CONFIG.position_manager,
            data=bytes.fromhex("12345678"),
            value=0,
            fees=FeePolicy(100, 2),
            on_signed=published.append,
        )
    assert "credential" not in str(error.value)
    assert len(calls) == len(published) == 1


@pytest.mark.parametrize(
    "value", [True, 1, 1.0, None, "-0x1", "2a", "0x00", "synthetic-query-credential"]
)
def test_quantity_reads_reject_noncanonical_values_without_echoing_them(monkeypatch, value):
    calls = memory_https(monkeypatch, lambda request, _: (200, {}, success(request, value)))
    with pytest.raises(ChainError) as error:
        Rpc(URL, retries=1).uint("eth_getBalance", [CONFIG.account, "latest"])
    assert "credential" not in str(error.value) and len(calls) == 1


@pytest.mark.parametrize("value", [[], "synthetic-query-credential", False])
def test_malformed_receipts_are_not_absent_receipts(monkeypatch, value):
    calls = memory_https(monkeypatch, lambda request, _: (200, {}, success(request, value)))
    with pytest.raises(ChainError) as error:
        Rpc(URL, retries=1).receipt(DIGEST)
    assert "credential" not in str(error.value) and len(calls) == 1


def test_zero_quantity_and_explicit_null_receipt_remain_distinct(monkeypatch):
    values = ["0x0", None, {"status": "0x1"}]
    calls = memory_https(
        monkeypatch, lambda request, count: (200, {}, success(request, values[count - 1]))
    )
    rpc = Rpc(URL, retries=1)
    assert rpc.uint("eth_getBalance", [CONFIG.account, "latest"]) == 0
    assert rpc.receipt(DIGEST) is None
    assert rpc.receipt(DIGEST) == {"status": "0x1"}
    assert len(calls) == 3
