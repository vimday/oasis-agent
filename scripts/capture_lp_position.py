"""Retain one declared v4 position's decision-block proof from two providers.

Run from the repository root as ``python -m scripts.capture_lp_position``.

``assemble_lp_cycle.py`` needs the declared position's own two retained snapshots at
the market window's decision block, and nothing in this repository produced them: the
only other retention point is the simulation plan, which needs the cycle that needs
the snapshots. This is the producer that closes that loop. It reads one NFT's state
from two independent providers at one explicit block, requires both to reconstruct the
same canonical snapshot, and retains both captures in the operator's own evidence
database, which must already hold the window's own captures.

The provider aliases are declared in the request instead of being assumed, because the
inventory deriver requires the position's providers to be exactly the market window's
own two. Each alias is paired with the environment variable that holds its endpoint,
so the aliases can match whatever window the operator already retained.

The emitted ``position`` object is exactly the shape the cycle request accepts, and the
scope in it is derived from the two agreeing snapshots rather than declared. The tool
holds no key, reserves no capital and offers no signing or broadcast path:
``execution_eligible`` is never asserted.
"""

import argparse
from collections.abc import Callable, Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Self

from pydantic import Field, model_validator

from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.execution.account_capture import one_shot_sessions
from oasis_agent.farming.position_quorum import PositionQuorum, collect_position_quorum
from oasis_agent.launch.models import Address, Positive, StrictModel
from oasis_agent.launch.models import Source as Alias
from oasis_agent.models.common import canonical_json, normalize_address
from oasis_agent.models.position_scope import LpPositionScope
from scripts.run_account_risk_reconcile import _budget_policies, _environment

SCHEMA = "oasis.lp-position-capture.v1"
_ENV_NAME = r"[A-Z_][A-Z0-9_]{0,127}"


class ProviderSource(StrictModel):
    """One provider alias, and the environment variable that holds its endpoint."""

    alias: Alias
    env: Annotated[str, Field(pattern=_ENV_NAME)]

    @model_validator(mode="after")
    def _endpoint_shaped(self) -> Self:
        """A declared variable must be named as an endpoint, never as key material."""
        if "RPC" not in self.env or "KEY" in self.env:
            raise ValueError("an endpoint must be named as one, never as key material")
        return self


class CaptureRequest(StrictModel):
    """The NFT to read, the block to read it at, and the two declared providers."""

    chain_id: Positive
    position_manager: Address
    state_view: Address
    token_id: Positive
    block_number: Annotated[int, Field(strict=True, ge=0, lt=2**256)]
    sources: tuple[ProviderSource, ProviderSource]

    @model_validator(mode="after")
    def _distinct_providers(self) -> Self:
        if (
            len({source.alias for source in self.sources}) != 2
            or len({source.env for source in self.sources}) != 2
        ):
            raise ValueError("two distinct provider aliases and endpoints required")
        return self


def endpoint_bindings(sources: Sequence[ProviderSource], env_file: Path) -> dict[str, str]:
    """Bind each declared alias to its declared endpoint, or refuse the run.

    Shared with the other provider-reading producer so one endpoint policy governs
    both: an environment variable that is not named as an endpoint, a missing one
    and two aliases pointing at the same URL are all refused before any request.
    """
    found = _environment(env_file, {source.env for source in sources})
    return {source.alias: found[source.env] for source in sources}


def capture(
    request: CaptureRequest,
    *,
    evidence_path: Path,
    output_path: Path,
    collect: Callable[[], PositionQuorum],
) -> None:
    """Retain the declared position's proof, or write nothing at all.

    ``collect`` is the only side effect: it returns a two-provider quorum, and every
    identity in it is checked against the request before the first write, so a
    capture of some other NFT, block or state view leaves the store untouched.
    """
    if not evidence_path.is_file():
        raise ValueError("an existing evidence database is required")
    if output_path.exists():
        raise ValueError("refusing to overwrite a retained capture")
    quorum = collect()
    manager = normalize_address(request.position_manager)
    state_view = normalize_address(request.state_view)
    block = quorum.left.header.ref
    snapshot = quorum.left.snapshot
    declared = {source.alias for source in request.sources}
    if {quorum.left.source, quorum.right.source} != declared:
        raise ValueError("the captured providers are not the declared aliases")
    if block.chain_id != request.chain_id or block.number != request.block_number:
        raise ValueError("the captured block is not the requested chain and height")
    if snapshot.position_manager != manager:
        raise ValueError("the captured position is not the requested manager")
    if snapshot.token_id != request.token_id:
        raise ValueError("the captured position is not the requested token id")
    if quorum.left.state_view != state_view or quorum.right.state_view != state_view:
        raise ValueError("the captured position is not the requested state view")
    scope = LpPositionScope(
        chain_id=block.chain_id,
        position_manager=manager,
        token_id=snapshot.token_id,
        pool_id=snapshot.pool_id,
        owner=snapshot.nft_owner,
    )
    store = EvidenceStore(evidence_path)
    try:
        references = quorum.retain(store)
    finally:
        store.close()
    payload = canonical_json(
        {
            "schema": SCHEMA,
            "position": {
                "scope": asdict(scope),
                "references": [asdict(reference) for reference in references],
            },
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

        def collect() -> PositionQuorum:
            pair = one_shot_sessions(aliases, urls=urls, policies=policies, budget=args.budget)
            return collect_position_quorum(
                pair[0],
                pair[1],
                chain_id=request.chain_id,
                position_manager=request.position_manager,
                state_view=request.state_view,
                token_id=request.token_id,
                block_number=request.block_number,
            )

        capture(
            request,
            evidence_path=args.evidence,
            output_path=args.output,
            collect=collect,
        )
    except Exception:  # noqa: BLE001 - never echo endpoint values or retained strings
        parser.exit(1, "Position capture failed; no execution eligibility granted.\n")
    print("Position proof retained; it is a cycle input, not permission to trade.")


if __name__ == "__main__":
    main()
