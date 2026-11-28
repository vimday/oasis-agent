"""One-shot probe: which Base WS log filters PublicNode accepts and how logs order against heads.

Read-only, anonymous, budgeted. Every frame is retained with its local monotonic receive time.
Stops on the first refusal. No retry.
"""

import argparse
import json
import ssl
import time
from pathlib import Path

import certifi
from websockets.sync.client import connect

from oasis_agent.fastpath.state_sync import (
    AERO_SYNC_TOPIC,
    V2_SYNC_TOPIC,
    V3_BURN_TOPIC,
    V3_MINT_TOPIC,
    V3_SWAP_TOPIC,
)
from oasis_agent.models.common import canonical_json, sha256_hex
from oasis_agent.rpc.budget import BudgetPolicy, RpcBudget

ENDPOINT = "wss://base-rpc.publicnode.com"
ROSTER = Path("reports/research/base-resident-forward-20261123/run/multihop_replay/manifest.json")
TOPICS = [V3_SWAP_TOPIC, V3_MINT_TOPIC, V3_BURN_TOPIC, V2_SYNC_TOPIC, AERO_SYNC_TOPIC]
# connection + chainId + newHeads + up to three log subscriptions, per arm
POLICIES = (BudgetPolicy("ws", 12, 0),)
WINDOW_S = 6.0
MAX_FRAMES = 20_000


def arm(root: Path, name: str, filters: list[dict[str, object]], budget: RpcBudget) -> dict:
    frames = root / f"{name}.jsonl"
    out = frames.open("xb")
    count = 0

    def spend() -> None:
        if budget.reserve("ws", now_ms=time.time_ns() // 1_000_000):
            raise ValueError("ws budget pacing")

    spend()
    result: dict[str, object] = {"arm": name, "filters": filters}
    with connect(
        ENDPOINT,
        proxy=None,
        ssl=ssl.create_default_context(cafile=certifi.where()),
        open_timeout=6,
        close_timeout=1,
        max_size=4 * 1024 * 1024,
        compression=None,
        ping_interval=None,
    ) as ws:
        requests = [("eth_chainId", []), ("eth_subscribe", ["newHeads"])]
        requests += [("eth_subscribe", ["logs", f]) for f in filters]
        for i, (method, params) in enumerate(requests, start=1):
            spend()
            ws.send(canonical_json({"jsonrpc": "2.0", "id": i, "method": method, "params": params}))
        roles: dict[str, str] = {}
        acks = 0
        start = time.monotonic_ns()
        deadline = start + int(WINDOW_S * 1e9)
        while time.monotonic_ns() < deadline and count < MAX_FRAMES:
            try:
                raw = ws.recv(timeout=max(0.01, (deadline - time.monotonic_ns()) / 1e9))
            except TimeoutError:
                break
            now = time.monotonic_ns()
            data = raw if isinstance(raw, bytes) else raw.encode()
            out.write(canonical_json({"mono_ns": now, "frame": data.decode()}) + b"\n")
            count += 1
            msg = json.loads(data)
            if "id" in msg:
                acks += 1
                if "error" in msg:
                    result.setdefault("refusals", []).append({"id": msg["id"], **msg["error"]})
                elif msg["id"] >= 2:
                    roles[msg["result"]] = "head" if msg["id"] == 2 else f"log{msg['id'] - 2}"
                if "error" in msg:
                    break
    out.close()
    result.update(
        frames=count,
        roles=roles,
        frames_sha256=sha256_hex(frames.read_bytes()),
        bytes=frames.stat().st_size,
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    root: Path = args.root
    root.mkdir(parents=True)
    pools = [row["pool_id"] for row in json.loads(ROSTER.read_bytes())["pool_specs"]]
    ledger = root / "original.sqlite3"
    results = []
    with RpcBudget(ledger, POLICIES) as budget:
        # A: all 58 addresses in one filter. B: topic-only (all Base pools, census volume).
        for name, filters in (
            ("addr58", [{"address": pools, "topics": [TOPICS]}]),
            ("topic_only", [{"topics": [TOPICS]}]),
        ):
            try:
                results.append(arm(root, name, filters, budget))
            except Exception as exc:  # noqa: BLE001 - record and stop the one-shot probe
                results.append({"arm": name, "error": type(exc).__name__, "detail": str(exc)})
                break
            if results[-1].get("refusals"):
                continue
        counts = budget.counts()
    summary = {"schema": "oasis.base-log-subscription-probe.v1", "results": results}
    summary["ledger_counts"] = counts
    (root / "summary.json").write_bytes(canonical_json(summary))
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
