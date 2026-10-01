"""Retain both pool currencies' pinned USD feed observations at one block.

Run from the repository root as ``python -m scripts.capture_reference_price``.

``assemble_lp_cycle.py`` needs a decision-block ``reference_price_x192`` that two
independent providers agree on, and this is the producer of the four retained feed
observations it is derived from: each pool currency's own USD feed, read through each
of the market window's own two providers at one explicit block. The derivation itself
lives in ``execution/lp_reference_price.py`` and runs when the cycle is assembled, so
an observation retained here is an input, never a value.

Each ERC-20 currency's on-chain ``decimals()`` is read beside its feed and must equal the
declaration. Native ETH instead requires its protocol-defined 18 decimals on an explicit
supported chain; the chain identity is read and retained beside its USD feed. The
provider aliases are declared in the request, each paired
with the environment variable holding its endpoint, so they can be the market window's
own two. The emitted ``reference`` object is exactly the shape the cycle request
accepts, and it is parsed back before the file is written.

The tool holds no key, reserves no capital and offers no signing or broadcast path:
``execution_eligible`` is never asserted.
"""

import argparse
from collections.abc import Callable, Mapping
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Self

from pydantic import Field, model_validator

from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.execution.account_capture import one_shot_session
from oasis_agent.execution.lp_reference_price import (
    CurrencyFeed,
    CurrencyFeedCapture,
    ReferenceDeclaration,
    ReferenceProvider,
    ReferenceRequest,
    collect_currency_feed,
    currency_feed,
    persist_currency_feed_evidence,
)
from oasis_agent.launch.models import Positive, StrictModel
from oasis_agent.models.chain import BlockSnapshot
from oasis_agent.models.common import canonical_json
from scripts.capture_lp_position import ProviderSource, endpoint_bindings
from scripts.run_account_risk_reconcile import _budget_policies

SCHEMA = "oasis.lp-reference-capture.v1"


class CaptureRequest(StrictModel):
    """The block to read, the reference declaration to satisfy, and the two providers."""

    chain_id: Positive
    block_number: Annotated[int, Field(strict=True, ge=0, lt=2**256)]
    reference: ReferenceDeclaration
    sources: tuple[ProviderSource, ProviderSource]

    @model_validator(mode="after")
    def _declared_providers(self) -> Self:
        aliases = {source.alias for source in self.sources}
        if len(aliases) != 2 or len({source.env for source in self.sources}) != 2:
            raise ValueError("two distinct provider aliases and endpoints required")
        if aliases != set(self.reference.providers):
            raise ValueError("the declared endpoints must be the reference's own providers")
        return self


def _verify_feed(
    request: CaptureRequest, feed: CurrencyFeed, observation: CurrencyFeedCapture
) -> BlockSnapshot:
    """Re-derive one observation and refuse anything but the declared feed and block."""
    if observation.source not in {source.alias for source in request.sources}:
        raise ValueError("a capture is not one of the declared providers")
    if observation.currency != feed.address or observation.policy != feed.policy:
        raise ValueError("a capture is not the declared currency and feed")
    if observation.currency_decimals != feed.decimals:
        raise ValueError("a capture is not the declared currency scale")
    header, _observation, decimals = observation.reconstruct()
    if decimals != feed.decimals:
        raise ValueError("the captured currency scale is not the declared one")
    if header.ref.chain_id != request.chain_id or header.ref.number != request.block_number:
        raise ValueError("the captured block is not the requested chain and height")
    return header


def capture(
    request: CaptureRequest,
    *,
    evidence_path: Path,
    output_path: Path,
    collect: Callable[[], Mapping[str, tuple[CurrencyFeedCapture, CurrencyFeedCapture]]],
) -> None:
    """Retain both currencies' observations, or write nothing at all.

    ``collect`` is the only side effect: it returns, per declared alias, that
    provider's observation of ``currency0`` and ``currency1`` in the pool's own order.
    Every identity in it is checked before the first write, so a capture of another
    block, currency, feed or provider leaves the store untouched.
    """
    if not evidence_path.is_file():
        raise ValueError("an existing evidence database is required")
    if output_path.exists():
        raise ValueError("refusing to overwrite a retained capture")
    gathered = collect()
    declared = {source.alias for source in request.sources}
    if set(gathered) != declared:
        raise ValueError("the captured providers are not the declared aliases")
    feeds = (
        currency_feed(request.chain_id, request.reference.currency0),
        currency_feed(request.chain_id, request.reference.currency1),
    )
    headers: list[BlockSnapshot] = []
    for alias in sorted(declared):
        observations = gathered[alias]
        if len(observations) != 2:
            raise ValueError("both declared currencies must be observed")
        for feed, observation in zip(feeds, observations, strict=True):
            if observation.source != alias:
                raise ValueError("a capture is not the provider that produced it")
            headers.append(_verify_feed(request, feed, observation))
    if any(header != headers[0] for header in headers):
        raise ValueError("the declared providers disagree on the block they read")
    block = headers[0].ref
    store = EvidenceStore(evidence_path)
    try:
        retained = {
            alias: tuple(
                persist_currency_feed_evidence(store, observation)
                for observation in gathered[alias]
            )
            for alias in sorted(declared)
        }
    finally:
        store.close()
    declaration = request.reference
    reference = ReferenceRequest(
        pool=declaration.pool,
        currency0=declaration.currency0,
        currency1=declaration.currency1,
        providers=tuple(
            ReferenceProvider(
                source=alias,
                currency0=retained[alias][0],
                currency1=retained[alias][1],
            )
            for alias in sorted(declared)
        ),
        max_reference_deviation=declaration.max_reference_deviation,
    )
    emitted = reference.model_dump(mode="json")
    # The emitted object must be what the cycle request accepts, so parse it back
    # before writing: a shape drift is refused here rather than at assembly time.
    if ReferenceRequest.from_json(canonical_json(emitted)) != reference:
        raise ValueError("the emitted reference is not the one that was built")
    payload = canonical_json(
        {
            "schema": SCHEMA,
            "reference": emitted,
            "block": asdict(block),
            "execution_eligible": False,
        }
    )
    with output_path.open("xb") as stream:
        stream.write(payload + b"\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--budget", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--interval-ms", type=int, default=3000)
    parser.add_argument("--max-requests", type=int, default=256)
    args = parser.parse_args()
    try:
        request = CaptureRequest.from_json(args.request.read_bytes())
        aliases = tuple(source.alias for source in request.sources)
        urls = endpoint_bindings(request.sources, args.env_file)
        policies = _budget_policies(
            set(urls),
            max_requests=args.max_requests,
            interval_ms=args.interval_ms,
        )
        feeds = (
            currency_feed(request.chain_id, request.reference.currency0),
            currency_feed(request.chain_id, request.reference.currency1),
        )

        def collect() -> dict[str, tuple[CurrencyFeedCapture, CurrencyFeedCapture]]:
            gathered: dict[str, tuple[CurrencyFeedCapture, CurrencyFeedCapture]] = {}
            for alias in aliases:
                captures = tuple(
                    collect_currency_feed(
                        one_shot_session(alias, urls=urls, policies=policies, budget=args.budget),
                        currency=feed,
                        block_number=request.block_number,
                    )
                    for feed in feeds
                )
                gathered[alias] = (captures[0], captures[1])
            return gathered

        capture(
            request,
            evidence_path=args.evidence,
            output_path=args.output,
            collect=collect,
        )
    except Exception:  # noqa: BLE001 - never echo endpoint values or retained strings
        parser.exit(1, "Reference price capture failed; no execution eligibility granted.\n")
    print("Reference price inputs retained; they are a cycle input, not permission to trade.")


if __name__ == "__main__":
    main()
