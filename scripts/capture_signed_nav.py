"""Plan or capture one signed-price NAV candidate using an existing RPC budget.

No price fetching, secret-file loading, signing, broadcasting or release promotion.
The default is an offline plan; --capture explicitly enables one bounded RPC pass.
"""

import argparse
import json
import os
import re
import stat
import time
from contextlib import closing
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlsplit

from oasis_agent.evidence.exact_capture import decode_header
from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.execution.account_capture import one_shot_session
from oasis_agent.live.config import LiveConfig
from oasis_agent.live.positions import PositionOutcomeScope
from oasis_agent.live.rpc_profile import LiveRpcProfile
from oasis_agent.live.signed_nav import (
    _validate_mapping,
    capture_signed_nav_candidate,
    verify_signed_nav_quorum,
)
from oasis_agent.live.signed_prices import MAX_RAW_BYTES, verify_prices
from oasis_agent.live.state import Position
from oasis_agent.models.common import canonical_json, sha256_hex
from oasis_agent.models.v4 import PoolKey
from oasis_agent.rpc.budget import RpcBudget
from scripts.verify_signed_nav import unique

SOURCES = ("live_primary", "live_secondary")


def read_regular(path, limit=64_000):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError("bounded regular input required")
        raw = handle.read(limit + 1)
    if len(raw) > limit:
        raise ValueError("input exceeds byte bound")
    return raw


def position_input(raw, c):
    data = json.loads(raw, object_pairs_hook=unique)
    if data is None:
        return None
    if (
        not isinstance(data, dict)
        or set(data) != {"token_id", "lower", "upper", "liquidity", "opened_at"}
        or any(type(v) is not int for v in data.values())
        or not 0 <= data["opened_at"] <= time.time_ns() // 1_000_000_000
    ):
        raise ValueError("explicit integer position or null required")
    p = Position(**data)
    scope = PositionOutcomeScope(
        c.chain_id,
        c.account,
        c.position_manager,
        c.state_view,
        c.expected_pins["position_manager"],
        c.expected_pins["state_view"],
        PoolKey(*c.pool.tuple()),
        p.token_id,
        p.lower,
        p.upper,
        p.liquidity,
        False,
    )
    if scope.pool_id != c.pool_id:
        raise ValueError("position pool differs from configuration")
    return p


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("config", "prices", "position", "rpc-profile", "budget", "out"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--primary-rpc-env", default="BASE_RPC_PRIMARY_ALT")
    parser.add_argument("--capture", action="store_true")
    args = parser.parse_args()
    # A new output directory does not create a new budget or a new spending allowance.
    if args.out.exists() or args.out.is_symlink():
        parser.error("output already exists; preserve previous evidence")
    args.out.mkdir(parents=True, mode=0o700)
    report = {
        "schema": "oasis.signed-nav-acquisition.v1",
        "passed": False,
        "mode": "capture" if args.capture else "offline_plan",
        "execution_eligible": False,
        "clock_source": "host",
        "candidate_verified": False,
        "scope": "declared ETH/USDC and optional LP; not full history or live release",
    }
    stage = "local_inputs"
    attempts = dict.fromkeys(SOURCES, 0)
    policies = None
    budget_valid = False
    request = None
    try:
        c = LiveConfig.from_json(read_regular(args.config))
        _validate_mapping(c)
        position = position_input(read_regular(args.position), c)
        profile = LiveRpcProfile(
            **json.loads(read_regular(args.rpc_profile), object_pairs_hook=unique)
        )
        policies = profile.policies(SOURCES)
        raw = read_regular(args.prices, MAX_RAW_BYTES)
        started = time.time_ns() // 1_000_000
        stage = "price_verification"
        prices = verify_prices(
            raw, observed_at_ms=started, max_age_ms=c.oracle_max_age_seconds * 1000
        )
        # Wallet: 10; core NAV without Chainlink: 5; optional NFT: 9 + fee reads: 2.
        per_source = 26 if position is not None else 15
        limits = {SOURCES[0]: per_source + 1, SOURCES[1]: per_source}
        stage = "existing_budget"
        if args.budget.is_symlink() or not args.budget.is_file():
            raise ValueError("existing budget required")
        with RpcBudget(args.budget, policies, require_existing=True) as ledger:
            before = ledger.counts()
        budget_valid = True
        if any(before[s] + limits[s] > profile.max_requests for s in SOURCES):
            raise ValueError("insufficient retained quota for one complete capture")
        report.update(
            config_hash=sha256_hex(canonical_json(asdict(c))),
            price_input_hash=sha256_hex(raw),
            sources=list(SOURCES),
            position=asdict(position) if position else None,
            request_limits=limits,
            request_counts_before=before,
            rpc_profile=asdict(profile),
            price_verification=prices,
            spending_scope="request quota only; monetary cost must be established externally",
        )
        if args.capture:
            stage = "rpc_environment"
            names = (args.primary_rpc_env, c.secondary_rpc_env)
            if any(re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", name) is None for name in names):
                raise ValueError("explicit RPC environment names required")
            urls = {source: os.environ[name] for source, name in zip(SOURCES, names, strict=True)}
            parsed = [urlsplit(url) for url in urls.values()]
            if (
                any(
                    u.scheme != "https" or not u.hostname or u.username or u.password or u.fragment
                    for u in parsed
                )
                or parsed[0].hostname == parsed[1].hostname
            ):
                raise ValueError("two distinct HTTPS provider hosts required")

            class LimitedTransport:
                def __init__(self, source, transport):
                    self.source, self.transport = source, transport

                def request(self, method, params):
                    now = time.time_ns() // 1_000_000
                    if (
                        now < started
                        or now - started > 120_000
                        or now - prices["timestamp_ms"] > c.oracle_max_age_seconds * 1000
                        or attempts[self.source] >= limits[self.source]
                    ):
                        raise ValueError("capture time or request bound reached")
                    attempts[self.source] += 1
                    return self.transport.request(method, params)

            def session(source):
                transport, recorder, alias = one_shot_session(
                    source,
                    urls=urls,
                    policies=policies,
                    budget=args.budget,
                    require_existing_budget=True,
                )
                return LimitedTransport(source, transport), recorder, alias

            stage = "head_capture"
            block = decode_header(
                session(SOURCES[0])[0].request("eth_getBlockByNumber", ["latest", False]),
                chain_id=c.chain_id,
            ).ref
            report["block"] = asdict(block)
            stage = "nav_capture"
            with closing(EvidenceStore(args.out / "evidence.db")) as store:
                _, refs = capture_signed_nav_candidate(
                    store,
                    c,
                    block,
                    position,
                    raw_prices=raw,
                    observed_at_ms=time.time_ns() // 1_000_000,
                    sources=SOURCES,
                    sessions=session,
                )
                stage = "current_clock_replay"
                result = verify_signed_nav_quorum(
                    store,
                    c,
                    refs,
                    sources=SOURCES,
                    position=position,
                    now_ms=time.time_ns() // 1_000_000,
                )
                finished = time.time_ns() // 1_000_000
                if (
                    finished < result["checked_at_ms"]
                    or finished < started
                    or finished - block.timestamp * 1000 > 120_000
                    or finished - prices["timestamp_ms"] > c.oracle_max_age_seconds * 1000
                ):
                    raise ValueError("capture expired during replay")
                request = {
                    "schema": "oasis.signed-nav-check-input.v1",
                    "sources": list(SOURCES),
                    "position": report["position"],
                    "evidence": [asdict(ref) for ref in refs],
                }
                report.update(candidate_verified=True, result=result, completed_at_ms=finished)
        report["passed"] = True
    except Exception:  # noqa: BLE001 - never echo provider, environment or file contents
        report["refusal_stage"] = stage
        report["refusal"] = "signed NAV acquisition refused; preserve evidence and budget"
    report["request_attempts"] = attempts
    if budget_valid:
        try:
            with RpcBudget(args.budget, policies, require_existing=True) as ledger:
                report["request_counts_after"] = ledger.counts()
        except Exception:  # noqa: BLE001 - never create a replacement budget
            report.update(passed=False, candidate_verified=False, refusal_stage="final_budget")
    if report["passed"] and report["candidate_verified"]:
        finished = time.time_ns() // 1_000_000
        if (
            finished < report["completed_at_ms"]
            or finished - report["block"]["timestamp"] * 1000 > 120_000
            or finished - report["price_verification"]["timestamp_ms"]
            > c.oracle_max_age_seconds * 1000
        ):
            report.update(passed=False, candidate_verified=False, refusal_stage="publication_clock")
        else:
            with (args.out / "check-input.json").open("x") as handle:
                json.dump(request, handle, indent=2)
            report["completed_at_ms"] = finished
    report["evidence_hash"] = sha256_hex(canonical_json(report))
    with (args.out / "report.json").open("x") as handle:
        json.dump(report, handle, indent=2)
    print(
        json.dumps(
            {
                k: report[k]
                for k in ("passed", "candidate_verified", "execution_eligible", "evidence_hash")
            }
        )
    )
    raise SystemExit(0 if report["passed"] else 3)


if __name__ == "__main__":
    main()
