"""Replay the fixed Aave and Metric trace research from retained, anonymous reads."""

import argparse
import sys
from collections import Counter
from pathlib import Path
from sqlite3 import Error as DatabaseError
from types import SimpleNamespace

from oasis_agent.arbitrage.cycle_evidence import read_bounded
from oasis_agent.arbitrage.cycle_plan import address, hash32, utc
from oasis_agent.arbitrage.cycle_protocols import abi, calldata, data_bytes
from oasis_agent.evidence.public_receipts import quantity
from oasis_agent.evidence.research_traces import first_failure, flat_calls, nested_calls
from oasis_agent.evidence.rpc_validation import strict_json_object
from oasis_agent.evidence.state_trie import account_proof, prague_header
from oasis_agent.models.common import canonical_json, sha256_hex
from oasis_agent.rpc.budget import BudgetPolicy, RpcBudget
from oasis_agent.rpc.protocol import response_envelope

from . import capture_public_rpc as rpc
from . import research_aave_priority as aave
from . import research_metric_pair as metric

BLOCKSCOUT = "https://eth.blockscout.com"
DRPC = "https://eth.drpc.org"
COMPRESSED = "0xe844701934d9c6085b292e428f41b911cc0b3872"
WORD_TIME = ((1 << 56) - 1) << 8
REVERTED = data_bytes(calldata("FeedStalled()"))
GET_QUOTE = data_bytes(calldata("getQuote()"))
METRIC_SWAP = data_bytes(calldata("swap(address,bool,int128,uint128,bytes,bytes)")[:10])
V3_SWAP = data_bytes(calldata("swap(address,bool,int256,uint160,bytes)")[:10])
GUARD = calldata("priceGuard(bytes32)")[:10]
GET_ORACLE_DATA = calldata("getOracleData(bytes32)")[:10]
REJECTED_PRINCIPAL = data_bytes(calldata("Rejected(uint8)", ("uint8",), (11,)))
WRAPPED_FAILURE = data_bytes(calldata("ExternalFailure(uint8)", ("uint8",), (1,)))


def retained(directory, source, specs):
    """Check complete campaign identity, reservations and the raw request/response joins."""
    if (directory / "STOP").exists() or (directory / "ACTIVE").exists():
        raise ValueError("stopped or unfinished trace campaign")
    plan_raw = read_bounded(directory / "plan.json", 256 * 1024)
    plan = strict_json_object(plan_raw)
    expiry = utc(plan["expires"])
    if plan_raw != canonical_json(
        {
            "schema": "oasis-local-ethereum-trace-capture-v1",
            "chain_id": 1,
            "source": source,
            "expires": plan["expires"],
            "max_requests": len(specs),
            "minimum_interval_ms": 750,
            "requests": specs,
        }
    ):
        raise ValueError("trace capture plan differs")
    ledger = directory / "original-budget.sqlite3"
    if aave.wallet.load(directory / "identity.json", 4096) != {
        "ledger": rpc.identity(ledger),
        "plan_sha256": sha256_hex(plan_raw),
        "root": str(directory.resolve()),
    }:
        raise ValueError("original trace budget required")
    with RpcBudget(ledger, (BudgetPolicy(source, len(specs), 750),), read_only=True) as budget:
        if budget.counts() != {source: len(specs)}:
            raise ValueError("trace budget reservation mismatch")
    previous_start = previous_finish = None
    for index, (method, url, payload) in enumerate(specs):
        request = read_bounded(directory / f"{index}.request.json", 64 * 1024)
        body = read_bounded(directory / f"{index}.response", 8 * 1024 * 1024)
        meta = aave.wallet.load(directory / f"{index}.meta.json", 4096)
        start, finish = utc(meta["start"]), utc(meta["finish"])
        if (
            request != canonical_json({"method": method, "url": url, "json": payload})
            or meta["http"] != 200
            or meta["reason"] is not None
            or (sha256_hex(request), sha256_hex(body))
            != (meta["request_sha256"], meta["response_sha256"])
            or not start <= finish <= expiry
            or (previous_start is not None and (start - previous_start).total_seconds() < 0.75)
            or (previous_finish is not None and start < previous_finish)
        ):
            raise ValueError("trace wire provenance mismatch")
        previous_start, previous_finish = start, finish
        yield body, meta


def aave_report(research, archive):
    original = research / "aave-five-traces-20261114"
    prior = research / "aave-regimes-20261114"
    receipts = original / "alternate-identity/campaign"
    baseline_path = prior / "wallet-plus-pair.json"
    index_path = research / "aave-ledger-20261114/wallet-index"
    priority = aave.replay(
        prior / "trace-priority.json",
        baseline_path,
        index_path,
        receipts,
        original / "tx-index",
        original / "block-balances/campaign",
    )
    baseline = aave.wallet.load(baseline_path, 4 * 1024 * 1024)
    account = address(priority["account"])
    windows = [row["block_residual_wei"] for row in priority["rows"]]
    if len(windows) != 5 or windows[0] != min(windows):
        raise ValueError("trace priority mismatch")
    movements, index_ref = aave.wallet.index(
        index_path,
        account,
        baseline["first_block"],
        baseline["last_block"],
        {row["address"]: row["decimals"] for row in baseline["assets"] if row["address"]},
    )
    if index_ref != priority["index_ref"]:
        raise ValueError("trace index changed")
    result = []
    offset = 0
    for campaign, targets in (
        ("aave-blockscout-first", priority["rows"][:1]),
        ("aave-blockscout-remaining", priority["rows"][1:]),
    ):
        specs = [
            (
                "GET",
                f"{BLOCKSCOUT}/api/v2/transactions/{hash32(row['transaction'])}/raw-trace",
                None,
            )
            for row in targets
        ]
        for row, (body, meta) in zip(
            targets, retained(archive / campaign, "blockscout", specs), strict=True
        ):
            wrapper = strict_json_object(b'{"frames":' + body + b"}")
            if set(wrapper) != {"frames"}:
                raise ValueError("invalid raw trace array")
            calls = flat_calls(wrapper["frames"])
            captured = rpc.replay(receipts / f"receipt-{offset}")
            tx = captured["exports"]["records"][3]["response"]["result"]
            if captured["capture_sha256"] != row["receipt_capture_sha256"]:
                raise ValueError("receipt capture changed")
            result.append(reconcile_trace(account, row, tx, calls, movements) | meta)
            offset += 1
    correction = sum(row["index_overstatement_wei"] for row in result)
    return {
        "account": account,
        "source": "ethereum_blockscout_raw_trace",
        "rows": result,
        "index_overstatement_wei": correction,
        "original_native_residual_wei": priority["native_residual_wei"],
        "residual_after_trace_correction_wei": priority["native_residual_wei"] + correction,
        "receipt_gap": priority["receipt_gap_after"],
        "execution_eligible": False,
    }


def reconcile_trace(account, row, tx, calls, movements):
    root = calls[()]
    if root.error or (root.sender, root.recipient, root.value, root.data) != (
        address(tx["from"]),
        address(tx["to"]),
        quantity(tx["value"]),
        data_bytes(tx["input"]),
    ):
        raise ValueError("trace root differs from the successful pinned transaction")
    indexed = Counter(
        (move.sender, move.recipient, move.value_raw)
        for move in movements
        if move.transaction == row["transaction"]
        and move.kind == "internal"
        and move.success
        and move.value_raw
    )
    seen, committed = Counter(), Counter()
    related = []
    for path, call in sorted(calls.items()):
        if (
            not path
            or call.kind != "CALL"
            or not call.value
            or account not in (call.sender, call.recipient)
        ):
            continue
        shape = (call.sender, call.recipient, call.value)
        seen[shape] += 1
        failure = first_failure(calls, path)
        if failure is None:
            committed[shape] += 1
        related.append(
            {
                "from": call.sender,
                "to": call.recipient,
                "value_wei": call.value,
                "path": path,
                "first_reverted_path": failure,
                "error": calls[failure].error if failure is not None else None,
            }
        )
    duplicates = Counter((s["from"], s["to"], s["value_wei"]) for s in row["shapes"])
    if (
        seen != indexed
        or seen - committed != duplicates
        or any(committed[shape] != 1 or seen[shape] != 2 for shape in duplicates)
    ):
        raise ValueError("trace movements differ from the pinned index hypothesis")
    overstatement = sum(
        ((recipient == account) - (sender == account)) * value * count
        for (sender, recipient, value), count in duplicates.items()
    )
    if row["block_residual_wei"] + overstatement:
        raise ValueError("trace-adjusted block differs from observed balance")
    committed_delta = sum(
        ((recipient == account) - (sender == account)) * value * count
        for (sender, recipient, value), count in committed.items()
    )
    return {
        "block": row["block"],
        "hash": row["block_hash"],
        "transaction": row["transaction"],
        "transaction_index": row["transaction_index"],
        "receipt_capture_sha256": row["receipt_capture_sha256"],
        "frame_count": len(calls),
        "related_calls": related,
        "committed_transaction_delta_wei": committed_delta,
        "index_overstatement_wei": overstatement,
        "corrected_block_residual_wei": 0,
    }


def post(url, method, params, i):
    return "POST", url, {"jsonrpc": "2.0", "id": i + 1, "method": method, "params": params}


def call_specs(calls, ids, extra=None):
    specs = []
    for i, position in enumerate(ids):
        method, (call, pin, state, block) = calls.requests()[position]
        if method != "eth_call":
            raise ValueError("expected pinned call")
        if extra is not None:
            slot, word = extra[i]
            state = state | {COMPRESSED: {"stateDiff": {slot: word}}}
        config = {
            "tracer": "callTracer",
            "timeout": "6s",
            "tracerConfig": {"onlyTopCall": False},
            "stateOverrides": state,
            "blockOverrides": block,
        }
        specs.append(post(DRPC, "debug_traceCall", [call, pin, config], i))
    return specs


def metric_inputs(research):
    prior = research / "metric-paired-20261114"
    feed = research / "metric-overlay-20261114"
    titan = research / "titan-state-20261114"
    peer = research / "metric-probe-20261114/campaign"
    scope, _ = rpc.load(prior / "pair/paired/scope.json", rpc.PublicScope)
    return metric.prepare(
        SimpleNamespace(
            design=prior / "pair-design.json",
            parent=prior / "context/parent",
            target=prior / "context/target",
            peer_parent=peer / "publicnode-parent",
            peer_target=peer / "publicnode-target",
            proof=prior / "proof/empty-account",
            titan=titan / "campaign/snapshots",
            analysis=titan / "analysis-plan.json",
            feeds=feed / "feed-plan.json",
            catalog=feed / "pyth-symbols.response",
            artifact=research / "metric-probe-codec-20261114/probe-artifact.json",
        ),
        scope.expires_utc,
    )


def observed_calls(directory, specs):
    for i, (body, meta) in enumerate(retained(directory, "drpc", specs)):
        result = response_envelope(body, i + 1)["result"]
        frames = nested_calls(result)
        call = specs[i][2]["params"][0]
        root = frames[()]
        if (root.sender, root.recipient, root.data, root.value) != (
            call["from"],
            call["to"],
            data_bytes(call["data"]),
            0,
        ) or not root.error:
            raise ValueError("trace root differs from the always-reverting probe")
        yield frames, meta


def unique_call(frames, contract, data, parent=None):
    matches = [
        (path, call)
        for path, call in frames.items()
        if call.recipient == contract
        and call.data.startswith(data)
        and (parent is None or path[:-1] == parent)
    ]
    if len(matches) != 1:
        raise ValueError("expected exactly one matching trace frame")
    return matches[0]


def metric_branch(frames, plan, context, oracle, overlaid, fresh=False):
    pool_path, pool = unique_call(frames, plan.metric, METRIC_SWAP)
    provider_path, provider = unique_call(frames, context["provider"], GET_QUOTE, pool_path)
    if len(provider.data) != 4:
        raise ValueError("provider calldata length")
    timestamp = plan.block_timestamp
    pyth = []
    guards = []
    for path, call in sorted(frames.items()):
        if path[:-1] != provider_path or call.recipient != oracle:
            continue
        selector = "0x" + call.data[:4].hex()
        if selector == calldata("price(bytes32,address)")[:10]:
            feed, consumer = abi(("bytes32", "address"), call.data[4:])
            mid, spread, _, ref = abi(("uint256", "uint256", "uint16", "uint256"), call.output)
            _, in_swap = unique_call(frames, plan.metric, data_bytes(calldata("inSwap()")), path)
            if (
                call.error
                or consumer != plan.metric
                or in_swap.error
                or abi(("address",), in_swap.output) != (context["provider"],)
            ):
                raise ValueError("attributed oracle read failed")
            pyth.append((int.from_bytes(feed), mid, spread, timestamp - ref))
        elif selector == GUARD:
            guard = abi(("uint128", "uint128"), call.output)
            if call.error or guard != (0, 0):
                raise ValueError("unexpected oracle guard")
            guards.append(abi(("bytes32",), call.data[4:])[0])
    if not fresh:
        if pool.output is None:
            raise ValueError("missing Metric failure data")
        (inner,) = abi(("bytes",), pool.output[4:])
        if (
            pool.output[:4] != data_bytes(calldata("PriceProviderFailed(bytes)")[:10])
            or inner != REVERTED
            or provider.output != REVERTED
            or not all((pool.error, provider.error))
        ):
            raise ValueError("unexpected Metric failure")
    if not overlaid:
        if pyth != [(2, 190372380999, 791, 42)] or guards:
            raise ValueError("unexpected baseline oracle sequence")
        if any(
            path[:-1] == provider_path and call.recipient != oracle for path, call in frames.items()
        ):
            raise ValueError("baseline reached an unexpected source")
        return {"pyth": pyth, "branch": "stale_pyth_base"}

    if pyth != [(2, 190596447284, 587, 4), (7, 99976927, 63, 4)] or [
        int.from_bytes(feed) for feed in guards
    ] != [2, 7]:
        raise ValueError("two-slot override did not reach both Pyth reads")
    source_path, source = unique_call(frames, context["source"], GET_QUOTE, provider_path)
    _, configured = unique_call(
        frames, context["provider"], data_bytes(calldata("source()")), source_path
    )
    if configured.error or abi(("address",), configured.output) != (context["source"],):
        raise ValueError("source() read differs")
    nested_path, nested = unique_call(frames, COMPRESSED, data_bytes(GET_ORACLE_DATA), source_path)
    (feed_id,) = abi(("bytes32",), nested.data[4:])
    mid, spread0, spread1, time_ms = abi(("uint64", "uint16", "uint16", "uint56"), nested.output)
    if nested.error or source.kind != "STATICCALL" or source.data != GET_QUOTE:
        raise ValueError("invalid configured source path")
    compressed_guard = [
        call
        for path, call in frames.items()
        if path[:-1] == source_path
        and call.recipient == COMPRESSED
        and call.data[:4].hex() == GUARD[2:]
    ]
    expected_age = timestamp - time_ms // 1000
    details = {}
    if fresh:
        if (
            source.error
            or provider.error
            or len(compressed_guard) != 1
            or compressed_guard[0].error
            or frames[()].output != WRAPPED_FAILURE
        ):
            raise ValueError("fresh source did not pass the price layer")
        for quote in (source, provider):
            bid, ask, reference = abi(("uint128", "uint128", "uint128"), quote.output)
            if not 0 < bid <= reference <= ask:
                raise ValueError("invalid source quote interval")
        v3_path, v3 = unique_call(frames, plan.v3, V3_SWAP)
        if plan.request.metric_first:
            first_path, first, second_path, second = pool_path, pool, v3_path, v3
        else:
            first_path, first, second_path, second = v3_path, v3, pool_path, pool
        if (
            first_path[:-1]
            or second_path[:-2] != first_path
            or frames[second_path[:-1]].recipient != frames[()].recipient
            or not first.error
            or first.output != REJECTED_PRINCIPAL
            or second.error
        ):
            raise ValueError("two swaps did not reach the final repayment check")
        # Metric token ordering can differ from the V3 ordering. Base the start
        # asset's position on the immutable plan for the venue returning deltas.
        start_is_token0 = plan.request.start_with_a
        if not plan.request.metric_first:
            start_is_token0 = start_is_token0 == plan.metric_token0_is_a
        types = ("int256", "int256") if plan.request.metric_first else ("int128", "int128")
        d0, d1 = abi(types, second.output)
        intermediate, returned = (d1, -d0) if start_is_token0 else (d0, -d1)
        if intermediate <= 0 or not 0 < returned < plan.request.amount_in:
            raise ValueError("expected second swap below starting principal")
        details = {
            "intermediate_raw": intermediate,
            "starting_principal_raw": plan.request.amount_in,
            "returned_principal_raw": returned,
            "principal_shortfall_raw": plan.request.amount_in - returned,
        }
    elif source.output != REVERTED or not source.error or compressed_guard:
        raise ValueError("configured source did not stop before its guard")
    return {
        **details,
        "pyth": pyth,
        "branch": "price_passed_probe_rejected_11" if fresh else "compressed_source_stalled",
        "source_path": source_path,
        "compressed_path": nested_path,
        "compressed_feed_id": "0x" + feed_id.hex(),
        "compressed_age_seconds": expected_age,
        "compressed_time_ms": time_ms,
        "compressed_quote": [mid, spread0, spread1],
    }


def proof_at_parent(archive, context, parent, slot):
    _, endpoint = rpc.SOURCES["alchemy_public_ethereum"]
    number = context["parent"]["ref"]["number"]
    head = ("eth_getBlockByNumber", [hex(number), False])
    pin = {"blockHash": parent, "requireCanonical": True}
    authorities = (
        (
            "metric-source-code",
            [
                ("eth_getCode", [context["source"], pin]),
                ("eth_getCode", [COMPRESSED, pin]),
            ],
        ),
        ("metric-source-proof", [("eth_getProof", [COMPRESSED, [slot], pin])]),
        ("metric-source-account", [("eth_getProof", [context["source"], [], pin])]),
    )
    values = []
    for directory, reads in authorities:
        specs = [
            post(endpoint, method, params, i)
            for i, (method, params) in enumerate([("eth_chainId", []), head, *reads, head])
        ]
        decoded = [
            response_envelope(body, i + 1)["result"]
            for i, (body, _) in enumerate(retained(archive / directory, "alchemy_public", specs))
        ]
        opening, root_hash = prague_header(decoded[1], number, parent)
        if (
            quantity(decoded[0], 64) != 1
            or opening != prague_header(decoded[-1], number, parent)[0]
            or root_hash != context["parent_state_root"]
        ):
            raise ValueError("source context is not the original hash and state root")
        values.extend(decoded[2:-1])
    source_code, compressed_code, proof, source_proof = values
    account = account_proof(context["parent_state_root"], COMPRESSED, (slot,), proof)
    if not account.present or not account.slots[0].present:
        raise ValueError("compressed oracle slot missing")
    account.verify_code(compressed_code)
    source_account = account_proof(
        context["parent_state_root"], context["source"], (), source_proof
    )
    source_account.verify_code(source_code)
    if not source_account.present or not data_bytes(source_code):
        raise ValueError("empty source code")
    return account.slots[0].value_raw, account.code_hash, source_account.code_hash


def compressed_slot(feed_id):
    feed = int(hash32(feed_id), 16)
    if (feed >> 16) & ((1 << 80) - 1) != 1 or feed & 255 >= 4 or not feed >> 96:
        raise ValueError("invalid Ethereum compressed feed ID")
    return "0x" + ((feed >> 96) << 96 | (feed >> 8 & 255)).to_bytes(32, "big").hex()


def titan_word(research, context, slot):
    capture = rpc.replay(research / "titan-state-20261114/campaign/snapshots")
    if capture["capture_sha256"] != context["evidence_sha256"]["titan"]:
        raise ValueError("compressed source requires the original Titan capture")
    snapshot = capture["exports"]["records"][0]["response"]["result"]
    (venue,) = [item for item in snapshot["venues"] if item["quoteAddress"] == COMPRESSED]
    if venue["blockNumber"] != int(context["block_context"]["number"], 16):
        raise ValueError("wrong Titan target block")
    word = hash32(venue["stateOverride"][COMPRESSED]["stateDiff"][slot])
    return word


def metric_report(research, archive):
    calls, raw, context = metric_inputs(research)
    pair = metric.review(calls, raw, context, research / "metric-paired-20261114/pair/paired")
    if pair["status"] != "complete" or pair["complete_two_leg_count"] != 0:
        raise ValueError("original paired capture unavailable")
    # The configured price source is discovered from the retained attributed tree
    # and then checked against the independent fixed-block code response.
    context = context | {
        "provider": rpc.load(
            research / "metric-paired-20261114/pair-design.json", metric.metric_pair.Design
        )[0].provider,
        "source": "0xad75dfdc62d65ad8416e510b6764bd165903bbad",
    }
    arms = []
    for folder, ids in (
        ("metric-drpc", (6, 7)),
        ("metric-drpc-remaining", (2, 3, 4, 5, 8, 9)),
    ):
        for index, (frames, meta) in zip(
            ids, observed_calls(archive / folder, call_specs(calls, ids)), strict=True
        ):
            plan = calls.plans[(index - 2) // 2]
            if frames[()].output != WRAPPED_FAILURE:
                raise ValueError("trace root disagrees with paired eth_call")
            arms.append(
                {
                    "ordinal": index,
                    "asset": plan.assets[0 if plan.request.start_with_a else 1].symbol,
                    "metric_first": plan.request.metric_first,
                    "titan_pyth": bool(index % 2),
                    "frames": len(frames),
                    **metric_branch(frames, plan, context, calls.oracle, bool(index % 2)),
                    **meta,
                }
            )
    arms.sort(key=lambda row: row["ordinal"])
    compressed_arms = [row for row in arms if row["titan_pyth"]]
    first = compressed_arms[0]
    feed_id = first["compressed_feed_id"]
    if any(
        (row["compressed_feed_id"], row["compressed_time_ms"], row["compressed_quote"])
        != (feed_id, first["compressed_time_ms"], first["compressed_quote"])
        for row in compressed_arms
    ):
        raise ValueError("inconsistent compressed source read")
    slot = compressed_slot(feed_id)
    word, code_hash, source_code_hash = proof_at_parent(archive, context, calls.parent_hash, slot)
    if (word & WORD_TIME) >> 8 != first["compressed_time_ms"]:
        raise ValueError("trace timestamp disagrees with the proven slot")
    builder_word = titan_word(research, context, slot)
    timestamp = calls.plans[2].block_timestamp
    diagnostics = []
    for folder, positions, samples in (
        ("metric-drpc-titan-compressed", (7,), (("titan", builder_word, 0),)),
        ("metric-drpc-titan-other-routes", (3, 5, 9), (("titan", builder_word, 0),) * 3),
        (
            "metric-drpc-compressed-boundary",
            (7,) * 2,
            tuple(time_sample(word, timestamp, age) for age in (15, 16)),
        ),
        (
            "metric-drpc-compressed-ages",
            (7,) * 7,
            tuple(time_sample(word, timestamp, age) for age in (0, 1, 2, 4, 5, 8, 10)),
        ),
    ):
        specs = call_specs(calls, positions, [(slot, w) for _, w, _ in samples])
        for index, (kind, _, age), (frames, meta) in zip(
            positions, samples, observed_calls(archive / folder, specs), strict=True
        ):
            plan = calls.plans[(index - 2) // 2]
            details = metric_branch(frames, plan, context, calls.oracle, True, fresh=age == 0)
            if (
                details["compressed_feed_id"] != feed_id
                or details["compressed_time_ms"] != (timestamp - age) * 1000
                or (
                    kind == "timestamp_only"
                    and details["compressed_quote"] != first["compressed_quote"]
                )
            ):
                raise ValueError("compressed override was not observed")
            diagnostics.append(
                {
                    "override": kind,
                    "ordinal": index,
                    "asset": plan.assets[0 if plan.request.start_with_a else 1].symbol,
                    "metric_first": plan.request.metric_first,
                    "frames": len(frames),
                    **details,
                    **meta,
                }
            )
    return {
        "source": "drpc_public_ethereum_call_trace",
        "parent": context["parent"],
        "target_hash": context["target_hash"],
        "block_context": context["block_context"],
        "original_pair_capture_sha256": pair["capture_sha256"],
        "compressed_oracle": COMPRESSED,
        "compressed_runtime_hash": code_hash,
        "compressed_slot": slot,
        "provider_source": context["source"],
        "provider_source_runtime_hash": source_code_hash,
        "arms": arms,
        "diagnostics": diagnostics,
        "net_profit": None,
        "execution_eligible": False,
    }


def time_sample(word, timestamp, age):
    value = (word & ~WORD_TIME) | ((timestamp - age) * 1000 << 8)
    return "timestamp_only", "0x" + value.to_bytes(32, "big").hex(), age


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--research", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    archive = args.research / "ethereum-traces-20261114"
    try:
        result = {
            "schema": "oasis-ethereum-trace-research-v1",
            "chain_id": 1,
            "aave": aave_report(args.research, archive),
            "metric": metric_report(args.research, archive),
        }
        rpc.publish(args.output, result)
    except (ValueError, TypeError, KeyError, IndexError, OSError, DatabaseError) as exc:
        print(f"Historical trace evidence refused ({type(exc).__name__})", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
