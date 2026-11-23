"""Exercise the Base same-provider head comparison and independent witness offline."""

import asyncio
import sqlite3
from pathlib import Path

import httpx
import pytest

from oasis_agent.evidence.base_head_compare import (
    POLL_ARM,
    WS_ARM,
    HeadPlan,
    HeadWindow,
    decode_head,
)
from oasis_agent.models.common import canonical_json, sha256_hex
from oasis_agent.rpc.protocol import strict_json_object
from scripts import compare_base_head_delivery as cli


def _hash(height: int) -> str:
    return "0x" + sha256_hex(str(height).encode())


def _header(height: int, *, hint: bool = False) -> dict[str, str]:
    timestamp = 1_795_443_992_000 + (height - 100) * 200
    value = {
        "number": hex(height),
        "hash": _hash(height),
        "parentHash": _hash(height - 1),
        "stateRoot": _hash(height + 1000),
        "timestamp": hex(timestamp // 1000),
    }
    if not hint:
        value["timestampMs"] = hex(timestamp)
    return value


class Socket:
    def __init__(self, notifications: tuple[dict[str, str], ...]) -> None:
        self.acks: list[bytes] = []
        self.notifications = iter(notifications)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def send(self, raw: bytes, *, text: bool) -> None:
        req = strict_json_object(raw)
        assert text
        assert req["method"] in ("eth_chainId", "eth_subscribe")
        self.acks.append(
            canonical_json(
                {
                    "jsonrpc": "2.0",
                    "id": req["id"],
                    "result": "0x2105" if req["method"] == "eth_chainId" else "0xabc",
                }
            )
        )

    async def recv(self, *, decode: bool) -> bytes:
        assert not decode
        if self.acks:
            return self.acks.pop(0)
        row = next(self.notifications, None)
        if row is None:
            await asyncio.sleep(2)
            raise AssertionError("window should time out before another frame")
        await asyncio.sleep(0.03)
        return canonical_json(
            {
                "jsonrpc": "2.0",
                "method": "eth_subscription",
                "params": {"subscription": "0xabc", "result": row},
            }
        )


def test_same_hash_capture_replay_and_missed_children(tmp_path: Path) -> None:
    poll = iter((100, 103))

    async def transport(request: httpx.Request) -> httpx.Response:
        data = strict_json_object(request.content)
        if data["method"] == "eth_chainId":
            body = "0x2105"
        elif data["params"] == ["latest", False]:
            assert request.url.host == "base-rpc.publicnode.com"
            body = _header(next(poll))
        else:
            assert request.url.host == "mainnet.base.org"
            body = _header(int(data["params"][0], 16))
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": data["id"], "result": body})

    root = tmp_path / "run"

    async def collect():
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            return await cli.capture(
                root,
                HeadPlan(2, 500, 8),
                http=client,
                socket_factory=lambda: Socket(
                    tuple(_header(n, hint=True) for n in (100, 101, 101, 102, 103))
                ),
            )

    result = asyncio.run(collect())
    assert result == cli.replay(root)
    assert result["height_denominator"] == 4
    assert result["direct_child_denominator"] == 3
    assert result["arms"][POLL_ARM]["missing"] == [101, 102]
    assert result["arms"][POLL_ARM]["direct_children"] == 0
    assert result["arms"][WS_ARM]["direct_children"] == 3
    assert result["arms"][WS_ARM]["exact_timestamp_ms"] == 0
    assert result["duplicates"][WS_ARM] == 1
    assert [row["hash"] for row in result["paired"]] == [_hash(100), _hash(103)]
    assert result["paired"][0]["poll_minus_new_heads_ns"] < 0
    assert result["paired"][1]["poll_minus_new_heads_ns"] > 0
    assert result["rpc_reservations"] == {POLL_ARM: 3, WS_ARM: 3, "witness": 5}

    with sqlite3.connect(root / "wire.sqlite3") as db:
        db.execute("UPDATE raw SET wall_ns = wall_ns + 1 WHERE seq = 1")
    with pytest.raises(ValueError, match="summary_changed"):
        cli.replay(root)


def test_first_http_refusal_preserves_raw_and_spends_once(tmp_path: Path) -> None:
    async def transport(request: httpx.Request) -> httpx.Response:
        message = strict_json_object(request.content)
        if message["method"] == "eth_chainId":
            return httpx.Response(
                200, json={"jsonrpc": "2.0", "id": message["id"], "result": "0x2105"}
            )
        assert message["params"] == ["latest", False]
        return httpx.Response(429, content=b"fixed refusal payload")

    root = tmp_path / "run"

    async def collect():
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            return await cli.capture(
                root,
                HeadPlan(2, 500, 8),
                http=client,
                socket_factory=lambda: Socket(()),
            )

    result = asyncio.run(collect())
    assert result == cli.replay(root)
    assert result["failure"] == {"lane": POLL_ARM, "reason": "http_429"}
    assert result["rpc_reservations"] == {WS_ARM: 3, POLL_ARM: 2, "witness": 1}
    assert (root / "STOP").read_bytes() == (root / "summary.json").read_bytes()
    with sqlite3.connect(root / "wire.sqlite3") as db:
        (payload,) = db.execute(
            "SELECT payload FROM raw WHERE role LIKE 'response-%-429'"
        ).fetchone()
    assert payload == b"fixed refusal payload"


@pytest.mark.parametrize("kind", ["hash", "parent"])
def test_conflicting_heads_are_fatal(kind: str) -> None:
    window = HeadWindow()
    window.observe(WS_ARM, decode_head(_header(100), hint=True), 100)
    bad = _header(100 if kind == "hash" else 101)
    field = "hash" if kind == "hash" else "parentHash"
    bad[field] = _hash(777)
    with pytest.raises(ValueError, match="same_height" if kind == "hash" else "adjacent_parent"):
        window.observe(POLL_ARM, decode_head(bad), 200)
