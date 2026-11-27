"""Verify the fixed Base V4 constructor read against full runtime bytes and all state words."""

import json
from dataclasses import asdict
from pathlib import Path

import rlp
from eth_abi.abi import encode
from eth_hash.auto import keccak
from replay_base_bloom_state_ab import head_for, scan

from oasis_agent.arbitrage.base_survival import decode_base_simulation
from oasis_agent.arbitrage.cycle_protocols import abi, calldata, data_bytes, topic
from oasis_agent.evidence.base_newheads import (
    DIRECTORY,
    replay_head_observation,
    replay_head_schedule,
)
from oasis_agent.fastpath.base_mixed_snapshot import (
    MANAGER_RUNTIME,
    OFFICIAL,
    PUBLICNODE,
    SOURCES,
    _decode_v4,
    _header,
    _request,
    _result,
    _v4_batch_replies,
    _v4_batch_request,
    _v4_calls,
    _v4_requests,
    _v4_static_request,
    address_roster,
)
from oasis_agent.fastpath.bridge import FastpathBridge
from oasis_agent.fastpath.live_shadow_runner import canonical_base_weth_usdc_pools
from oasis_agent.fastpath.multicall_universe import (
    MULTICALL3,
    _encode_aggregate3_calldata,
    fetch_pinned_universe_snapshot,
)
from oasis_agent.fastpath.multihop_replay import load_multihop_replay
from oasis_agent.models.common import canonical_json, sha256_hex
from oasis_agent.rpc.budget import BudgetPolicy, RpcBudget
from scripts.measure_robinhood_quote_delivery import _body, _read
from scripts.replay_base_shadow_survival import replay
from scripts.run_multicall_100tick_campaign import decode_retained_multihop_snapshot

ROOT = Path(__file__).resolve().parents[3]
PAIR = ROOT / "reports/research/base-static-state-pair-20261127"
BIN = PAIR / "BaseV4StaticStateRead.bin"
SOL = Path(__file__).with_name("BaseV4StaticStateRead.sol")


def static_request(block_hash):
    calls = data_bytes(_encode_aggregate3_calldata(_v4_calls()))
    assert keccak(calls).hex() == "f5596037673bfeaa35f94a5dd0a7dbc8a926ebd8c841a8bdc7fecc99f1be190d"
    code = bytes.fromhex(BIN.read_text().strip()) + encode(["bytes"], [calls])
    return _request(
        "eth_call",
        [
            {"data": "0x" + code.hex(), "gas": hex(9_000_000)},
            {"blockHash": block_hash, "requireCanonical": True},
        ],
        req_id=2,
    )


def decode_state(arm, raw, header):
    if arm == "batch":
        code, state = _v4_batch_replies(raw)
        expected = keccak(data_bytes(_result(code)))
        amounts = data_bytes(_result(state, 2))
    else:
        assert arm == "static"
        expected, amounts = abi(("bytes32", "bytes"), data_bytes(_result(raw, 2)))
        state = canonical_json({"jsonrpc": "2.0", "id": 2, "result": "0x" + amounts.hex()})
    assert "0x" + expected.hex() == MANAGER_RUNTIME
    v4 = _decode_v4(state, _v4_calls(), header, bitmap_aware=True)
    return amounts, v4


def read(p):
    return json.loads(p.read_bytes())


def wire(dirpath, label, expected, source):
    m = read(dirpath / (label + ".meta"))
    req, res = (dirpath / (label + k) for k in (".request", ".response"))
    assert req.read_bytes() == expected and sha256_hex(expected) == m["request_sha256"]
    raw = res.read_bytes()
    assert (
        sha256_hex(raw) == m["response_sha256"] and m["endpoint"] == source and m["status"] == 200
    )
    assert m["release_ns"] <= m["sent_ns"] <= m["received_ns"] and m["utc"]
    return raw, m


def verify_pairs():

    scope_path = PAIR / "active-scope.json"
    scope, stop = read(scope_path), read(PAIR / "STOP.json")
    assert (
        sha256_hex(scope_path.read_bytes())
        == "db65ad20519f87613a907f2beceb8e915a448dd785669b46a21f4f3b1dac8303"
    )
    prior = PAIR.parent / "base-resident-forward-20261123/run/multihop_replay/manifest.json"
    for k, p in {
        "bin": BIN,
        "sol": SOL,
        "driver": PAIR / "capture.py",
        "state_reader": PAIR / "original_state_reader.py",
        "scan": SOL.with_name("replay_base_bloom_state_ab.py"),
        "roster": prior,
    }.items():
        assert sha256_hex(p.read_bytes()) == scope["code"][k]
    raw_roster = read(prior)
    proof = load_multihop_replay(
        prior,
        base_token="0x4200000000000000000000000000000000000006",
        decode_snapshot=decode_retained_multihop_snapshot,
    )
    assert raw_roster["manifest_sha256"] == proof.metadata["manifest_sha256"]
    roster = address_roster(raw_roster["pool_specs"])
    policies = tuple(BudgetPolicy(src, cap, 250) for src, cap in scope["origin_caps"].items())
    with RpcBudget(PAIR / "original.sqlite3", policies, read_only=True) as b:
        assert b.counts() == stop["counts"] == {PUBLICNODE: 16, OFFICIAL: 24}
    schedule = replay_head_schedule(PAIR / DIRECTORY, stop["schedule_hash"])
    frames = {
        i["observation"]["head"]["height"]: i["observation"]
        for i in schedule["frames"]
        if "observation" in i
    }
    assert stop["status"] == "complete" and len(scope["orders"]) == stop["completed"] == 8
    controls = {p.address for p in canonical_base_weth_usdc_pools()}
    bridge = FastpathBridge()
    rows = []
    for ix, order in enumerate(scope["orders"]):
        saved = read(PAIR / f"{ix}-row.json")
        obs = saved["head"]
        height = obs["head"]["height"]
        assert (
            obs == frames[height]
            and frames[height + 1]["head"]["parent_hash"] == obs["head"]["block_hash"]
        )
        start = obs["received_monotonic_ns"]
        child_time = frames[height + 1]["received_monotonic_ns"]
        head_req = _request("eth_getBlockByNumber", [hex(height), False])
        p, m1 = wire(PAIR, f"{ix}-primary", head_req, SOURCES[PUBLICNODE])
        o, m2 = wire(PAIR, f"{ix}-official", head_req, SOURCES[OFFICIAL])
        block = _header(p)
        assert block == _header(o) and head_for(block).block_hash == obs["head"]["block_hash"]
        assert asdict(head_for(block)) == obs["head"]
        metas = {"primary": m1, "official": m2}

        def address_transport(_url, req, _timeout, head_req=head_req, p=p, metas=metas, ix=ix):
            if req == head_req:
                return p
            data, metas["address"] = wire(PAIR, f"{ix}-address", req, SOURCES[PUBLICNODE])
            return data

        address = fetch_pinned_universe_snapshot(
            SOURCES[PUBLICNODE],
            roster,
            transport=address_transport,
            block_tag=hex(height),
            pin_call_to_hash=True,
        )
        arms = {}
        last_words = None
        last_finish = 0
        for arm in order:
            if arm == "batch":
                req = _v4_batch_request(_v4_requests(block.block_hash, _v4_calls()))
            else:
                req = static_request(block.block_hash)
                assert req == _v4_static_request(block.block_hash, _v4_calls())
            res, m = wire(PAIR, f"{ix}-{arm}", req, SOURCES[OFFICIAL])
            words, v4 = decode_state(arm, res, block)
            a = saved["arms"][arm]
            assert m["sent_ns"] >= max(last_finish, m2["received_ns"])
            assert a["start"] <= m["release_ns"] <= m["received_ns"] <= a["ready"] <= a["finished"]
            assert a["ready"] >= metas["address"]["received_ns"] >= m1["received_ns"] >= start
            assert a["words_sha256"] == sha256_hex(words) and a["scan"] == scan(
                address, v4[0], controls, bridge
            )
            if last_words is not None:
                assert words == last_words
            last_words = words
            last_finish = a["finished"]
            metas[arm] = m
            arms[arm] = {
                "head_to_ready_ns": a["ready"] - start,
                "head_to_search_ns": a["finished"] - start,
                "reader_release_to_received_ns": m["received_ns"] - m["release_ns"],
                "reader_wire_ns": m["received_ns"] - m["sent_ns"],
                "reader_wire_response_bytes": len(res),
                "words_sha256": a["words_sha256"],
                "cohorts": a["scan"],
            }
        rows.append(
            {
                "block": height,
                "hash": block.block_hash,
                "parent": block.parent_hash,
                "timestamp_ms": block.timestamp_ms,
                "child": frames[height + 1]["head"]["block_hash"],
                "observed_child_ns": child_time - start,
                "order": order,
                "arms": arms,
                "wire": metas,
            }
        )
    return {
        "scope_sha256": sha256_hex(scope_path.read_bytes()),
        "chain_id": 8453,
        "start": read(PAIR / "started.json"),
        "stop": stop,
        "ws_frames": len(frames),
        "heads": rows,
        "evidence_directory": str(PAIR.relative_to(ROOT)),
    }


def control():

    d = PAIR.parent / "base-fixed-actual-control-20261128"
    scope = read(d / "scope.json")
    r = read(d / "report.json")
    run = d / "run"
    assert (
        sha256_hex((d / "scope.json").read_bytes())
        == "d883a4f5fa74351123c28e567bf696ecc009837574b976816b6ed1dbf4ae4b63"
    )
    assert sha256_hex((d / "control.py").read_bytes()) == scope["code_sha256"]
    assert r["status"] == "complete" and read(d / "STOP.json") == {
        "reason": None,
        "status": "complete",
    }
    with RpcBudget(
        run / "original.sqlite3", (BudgetPolicy("official", 11, 250),), read_only=True
    ) as b:
        assert b.counts() == r["official_counts"] == {"official": 11}
    ids = dict(r["request_sequence"])

    def response(label, method, params):
        expected = _body(ids[label], method, params)
        return _read(run / label, expected, ids[label], 1)

    sender = scope["from"]
    router = scope["to"]
    native = scope["value"]
    route = [tuple(x) for x in scope["route"]]
    gas = scope["gas"]
    weth = route[0][0]
    typ = "(address,address,bool,address)[]"
    hobs, cobs = (
        replay_head_observation(run / "heads", r[k]) for k in ("observed_head", "observed_child")
    )
    assert (
        hobs.head.block_hash == cobs.head.parent_hash and hobs.head.height + 1 == cobs.head.height
    )
    nonce, _ = response("nonce", "eth_getTransactionCount", [sender, "latest"])
    data = calldata(
        f"swapExactETHForTokens(uint256,{typ},address,uint256)",
        ("uint256", typ, "address", "uint256"),
        (scope["min_out"], route, sender, hobs.head.timestamp_s + 60),
    )
    envelope = {
        "from": sender,
        "to": router,
        "nonce": nonce,
        "data": data,
        "value": hex(native),
        "gas": hex(gas),
        "maxFeePerGas": hex(scope["max_fee"]),
        "maxPriorityFeePerGas": hex(scope["tip"]),
        "accessList": [],
    }
    assert envelope == r["envelope"] == read(run / "envelope.json")
    size = len(
        bytes([2])
        + rlp.encode(
            [
                8453,
                int(nonce, 16),
                scope["tip"],
                scope["max_fee"],
                gas,
                data_bytes(router),
                native,
                data_bytes(data),
                [],
            ]
        )
    )

    def call(to, sig, types=(), args=()):
        return to, False, data_bytes(calldata(sig, types, args))

    gpo = "0x420000000000000000000000000000000000000f"
    calls = [
        call(router, f"getAmountsOut(uint256,{typ})", ("uint256", typ), (native, route)),
        call(gpo, "getL1FeeUpperBound(uint256)", ("uint256",), (size,)),
        call(gpo, "getOperatorFee(uint256)", ("uint256",), (gas,)),
        call(MULTICALL3, "getEthBalance(address)", ("address",), (sender,)),
        *(
            call(
                router,
                "poolFor(address,address,bool,address)",
                ("address", "address", "bool", "address"),
                step,
            )
            for step in route
        ),
    ]
    stages = {}
    for key, label, hd, tag in [
        ("H", "H", "H-header", hex(hobs.head.height)),
        ("K", "sim_reply_latest", "latest-immediate-header", "latest"),
        ("C", "Hplus1", "Hplus1-header", hex(cobs.head.height)),
    ]:
        hdr, _ = response(hd, "eth_getBlockByNumber", [tag, False])
        b = _header(canonical_json({"jsonrpc": "2.0", "id": 1, "result": hdr}))
        row = r["stages"][label]
        assert b.block_hash == row["pin"]["block_hash"]
        pin = {"blockHash": b.block_hash, "requireCanonical": True}
        if key == "H":
            code, _ = response("H-runtime", "eth_getCode", [router, pin])
            assert (
                sha256_hex(data_bytes(code)) == scope["router_runtime_sha256"]
                and b.block_hash == hobs.head.block_hash
            )
        elif key == "C":
            assert b.block_hash == cobs.head.block_hash
        raw, _ = response(
            label + "-quote",
            "eth_call",
            [{"to": MULTICALL3, "data": _encode_aggregate3_calldata(calls)}, pin],
        )
        (rows,) = abi(("(bool,bytes)[]",), data_bytes(raw))
        assert len(rows) == 7 and all(ok for ok, _ in rows)
        (amts,) = abi(("uint256[]",), rows[0][1])
        l1, op, bal = (abi(("uint256",), x[1])[0] for x in rows[1:4])
        pools = [abi(("address",), x[1])[0] for x in rows[4:]]
        assert (
            amts[0] == native
            and bal >= native + gas * scope["max_fee"] + l1 + op
            and len(set(pools)) == 3
        )
        sim, clock = response(
            label + "-simulate",
            "eth_simulateV1",
            [
                {
                    "blockStateCalls": [{"calls": [envelope]}],
                    "traceTransfers": False,
                    "validation": True,
                },
                hex(b.number),
            ],
        )
        out = decode_base_simulation(
            sim, parent_number=b.number, parent_hash=b.block_hash, recipient=sender, gas_limit=gas
        )
        assert (
            out.success
            and abi(("uint256[]",), out.return_data) == (amts,)
            and out.weth_delta == amts[-1]
        )
        logs = sim[0]["calls"][0]["logs"]

        def unique(token, topics, amount, logs=logs):
            match = [
                int(x["logIndex"], 16)
                for x in logs
                if x["address"] == token
                and x["topics"] == topics
                and abi(("uint256",), data_bytes(x["data"])) == (amount,)
            ]
            assert len(match) == 1
            return match[0]

        def indexed(x):
            return "0x" + bytes(12).hex() + x[2:]

        dep = unique(weth, [topic("Deposit(address,uint256)"), indexed(router)], native)
        proof = []
        coins = [weth, route[1][0], route[2][0], weth]
        for i, (a, z) in enumerate(zip([router, *pools], [*pools, sender], strict=True)):
            proof.append(
                unique(
                    coins[i],
                    [topic("Transfer(address,address,uint256)"), indexed(a), indexed(z)],
                    amts[i],
                )
            )
        assert [dep, *proof] == sorted({dep, *proof}) and out.base_fee_wei_per_gas <= scope[
            "max_fee"
        ]
        l2 = out.gas_used * min(scope["max_fee"], scope["tip"] + out.base_fee_wei_per_gas)
        net = out.weth_delta - native - l1 - op - l2
        assert (
            list(amts) == row["quoted_amounts"] and net == row["whole_net_upper_cost_scenario_wei"]
        )
        stages[key] = {
            "height": b.number,
            "hash": b.block_hash,
            "quote_amounts": amts,
            "actual_principal_eth_wei": native,
            "deposit_log": dep,
            "payment_transfer_logs": proof,
            "pools": pools,
            "sender_native_balance": bal,
            "simulated_gas": out.gas_used,
            "l2_fee_wei": l2,
            "l1_upper_wei": l1,
            "operator_upper_wei": op,
            "whole_weth_to_sender": out.weth_delta,
            "whole_net_wei": net,
            "quote_gross_wei": amts[-1] - native,
            "whole_reply_ns": clock,
        }
    all_meta = {k: read(run / (k + ".meta")) for k in ids}
    k = all_meta["latest-immediate-header"]
    h = stages["H"]["whole_reply_ns"]
    assert k["received_ns"] >= k["sent_ns"] >= h
    return {
        "source_http": scope["source_http"],
        "source_ws": scope["source_ws"],
        "scope_sha256": sha256_hex((d / "scope.json").read_bytes()),
        "observed_utc": read(d / "started.json")["utc"],
        "envelope_sha256": sha256_hex(canonical_json(envelope)),
        "nonce": int(nonce, 16),
        "from": sender,
        "to": router,
        "stages": stages,
        "components": "negative control, ETH/WETH parity, L1 and operator fee upper bounds",
        "head_to_h_sim_ns": h - hobs.received_monotonic_ns,
        "head_to_first_child_ns": cobs.received_monotonic_ns - hobs.received_monotonic_ns,
        "k_request_after_h_reply_ns": k["sent_ns"] - h,
        "k_reply_after_h_reply_ns": k["received_ns"] - h,
        "wire": all_meta,
        "rpc_counts": r["official_counts"] | r["ws_counts"],
        "evidence_directory": str(d.relative_to(ROOT)),
    }


def campaign():

    d = PAIR.parent / "base-static-normal-20261127"
    s = read(d / "preregistered.json")
    assert (
        sha256_hex((d / "preregistered.json").read_bytes())
        == "385fd4a2a50d213b7ffc9baf18983e2fd05888c13604c21d5a72a0bd6761751d"
    )
    assert (
        s["v4_static_read"]
        and sha256_hex((d / "run_capture.py").read_bytes()) == s["driver_sha256"]
    )
    r = replay(d / "run", arrival_deadlines=True)
    sumry = read(d / "run/shadow_summary.json")
    assert (
        sumry["base_newheads"]["static_v4"]
        and sumry["ticks_completed"] == 4
        and not sumry["stopped_early"]
    )
    return {
        k: r[k]
        for k in (
            "heads",
            "observed_raw_child_deadlines",
            "normal_discovery_candidates",
            "whole_transaction_simulations",
            "positive_survival_rate",
        )
    } | {
        "frames": len(r["head_schedule"]["frames"]),
        "schedule_sha256": sumry["base_newheads"]["schedule_sha256"],
        "frozen_utc": s["frozen_utc"],
        "source_identity": s["code_identity"],
        "rpc_counts": sumry["base_newheads"]["rpc_attempts_by_source"]
        | sumry["mixed_base_state"]["rpc_attempts_by_source"],
        "scope_sha256": sha256_hex((d / "preregistered.json").read_bytes()),
        "evidence_directory": str(d.relative_to(ROOT)),
    }


if __name__ == "__main__":
    if not __debug__:
        raise SystemExit("run without -O")
    print(
        json.dumps(
            {
                "schema": "oasis.base-static-reader-actual-principal.v1",
                "paired": verify_pairs(),
                "normal": campaign(),
                "control": control(),
                "rpc_usd": 0,
            },
            sort_keys=True,
            indent=2,
        )
    )
