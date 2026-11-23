"""Replay two retained Robinhood feeds into a read-only research receipt workflow."""

import argparse
from dataclasses import replace
from itertools import groupby
from pathlib import Path

from oasis_agent.arbitrage.cycle_evidence import read_bounded
from oasis_agent.arbitrage.cycle_plan import hash32
from oasis_agent.evidence.nitro_block import nitro_header, verify_receipt_root
from oasis_agent.evidence.public_receipts import decode_receipt
from oasis_agent.evidence.robinhood_nitro import (
    CHAIN_ID,
    MAX_RPC_BYTES,
    OFFICIAL_RPC,
    FeedReplay,
    NitroHintJournal,
    RpcAnchor,
)
from oasis_agent.evidence.robinhood_pair import (
    FeedArrival,
    receipt_swap_hints,
    replay_arrivals,
)
from oasis_agent.evidence.rpc_validation import (
    rpc_quantity,
    strict_integer,
    strict_json_object,
    strict_list,
    strict_object,
)
from oasis_agent.models.common import canonical_json, sha256_hex
from oasis_agent.rpc.budget import BudgetPolicy, RpcBudget
from oasis_agent.rpc.protocol import response_envelope
from scripts.capture_robinhood_nitro_feed import (
    FEED_HOSTS,
    decode_pair_capture,
    summarize_pair_capture,
)

ANCHOR_SCHEMA = "robinhood-pair-independent-anchor-20261124"
ENDPOINTS = {"official": OFFICIAL_RPC, "publicnode": "https://robinhood-rpc.publicnode.com"}
POLICIES = (BudgetPolicy("official", 3, 250), BudgetPolicy("publicnode", 5, 250))
RPC_LIMITS = {policy.source: policy.max_requests for policy in POLICIES}


def _rpc(root, name, request_id, method, params):
    request = read_bounded(root / f"{name}.request.json", 16384)
    response = read_bounded(root / f"{name}.response.json", MAX_RPC_BYTES)
    metadata = strict_json_object(read_bounded(root / f"{name}.meta.json", 16384))
    req = strict_json_object(request)
    digest = sha256_hex(response)
    if (
        req != {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        or type(req["id"]) is not int
        or metadata.get("status") != 200
        or metadata.get("response_sha256") != digest
        or strict_integer(metadata.get("received_ns"), "RPC receive time")
        < strict_integer(metadata.get("sent_ns"), "RPC send time")
        or strict_integer(metadata.get("retrieved_unix_ns"), "RPC retrieval time") <= 0
    ):
        raise ValueError("retained Robinhood RPC envelope or metadata disagrees")
    result = response_envelope(response, request_id).get("result")
    if result is None:
        raise ValueError("retained Robinhood RPC result missing")
    return result, digest


def _anchors(root, pair_report, primary):
    scope = strict_json_object(read_bounded(root / "scope.json", 16384))
    blocks = strict_list(scope.get("blocks"), "retained Robinhood anchor blocks")
    if (
        scope.get("schema") != ANCHOR_SCHEMA
        or scope.get("endpoints") != ENDPOINTS
        or scope.get("limits") != RPC_LIMITS
        or strict_integer(scope.get("usd"), "RPC cap") != 0
        or scope.get("pair_report_sha256") != sha256_hex(canonical_json(pair_report) + b"\n")
        or len(blocks) != 2
        or strict_json_object(read_bounded(root / "STOP", 16384)) != {"status": "complete"}
    ):
        raise ValueError("retained Robinhood RPC scope disagrees with paired capture")
    with RpcBudget(root / "original.sqlite3", POLICIES, read_only=True) as budget:
        if budget.counts() != RPC_LIMITS:
            raise ValueError("retained Robinhood RPC ledger counts disagree")
    for source, index in (("official", 1), ("publicnode", 4)):
        observed, _ = _rpc(root, source + "-chain", index, "eth_chainId", [])
        if rpc_quantity(observed) != CHAIN_ID:
            raise ValueError("retained Robinhood RPC chain ID mismatch")
    both = {row["sequence"]: row["block_hash"] for row in pair_report["pairs"]}
    by_height = {message.sequence: message for message in primary.messages}
    pins, anchors = [], []
    for i, block in enumerate(blocks):
        row = strict_object(block, "anchor block")
        sequence = strict_integer(row.get("height"), "anchor height")
        block_hash = hash32(row.get("hash"))
        message = by_height.get(sequence)
        if (
            message is None
            or message.block_hash != block_hash
            or both.get(sequence) != block_hash
            or message.timestamp != strict_integer(row.get("feed_timestamp"), "feed timestamp")
            or list(message.transactions) != row.get("feed_transactions")
            or (i and sequence <= pins[-1]["sequence"])
        ):
            raise ValueError("retained Robinhood anchor scope disagrees with feed payload")
        official, official_sha = _rpc(
            root, f"official-{sequence}", 2 + i, "eth_getBlockByNumber", [hex(sequence), False]
        )
        peer, peer_sha = _rpc(
            root, f"publicnode-{sequence}", 5 + i, "eth_getBlockByNumber", [hex(sequence), False]
        )
        pin = nitro_header(official, sequence, block_hash, chain_id=CHAIN_ID)
        if (
            pin != nitro_header(peer, sequence, block_hash, chain_id=CHAIN_ID)
            or pin.ref.timestamp != message.timestamp
            or pin.transactions[1:] != message.transactions
        ):
            raise ValueError(
                "independent Robinhood headers disagree with feed transaction positions"
            )
        rows, receipts_sha = _rpc(
            root, f"publicnode-receipts-{sequence}", 7 + i, "eth_getBlockReceipts", [block_hash]
        )
        committed = verify_receipt_root(pin, rows)
        hints = []
        for manifest, raw in zip(committed[1:], rows[1:], strict=True):
            if candidate := receipt_swap_hints(
                decode_receipt(raw, pin.ref, manifest.tx_hash, manifest.index)
            ):
                hints.append(candidate)
        anchors.append(
            RpcAnchor(sequence, block_hash, pin.ref.timestamp, pin.transactions, official_sha)
        )
        pins.append(
            {
                "sequence": sequence,
                "timestamp": pin.ref.timestamp,
                "parent_hash": pin.parent_hash,
                "block_hash": block_hash,
                "state_root": pin.state_root,
                "transactions_root": pin.transactions_root,
                "transactions_root_rebuilt": False,
                "receipts_root": pin.receipts_root,
                "receipt_count": len(committed),
                "signed_feed_tx_positions": list(range(1, len(committed))),
                "official_header_response_sha256": official_sha,
                "publicnode_header_response_sha256": peer_sha,
                "publicnode_receipts_response_sha256": receipts_sha,
                "quote_state_verified": False,
                "receipt_swap_candidates": hints,
            }
        )
    return tuple(anchors), pins


def _arrivals(feeds, scope, stop):
    replays, arrivals = {}, []
    start = strict_integer(scope.get("started_ns"), "capture start time")
    end = strict_integer(stop.get("stopped_ns"), "capture stop time")
    if not 0 <= start <= end:
        raise ValueError("invalid Robinhood paired capture clock")
    for source in FEED_HOSTS:
        capture = feeds[source]
        replay = FeedReplay.parse(canonical_json(capture))
        replays[source] = replay
        rows = list(zip(capture["messages"], replay.messages, strict=True))
        for (frame, clock), group in groupby(
            rows, key=lambda row: (row[0]["frame"], row[0]["received"]["monotonic_ns"])
        ):
            if not start <= clock <= end:
                raise ValueError("Robinhood observation outside paired capture clock")
            arrivals.append(
                FeedArrival(
                    source, frame, clock, replace(replay, messages=tuple(msg for _, msg in group))
                )
            )
    return tuple(arrivals), replays


def _gap(value):
    try:
        first, last = (int(number) for number in value.split(":"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("use FIRST:LAST, inclusive block numbers") from exc
    return first, last


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", type=Path)
    parser.add_argument("--sources-dir", type=Path, required=True)
    parser.add_argument("--rpc-capture", type=Path)
    parser.add_argument("--checkpoint", type=Path, help="optional SQLite continuity checkpoint")
    parser.add_argument("--omit-primary", type=_gap, help="inclusive interior fault injection")
    args = parser.parse_args()

    scope, stop, feeds = decode_pair_capture(args.capture, args.sources_dir)
    arrivals, replays = _arrivals(feeds, scope, stop)
    pins = []
    journal = NitroHintJournal(args.checkpoint)
    try:
        # An anchor archive has a fixed digest of the original paired report. An
        # injected omission only changes the journal projection.
        anchors = ()
        if args.rpc_capture is not None:
            paired = summarize_pair_capture(scope, stop, feeds)
            anchors, pins = _anchors(args.rpc_capture, paired, replays["primary"])
        report = replay_arrivals(journal, arrivals, anchors, omit_primary=args.omit_primary)
    finally:
        journal.close()
    continuity = strict_object(report["continuity"], "continuity replay")
    through = continuity["contiguous_through"]
    visible = [] if type(through) is not int else [p for p in pins if p["sequence"] <= through]
    report.update(
        {
            "schema": "oasis-robinhood-nitro-pair-shadow-v1",
            "capture_sha256": stop["captures"],
            "pair_scope_sha256": stop["scope_sha256"],
            "state_and_receipt_pins": visible,
            "rpc_timing_measured": False,
            "execution_eligible": False,
        }
    )
    print(canonical_json(report).decode())
    return 0 if continuity["status"] == "contiguous" else 3


if __name__ == "__main__":
    raise SystemExit(main())
