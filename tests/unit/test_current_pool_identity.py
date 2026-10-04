"""Finalized resolution and complete exact-wire identity fail closed."""

import json
from dataclasses import replace

import pytest
from eth_abi.abi import encode

from oasis_agent.evidence.exact_capture import decode_header
from oasis_agent.farming.current_pool_identity import CurrentIdentityPlan, replay_current_identity
from oasis_agent.models.common import ZERO_ADDRESS, canonical_json
from oasis_agent.models.v4 import PoolKey
from oasis_agent.rpc.transport import RpcExchange

MANAGER, VIEW, TOKEN = ("0x" + c * 40 for c in ("1", "2", "3"))
HEADER = {
    "number": "0x64",
    "hash": "0x" + "ab" * 32,
    "parentHash": "0x" + "cd" * 32,
    "timestamp": "0x3e8",
}


def plan():
    return CurrentIdentityPlan(
        1,
        MANAGER,
        VIEW,
        (TOKEN,),
        (PoolKey(ZERO_ADDRESS, TOKEN, 500, 10, ZERO_ADDRESS),),
        ("public_a", "public_b"),
        1800,
    )


def pair(p):
    h = decode_header(HEADER, chain_id=1)
    methods = [
        ("eth_chainId", []),
        ("eth_getBlockByNumber", ["finalized", False]),
        *p.state_requests(h),
        ("eth_getBlockByNumber", ["0x64", False]),
    ]
    results = [
        "0x1",
        HEADER,
        "0x6000",
        "0x6000",
        "0x6000",
        "0x" + encode(["uint8"], [18]).hex(),
        "0x" + encode(["address"], [MANAGER]).hex(),
        "0x" + encode(["uint160", "int24", "uint24", "uint24"], [1 << 96, 0, 0, 500]).hex(),
        "0x" + encode(["uint128"], [42]).hex(),
        HEADER,
    ]
    ex = tuple(
        RpcExchange(
            m,
            i + 1,
            canonical_json({"jsonrpc": "2.0", "id": i + 1, "method": m, "params": ps}),
            canonical_json({"jsonrpc": "2.0", "id": i + 1, "result": v}),
            1,
        )
        for i, ((m, ps), v) in enumerate(zip(methods, results, strict=True))
    )
    return ((p.providers[0], ex), (p.providers[1], ex))


def mutate(captures, index, change, *, request=False, side=0):
    source, ex = captures[side]
    items = list(ex)
    field = "request_payload" if request else "response_payload"
    payload = json.loads(getattr(items[index], field))
    change(payload)
    items[index] = replace(items[index], **{field: canonical_json(payload)})
    result = list(captures)
    result[side] = (source, tuple(items))
    return tuple(result)


def test_identity_is_observation_only():
    p = plan()
    result = replay_current_identity(p, pair(p), captured_at_seconds=1100)
    assert result["runtime_and_state_quorum"] is True
    assert result["token_decimals"] == {TOKEN: 18}
    assert result["pools"][0]["liquidity"] == 42
    assert result["runtimes"][2]["static_proxy_indicators"]["token_admitted"] is False
    assert result["net_pnl"] is None
    for flag in (
        "token_authority_verified",
        "deployed_source_reproduced",
        "target_admitted",
        "oos_accepted",
        "canary_accepted",
        "execution_eligible",
    ):
        assert result[flag] is False


@pytest.mark.parametrize(
    ("index", "change", "mutate_request"),
    [
        (1, lambda d: d["params"].__setitem__(0, "latest"), True),
        (2, lambda d: d["params"][1].update(requireCanonical=False), True),
        (2, lambda d: d["params"].__setitem__(1, "0x64"), True),
        (2, lambda d: d.update(result="0x"), False),
        (2, lambda d: d.update(result="0x6001"), False),
        (2, lambda d: d.update(id=True), False),
        (0, lambda d: d.update(result="0x2"), False),
        (5, lambda d: d.update(result="0x" + encode(["uint8"], [78]).hex()), False),
        (5, lambda d: d.update(result=d["result"] + "00" * 32), False),
        (6, lambda d: d.update(result="0x" + encode(["address"], [TOKEN]).hex()), False),
        (
            7,
            lambda d: d.update(
                result="0x" + encode(["uint160", "int24", "uint24", "uint24"], [0, 0, 0, 500]).hex()
            ),
            False,
        ),
        (
            7,
            lambda d: d.update(
                result="0x"
                + encode(["uint160", "int24", "uint24", "uint24"], [1 << 96, 0, 0, 3000]).hex()
            ),
            False,
        ),
        (8, lambda d: d.update(result="0x" + encode(["uint256"], [1 << 128]).hex()), False),
        (9, lambda d: d["result"].update(hash="0x" + "ef" * 32), False),
    ],
)
def test_refuses_malformed_or_disagreeing_inputs(index, change, mutate_request):
    p = plan()
    with pytest.raises((ValueError, TypeError)):
        replay_current_identity(
            p, mutate(pair(p), index, change, request=mutate_request), captured_at_seconds=1100
        )


@pytest.mark.parametrize("now", [999, 2801, False, 1100.0])
def test_explicit_head_freshness(now):
    p = plan()
    with pytest.raises(ValueError):
        replay_current_identity(p, pair(p), captured_at_seconds=now)


def test_missing_and_extra_reads_and_source_aliases():
    p = plan()
    c = pair(p)
    for changed in (
        (c[0], ("public_a", c[1][1])),
        ((c[0][0], c[0][1][:-1]), c[1]),
        ((c[0][0], c[0][1] + c[0][1][-1:]), c[1]),
    ):
        with pytest.raises(ValueError):
            replay_current_identity(p, changed, captured_at_seconds=1100)


def test_plan_cannot_weaken_scope():
    p = plan()
    for changed in (
        {"chain_id": True},
        {"max_head_age_seconds": 1801},
        {"providers": ("public_a", "public_a")},
        {"tokens": (VIEW,)},
        {"pools": p.pools * 2},
    ):
        with pytest.raises(ValueError):
            replace(p, **changed)
