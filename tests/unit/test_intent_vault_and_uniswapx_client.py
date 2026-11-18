"""Unit tests for BaseTime probe, UniswapX orderbook client, and OasisIntentVault ABI."""

import json
import math
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import pytest
from eth_hash.auto import keccak

from oasis_agent.errors import RpcError
from oasis_agent.evidence.base_denim import (
    BASE_CHAIN_ID,
    BASE_TIME,
    DENIM_ACTIVATION_TIMESTAMP,
    TIMESTAMP_MILLIS_PART_SELECTOR,
    TIMESTAMP_MS_SELECTOR,
)
from oasis_agent.evidence.base_time_probe import (
    BaseTimeVerificationReceipt,
    verify_base_time_predeploy,
)
from oasis_agent.intents import (
    ALLOWED_HOSTS,
    UNISWAPX_ORDERS_URL,
    DutchV3Order,
    UniswapXClientError,
    UniswapXOrderbookClient,
    UniswapXPollResult,
    evaluate_dutch_v3_fill,
    parse_uniswapx_v2_orders,
    resolve_dutch_v3_amounts,
)
from oasis_agent.intents.dutch_v3 import DEPRECATED_PRIORITY_REACTOR_BASE
from oasis_agent.models.common import sha256_hex

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPILED_VAULT_ABI = (
    REPO_ROOT / "contracts" / "out" / "OasisIntentVault.sol" / "OasisIntentVault.json"
)

REACTOR_ADDR = "0x" + "33" * 20
SWAPPER_ADDR = "0x" + "44" * 20
EXCLUSIVE_ADDR = "0x" + "55" * 20
FILLER_ADDR = "0x" + "66" * 20
WETH_ADDR = "0x" + "77" * 20
USDC_ADDR = "0x" + "88" * 20
BLOCK_HASH = "0x" + "ab" * 32
ORDER_HASH = "0x" + "cd" * 32


def _canonical_param_type(param: dict[str, Any]) -> str:
    raw_type: str = param["type"]
    if not raw_type.startswith("tuple"):
        return raw_type
    suffix = raw_type[len("tuple") :]
    inner = ",".join(_canonical_param_type(c) for c in param["components"])
    return f"({inner}){suffix}"


def _encode_uint256_word(value: int) -> str:
    return "0x" + format(value, "064x")


def _sample_uniswapx_payload(
    *,
    chain_id: int = BASE_CHAIN_ID,
    reactor: str = REACTOR_ADDR,
    order_type: str = "Dutch_V3",
    order_status: str = "open",
) -> bytes:
    payload = {
        "orders": [
            {
                "type": order_type,
                "orderStatus": order_status,
                "chainId": chain_id,
                "orderHash": ORDER_HASH,
                "reactor": reactor,
                "swapper": SWAPPER_ADDR,
                "nonce": "42",
                "decayStartBlock": 1_000,
                "decayEndBlock": 1_030,
                "deadlineBlock": 1_040,
                "exclusivityEndBlock": 1_010,
                "exclusiveFiller": EXCLUSIVE_ADDR,
                "exclusivityOverrideBps": 50,
                "input": {
                    "token": WETH_ADDR,
                    "startAmount": str(10**18),
                    "endAmount": str(10**18),
                },
                "outputs": [
                    {
                        "token": USDC_ADDR,
                        "startAmount": "2520000000",
                        "endAmount": "2480000000",
                        "recipient": SWAPPER_ADDR,
                        "curve": {
                            "relativeBlocks": [10, 30],
                            "relativeAmounts": ["24000000", "40000000"],
                        },
                    }
                ],
            }
        ],
        "cursor": "",
    }
    return json.dumps(payload).encode("utf-8")


# ---------------------------------------------------------------------------
# BaseTime predeploy verifier tests
# ---------------------------------------------------------------------------


def test_verify_base_time_predeploy_happy_path_and_pinned_batch() -> None:
    block_number = 57_794_191
    header_ts = DENIM_ACTIVATION_TIMESTAMP + 12
    ts_ms = header_ts * 1000 + 800
    seen_requests: list[tuple[str, bytes, float]] = []

    header_resp = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "number": hex(block_number),
                "hash": BLOCK_HASH,
                "timestamp": hex(header_ts),
                "timestampMs": hex(ts_ms),
            },
        }
    ).encode("utf-8")
    # Deliberately return batch items in reverse order to verify ID-based correlation.
    batch_resp = json.dumps(
        [
            {"jsonrpc": "2.0", "id": 3, "result": _encode_uint256_word(800)},
            {"jsonrpc": "2.0", "id": 2, "result": _encode_uint256_word(ts_ms)},
        ]
    ).encode("utf-8")

    def fake_transport(url: str, payload: bytes, timeout_s: float) -> bytes:
        seen_requests.append((url, payload, timeout_s))
        if len(seen_requests) == 1:
            return header_resp
        return batch_resp

    receipt = verify_base_time_predeploy(
        "https://rpc.example.invalid/v1/secret-key-path?token=redacted",
        block_tag="latest",
        timeout_s=5.0,
        max_requests=4,
        transport=fake_transport,
    )

    assert isinstance(receipt, BaseTimeVerificationReceipt)
    assert receipt.chain_id == BASE_CHAIN_ID
    assert receipt.predeploy_address == BASE_TIME
    assert receipt.block_number == block_number
    assert receipt.block_hash == BLOCK_HASH
    assert receipt.header_timestamp == header_ts
    assert receipt.header_timestamp_ms == ts_ms
    assert receipt.predeploy_timestamp_ms == ts_ms
    assert receipt.predeploy_millis_part == 800
    assert receipt.requests_used == 3
    assert receipt.max_requests == 4
    assert receipt.rpc_origin == "https://rpc.example.invalid"
    assert "secret-key-path" not in receipt.rpc_origin
    assert "redacted" not in receipt.rpc_origin

    assert len(seen_requests) == 2
    req1 = json.loads(seen_requests[0][1])
    assert req1 == {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_getBlockByNumber",
        "params": ["latest", False],
    }
    req2 = json.loads(seen_requests[1][1])
    assert req2 == [
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "eth_call",
            "params": [{"to": BASE_TIME, "data": TIMESTAMP_MS_SELECTOR}, hex(block_number)],
        },
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "eth_call",
            "params": [
                {"to": BASE_TIME, "data": TIMESTAMP_MILLIS_PART_SELECTOR},
                hex(block_number),
            ],
        },
    ]
    assert receipt.header_request_sha256 == sha256_hex(seen_requests[0][1])
    assert receipt.header_response_sha256 == sha256_hex(header_resp)
    assert receipt.batch_request_sha256 == sha256_hex(seen_requests[1][1])
    assert receipt.batch_response_sha256 == sha256_hex(batch_resp)

    with pytest.raises(FrozenInstanceError):
        receipt.requests_used = 1  # type: ignore[misc]


def test_verify_base_time_predeploy_pre_denim_header_and_cross_check_failures() -> None:
    pre_denim_ts = DENIM_ACTIVATION_TIMESTAMP - 100
    pre_denim_ms = pre_denim_ts * 1000 + 400

    def pre_denim_transport(_: str, payload: bytes, __: float) -> bytes:
        decoded = json.loads(payload)
        if isinstance(decoded, dict):
            return json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {
                        "number": "0x100",
                        "hash": BLOCK_HASH,
                        "timestamp": hex(pre_denim_ts),
                    },
                }
            ).encode("utf-8")
        return json.dumps(
            [
                {"jsonrpc": "2.0", "id": 2, "result": _encode_uint256_word(pre_denim_ms)},
                {"jsonrpc": "2.0", "id": 3, "result": _encode_uint256_word(400)},
            ]
        ).encode("utf-8")

    receipt = verify_base_time_predeploy(
        "https://rpc.example.invalid",
        block_tag="0x100",
        transport=pre_denim_transport,
    )
    assert receipt.header_timestamp_ms is None
    assert receipt.predeploy_timestamp_ms == pre_denim_ms
    assert receipt.predeploy_millis_part == 400

    # Mismatch between header timestampMs (600ms) and BaseTime timestampMs (800ms) fails closed.
    post_ts = DENIM_ACTIVATION_TIMESTAMP + 5

    def mismatch_transport(_: str, payload: bytes, __: float) -> bytes:
        decoded = json.loads(payload)
        if isinstance(decoded, dict):
            return json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {
                        "number": "0x200",
                        "hash": BLOCK_HASH,
                        "timestamp": hex(post_ts),
                        "timestampMs": hex(post_ts * 1000 + 600),
                    },
                }
            ).encode("utf-8")
        return json.dumps(
            [
                {"jsonrpc": "2.0", "id": 2, "result": _encode_uint256_word(post_ts * 1000 + 800)},
                {"jsonrpc": "2.0", "id": 3, "result": _encode_uint256_word(800)},
            ]
        ).encode("utf-8")

    with pytest.raises(ValueError, match="base_time_header_timestamp_ms_mismatch"):
        verify_base_time_predeploy("https://rpc.example.invalid", transport=mismatch_transport)

    # Empty "0x" predeploy response fails closed.
    def empty_call_transport(_: str, payload: bytes, __: float) -> bytes:
        decoded = json.loads(payload)
        if isinstance(decoded, dict):
            return json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {
                        "number": "0x200",
                        "hash": BLOCK_HASH,
                        "timestamp": hex(post_ts),
                        "timestampMs": hex(post_ts * 1000 + 800),
                    },
                }
            ).encode("utf-8")
        return json.dumps(
            [
                {"jsonrpc": "2.0", "id": 2, "result": "0x"},
                {"jsonrpc": "2.0", "id": 3, "result": _encode_uint256_word(800)},
            ]
        ).encode("utf-8")

    with pytest.raises(ValueError, match="32 bytes"):
        verify_base_time_predeploy("https://rpc.example.invalid", transport=empty_call_transport)

    # Batch JSON-RPC error raises RpcError.
    def batch_error_transport(_: str, payload: bytes, __: float) -> bytes:
        decoded = json.loads(payload)
        if isinstance(decoded, dict):
            return json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {
                        "number": "0x200",
                        "hash": BLOCK_HASH,
                        "timestamp": hex(post_ts),
                        "timestampMs": hex(post_ts * 1000 + 800),
                    },
                }
            ).encode("utf-8")
        return json.dumps(
            [
                {"jsonrpc": "2.0", "id": 2, "error": {"code": -32000, "message": "reverted"}},
                {"jsonrpc": "2.0", "id": 3, "result": _encode_uint256_word(800)},
            ]
        ).encode("utf-8")

    with pytest.raises(RpcError, match="JSON-RPC error"):
        verify_base_time_predeploy("https://rpc.example.invalid", transport=batch_error_transport)


def test_verify_base_time_predeploy_budget_and_ssrf_guards() -> None:
    calls = 0

    def counting_transport(_: str, __: bytes, ___: float) -> bytes:
        nonlocal calls
        calls += 1
        ts = DENIM_ACTIVATION_TIMESTAMP + 1
        return json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "number": "0x1",
                    "hash": BLOCK_HASH,
                    "timestamp": hex(ts),
                    "timestampMs": hex(ts * 1000),
                },
            }
        ).encode("utf-8")

    # max_requests=2 allows call 1 (cost 1), then refuses BEFORE sending the 2-call batch.
    with pytest.raises(ValueError, match="budget exceeded"):
        verify_base_time_predeploy(
            "https://rpc.example.invalid", max_requests=2, transport=counting_transport
        )
    assert calls == 1

    # SSRF: http, credentials, private/loopback/link-local/localhost, bad block tags, bad timeouts.
    for bad_url in (
        "http://rpc.example.invalid",
        f"https://{'u'}:{'p'}@rpc.example.invalid",
        "https://user@rpc.example.invalid",
        "https:///path",
        "",
    ):
        with pytest.raises(ValueError):
            verify_base_time_predeploy(bad_url, transport=counting_transport)

    for private_url in (
        "https://127.0.0.1",
        "https://10.0.0.1",
        "https://169.254.169.254",
        "https://[::1]",
        "https://localhost:8545",
    ):
        with pytest.raises(ValueError):
            verify_base_time_predeploy(private_url)

    with pytest.raises(ValueError):
        verify_base_time_predeploy(
            "https://rpc.example.invalid", block_tag="pending", transport=counting_transport
        )
    with pytest.raises(ValueError):
        verify_base_time_predeploy(
            "https://rpc.example.invalid", timeout_s=math.nan, transport=counting_transport
        )


# ---------------------------------------------------------------------------
# UniswapXOrderbookClient & parse_uniswapx_v2_orders tests
# ---------------------------------------------------------------------------


def test_parse_uniswapx_v2_orders_empty_valid_and_malformed() -> None:
    assert parse_uniswapx_v2_orders(b'{"orders": []}') == ()

    raw = _sample_uniswapx_payload()
    orders = parse_uniswapx_v2_orders(raw, chain_id=BASE_CHAIN_ID)
    assert len(orders) == 1
    order = orders[0]
    assert isinstance(order, DutchV3Order)
    assert order.chain_id == BASE_CHAIN_ID
    assert order.order_hash == ORDER_HASH
    assert order.nonce == 42
    assert order.exclusivity_override_bps == 50

    # End-to-end integration with Dutch V3 resolution & shadow evaluator.
    resolved = resolve_dutch_v3_amounts(order, 1_005, FILLER_ADDR)
    assert resolved.output_amounts == (2_520_540_000,)
    decision = evaluate_dutch_v3_fill(
        order=order,
        fill_block=1_030,
        filler_address=FILLER_ADDR,
        input_token_decimals=18,
        input_hedge_bid_usd_micros=2_500_000_000,
        output_token_decimals=(6,),
        output_hedge_ask_usd_micros=(1_000_000,),
        hedge_fee_bps=2,
        hedge_slippage_bps=1,
        reactor_callback_gas_usd_micros=40_000,
        quote_latency_ms=150,
    )
    assert decision.approved is True

    # Deprecated PriorityOrderReactor on Base is rejected.
    with pytest.raises(ValueError, match="PriorityOrderReactor"):
        parse_uniswapx_v2_orders(_sample_uniswapx_payload(reactor=DEPRECATED_PRIORITY_REACTOR_BASE))

    # Wrong chainId, wrong order type, wrong status, duplicate JSON keys, missing orders rejected.
    with pytest.raises(ValueError, match="chainId"):
        parse_uniswapx_v2_orders(_sample_uniswapx_payload(chain_id=1))
    with pytest.raises(ValueError, match="order type"):
        parse_uniswapx_v2_orders(_sample_uniswapx_payload(order_type="Dutch_V2"))
    with pytest.raises(ValueError, match="orderStatus"):
        parse_uniswapx_v2_orders(_sample_uniswapx_payload(order_status="expired"))
    with pytest.raises(ValueError, match="duplicate"):
        parse_uniswapx_v2_orders(b'{"orders": [], "orders": []}')
    with pytest.raises(ValueError, match="orders"):
        parse_uniswapx_v2_orders(b'{"cursor": ""}')


def test_uniswapx_orderbook_client_ssrf_rate_limit_and_budget() -> None:
    assert "api.uniswap.org" in ALLOWED_HOSTS
    assert len(ALLOWED_HOSTS) == 1

    for bad_url in (
        "http://api.uniswap.org/v2/orders",
        "https://evil.example/v2/orders",
        "https://api.uniswap.org:8443/v2/orders",
        "https://user@api.uniswap.org/v2/orders",
    ):
        with pytest.raises(UniswapXClientError):
            UniswapXOrderbookClient(base_url=bad_url)

    with pytest.raises(ValueError, match="min_interval_s"):
        UniswapXOrderbookClient(min_interval_s=0.24)
    with pytest.raises(ValueError, match="min_interval_s"):
        UniswapXOrderbookClient(min_interval_s=True)  # type: ignore[arg-type]

    now_s = 100.0
    sleeps: list[float] = []
    seen_urls: list[str] = []
    payload = _sample_uniswapx_payload()

    def fake_clock() -> float:
        return now_s

    def fake_sleep(seconds: float) -> None:
        nonlocal now_s
        sleeps.append(seconds)
        now_s += seconds

    def fake_transport(url: str, _: float) -> bytes:
        seen_urls.append(url)
        return payload

    client = UniswapXOrderbookClient(
        base_url=UNISWAPX_ORDERS_URL,
        min_interval_s=0.5,
        max_requests=2,
        transport=fake_transport,
        clock=fake_clock,
        sleeper=fake_sleep,
    )

    res1 = client.poll_open_orders(limit=25, polled_at_ms=1_794_750_000_000)
    assert isinstance(res1, UniswapXPollResult)
    assert len(res1.orders) == 1
    assert res1.raw_sha256 == sha256_hex(payload)
    assert res1.polled_at_ms == 1_794_750_000_000
    assert "chainId=8453" in res1.request_url
    assert "orderType=Dutch_V3" in res1.request_url
    assert "orderStatus=open" in res1.request_url
    assert "limit=25" in res1.request_url
    assert sleeps == []
    assert client.requests_remaining == 1

    # Advance clock only 0.1s (< min_interval_s 0.5s) -> client sleeps 0.4s before 2nd poll.
    now_s += 0.1
    res2 = client.poll_open_orders(polled_at_ms=1_794_750_000_500)
    assert len(res2.orders) == 1
    assert len(sleeps) == 1
    assert sleeps[0] == pytest.approx(0.4)
    assert client.requests_remaining == 0

    # 3rd poll exceeds max_requests=2 without touching transport.
    with pytest.raises(UniswapXClientError, match="budget exhausted"):
        client.poll_open_orders(polled_at_ms=1_794_750_001_000)
    assert len(seen_urls) == 2


# ---------------------------------------------------------------------------
# Foundry compiled artifact ABI verification for OasisIntentVault
# ---------------------------------------------------------------------------


def test_compiled_oasis_intent_vault_abi_exposes_required_surface() -> None:
    artifact = json.loads(COMPILED_VAULT_ABI.read_text())
    abi = artifact["abi"]
    method_ids: dict[str, str] = artifact["methodIdentifiers"]

    required_functions = {
        "isValidSignature": "1626ba7e",
        "reactorCallback": None,
        "executeReactorFill": None,
        "rotateQuoteSigner": None,
        "pause": None,
        "unpause": None,
        "revokeDigest": None,
        "sweepERC20": None,
        "sweepNative": None,
    }

    for fn_name, expected_selector in required_functions.items():
        entry = next(
            (item for item in abi if item["type"] == "function" and item["name"] == fn_name),
            None,
        )
        assert entry is not None, f"missing function {fn_name} in OasisIntentVault ABI"
        compiled_sig = f"{fn_name}({','.join(_canonical_param_type(p) for p in entry['inputs'])})"
        computed_selector = keccak(compiled_sig.encode())[:4].hex()
        assert method_ids[compiled_sig] == computed_selector
        if expected_selector is not None:
            assert computed_selector == expected_selector

    event_names = {item["name"] for item in abi if item["type"] == "event"}
    assert {
        "EarlyAborted",
        "ReactorFillExecuted",
        "QuoteSignerRotated",
        "DigestRevoked",
        "ERC20Swept",
        "NativeSwept",
        "PausedSet",
    }.issubset(event_names)
