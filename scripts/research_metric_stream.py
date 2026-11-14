"""Replay the Ethereum Metric stream pilot against proofs and relay deliveries."""

import argparse
import re
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from sqlite3 import Error as DatabaseError

from oasis_agent.arbitrage import metric_builder, titan_state
from oasis_agent.arbitrage.cycle_evidence import read_bounded
from oasis_agent.arbitrage.cycle_plan import address, hash32, utc
from oasis_agent.arbitrage.metric_builder import compressed_time
from oasis_agent.arbitrage.titan_state import attribution
from oasis_agent.evidence.rpc_validation import strict_integer, strict_json_object
from oasis_agent.evidence.state_trie import account_proof, prague_header
from oasis_agent.models.common import canonical_json, sha256_hex
from oasis_agent.rpc.budget import BudgetPolicy, RpcBudget

from . import capture_public_rpc as rpc
from .research_ethereum_traces import COMPRESSED

PYTH = "0x30a88bf894d1b806125a23ebf23d715496fd7102"
COMPRESSED_SLOT = "0x8dfdc825dd0453e7c80b1c1d32ae910e35d6efe9000000000000000000000000"
COMPRESSED_CODE = "0x546b1ee252053f41cd7672c50a59d16dde0d7aed8cb753c413e0ce5eddf8f340"
TITAN_KEYS = "https://docs.titanbuilder.xyz/builder-public-keys.md"
TITAN_RELAY = "https://titanrelay.xyz/relay/v1/data/bidtraces/proposer_payload_delivered"
TITAN_WS = "wss://ap.data.titanbuilder.xyz/ws/pamm_quote_stream_v2"
TITAN_FEE_RECIPIENT = "0x4838b106fce9647bdf1e7877bf73ce8b0bad5f97"
WORD_SECONDS = 10**9
PAIR = frozenset(
    ("0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2", "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48")
)


def load(path, size=4096):
    return strict_json_object(read_bounded(path, size))


def original(root, source, count, interval, plan_raw, *, stream=False):
    ledger = root / "original-budget.sqlite3"
    if any(
        (root / name).exists() or (root / name).is_symlink()
        for name in ("STOP", "ACTIVE", "ACTIVE.json")
    ):
        raise ValueError("campaign incomplete")
    if load(root / "identity.json") != {
        "ledger": rpc.identity(ledger),
        "plan_sha256": sha256_hex(plan_raw),
        "root": str(root.resolve()),
    }:
        raise ValueError("campaign identity mismatch")
    with RpcBudget(ledger, (BudgetPolicy(source, count, interval),), read_only=True) as budget:
        if budget.counts() != {source: 1 if stream else count}:
            raise ValueError("campaign reservation mismatch")


def diff(venue, oracle):
    fields = venue["stateOverride"].get(oracle, {})
    if fields.keys() & {"state", "code"}:
        raise ValueError("unsupported replacement in target oracle")
    return titan_state.storage(fields.get("stateDiff", {}))


def observe(rows, body, ordinal, received, digest):
    frame = strict_json_object(body)
    if set(frame) != {"venues"}:
        raise ValueError("stream envelope schema")
    selected = [
        v
        for v in titan_state.bounded_list(frame["venues"], 512, "venues")
        if isinstance(v, dict)
        and isinstance(v.get("quoteAddress"), str)
        and v["quoteAddress"].lower() in {PYTH, COMPRESSED}
    ]
    titan_state.decode_snapshot({"venues": selected})
    venues = {address(v["quoteAddress"]): v for v in selected}
    c = venues.get(COMPRESSED)
    if c is None or c["slot"] not in rows:
        return
    if c["maker"] != "Metric":
        raise ValueError("compressed venue maker mismatch")
    slot, block = c["slot"], c["blockNumber"]
    row = rows[slot]
    if row.setdefault("block", block) != block:
        raise ValueError("conflicting target heights")
    target = titan_state.BEACON_GENESIS + 12 * slot
    counts = row["counts"]
    counts["compressed_frames"] += 1
    word = diff(c, COMPRESSED).get(COMPRESSED_SLOT)
    if word is None or compressed_time(word) // 1000 != target:
        return
    counts["same_second_frames"] += 1
    a = venues.get(PYTH)
    if a is None or (a["slot"], a["blockNumber"]) != (slot, block):
        return
    if a["maker"] != "Metric":
        raise ValueError("Pyth venue maker mismatch")
    if not any(
        {address(p["token_1"]), address(p["token_2"])} == PAIR
        for p in a["assetPairs"] + c["assetPairs"]
    ):
        return
    words = diff(a, PYTH)
    keys = [metric_builder.feed_slot(i) for i in (2, 7)]
    if not set(keys) <= words.keys() or any(
        not 0 <= target - metric_builder.decode_word(words[k])["reference_time_ms"] // 1000 <= 15
        for k in keys
    ):
        return
    counts["three_slot_frames"] += 1
    if received >= target * WORD_SECONDS:
        return
    counts["before_target_wall_time"] += 1
    skew = max(a["timestamp"], c["timestamp"]) - received
    sample = {
        "frame": ordinal,
        "sha256": digest,
        "received_ns": received,
        "target": target,
        "sender_minus_received_ns": skew,
        "state": {
            PYTH: {"stateDiff": {k: words[k] for k in keys}},
            COMPRESSED: {"stateDiff": {COMPRESSED_SLOT: word}},
        },
    }
    row.setdefault("first_local", sample)
    if skew <= 0:
        counts["sender_time_ordered"] += 1
        row.setdefault("first_strict", sample)


def stream(root):
    raw = read_bounded(root / "plan.json", 4096)
    plan = strict_json_object(raw)
    start = strict_integer(plan["first_slot"], "first_slot")
    locked = strict_integer(plan["locked_ns"], "locked_ns")
    slots = strict_integer(plan["slots"], "slots")
    end_sec = titan_state.BEACON_GENESIS + 12 * (start + slots)
    connected = strict_integer(load(root / "connected.json")["received_ns"], "connected")
    terminal = load(root / "terminal.json")
    count = strict_integer(terminal["count"], "count")
    ended = strict_integer(terminal["ended_ns"], "ended")
    if (
        plan["schema"] != "oasis-titan-ws-pilot-v1"
        or plan["url"] != TITAN_WS
        or not 1 <= slots <= 64
        or not 1 <= count <= plan["max_frames"] <= 1000
        or start != (locked // WORD_SECONDS - titan_state.BEACON_GENESIS) // 12 + 2
        or int(utc(plan["end_utc"]).timestamp()) != end_sec
        or terminal["error"] is not None
        or not locked <= connected < (end_sec - slots * 12) * WORD_SECONDS <= ended
        or not 0 < terminal["bytes"] <= plan["max_bytes"] <= 120_000_000
    ):
        raise ValueError("invalid frozen stream window")
    original(root, "titan_ws", 1, 0, raw, stream=True)
    rows = {i: {"counts": Counter()} for i in range(start, start + slots)}
    last, size = connected, 0
    for i in range(count):
        body = read_bounded(root / f"{i}.msg", 2 * 1024 * 1024)
        meta = load(root / f"{i}.meta")
        received = strict_integer(meta["received_ns"], "received_ns")
        digest = sha256_hex(body)
        size += len(body)
        if meta["sha256"] != digest or not last <= received <= ended or size > plan["max_bytes"]:
            raise ValueError("stream frame integrity")
        observe(rows, body, i, received, digest)
        last = received
    if size != terminal["bytes"] or len(list(root.glob("*.msg"))) != count:
        raise ValueError("stream count or size mismatch")
    return plan, count, rows, sha256_hex(raw)


@dataclass(frozen=True)
class LocalChainPlan:
    entries: tuple
    source: str = "alchemy_public_ethereum"
    chain_id: int = 1

    def requests(self):
        return self.entries


def chain_capture(campaign, block, slot, expected_hash=None):
    ts = titan_state.BEACON_GENESIS + 12 * slot
    entries = [("eth_chainId", []), ("eth_getBlockByNumber", [hex(block), False])]
    if expected_hash is not None:
        entries.append(
            (
                "eth_getProof",
                [
                    COMPRESSED,
                    [COMPRESSED_SLOT],
                    {"blockHash": expected_hash, "requireCanonical": True},
                ],
            )
        )
    plan = LocalChainPlan(tuple(entries))
    raw = canonical_json(
        {
            "schema": "metric-stream-pilot-chain-local-v1",
            "source": plan.source,
            "entries": entries,
            "anchor": expected_hash,
            "target": block,
            "target_time": ts,
        }
    )

    def validate(index, response, first):
        result = response["result"]
        if index == 0:
            if result != "0x1":
                raise ValueError("wrong chain")
        elif index == 1:
            first, _ = prague_header(result, block, expected_hash or hash32(result["hash"]))
            if first.ref.timestamp != ts:
                raise ValueError("target slot changed")
        elif first is None:
            raise ValueError("proof without header")
        return first

    name = f"{'proof' if expected_hash else 'anchor'}-{block}"
    report = rpc.replay_loaded(campaign / name, plan, raw, validate=validate)
    if report["status"] != "complete":
        raise ValueError("incomplete block observation")
    return report["exports"]["records"]


def mined(root, slots, stream_hash):
    scope, scope_raw = rpc.load(root / "scope.json", rpc.PublicScope)
    ledger = root / "original-budget.sqlite3"
    if (
        load(root / "identity.json")
        != {
            "ledger_identity": rpc.identity(ledger),
            "scope_sha256": sha256_hex(scope_raw),
            "root": str(root.resolve()),
        }
        or any((root / name).exists() for name in ("STOP", "ACTIVE.json"))
        or [(s.source, s.max_requests) for s in scope.sources]
        != [("alchemy_public_ethereum", len(slots) * 5)]
    ):
        raise ValueError("invalid original chain campaign")
    with RpcBudget(ledger, rpc.policies(scope), read_only=True) as budget:
        if budget.counts() != {"alchemy_public_ethereum": len(slots) * 5}:
            raise ValueError("chain reservation mismatch")
    if load(root.parent / "design.json") != {
        "stream_plan_sha256": stream_hash,
        "targets": {str(slot): row["block"] for slot, row in slots.items()},
        "policy": "target plus immediate parent, then fixed-hash proof and close",
    }:
        raise ValueError("stream/chain join differs from fixed design")
    previous = None
    for slot, row in slots.items():
        n = row["block"]
        head = chain_capture(root, n, slot)[1]["response"]["result"]
        responses = chain_capture(root, n, slot, head["hash"])
        if responses[1]["response"]["result"] != head or (
            previous and head["parentHash"] != previous
        ):
            raise ValueError("block lineage mismatch")
        _, state_root = prague_header(head, n, head["hash"])
        proof = responses[2]["response"]["result"]
        account = account_proof(state_root, COMPRESSED, (COMPRESSED_SLOT,), proof)
        if (
            account.code_hash != COMPRESSED_CODE
            or not account.present
            or not account.slots[0].present
        ):
            raise ValueError("wrong compressed oracle identity")
        word = "0x" + account.slots[0].value_raw.to_bytes(32, "big").hex()
        timestamp = compressed_time(word)
        row.update(
            {
                "hash": head["hash"],
                "parent_hash": head["parentHash"],
                "fee_recipient": address(head["miner"]),
                "mined_ref_ms": timestamp,
                "mined_same_second": timestamp // 1000 == int(head["timestamp"], 16),
                "mined_word": word,
                "mined_proof_response_sha256": load(root / f"proof-{n}/2.meta.json")[
                    "response_sha256"
                ],
            }
        )
        previous = head["hash"]


def get_replay(root, source, urls):
    raw = read_bounded(root / "plan.json", 16384)
    plan = strict_json_object(raw)
    if raw != canonical_json(
        {
            "schema": "oasis-local-relay-read-v1",
            "source": source,
            "requests": urls,
            "expires_utc": plan["expires_utc"],
            "minimum_interval_ms": 650,
        }
    ):
        raise ValueError("invalid retained relay plan")
    original(root, source, len(urls), 650, raw)
    last = None
    for i, url in enumerate(urls):
        request = read_bounded(root / f"{i}.request", 4096)
        body = read_bounded(root / f"{i}.response", 256 * 1024)
        meta = load(root / f"{i}.meta")
        start, finish = utc(meta["start"]), utc(meta["finish"])
        if (
            request != canonical_json({"method": "GET", "url": url})
            or meta["http"] != 200
            or meta["error"] is not None
            or sha256_hex(request) != meta["request_sha256"]
            or sha256_hex(body) != meta["response_sha256"]
            or not start <= finish <= utc(plan["expires_utc"])
            or (last and start < last)
        ):
            raise ValueError("relay response provenance mismatch")
        last = finish
        yield body


def run(research, relay):
    pilot = research / "metric-aave-followup-20261114"
    design, count, slots, stream_hash = stream(pilot / "titan-ws-pilot")
    mined(pilot / "metric-chain/campaign", slots, stream_hash)
    (key_bytes,) = get_replay(relay / "keys", "keys", [TITAN_KEYS])
    keys = set(re.findall(r"^0x[0-9a-f]{96}$", key_bytes.decode(), re.MULTILINE))
    if not 1 <= len(keys) <= 128:
        raise ValueError("missing official Titan keys")
    urls = [f"{TITAN_RELAY}?slot={slot}" for slot in slots]
    for (slot, row), body in zip(
        slots.items(), get_replay(relay / "titan", "titan", urls), strict=True
    ):
        payload = strict_json_object(b'{"bids":' + body + b"}")
        state, key = attribution(payload["bids"], slot, row, keys)
        row.update(
            {
                "builder": state,
                "builder_pubkey": key,
                "relay_body_sha256": sha256_hex(body),
                "titan_fee_recipient_hint": row["fee_recipient"] == TITAN_FEE_RECIPIENT,
                "first_declared_word_landed": row["first_local"]["state"][COMPRESSED]["stateDiff"][
                    COMPRESSED_SLOT
                ]
                == row["mined_word"]
                if "first_local" in row
                else None,
            }
        )
    matched = [row for row in slots.values() if row["builder"] == "titan_confirmed"]
    eligible = [row for row in matched if "first_local" in row]
    return {
        "schema": "oasis-metric-stream-builder-review-v1",
        "window": design,
        "frame_count": count,
        "keys_sha256": sha256_hex(key_bytes),
        "rows": slots,
        "titan_confirmed": len(matched),
        "titan_with_local_preblock_declaration": len(eligible),
        "titan_canonical_same_second": sum(row["mined_same_second"] for row in eligible),
        "titan_exact_first_word_landed": sum(row["first_declared_word_landed"] for row in eligible),
        "titan_with_strict_clock_declaration": sum("first_strict" in row for row in eligible),
        "titan_strict_clock_same_second": sum(
            row["mined_same_second"] and "first_strict" in row for row in eligible
        ),
        "execution_eligible": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("research", "relay", "output"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    args = parser.parse_args()
    try:
        rpc.publish(args.output, run(args.research, args.relay))
    except (ValueError, TypeError, KeyError, OSError, DatabaseError) as exc:
        print(f"Metric stream replay refused ({type(exc).__name__})", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
