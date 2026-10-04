"""Raw grid/spot composition and conservative diagnostic capacity, synthetic only."""

import hashlib
import json
from dataclasses import asdict, replace
from fractions import Fraction

import pytest
from eth_abi.abi import decode, encode
from eth_hash.auto import keccak

from oasis_agent.evidence.rpc_capture import BoundedExchangeRecorder
from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.farming.quote_grid_replay import (
    QuoteGridRequest,
    QuoteRuntimePin,
    replay_quote_grid,
)
from oasis_agent.models.common import BlockRef, EvidenceRef, canonical_json
from oasis_agent.models.v4 import PoolKey
from oasis_agent.rpc.transport import JsonRpcTransport
from oasis_agent.shadow.pool_state_capture import (
    Slot0WindowQuorum,
    collect_slot0_window,
    persist_slot0_window_evidence,
)
from oasis_agent.uniswap_v4.pool_id import pool_id_from_key
from oasis_agent.uniswap_v4.quotes import (
    QuoteDirection,
    QuoteKind,
    SinglePoolQuoteRequest,
)
from scripts import run_v4_route_probe as probe
from scripts.replay_quote_grid import Request, run
from tests.unit.test_v4_route_probe import config_payload, write_config


@pytest.fixture
def inputs(tmp_path, monkeypatch):
    payload = config_payload()
    payload["pool_key"].update(fee=500, tick_spacing=10, hooks="0x" + "00" * 20)
    payload["probes"] = [
        {"kind": k.value, "direction": d.value, "amount": n}
        for k in QuoteKind
        for d in QuoteDirection
        for n in (10000, 100000, 1000000)
    ]
    digest = "0x" + keccak(bytes.fromhex("6000")).hex()
    payload["expected_hashes"] = dict.fromkeys(probe.HASH_ROLES, digest)
    config = probe.load_config(
        write_config(tmp_path / "config.json", payload),
        {"RPC_A": "https://a.example", "RPC_B": "https://b.example"},
    )
    view = "0x" + "03" * 20
    header = {
        "number": "0x64",
        "hash": "0x" + "ab" * 32,
        "timestamp": "0x3e8",
        "parentHash": "0x" + "cd" * 32,
    }
    observed = []

    class Poster:
        def __init__(self, url):
            self.url = url

        def post(self, body):
            req = json.loads(body)
            method, params = req["method"], req["params"]
            observed.append((method, params))
            if method == "eth_chainId":
                value = hex(config.chain_id)
            elif method == "eth_getBlockByNumber":
                value = header
            elif method == "eth_getCode":
                value = "0x6000"
            elif params[0]["data"] == probe.POOL_MANAGER_SELECTOR:
                value = "0x" + encode(["address"], [config.pool_manager]).hex()
            elif params[0]["to"] == view:
                value = (
                    "0x"
                    + encode(["uint160", "int24", "uint24", "uint24"], [1 << 96, 0, 0, 500]).hex()
                )
            else:
                _key, _direction, amount, _hook = decode(
                    ["((address,address,uint24,int24,address),bool,uint128,bytes)"],
                    bytes.fromhex(params[0]["data"][10:]),
                )[0]
                exact_input = (
                    params[0]["data"][:10]
                    == probe.encode_single_pool_quote(
                        config.pool,
                        SinglePoolQuoteRequest(
                            QuoteKind.EXACT_INPUT, QuoteDirection.ZERO_FOR_ONE, 1
                        ),
                    )[:10]
                )
                quoted = (
                    amount - amount // 2000 if exact_input else amount + (amount + 1999) // 2000
                )
                value = "0x" + encode(["uint256", "uint256"], [quoted, 50000]).hex()
            return canonical_json({"jsonrpc": "2.0", "id": req["id"], "result": value})

    monkeypatch.setattr(probe, "CurlPoster", Poster)
    report = probe.run(config, 100, tmp_path / "quotes")
    captures = []
    for endpoint in config.endpoints:
        recorder = BoundedExchangeRecorder()
        transport = JsonRpcTransport(
            endpoint.url, http=Poster(endpoint.url), retries=0, recorder=recorder
        )
        captures.append(
            collect_slot0_window(
                (transport, recorder, endpoint.name),
                chain_id=config.chain_id,
                state_view=view,
                pool_manager=config.pool_manager,
                pool_id=pool_id_from_key(PoolKey(**payload["pool_key"])),
                block_numbers=(100,),
                state_view_runtime_keccak=digest,
                pool_manager_runtime_keccak=digest,
            )
        )
    quotes = EvidenceStore(tmp_path / "quotes/evidence.db")
    states = EvidenceStore(tmp_path / "states.db")
    refs = persist_slot0_window_evidence(states, Slot0WindowQuorum(*captures))
    request = QuoteGridRequest(
        BlockRef(**report["block"]),
        config.pool,
        tuple(
            QuoteRuntimePin(role, getattr(config, role), digest)
            for role in ("pool_manager", "quoter", "universal_router")
        ),
        config.probes,
        tuple(e.name for e in config.endpoints),
        tuple(EvidenceRef(**{**r, "block": BlockRef(**r["block"])}) for r in report["evidence"]),
        refs,
    )
    try:
        yield quotes, states, request, tmp_path, observed
    finally:
        quotes.close()
        states.close()


def test_raw_average_shortfall_counts_fee_and_preserves_both_quote_kinds(inputs):
    qs, ss, request, _, observed = inputs
    count = len(observed)
    result = replay_quote_grid(qs, ss, request)
    assert len(observed) == count
    assert len(result.quotes) == 12 and result.sqrt_price_x96 == 1 << 96
    for row in result.quotes:
        exact = Fraction(row.amount_in - row.amount_out, row.amount_in) * 1000000
        assert row.shortfall_ppm_ceiling >= exact
        assert row.shortfall_ppm_ceiling - exact < 1
    assert result.sampled_capacity[0].two_sided_spot_notional_floor_units1 == 0
    assert result.sampled_capacity[1].two_sided_spot_notional_floor_units1 == 1000000
    assert not result.target_admitted and not result.execution_eligible
    assert result.settled_exit_depth is None and result.approved_impact_threshold is None


@pytest.mark.parametrize(
    "case",
    ["header", "sources", "states", "missing_quote", "duplicate_quote", "manager_pin", "pool"],
)
def test_block_pool_identity_and_complete_provider_coverage_refuse(inputs, case):
    qs, ss, r, _, _ = inputs
    if case == "header":
        r = replace(r, block=replace(r.block, timestamp=1001))
    elif case == "sources":
        r = replace(r, providers=r.providers[::-1])
    elif case == "states":
        r = replace(r, states=(r.states[0], r.states[0]))
    elif case == "missing_quote":
        r = replace(r, references=r.references[:-1])
    elif case == "duplicate_quote":
        r = replace(r, references=(r.references[0], *r.references[:-1]))
    elif case == "manager_pin":
        r = replace(
            r, runtimes=(replace(r.runtimes[0], runtime_keccak="0x" + "44" * 32), *r.runtimes[1:])
        )
    else:
        r = replace(r, pool=replace(r.pool, fee=3000))
    with pytest.raises(ValueError):
        replay_quote_grid(qs, ss, r)


@pytest.mark.parametrize(
    "case",
    [
        "amount",
        "getter",
        "unpin",
        "height_only",
        "pin_bool",
        "raw_quoted",
        "raw_error",
        "interpretation",
        "kind",
    ],
)
def test_retained_wire_facts_and_pinned_call_are_not_trusted(inputs, case):
    qs, ss, r, _, _ = inputs
    index = 3 if case == "getter" else 4
    ref = r.references[index]
    old = qs.get_record(ref)
    fact = json.loads(old.payload)
    interpretation = json.loads(old.interpretation_payload)
    req = json.loads(old.request_payload)
    res = json.loads(old.raw_response_payload)
    body = json.loads(bytes.fromhex(req["payload"]))
    reply = json.loads(bytes.fromhex(res["payload"]))
    if case == "amount":
        fact["amount"] += 1
    elif case == "getter":
        reply["result"] = "0x" + encode(["address"], ["0x" + "77" * 20]).hex()
    elif case == "unpin":
        body["params"][-1] = "latest"
    elif case == "height_only":
        body["params"][-1] = hex(r.block.number)
    elif case == "pin_bool":
        body["params"][-1]["requireCanonical"] = 1
    elif case == "raw_quoted":
        reply["result"] = "0x" + encode(["uint256", "uint256"], [9, 50000]).hex()
    elif case == "raw_error":
        reply.pop("result")
        reply["error"] = {"code": 3, "message": "test"}
    elif case == "interpretation":
        interpretation["gas_estimate"] = 1
    req["payload"] = canonical_json(body).hex()
    res["payload"] = canonical_json(reply).hex()
    bad = qs.put(
        "other" if case == "kind" else ref.kind,
        canonical_json(fact),
        ref.block,
        ref.source,
        request=req,
        raw_response=res,
        interpretation=interpretation,
        retrieved_at=ref.retrieved_at,
        schema_version=1,
    )
    refs = list(r.references)
    refs[index] = bad
    with pytest.raises(ValueError):
        replay_quote_grid(qs, ss, replace(r, references=tuple(refs)))


def test_cli_uses_offline_evidence_and_canonical_repeat_without_overwrite(inputs):
    _, _, r, root, _ = inputs
    input_path = root / "request.json"
    input_path.write_bytes(
        canonical_json({"schema": "oasis.quote-grid-input.v1", "request": asdict(r)})
    )
    q, s = root / "quotes/evidence.db", root / "states.db"
    before = tuple(hashlib.sha256(p.read_bytes()).hexdigest() for p in (q, s))
    run(input_path, q, s, root / "one.json")
    run(input_path, q, s, root / "two.json")
    assert (root / "one.json").read_bytes() == (root / "two.json").read_bytes()
    assert tuple(hashlib.sha256(p.read_bytes()).hexdigest() for p in (q, s)) == before
    with pytest.raises(ValueError):
        run(input_path, q, s, root / "one.json")
    raw = json.loads(input_path.read_bytes())
    raw["request"]["target_admitted"] = True
    with pytest.raises(ValueError):
        Request.from_json(canonical_json(raw))
