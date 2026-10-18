"""One retained Base NAV check against two fixed free endpoints, without retries."""

import argparse
import json
import time
from contextlib import closing
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from oasis_agent.evidence.exact_capture import decode_header, wire_envelope
from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.execution.account_capture import one_shot_session
from oasis_agent.live.config import LiveConfig
from oasis_agent.live.valuation import capture_nav, replay_nav
from oasis_agent.models.common import canonical_json, sha256_hex
from oasis_agent.rpc.budget import BudgetPolicy, RpcBudget

URLS = {"base_public": "https://mainnet.base.org", "publicnode": "https://base-rpc.publicnode.com"}
POLICIES = tuple(BudgetPolicy(source, 32, 1000) for source in URLS)


def require_recent_head(timestamp: int | None) -> None:
    if timestamp is None or not 0 <= time.time() - timestamp <= 120:
        raise ValueError("observation head is missing, future or older than 120 seconds")


def verify(directory: Path, config: LiveConfig) -> dict[str, object]:
    if directory.exists():
        raise ValueError("verification directory already exists; refusing a repeated campaign")
    directory.mkdir(parents=True)
    budget = directory / "budget.db"
    sessions = []

    def session(source):
        result = one_shot_session(source, urls=URLS, policies=POLICIES, budget=budget)
        sessions.append(result)
        return result

    report: dict[str, object] = {
        "schema": "oasis.free-live-nav-verification.v1",
        "sources": URLS,
        "retrieved_at": datetime.now(UTC).isoformat(),
        "config_hash": sha256_hex(canonical_json(asdict(config))),
        "account": config.account,
        "chain_id": config.chain_id,
        "execution_eligible": False,
        "nav_verified": False,
        "scope": "declared wallet assets only; no position or complete account history acceptance",
    }
    try:
        head = session("base_public")[0].request("eth_getBlockByNumber", ["latest", False])
        block = decode_header(head, chain_id=config.chain_id).ref
        report["block"] = asdict(block)
        require_recent_head(block.timestamp)
        with closing(EvidenceStore(directory / "evidence.db")) as store:
            value, refs = capture_nav(
                store, config, block, None, sources=tuple(URLS), sessions=session
            )
            for ref in refs:
                replay_nav(store, ref, config)
            require_recent_head(block.timestamp)
            report.update(
                nav_verified=True, nav_micros=value.nav_micros, evidence=[asdict(r) for r in refs]
            )
    except Exception:  # noqa: BLE001 - retain wire bytes; do not echo provider exceptions
        report["refusal"] = (
            "raw observation or NAV validation refused; inspect retained public wire"
        )
    report["captures"] = [
        {
            "source": s[2],
            "request": wire_envelope(s[1].exchanges, response=False),
            "response": wire_envelope(s[1].exchanges, response=True),
        }
        for s in sessions
    ]
    with RpcBudget(budget, POLICIES) as ledger:
        report["request_counts"] = ledger.counts()
    report["evidence_hash"] = sha256_hex(canonical_json(report))
    with (directory / "report.json").open("x") as output:
        json.dump(report, output, indent=2)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out", type=Path, default=Path("reports/evidence/live-nav-acceptance-20261018")
    )
    args = parser.parse_args()
    report = verify(args.out, LiveConfig.load(Path("configs/live.base-eth-usdc.json")))
    print(
        json.dumps(
            {key: report[key] for key in ("nav_verified", "request_counts", "evidence_hash")}
        )
    )


if __name__ == "__main__":
    main()
