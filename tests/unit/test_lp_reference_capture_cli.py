"""The operator CLI that retains both pool currencies' pinned USD feed observations.

``assemble_lp_cycle.py`` derives the decision-block reference price from four retained
observations, and this is the producer of them: each pool currency's own USD feed, read
through each of the window's own two providers at one explicit block. These tests assert
that the emitted ``reference`` is exactly the shape the cycle request accepts, that both
currencies' own scale is re-checked on chain, and that every identity is checked before
the store is written to. No case ends with an execution permit.
"""

import json
from collections.abc import Mapping
from dataclasses import asdict, replace
from pathlib import Path

import pytest

from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.execution.lp_reference_price import (
    CURRENCY_FEED_KIND,
    CurrencyFeed,
    CurrencyFeedCapture,
    ReferenceRequest,
    replay_currency_feed_evidence,
)
from oasis_agent.models.chain import BlockSnapshot
from oasis_agent.models.common import canonical_json
from scripts.capture_reference_price import CaptureRequest, capture
from tests.unit.test_lp_cycle_assembly import evidence_rows
from tests.unit.test_lp_reference_price import (
    DECISION,
    HEADERS,
    LATE,
    currency_feeds,
    declarations,
)
from tests.unit.test_lp_reference_price import (
    capture as feed_capture,
)
from tests.unit.test_market_window import POOL

ALIASES = ("a", "b")
Gathered = Mapping[str, tuple[CurrencyFeedCapture, CurrencyFeedCapture]]


def reference_declaration(*, providers: tuple[str, ...] = ALIASES) -> dict[str, object]:
    first, second = declarations()
    return {
        "pool": asdict(POOL),
        "currency0": first.model_dump(mode="json"),
        "currency1": second.model_dump(mode="json"),
        "providers": list(providers),
        "max_reference_deviation": "0.05",
    }


def request_for(**overrides: object) -> CaptureRequest:
    values: dict[str, object] = {
        "chain_id": DECISION.ref.chain_id,
        "block_number": DECISION.ref.number,
        "reference": reference_declaration(),
        "sources": [
            {"alias": ALIASES[0], "env": "PRIMARY_RPC_URL"},
            {"alias": ALIASES[1], "env": "SECONDARY_RPC_URL"},
        ],
    }
    values.update(overrides)
    return CaptureRequest.from_json(canonical_json(values))


def gather(
    *,
    aliases: tuple[str, ...] = ALIASES,
    block: BlockSnapshot = DECISION,
    swapped: str | None = None,
    mislabelled: str | None = None,
    scale0: int | None = None,
) -> dict[str, tuple[CurrencyFeedCapture, CurrencyFeedCapture]]:
    """Both currencies' observations, one pair per declared alias, as read."""
    first, second = currency_feeds()
    if scale0 is not None:
        first = CurrencyFeed(POOL.currency0, scale0, first.policy)
    gathered: dict[str, tuple[CurrencyFeedCapture, CurrencyFeedCapture]] = {}
    for alias in aliases:
        one = feed_capture(alias, first, block=block)
        two = feed_capture(alias, second, block=block)
        if swapped == alias:
            one, two = two, one
        if mislabelled == alias:
            one = replace(one, source="c")
        gathered[alias] = (one, two)
    return gathered


def store_path(tmp_path: Path) -> Path:
    """An existing evidence database, as a window capture would have left behind."""
    path = tmp_path / "evidence.db"
    store = EvidenceStore(path)
    store.close()
    return path


def test_the_capture_emits_a_reference_the_cycle_request_accepts(tmp_path: Path) -> None:
    path = store_path(tmp_path)
    output = tmp_path / "reference.json"
    gathered = gather()
    capture(request_for(), evidence_path=path, output_path=output, collect=lambda: gathered)
    emitted = json.loads(output.read_bytes())
    assert emitted["schema"] == "oasis.lp-reference-capture.v1"
    assert emitted["execution_eligible"] is False
    assert emitted["block"] == asdict(DECISION.ref)
    # The emitted object is what the cycle assembler consumes, verbatim.
    accepted = ReferenceRequest.from_json(canonical_json(emitted["reference"]))
    assert [provider.source for provider in accepted.providers] == list(ALIASES)
    assert accepted.pool == POOL
    assert [provider.currency0.kind for provider in accepted.providers] == [
        CURRENCY_FEED_KIND,
        CURRENCY_FEED_KIND,
    ]
    first, _second = currency_feeds()
    store = EvidenceStore(path)
    try:
        assert [
            replay_currency_feed_evidence(store, provider.currency0, currency=first)
            for provider in accepted.providers
        ] == [gathered[alias][0] for alias in ALIASES]
    finally:
        store.close()
    assert evidence_rows(path) == 4


@pytest.mark.parametrize(
    ("expected", "mutate"),
    [
        ("captured providers are not the declared aliases", {"aliases": ("a", "c")}),
        ("not the requested chain and height", {"block": HEADERS[1]}),
        ("not the declared currency and feed", {"swapped": "a"}),
        ("not the declared currency scale", {"scale0": 6}),
        ("not the provider that produced it", {"mislabelled": "b"}),
    ],
)
def test_a_capture_that_is_not_the_declared_request_retains_nothing(
    tmp_path: Path, expected: str, mutate: dict[str, object]
) -> None:
    """A feed of some other block, currency, scale or provider is not this request."""
    path = store_path(tmp_path)
    output = tmp_path / "reference.json"
    gathered = gather(**mutate)
    with pytest.raises(ValueError, match=expected):
        capture(request_for(), evidence_path=path, output_path=output, collect=lambda: gathered)
    assert not output.exists()
    assert evidence_rows(path) == 0


def test_providers_that_read_different_blocks_are_refused(tmp_path: Path) -> None:
    """A reorg between the two providers is a disagreement, not a merge."""
    path = store_path(tmp_path)
    output = tmp_path / "reference.json"
    gathered = gather()
    assert LATE.ref.number == DECISION.ref.number
    gathered[ALIASES[1]] = gather(block=LATE)[ALIASES[1]]
    with pytest.raises(ValueError, match="disagree on the block they read"):
        capture(request_for(), evidence_path=path, output_path=output, collect=lambda: gathered)
    assert not output.exists()
    assert evidence_rows(path) == 0


def test_a_capture_refuses_to_overwrite_or_to_invent_its_store(tmp_path: Path) -> None:
    path = store_path(tmp_path)
    output = tmp_path / "reference.json"
    output.write_bytes(b"{}\n")
    with pytest.raises(ValueError, match="overwrite a retained capture"):
        capture(request_for(), evidence_path=path, output_path=output, collect=gather)
    assert output.read_bytes() == b"{}\n"
    absent = tmp_path / "absent" / "reference.json"
    with pytest.raises(ValueError, match="existing evidence database"):
        capture(
            request_for(),
            evidence_path=tmp_path / "absent.db",
            output_path=absent,
            collect=gather,
        )
    assert not absent.exists()


def test_the_request_refuses_a_shared_alias_or_a_key_shaped_endpoint() -> None:
    first = {"alias": ALIASES[0], "env": "PRIMARY_RPC_URL"}
    for sources in (
        [first, {"alias": ALIASES[0], "env": "SECONDARY_RPC_URL"}],
        [first, {"alias": ALIASES[1], "env": "PRIMARY_RPC_URL"}],
        [first, {"alias": ALIASES[1], "env": "SECONDARY_RPC_KEY"}],
        [first, {"alias": ALIASES[1], "env": "secondary_endpoint"}],
    ):
        with pytest.raises(ValueError):
            request_for(sources=sources)


def test_the_declared_endpoints_must_be_the_references_own_providers() -> None:
    with pytest.raises(ValueError, match="the reference's own providers"):
        request_for(reference=reference_declaration(providers=("a", "c")))
    with pytest.raises(ValueError, match="the reference's own providers"):
        request_for(
            sources=[
                {"alias": "x", "env": "PRIMARY_RPC_URL"},
                {"alias": ALIASES[1], "env": "SECONDARY_RPC_URL"},
            ]
        )
