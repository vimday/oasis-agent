"""Block-bound signed-price NAV candidates; never accepted by production replay.

The oracle's signed clock must precede both agreeing wallet headers. The explicit
observation clock is retained for replay, not proof that a historical capture is
fresh now. No signing/release policy consumes these candidate evidence kinds.
"""

import json
from dataclasses import asdict, dataclass
from typing import Any

from ..evidence.exact_capture import successful_results, validate_session_pair, wire_envelope
from ..evidence.rpc_validation import restore_exchanges
from ..evidence.store import EvidenceStore
from ..execution.account_capture import SessionFactory
from ..models.common import (
    ZERO_ADDRESS,
    BlockRef,
    EvidenceRef,
    canonical_json,
    evidence_ref_from_json,
    sha256_hex,
)
from .acquisition import acquire_pair
from .config import BASE_USDC, LiveConfig
from .positions import collect_position_outcome
from .signed_prices import SERVICE, verify_prices
from .state import Position
from .valuation import AccountValue, NavCapture, NavRefusal, requests, require_usdc_nav_band
from .wallet import WalletScope, capture_wallet, replay_wallet

PRICE_KIND = "live_signed_price_candidate"
NAV_KIND = "live_signed_nav_candidate"


def _validate_mapping(c: LiveConfig) -> None:
    c.validate()
    if c.pool.currency0 != ZERO_ADDRESS or c.pool.currency1 != BASE_USDC:
        raise NavRefusal("signed USD mapping requires native ETH and Base USDC")


def verify_nav_prices(
    raw: bytes, c: LiveConfig, *, observed_at_ms: int
) -> tuple[dict[str, Any], dict[str, dict[str, int]]]:
    """Authenticate packages, then apply each feed's own configured age limit.

    The package verifier retains its absolute 1800-second ceiling. Per-feed
    deadlines come from the authenticated witness clocks, never gateway labels
    or a shared minimum age that obscures the two independent policy limits.
    """
    _validate_mapping(c)
    verified = verify_prices(raw, observed_at_ms=observed_at_ms)
    deadlines = {}
    for feed, maximum in (
        ("ETH", c.oracle_max_age_seconds * 1000),
        ("USDC", c.usdc_oracle_max_age_seconds * 1000),
    ):
        clocks = [w["timestamp_ms"] for w in verified["prices"][feed]["witnesses"]]
        if any(not 0 <= observed_at_ms - clock <= maximum for clock in clocks):
            raise NavRefusal(f"signed NAV {feed} price stale or future")
        deadlines[feed] = {"max_age_ms": maximum, "valid_until_ms": min(clocks) + maximum}
    require_usdc_nav_band(verified["prices"]["USDC"]["median"])
    return verified, deadlines


def bound_prices(
    store: EvidenceStore,
    c: LiveConfig,
    wallets: tuple[EvidenceRef, EvidenceRef],
    raw: bytes,
    observed_at_ms: int,
    *,
    schema: str = "oasis.block-bound-signed-price-candidate.v2",
) -> dict[str, Any]:
    _validate_mapping(c)
    if schema not in {
        "oasis.block-bound-signed-price-candidate.v1",
        "oasis.block-bound-signed-price-candidate.v2",
    }:
        raise NavRefusal("signed price binding schema unsupported")
    if len(wallets) != 2 or wallets[0].source == wallets[1].source:
        raise NavRefusal("signed price binding requires two wallet sources")
    proofs = [replay_wallet(store, ref) for ref in wallets]
    if any(p.scope != WalletScope.from_config(c) for p in proofs):
        raise NavRefusal("signed price wallet differs from configured account/asset")
    balances = [p.reconstruct() for p in proofs]
    if balances[0] != balances[1]:
        raise NavRefusal("signed price wallet quorum disagrees")
    block = balances[0].header.ref
    timestamp = block.timestamp
    if (
        type(observed_at_ms) is not int
        or timestamp is None
        or not 0 <= observed_at_ms - timestamp * 1000 <= 120_000
    ):
        raise NavRefusal("signed price observation requires a recent nonfuture wallet block")
    verified, deadlines = verify_nav_prices(raw, c, observed_at_ms=observed_at_ms)
    if verified["timestamp_ms"] > timestamp * 1000:
        raise NavRefusal("signed price postdates the wallet block")
    if schema == "oasis.block-bound-signed-price-candidate.v1":
        # Preserve historical payload bytes, after independently enforcing both
        # current feed policies above. Legacy metadata never waives USDC's limit.
        verified = verify_prices(
            raw, observed_at_ms=observed_at_ms, max_age_ms=c.oracle_max_age_seconds * 1000
        )
    result = {
        "schema": schema,
        "config_hash": sha256_hex(canonical_json(asdict(c))),
        "block": asdict(block),
        "wallets": [asdict(ref) for ref in wallets],
        "asset_mapping": {
            "ETH": {"chain_id": 8453, "token": ZERO_ADDRESS, "decimals": 18},
            "USDC": {"chain_id": 8453, "token": BASE_USDC, "decimals": 6},
        },
        "quote_currency": "USD",
        "clock_source": "explicit_replay_observation",
        "verified": verified,
        "execution_eligible": False,
    }
    if schema == "oasis.block-bound-signed-price-candidate.v2":
        result["feed_deadlines"] = deadlines
    return result


def retain_prices(
    store: EvidenceStore,
    c: LiveConfig,
    wallets: tuple[EvidenceRef, EvidenceRef],
    raw: bytes,
    observed_at_ms: int,
) -> EvidenceRef:
    payload = bound_prices(store, c, wallets, raw, observed_at_ms)
    return store.put(
        PRICE_KIND,
        canonical_json(payload),
        wallets[0].block,
        SERVICE,
        request=canonical_json(
            {"wallets": [asdict(ref) for ref in wallets], "observed_at_ms": observed_at_ms}
        ),
        raw_response=raw,
        interpretation={"execution_eligible": False},
    )


def replay_prices(store: EvidenceStore, ref: EvidenceRef, c: LiveConfig) -> dict[str, Any]:
    if ref.kind != PRICE_KIND or ref.source != SERVICE:
        raise NavRefusal("signed price candidate kind/source differs")
    record = store.get_record(ref)
    if record.request_payload is None:
        raise NavRefusal("signed price binding request missing")
    request = json.loads(record.request_payload)
    if set(request) != {"wallets", "observed_at_ms"} or len(request["wallets"]) != 2:
        raise NavRefusal("signed price binding request differs")
    wallets = tuple(evidence_ref_from_json(r) for r in request["wallets"])
    payload = bound_prices(
        store,
        c,
        (wallets[0], wallets[1]),
        record.raw_response_payload,
        request["observed_at_ms"],
        schema=json.loads(record.payload).get("schema"),
    )
    if (
        record.request_payload != canonical_json(request)
        or ref.block != wallets[0].block
        or canonical_json(payload) != record.payload
    ):
        raise NavRefusal("signed price interpretation contradicts retained bytes")
    return payload


@dataclass(frozen=True, slots=True)
class SignedNavCapture(NavCapture):
    price_evidence: EvidenceRef

    def _requests(self, c: LiveConfig, block: BlockRef) -> list[tuple[str, list[object]]]:
        calls = requests(c, block, self.position, inventory_checked=self.inventory_checked)
        return calls[:4] + calls[12:]  # Same inventory proof, without Chainlink feed reads.

    def _prices(
        self, store: EvidenceStore, c: LiveConfig, block: BlockRef, vals: tuple[object, ...]
    ) -> list[int]:
        prices = replay_prices(store, self.price_evidence, c)
        if prices["block"] != asdict(block) or asdict(self.wallet) not in prices["wallets"]:
            raise NavRefusal("signed NAV price binding belongs to another wallet proof/block")
        return [int(prices["verified"]["prices"][f]["median"]) for f in ("ETH", "USDC")]

    def payload(self, store: EvidenceStore, c: LiveConfig) -> bytes:
        result = json.loads(NavCapture.payload(self, store, c))
        result["schema"] = (
            "oasis.signed-nav-candidate.v2"
            if self.inventory_checked
            else "oasis.signed-nav-candidate.v1"
        )
        result["price_evidence"] = asdict(self.price_evidence)
        return canonical_json(result)


def replay_signed_nav(store: EvidenceStore, ref: EvidenceRef, c: LiveConfig) -> SignedNavCapture:
    if ref.kind != NAV_KIND:
        raise NavRefusal("signed NAV candidate kind differs")
    record = store.get_record(ref)
    if record.request_payload is None:
        raise NavRefusal("signed NAV raw proof missing")
    d = json.loads(record.payload)
    if d.get("schema") not in {"oasis.signed-nav-candidate.v1", "oasis.signed-nav-candidate.v2"}:
        raise NavRefusal("signed NAV schema unsupported")
    capture = SignedNavCapture(
        evidence_ref_from_json(d["wallet"]),
        Position(**d["position"]) if d["position"] else None,
        evidence_ref_from_json(d["position_evidence"]) if d["position_evidence"] else None,
        restore_exchanges(record.request_payload, record.raw_response_payload),
        evidence_ref_from_json(d["price_evidence"]),
        inventory_checked=d["schema"] == "oasis.signed-nav-candidate.v2",
    )
    if (
        ref.block != capture.wallet.block
        or ref.source != capture.wallet.source
        or capture.payload(store, c) != record.payload
    ):
        raise NavRefusal("signed NAV interpretation contradicts retained bytes")
    return capture


def verify_signed_nav_quorum(
    store: EvidenceStore,
    c: LiveConfig,
    refs: tuple[EvidenceRef, EvidenceRef],
    *,
    sources: tuple[str, str],
    position: Position | None,
    now_ms: int,
) -> dict[str, Any]:
    """Revalidate both retained candidates and freshness at the consumer's clock.

    Source aliases and declared inventory are expectations, not discovery or proof
    of provider independence. A passing report is never a production permission.
    """
    _validate_mapping(c)
    if position is not None and any(type(v) is not int for v in asdict(position).values()):
        raise NavRefusal("signed NAV expected position requires integer fields")
    if (
        type(now_ms) is not int
        or now_ms < 0
        or len(refs) != 2
        or len(sources) != 2
        or not all(isinstance(s, str) and s for s in sources)
        or sources[0] == sources[1]
        or tuple(r.source for r in refs) != sources
        or refs[0].block != refs[1].block
    ):
        raise NavRefusal("signed NAV quorum requires expected distinct sources and one block")
    block = refs[0].block
    if (
        block is None
        or block.timestamp is None
        or not 0 <= now_ms - block.timestamp * 1000 <= 120_000
    ):
        raise NavRefusal("signed NAV account block is stale or future")
    captures = [replay_signed_nav(store, ref, c) for ref in refs]
    if not all(p.inventory_checked for p in captures):
        raise NavRefusal("signed NAV historical proof lacks current position inventory")
    if any(p.position != position for p in captures):
        raise NavRefusal("signed NAV quorum omits or changes declared position")
    if captures[0].price_evidence != captures[1].price_evidence:
        raise NavRefusal("signed NAV quorum uses different price bindings")
    price_ref = captures[0].price_evidence
    binding = replay_prices(store, price_ref, c)
    if binding["wallets"] != [asdict(p.wallet) for p in captures]:
        raise NavRefusal("signed NAV quorum does not cover its exact wallet pair")
    if binding["verified"]["observed_at_ms"] > now_ms:
        raise NavRefusal("signed NAV capture clock is in the future")
    # Recheck original signatures/age at consumption, never reuse capture freshness.
    current_prices, deadlines = verify_nav_prices(
        store.get_record(price_ref).raw_response_payload,
        c,
        observed_at_ms=now_ms,
    )
    values = [p.reconstruct(store, c) for p in captures]
    calls = captures[0]._requests(c, block)
    if values[0] != values[1] or successful_results(
        captures[0].exchanges, calls
    ) != successful_results(captures[1].exchanges, calls):
        raise NavRefusal("signed NAV quorum raw state disagrees")
    result: dict[str, Any] = {
        "schema": "oasis.signed-nav-quorum-verification.v1",
        "execution_eligible": False,
        "config_hash": sha256_hex(canonical_json(asdict(c))),
        "checked_at_ms": now_ms,
        "block": asdict(block),
        "account": c.account,
        "sources": list(sources),
        "position": asdict(position) if position is not None else None,
        "value": asdict(values[0]),
        "evidence": [asdict(ref) for ref in refs],
        "price_evidence": asdict(price_ref),
        "current_price_verification": current_prices,
        "feed_deadlines": deadlines,
        "account_age_ms": now_ms - block.timestamp * 1000,
        "scope": "declared wallet assets and position; not complete account history or release",
    }
    result["evidence_hash"] = sha256_hex(canonical_json(result))
    return result


def capture_signed_nav_candidate(
    store: EvidenceStore,
    c: LiveConfig,
    block: BlockRef,
    position: Position | None,
    *,
    raw_prices: bytes,
    observed_at_ms: int,
    sources: tuple[str, str],
    sessions: SessionFactory,
) -> tuple[AccountValue, tuple[EvidenceRef, EvidenceRef]]:
    # Validate signatures/freshness before spending any acquisition requests.
    _validate_mapping(c)
    verified, _ = verify_nav_prices(raw_prices, c, observed_at_ms=observed_at_ms)
    if (
        block.timestamp is None
        or not 0 <= observed_at_ms - block.timestamp * 1000 <= 120_000
        or verified["timestamp_ms"] > block.timestamp * 1000
    ):
        raise NavRefusal("signed NAV candidate block/clock differs")
    wallets = capture_wallet(store, c, block, sources=sources, sessions=sessions)
    price = retain_prices(store, c, wallets, raw_prices, observed_at_ms)
    positions = (
        None
        if position is None
        else collect_position_outcome(
            store,
            c,
            block,
            token_id=position.token_id,
            lower=position.lower,
            upper=position.upper,
            liquidity=position.liquidity,
            burned=False,
            sources=sources,
            sessions=sessions,
        )
    )
    pair = (sessions(sources[0]), sessions(sources[1]))
    validate_session_pair(*pair)
    if (pair[0][2], pair[1][2]) != sources:
        raise NavRefusal("signed NAV source aliases changed")
    captures = [
        SignedNavCapture(wallets[i], position, positions[i] if positions else None, (), price)
        for i in range(2)
    ]
    calls = captures[0]._requests(c, block)
    acquire_pair(pair, calls)
    captures = [
        SignedNavCapture(
            wallets[i], position, positions[i] if positions else None, pair[i][1].exchanges, price
        )
        for i in range(2)
    ]
    values = [p.reconstruct(store, c) for p in captures]
    if values[0] != values[1] or successful_results(
        captures[0].exchanges, calls
    ) != successful_results(captures[1].exchanges, calls):
        raise NavRefusal("signed NAV providers disagree")
    refs = [
        store.put(
            NAV_KIND,
            p.payload(store, c),
            block,
            p.wallet.source,
            request=wire_envelope(p.exchanges, response=False),
            raw_response=wire_envelope(p.exchanges, response=True),
            interpretation={"execution_eligible": False},
        )
        for p in captures
    ]
    for ref in refs:
        replay_signed_nav(store, ref, c)
    return values[0], (refs[0], refs[1])
