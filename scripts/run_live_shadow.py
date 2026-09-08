"""Bounded live LP shadow observation against one public RPC (WP6).

Acquires a block-pinned log window, reconstructs observed swaps, quotes the
configured fee strategy, and writes evidence + comparison report. Never signs,
never broadcasts, never produces an execution-eligible decision. Research
single-provider: full decision features require a second agreeing provider.
"""

import argparse
import hashlib
import json
import os
import subprocess
import time
from collections.abc import Mapping
from dataclasses import asdict
from pathlib import Path

from oasis_agent.evidence.rpc_capture import BoundedExchangeRecorder
from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.farming.strategy import OnchainAmountBandFeePolicy
from oasis_agent.models.common import canonical_json, normalize_address, normalize_hash32
from oasis_agent.models.v4 import PoolKey
from oasis_agent.rpc.transport import RpcExchange
from oasis_agent.shadow.producer import (
    ObservedPoolKey,
    acquire_observation,
    build_observation,
    open_observation_rpc,
    persist_observation,
)
from oasis_agent.shadow.service import parse_policy
from oasis_agent.uniswap_v4.events import SWAP_TOPIC
from oasis_agent.uniswap_v4.pool_id import pool_id_from_key

DYNAMIC_FEE_SENTINEL = 0x800000
TICK_SPACING = 60


class WireRecorder(BoundedExchangeRecorder):
    """Bounded recorder that appends public RPC exchanges to a JSONL file."""

    def __init__(self, output: Path) -> None:
        super().__init__(max_exchanges=5_000, max_bytes=50_000_000)
        self.path = output

    def record(self, exchange: RpcExchange) -> None:
        super().record(exchange)
        with self.path.open("ab") as handle:
            handle.write(
                canonical_json(
                    {
                        "method": exchange.method,
                        "request_id": exchange.request_id,
                        "attempt": exchange.attempt,
                        "request": exchange.request_payload.hex(),
                        "response": exchange.response_payload.hex(),
                    }
                )
                + b"\n"
            )


TRANSIENT_CURL_EXIT_CODES = {28, 35, 52, 56}


class CurlPoster:
    """System curl/TLS stack for public endpoints whose certificate chain the
    sandboxed urllib cannot verify. No credential material; -k is never used.

    Public endpoints intermittently close connections with an empty reply
    (exit 52), a dropped read (56), a timeout (28), or a transient TLS
    failure (35). Those are retried with bounded exponential backoff so a
    single hiccup cannot truncate the evidence window. Any other failure or
    exhaustion of retries is surfaced as an error: the run fails closed.
    """

    def __init__(self, url: str, *, max_attempts: int = 5, base_delay: float = 1.0) -> None:
        if max_attempts < 1 or max_attempts > 10:
            raise ValueError("max_attempts must be 1..10")
        if base_delay <= 0:
            raise ValueError("base_delay must be positive")
        self.url = url
        self.max_attempts = max_attempts
        self.base_delay = base_delay

    def post(self, payload: bytes) -> bytes:
        for attempt in range(self.max_attempts):
            if attempt:
                time.sleep(self.base_delay * (2 ** (attempt - 1)))
            try:
                return subprocess.run(
                    [
                        "/usr/bin/curl",
                        "--http1.1",
                        "--fail",
                        "--silent",
                        "--show-error",
                        "--max-time",
                        "20",
                        self.url,
                        "-H",
                        "Content-Type: application/json",
                        "--data-binary",
                        "@-",
                    ],
                    input=payload,
                    capture_output=True,
                    check=True,
                    timeout=25,
                ).stdout
            except subprocess.CalledProcessError as exc:
                if exc.returncode not in TRANSIENT_CURL_EXIT_CODES:
                    raise OSError(f"RPC HTTPS request failed (exit {exc.returncode})") from None
        raise OSError(f"RPC HTTPS request failed after {self.max_attempts} attempts") from None


class PacedPoster:
    """Insert a fixed delay before every request except the first.

    Public RPC endpoints commonly close connections under burst; pacing the
    observation run keeps the recorded exchanges complete and block-pinned
    without touching the shared transport. Retries are paced identically.
    """

    def __init__(self, poster: object, delay_seconds: float) -> None:
        self._poster = poster
        self._delay = delay_seconds
        self._first = True

    def post(self, payload: bytes) -> bytes:
        if not self._first:
            time.sleep(self._delay)
        else:
            self._first = False
        return self._poster.post(payload)  # type: ignore[no-any-return]


def write_manifest(directory: Path) -> None:
    lines = []
    for path in sorted(directory.rglob("*")):
        if path.is_file() and path.name != "MANIFEST.sha256":
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            lines.append(f"{digest}  {path.relative_to(directory).as_posix()}")
    (directory / "MANIFEST.sha256").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    values: dict[str, object] = {}
    for key, value in pairs:
        if key in values:
            raise ValueError(f"duplicate JSON key: {key}")
        values[key] = value
    return values


def load_hook_fee_policy(path: Path) -> OnchainAmountBandFeePolicy:
    raw = json.loads(path.read_text(), object_pairs_hook=_unique_object)
    if not isinstance(raw, Mapping):
        raise TypeError("hook fee config must be a JSON object")
    expected = {
        "base_fee",
        "medium_fee",
        "high_fee",
        "medium_amount0_threshold",
        "high_amount0_threshold",
        "medium_amount1_threshold",
        "high_amount1_threshold",
        "rule_version",
    }
    if set(raw) != expected:
        raise ValueError("hook fee config has invalid fields")
    return OnchainAmountBandFeePolicy(**raw)  # type: ignore[arg-type]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    rpc_group = parser.add_mutually_exclusive_group(required=True)
    rpc_group.add_argument("--rpc-url", help="public RPC URL only")
    rpc_group.add_argument(
        "--rpc-url-env", help="environment variable containing a credentialed RPC URL"
    )
    parser.add_argument("--chain-id", type=int, required=True)
    parser.add_argument("--pool-manager", required=True)
    parser.add_argument("--token0", required=True)
    parser.add_argument("--token1", required=True)
    parser.add_argument("--hook", required=True)
    parser.add_argument("--pool-id", default=None, help="optional expected PoolId to verify")
    parser.add_argument("--start-block", type=int, required=True)
    parser.add_argument("--end-block", type=int, required=True)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument(
        "--hook-fee-config",
        type=Path,
        help="deployed OasisDynamicFeeHook FeeConfig mirror for exact fee checks",
    )
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--source", default="robinhood-testnet-public")
    parser.add_argument(
        "--poster",
        choices=("urllib", "curl"),
        default="urllib",
        help="HTTP transport: urllib (default) or system curl for public "
        "endpoints whose TLS chain urllib cannot verify",
    )
    parser.add_argument(
        "--request-delay",
        type=float,
        default=0.0,
        help="seconds to sleep between RPC requests (pacing for public "
        "endpoints that close connections under burst)",
    )
    args = parser.parse_args()

    rpc_url = args.rpc_url
    if args.rpc_url_env is not None:
        rpc_url = os.environ.get(args.rpc_url_env)
        if not rpc_url:
            parser.error(f"RPC URL environment variable is missing: {args.rpc_url_env}")
    assert rpc_url is not None

    token0 = normalize_address(args.token0)
    token1 = normalize_address(args.token1)
    hook = normalize_address(args.hook)
    if token0 >= token1:
        parser.error("token0 must be strictly less than token1")
    pool_key = PoolKey(token0, token1, DYNAMIC_FEE_SENTINEL, TICK_SPACING, hook)
    computed_pool_id = pool_id_from_key(pool_key)
    if args.pool_id is not None and normalize_hash32(args.pool_id) != computed_pool_id:
        parser.error("provided pool-id does not match the computed PoolId")

    if args.end_block < args.start_block or args.end_block - args.start_block + 1 > 1_000:
        parser.error("observation window must be 1..1000 blocks")

    values = parse_policy(args.policy.read_bytes())
    strategy = values["strategy"]
    min_benefit = values["min_benefit"]
    hook_fee_policy = (
        None if args.hook_fee_config is None else load_hook_fee_policy(args.hook_fee_config)
    )

    args.out_dir.mkdir(parents=True, exist_ok=True)
    store = EvidenceStore(args.out_dir / "evidence.db")
    recorder = WireRecorder(args.out_dir / "wire.jsonl")
    poster = CurlPoster(rpc_url) if args.poster == "curl" else None
    if args.request_delay > 0:
        if poster is None:
            raise ValueError("--request-delay requires --poster curl")
        poster = PacedPoster(poster, args.request_delay)  # type: ignore[assignment]
    rpc = open_observation_rpc(
        url=rpc_url, chain_id=args.chain_id, recorder=recorder, poster=poster
    )
    try:
        dataset, exchanges = acquire_observation(
            rpc,
            recorder,
            source=args.source,
            start_block=args.start_block,
            end_block=args.end_block,
            address=args.pool_manager,
            topics=(SWAP_TOPIC,),
            max_blocks=1_000,
        )
        evidence = persist_observation(store, dataset, exchanges, retrieved_at=None)
        report = build_observation(
            dataset=dataset,
            window_evidence=evidence,
            pool=ObservedPoolKey(
                chain_id=args.chain_id,
                token0=token0,
                token1=token1,
                hook=hook,
                pool_id=computed_pool_id,
            ),
            strategy=strategy,
            min_benefit=min_benefit,
            source=args.source,
            deployed_hook_policy=hook_fee_policy,
        )
    finally:
        store.close()

    with (args.out_dir / "observations.jsonl").open("ab") as handle:
        for comparison in report.comparisons:
            handle.write(canonical_json(asdict(comparison)) + b"\n")
    with (args.out_dir / "report.json").open("wb") as handle:
        handle.write(report.payload + b"\n")
    with (args.out_dir / "README.md").open("w", encoding="utf-8") as handle:
        handle.write(
            "LP live shadow observation (single-provider research).\n"
            f"source: {args.source}\n"
            f"chain-id: {args.chain_id}; pool-manager: {args.pool_manager}\n"
            f"--token0 {token0} --token1 {token1} --hook {hook} "
            f"--start-block {args.start_block} --end-block {args.end_block} "
            f"--policy {args.policy} --out-dir {args.out_dir}\n"
            f"report digest: {report.digest}\n"
            f"trust: single_provider_research; execution_eligible: false\n"
        )
    write_manifest(args.out_dir)
    print(f"report digest {report.digest}")
    print(f"blocks {report.block_count} swaps {report.swap_count} chain {report.chain_id}")
    for comparison in report.comparisons:
        print(
            f"  swap@{comparison.block_number} observed_fee={comparison.observed_fee_pips} "
            f"quote_in={comparison.quote_exact_input_fee_pips} "
            f"quote_out={comparison.quote_exact_output_fee_pips} "
            f"match={comparison.exact_fee_match}"
        )
    print(f"evidence written to {args.out_dir}")


if __name__ == "__main__":
    main()
