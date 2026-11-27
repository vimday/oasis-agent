"""Verify the USDC six-swap and four-action controls in the frozen November 21 window."""

import json
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import asdict
from itertools import pairwise
from pathlib import Path

from measure_eth_nov21_window import amount

from oasis_agent.arbitrage.cycle_protocols import abi, data_bytes, topic
from oasis_agent.evidence.public_receipts import decode_receipt
from oasis_agent.evidence.research_traces import first_failure, nested_calls
from oasis_agent.evidence.state_trie import prague_header
from oasis_agent.evidence.swap_flow_trace import _receipt_positions, _transfers
from oasis_agent.evidence.weth_principal import MANAGER, WETH
from oasis_agent.models.common import sha256_hex

USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"


def raw_digest(path):
    return sha256_hex(path.read_bytes())


def stopped_day(root):
    stop = json.loads((root / "STOP.json").read_bytes())
    ledger = [json.loads(line) for line in (root / "ledger.jsonl").read_bytes().splitlines()]
    assert [(x["id"], x["phase"]) for x in ledger] == [
        (1, "reserve"),
        (1, "received"),
        (2, "reserve"),
    ]
    form = ET.fromstring((root / "list.xml").read_bytes())
    assert form.findtext("{*}IsTruncated") == "false"
    assert form.findtext("{*}KeyCount") == "1"
    assert stop["exception"] == "TimeoutError"
    assert ledger[1]["status"] == 200 and ledger[1]["sha256"] == raw_digest(root / "list.xml")
    listed = int(form.findtext(".//{*}Size"))
    received = int((root / "partial-bytes.txt").read_text())
    assert 0 < received < listed
    return {
        "date": "2026-11-26",
        "advertised_bytes": listed,
        "partial_bytes": received,
        "partial_sha256": (root / "partial-sha256.txt").read_text().split()[0],
        "attempts": 2,
        "object_reserved_to_stop_ns": stop["stop_ns"] - ledger[-1]["at_ns"],
        "stop": stop,
        "stop_sha256": raw_digest(root / "STOP.json"),
        "thresholds": None,
    }


def witnesses(root, result):
    winner = result["ranked_top"][0]
    n, i, tx = (winner[k] for k in ("height", "index", "hash"))

    def get(label):
        return json.loads((root / f"{label}.response.json").read_bytes())["result"]

    header = get(f"{n}-header")
    block, _ = prague_header(header, n, header["hash"])
    receipt = decode_receipt(get(f"{n}-receipts")[i], block.ref, tx, i)
    trace = get(f"{tx[2:14]}-trace")
    frames = nested_calls(trace)
    positions = _receipt_positions(trace, frames, receipt)
    log = {row.log_index: row for row in receipt.logs}
    transfers = {row.index: row for row in _transfers(receipt, positions)}
    topics = json.loads((root / "topics.json").read_bytes())
    groups = defaultdict(list)
    for index, row in log.items():
        if row.topics and row.topics[0] in topics:
            groups[positions[index][0]].append(index)
    assert sum(map(len, groups.values())) == winner["events"]
    failed = [p[0] for p, f in frames.items() if len(p) == 1 and f.error]
    assert not set(failed).intersection(groups)

    def verify_leg(group, event, incoming, outgoing):
        row = log[event]
        fin, fout = transfers[incoming], transfers[outgoing]
        assert row.address == fin.recipient == fout.sender
        assert first_failure(frames, positions[event]) is None
        assert all(positions[k][0] == group for k in (event, incoming, outgoing))
        kind = topics[row.topics[0]]
        assert amount(kind, row.data[2:]) == (fin.amount, fout.amount)
        return {
            "swap_log": event,
            "kind": kind,
            "pool": row.address,
            "call_path": positions[event],
            "input": asdict(fin),
            "output": asdict(fout),
        }

    def all_touches(group, token, exact):
        assert [
            t.index
            for t in transfers.values()
            if t.path and t.path[0] == group and t.token == token
        ] == exact

    def cash(group, start, gross, repay, residue, lender, operator):
        a, b, c, d = (transfers[idx] for idx in (start, gross, repay, residue))
        assert all(t.token == USDC and t.path[0] == group for t in (a, b, c, d))
        assert (a.sender, b.recipient, c.sender, c.recipient, d.sender, d.recipient) == (
            lender,
            operator,
            operator,
            lender,
            operator,
            receipt.recipient,
        )
        assert a.amount == c.amount
        assert b.amount - c.amount == d.amount > 0
        return {
            k: asdict(v)
            for k, v in zip(("principal", "gross", "repaid", "residual"), (a, b, c, d), strict=True)
        }

    six = [
        verify_leg(6, *leg)
        for leg in (
            (965, 963, 964),
            (973, 971, 972),
            (981, 979, 980),
            (984, 983, 982),
            (987, 986, 985),
            (990, 988, 989),
        )
    ]
    assert [x["swap_log"] for x in six] == winner["proxy_events"]
    for source, target in pairwise(six):
        assert (source["output"]["token"], source["output"]["amount"]) == (
            target["input"]["token"],
            target["input"]["amount"],
        )
    actor = transfers[979].sender
    helper = transfers[963].sender
    assert (transfers[962].recipient, transfers[964].recipient) == (helper, helper)
    assert transfers[962].amount == transfers[963].amount
    basket = [964, 966, 970, 971, 978]
    all_touches(6, transfers[964].token, basket)
    assert len({transfers[k].amount for k in basket}) == 1
    assert (transfers[966].sender, transfers[966].recipient) == (helper, MANAGER)
    assert (transfers[970].sender, transfers[970].recipient) == (MANAGER, actor)
    assert transfers[971].sender == MANAGER
    assert (transfers[978].sender, transfers[978].recipient) == (actor, MANAGER)
    for indices in ([972, 977, 979], [980, 983], [982, 986], [985, 988]):
        all_touches(6, transfers[indices[0]].token, indices)
        assert len({transfers[k].amount for k in indices}) == 1
        assert all(transfers[k].recipient == transfers[m].sender for k, m in pairwise(indices))
    all_touches(6, USDC, [962, 963, 989, 991, 992])
    six_cash = cash(6, 962, 989, 991, 992, MANAGER, actor)

    four = [
        verify_leg(11, *leg) for leg in ((1046, 1045, 1032), (1036, 1033, 1034), (1039, 1038, 1037))
    ]
    for indices in ([1032, 1033], [1034, 1038]):
        all_touches(11, transfers[indices[0]].token, indices)
        source, target = (transfers[k] for k in indices)
        assert (source.recipient, source.amount) == (target.sender, target.amount)
    ingress = transfers[1037]
    withdrawal = log[1040]
    wpath = positions[1040]
    assert withdrawal.address == WETH and withdrawal.topics[0] == topic(
        "Withdrawal(address,uint256)"
    )
    assert abi(("address",), data_bytes(withdrawal.topics[1])) == (actor,)
    assert abi(("uint256",), data_bytes(withdrawal.data)) == (ingress.amount,)
    native, router = frames[(*wpath, 0)], frames[positions[1044]]
    assert (native.sender, native.recipient, native.value) == (WETH, actor, ingress.amount)
    assert (router.sender, router.recipient, router.value) == (
        actor,
        log[1044].address,
        ingress.amount,
    )
    assert not first_failure(frames, positions[1044])
    _, paid, received, recipient = abi(
        ("uint256", "uint256", "uint256", "address"), data_bytes(log[1044].data)
    )
    assert (paid, received, recipient) == (ingress.amount, transfers[1042].amount, actor)
    assert (transfers[1042].token, transfers[1042].recipient) == (USDC, actor)
    assert positions[1042][: len(positions[1044])] == positions[1044]
    all_touches(11, USDC, [1042, 1045, 1047])
    assert transfers[1045].recipient == four[0]["pool"]
    assert transfers[1042].amount - transfers[1045].amount == transfers[1047].amount
    assert transfers[1047].recipient == receipt.recipient
    assert transfers[1045].path[: len(positions[1046])] == positions[1046]
    four.append(
        {
            "protocol_operation_log": 1044,
            "external_call": positions[1044],
            "wrapped_output": asdict(ingress),
            "unwrap_log": 1040,
            "native_forward": (*wpath, 0),
            "native_spend": router.value,
            "usdc_output": asdict(transfers[1042]),
            "usdc_repayment": asdict(transfers[1045]),
            "usdc_residual": asdict(transfers[1047]),
        }
    )
    assert all(log[i].address == MANAGER and positions[i][0] == 6 for i in (969, 976))
    return {
        "committed_groups": sorted(groups.items()),
        "failed_direct_subcalls": sorted(failed),
        "six_swap_legs": six,
        "v4_events_excluded_from_six": [
            {"log_index": i, "path": positions[i], "pool_id": log[i].topics[1]} for i in (969, 976)
        ],
        "six_swap_usdc_cash": six_cash,
        "six_swap_crvusd_netting_transfers": [asdict(transfers[k]) for k in basket],
        "four_action_callback": four,
    }


def main():
    if not __debug__:
        raise SystemExit("run this verifier without Python optimization")
    root = Path(sys.argv[1])
    result = json.loads((root / "result.json").read_bytes())
    report = {
        "schema": "ethereum-independent-window-usdc-closures-v1",
        "incomplete_utc_day": stopped_day(root.parent / "eth-unseen-date-20261127"),
        "limited_window": result,
        "normal_replay_sha256": raw_digest(root / "result.json"),
        "detailed_receipt_witness": witnesses(root, result),
    }
    print(json.dumps(report, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
