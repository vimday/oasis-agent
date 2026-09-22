"""Bounded, budgeted pool-state observations; never invent complete LP decision features."""

import argparse
import json
import re
import shlex
import time
from collections.abc import Callable
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import httpx
from pydantic import Field

from oasis_agent.evidence.exact_capture import wire_envelope
from oasis_agent.evidence.rpc_capture import BoundedExchangeRecorder, persist_block_range_evidence
from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.launch.models import Address, Hash, Positive, StrictModel
from oasis_agent.models.common import canonical_json, sha256_hex
from oasis_agent.models.v4 import PoolKey
from oasis_agent.rpc.budget import BudgetedPoster, BudgetPolicy, RpcBudget
from oasis_agent.rpc.client import RpcClient
from oasis_agent.rpc.collector import collect_block_range
from oasis_agent.rpc.transport import HttpPoster, JsonRpcTransport
from oasis_agent.shadow.market_checkpoint import MarketCheckpoint, MarketScope
from oasis_agent.shadow.market_window import MarketWindow, build_market_window
from oasis_agent.shadow.pool_state_capture import (
    Slot0WindowQuorum,
    collect_slot0_window,
    persist_slot0_capture,
    replay_slot0_window_evidence,
)
from oasis_agent.uniswap_v4.events import SWAP_TOPIC
from oasis_agent.uniswap_v4.pool_id import pool_id_from_key


class MarketConfig(StrictModel):
    pool: PoolKey
    blocks: Annotated[int, Field(ge=2, le=10)]
    horizon_seconds: Annotated[int, Field(ge=1, le=300)]
    max_lateness_seconds: Annotated[int, Field(ge=1, le=300)]


class Config(StrictModel):
    market: MarketConfig | None = None
    chain_id: Positive
    pool_id: Hash
    state_view: Address
    pool_manager: Address
    state_view_runtime_keccak: Hash
    pool_manager_runtime_keccak: Hash
    public_request_cap: Annotated[int, Field(ge=1, le=1000)]
    paid_request_cap: Annotated[int, Field(ge=1, le=100)]
    public_interval_ms: Annotated[int, Field(ge=1000, le=60000)]
    paid_interval_ms: Annotated[int, Field(ge=3000, le=60000)]
    max_age_seconds: Annotated[int, Field(ge=1, le=300)]

    def policies(self) -> tuple[BudgetPolicy, ...]:
        return (
            BudgetPolicy("public", self.public_request_cap, self.public_interval_ms),
            BudgetPolicy("paid", self.paid_request_cap, self.paid_interval_ms),
        )


def env_urls(
    path: Path,
    *,
    secondary: bool,
    primary_env: str = "ROBINHOOD_RPC_URL",
    secondary_env: str = "ROBINHOOD_ALCHEMY_RPC_URL",
) -> dict[str, str]:
    names = (primary_env, secondary_env) if secondary else (primary_env,)
    if len(set(names)) != len(names) or any(
        not re.fullmatch(r"[A-Z_][A-Z0-9_]{0,127}", name) or "RPC" not in name or "KEY" in name
        for name in names
    ):
        raise ValueError("distinct explicit RPC environment names required")
    wanted = {primary_env: "public"}
    if secondary:
        wanted[secondary_env] = "paid"
    result = {}
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip().removeprefix("export ").strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() not in wanted:
            continue
        alias = wanted[key.strip()]
        tokens = shlex.split(value, comments=True)
        if len(tokens) != 1 or alias in result:
            raise ValueError("ambiguous RPC configuration")
        url = httpx.URL(tokens[0])
        if url.scheme != "https" or not url.host or url.userinfo or url.fragment:
            raise ValueError("RPC must be a credential-safe HTTPS endpoint")
        result[alias] = tokens[0]
    if set(result) != set(wanted.values()) or len(set(result.values())) != len(result):
        raise ValueError("distinct configured RPC endpoints required")
    return result


class Http:
    def __init__(self, url: str):
        self.url = url
        self.client = httpx.Client(timeout=10, follow_redirects=False)

    def post(self, payload: bytes) -> bytes:
        response = self.client.post(
            self.url, content=payload, headers={"Content-Type": "application/json"}
        )
        response.raise_for_status()
        return response.content


def sample(
    config: Config,
    posters: dict[str, HttpPoster],
    *,
    budget_path: Path,
    output: Path,
    now: int | None = None,
    check_market: Callable[[MarketWindow], None] | None = None,
) -> dict[str, object]:
    """Bounded state or market window: public discovery plus independently pinned reads."""
    if set(posters) not in ({"public"}, {"public", "paid"}):
        raise ValueError("public provider required; optional paid verifier only")
    market = config.market
    if market is not None and (
        set(posters) != {"public", "paid"} or pool_id_from_key(market.pool) != config.pool_id
    ):
        raise ValueError("market acquisition requires matching PoolKey and two sources")
    output.mkdir(parents=True, exist_ok=False)
    policies = config.policies()
    with RpcBudget(budget_path, policies) as budget:
        before = budget.counts()
        # Slot0: 1 + 6N; activity: chain + N headers + logs + closing.
        per_source = 7 if market is None else 7 * market.blocks + 4
        needed = {"public": per_source + 2, **({"paid": per_source} if "paid" in posters else {})}
        if any(
            before[source] + count > budget.policies[source].max_requests
            for source, count in needed.items()
        ):
            raise ValueError("insufficient remaining RPC budget for complete sample")
    store = EvidenceStore(output / "evidence.db")
    records: list[tuple[str, BoundedExchangeRecorder]] = []
    report: dict[str, object] = {
        "schema": "oasis.pool-state-observer.v1",
        "execution_eligible": False,
        "status": "halt",
    }

    def transport(source: str) -> JsonRpcTransport:
        recorder = BoundedExchangeRecorder()
        records.append((source, recorder))
        return JsonRpcTransport(
            "https://observation.invalid",
            recorder=recorder,
            retries=0,
            http=BudgetedPoster(posters[source], budget_path, policies, source),
        )

    try:
        head = RpcClient(
            transport("public"), expected_chain_id=config.chain_id
        ).get_block_snapshot()
        observed_at = int(time.time()) if now is None else now
        if (
            head.ref.timestamp is None
            or not 0 <= observed_at - head.ref.timestamp <= config.max_age_seconds
        ):
            raise ValueError("stale or future public head")
        captures, references, swap_references = [], [], []
        numbers = (
            (head.ref.number,)
            if market is None
            else tuple(range(head.ref.number - market.blocks + 1, head.ref.number + 1))
        )
        for source in posters:
            rpc = transport(source)
            capture = collect_slot0_window(
                (rpc, records[-1][1], source),
                chain_id=config.chain_id,
                state_view=config.state_view,
                pool_manager=config.pool_manager,
                pool_id=config.pool_id,
                block_numbers=numbers,
                state_view_runtime_keccak=config.state_view_runtime_keccak,
                pool_manager_runtime_keccak=config.pool_manager_runtime_keccak,
            )
            if capture.headers[-1] != head:
                raise ValueError("selected head changed before capture")
            reference = persist_slot0_capture(
                store, capture, retrieved_at=datetime.now(UTC).isoformat()
            )
            if replay_slot0_window_evidence(store, reference) != capture:
                raise ValueError("retained pool state replay mismatch")
            captures.append(capture)
            references.append(reference)
            if market is not None:
                activity_rpc = transport(source)
                activity = collect_block_range(
                    RpcClient(activity_rpc, expected_chain_id=config.chain_id),
                    source=source,
                    start_block=numbers[0],
                    end_block=numbers[-1],
                    address=config.pool_manager,
                    topics=(SWAP_TOPIC, config.pool_id),
                    max_blocks=market.blocks,
                )
                if activity.blocks != capture.headers:
                    raise ValueError("activity changed after state acquisition")
                swap_references.append(
                    persist_block_range_evidence(store, activity, records[-1][1].exchanges)
                )
        if len(captures) == 2:
            Slot0WindowQuorum(*captures)
        market_payload = None
        captured_window = None
        if market is not None:
            window = build_market_window(
                store,
                swap_references=tuple(swap_references),
                price_references=tuple(references),
                pool=market.pool,
                pool_manager=config.pool_manager,
                horizon_seconds=market.horizon_seconds,
                max_lateness_seconds=market.max_lateness_seconds,
                mature_prefix=True,
            )
            if not window.markouts_complete:
                raise ValueError("market window has unavailable mature labels")
            captured_window = window
            market_payload = json.loads(window.payload)
        finished = int(time.time()) if now is None else now
        if not 0 <= finished - head.ref.timestamp <= config.max_age_seconds:
            raise ValueError("snapshot expired during acquisition")
        if captured_window is not None and check_market is not None:
            check_market(captured_window)
        report.update(
            status="observed",
            trust="two_source_state" if len(captures) == 2 else "single_source_research",
            block=asdict(head.ref),
            pool_id=config.pool_id,
            state=asdict(captures[0].slot0[-1]),
            evidence=[asdict(ref) for ref in references],
            lp_action="unavailable",
            market_window=market_payload,
            missing_lp_inputs=[
                "independent_reference_price",
                "reconciled_inventory",
                "matured_swap_markouts",
                "executable_route_depth",
                "complete_cost_model",
                "out_of_sample_economic_acceptance",
            ],
        )
        if market_payload is not None:
            report["missing_lp_inputs"] = [
                item for item in report["missing_lp_inputs"] if item != "matured_swap_markouts"
            ]
    except Exception:  # noqa: BLE001 - RPC messages and URLs may contain credentials
        report["reason"] = "state_acquisition_or_validation_failed"
    finally:
        wire_refs = []
        for source, recorder in records:
            if recorder.exchanges:
                wire_refs.append(
                    asdict(
                        store.put(
                            "pool_observer_wire",
                            canonical_json({"execution_eligible": False}),
                            None,
                            source,
                            request=wire_envelope(recorder.exchanges, response=False),
                            raw_response=wire_envelope(recorder.exchanges, response=True),
                        )
                    )
                )
        report["wire_evidence"] = wire_refs
        with RpcBudget(budget_path, policies) as budget:
            after = budget.counts()
        report["campaign_requests_spent"] = after
        report["sample_requests_spent"] = {key: after[key] - before[key] for key in before}
        report["observed_at"] = datetime.now(UTC).isoformat()
        store.close()
        with (output / "report.json").open("xb") as handle:
            handle.write(canonical_json(report) + b"\n")
    return report


def open_market_checkpoint(
    config: Config, budget_path: Path, checkpoint_path: Path
) -> MarketCheckpoint:
    market = config.market
    if market is None:
        raise ValueError("market configuration required")
    if budget_path.exists() and not checkpoint_path.exists():
        with RpcBudget(budget_path, config.policies()) as budget:
            if any(budget.counts().values()):
                raise ValueError("spent market budget has lost its checkpoint")
    checkpoint = MarketCheckpoint(
        checkpoint_path,
        MarketScope(
            config.chain_id,
            market.pool,
            config.pool_manager,
            market.horizon_seconds,
            market.max_lateness_seconds,
            sha256_hex(config.model_dump_json().encode()),
        ),
    )
    if not budget_path.exists() and checkpoint.read() is not None:
        checkpoint.close()
        raise ValueError("retained market checkpoint has lost its RPC budget")
    return checkpoint


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--checkpoint", type=Path, help="reuse market cursor across restarts")
    parser.add_argument(
        "--budget", type=Path, required=True, help="reuse this ledger across restarts"
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument(
        "--verify-secondary", action="store_true", help="use the bounded secondary provider"
    )
    parser.add_argument("--primary-env", default="ROBINHOOD_RPC_URL")
    parser.add_argument("--secondary-env", default="ROBINHOOD_ALCHEMY_RPC_URL")
    parser.add_argument("--samples", type=int, default=1)
    parser.add_argument("--interval-seconds", type=int, default=10)
    args = parser.parse_args()
    if not 1 <= args.samples <= 10 or not 5 <= args.interval_seconds <= 60:
        parser.error("1..10 samples and 5..60 second interval required")
    posters = {}
    checkpoint = None
    try:
        config = Config.from_json(args.config.read_bytes())
        if config.market is not None:
            checkpoint = open_market_checkpoint(
                config,
                args.budget,
                args.checkpoint or args.budget.with_name(args.budget.name + ".market.db"),
            )
        urls = env_urls(
            args.env_file,
            secondary=args.verify_secondary,
            primary_env=args.primary_env,
            secondary_env=args.secondary_env,
        )
        posters = {source: Http(url) for source, url in urls.items()}
        for index in range(args.samples):
            if index:
                time.sleep(args.interval_seconds)
            sample_output = args.output / str(index + 1)

            def retain_market(window: MarketWindow, directory: Path = sample_output) -> None:
                if checkpoint is not None:
                    checkpoint.append(directory / "evidence.db", window)

            result = sample(
                config,
                posters,
                budget_path=args.budget,
                output=sample_output,
                check_market=retain_market,
            )
            print(
                json.dumps({key: result[key] for key in ("status", "sample_requests_spent")}),
                flush=True,
            )
            if result["status"] != "observed":
                raise ValueError("observer halted")
    except Exception:  # noqa: BLE001 - never expose raw endpoint errors
        parser.exit(
            1, "Pool observer stopped; inspect evidence and budget; no eligibility granted.\n"
        )
    finally:
        if checkpoint is not None:
            checkpoint.close()
        for poster in posters.values():
            poster.client.close()


if __name__ == "__main__":
    main()
