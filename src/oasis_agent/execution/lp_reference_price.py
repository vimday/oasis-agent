"""The decision-block reference price as two-provider agreed evidence.

``LpFeatureWindow`` needs a ``reference_price_x192`` that is a content-hash-bound
``AgreedInteger`` at the market window's own decision block, and nothing in this
repository produced one: the only other retention point was the cycle assembler
reading a value somebody else wrote. This module derives it from the two pool
currencies' own pinned USD feed observations, which is the source the owner
selected. It is a ratio of two observed feed answers, not a stablecoin peg
assumption, not a settlement price and not an economic estimate.

Every input is re-derived from retained raw RPC bytes before the first write:

* each currency's feed observation is replayed through ``UsdPriceCapture``, which
  re-checks the feed runtime, description, decimals, round identity and freshness
  against the observation's own pinned block, so the block is read out of the
  evidence rather than supplied beside it;
* ERC-20 ``decimals()`` is read on chain and compared with the declaration;
  native ETH uses its protocol-defined 18 decimals only on explicit supported
  chains, whose identity is checked in the same retained wire evidence;
* the derived ratio is compared with the pool's own decision-block price, so an
  inverted, mis-scaled or simply foreign pair of feeds is refused instead of
  becoming the denominator of every inventory valuation.

The value is expressed in the same Q192 fixed point as ``sqrtPriceX96 ** 2``, so
``produce_pool_state`` can compare the two without any conversion. Retention
follows the established derived-integer pattern: one record per provider, each
borrowing that provider's own retained wire envelope, with the agreed payload the
constructor re-hashes. This module holds no key, reserves no capital, signs
nothing and broadcasts nothing.
"""

from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Annotated, Final, Self, cast

from eth_abi.abi import decode as abi_decode
from pydantic import Field, model_validator

from ..evidence.exact_capture import (
    RecordedSession,
    decode_header,
    successful_results,
    wire_envelope,
)
from ..evidence.rpc_validation import decode_exchange, restore_exchanges, rpc_quantity
from ..evidence.store import EvidenceRecord, EvidenceStore
from ..farming.account_inventory import AccountInventoryCapture
from ..farming.price_evidence import UsdFeedPolicy, UsdPriceCapture, UsdPriceObservation, selector
from ..launch.models import Address, Hash, StrictModel
from ..launch.models import Source as Alias
from ..models.chain import BlockSnapshot
from ..models.common import (
    ZERO_ADDRESS,
    BlockRef,
    EvidenceRef,
    canonical_json,
    normalize_address,
    normalize_hex,
)
from ..models.v4 import PoolKey
from ..rpc.client import RpcClient
from ..rpc.transport import RpcExchange
from ..shadow.features import Q192, AgreedInteger, agreed_value_payload
from ..shadow.market_window import MarketWindow
from ..uniswap_v4.pool_id import pool_id_from_key

#: The role name ``build_lp_cycle`` requires of the reference price.
REFERENCE_NAME: Final[str] = "reference_price_x192"
#: The retained record kind of a derived reference price.
DERIVED_REFERENCE_KIND: Final[str] = "derived_reference_integer"
REFERENCE_SCHEMA: Final[str] = "oasis.derived-reference-price.v1"
#: The retained record kind of one provider's pinned currency feed observation.
CURRENCY_FEED_KIND: Final[str] = "currency_feed_observation"
CURRENCY_FEED_SCHEMA: Final[str] = "oasis.currency-feed-observation.v1"

#: The seven-exchange sequence ``UsdPriceCapture`` itself validates, with this
#: capture's own currency-decimals call inserted before the closing header.
_FEED_SEQUENCE: Final[tuple[int, ...]] = (0, 1, 2, 3, 4, 5, 7)
_CHAIN: Final[int] = 0
_OPENING_HEADER: Final[int] = 1
_TOKEN_DECIMALS: Final[int] = 6
_EXPECTED_EXCHANGES: Final[int] = 8
_MAX_ENVELOPE_BYTES: Final[int] = 1_000_000
# Explicit ETH-denominated deployments already supported by this repository.
# A native asset has no ERC-20 decimals() getter. Unknown chains fail closed.
_NATIVE_ETH_CHAINS: Final[frozenset[int]] = frozenset({1, 8453, 84532, 4663, 46630})
#: The three getters ``UsdPriceCapture`` itself validates, in its own order.
FEED_SIGNATURES: Final[tuple[str, ...]] = ("decimals()", "description()", "latestRoundData()")
#: The explicit input-quality bound: a plain decimal string between zero and one.
_DEVIATION_PATTERN: Final[str] = r"^(0\.[0-9]{1,18}|1\.0{1,18})$"


class FeedPin(StrictModel):
    """The declared identity of one USD feed; every field is re-checked on chain."""

    address: Address
    description: Annotated[str, Field(min_length=3, max_length=200)]
    runtime_keccak: Hash
    max_age_seconds: Annotated[int, Field(strict=True, ge=1, le=604_800)]

    @model_validator(mode="after")
    def _usd_denominated(self) -> Self:
        if not self.description.endswith(" / USD"):
            raise ValueError("an explicit USD feed description is required")
        return self


class CurrencyDeclaration(StrictModel):
    """One pool currency, its base-unit scale, and the feed that prices it."""

    address: Address
    decimals: Annotated[int, Field(strict=True, ge=0, le=255)]
    feed: FeedPin


class ReferenceProvider(StrictModel):
    """One provider's own retained observation of each pool currency's feed."""

    source: Alias
    currency0: EvidenceRef
    currency1: EvidenceRef


class ReferenceTerms(StrictModel):
    """Pool, currencies and deviation bound: what every reference price declares.

    ``currency0``/``currency1`` are declared in the pool's own canonical order, so a
    swapped feed pairing cannot hide behind the declaration, and
    ``max_reference_deviation`` is the operator's own input-quality bound: the largest
    relative gap between the derived ratio and the pool's own price that still counts
    as the same price. It is not an economic model term.
    """

    pool: PoolKey
    currency0: CurrencyDeclaration
    currency1: CurrencyDeclaration
    max_reference_deviation: Annotated[str, Field(pattern=_DEVIATION_PATTERN)]

    @model_validator(mode="after")
    def _currencies_are_the_pool(self) -> Self:
        if (self.currency0.address, self.currency1.address) != (
            self.pool.currency0,
            self.pool.currency1,
        ):
            raise ValueError("the declared currencies must be the pool's own currencies")
        return self


class ReferenceDeclaration(ReferenceTerms):
    """A producer's request: the two aliases whose observations it will retain.

    A producer names its providers here and retains the references they produce, so no
    caller has to write a reference it has not observed yet.
    """

    providers: tuple[Alias, Alias]

    @model_validator(mode="after")
    def _two_distinct_providers(self) -> Self:
        if len(set(self.providers)) != 2:
            raise ValueError("two distinct reference providers are required")
        return self


class ReferenceRequest(ReferenceTerms):
    """The retained inputs one derived reference price is rebuilt from."""

    providers: tuple[ReferenceProvider, ReferenceProvider]

    @model_validator(mode="after")
    def _two_distinct_provider_sources(self) -> Self:
        if len({item.source for item in self.providers}) != 2:
            raise ValueError("two distinct reference providers are required")
        return self


@dataclass(frozen=True, slots=True)
class CurrencyFeed:
    """One priced pool currency and the pinned feed policy that prices it."""

    address: str
    decimals: int
    policy: UsdFeedPolicy

    def __post_init__(self) -> None:
        object.__setattr__(self, "address", normalize_address(self.address))
        if type(self.decimals) is not int or not 0 <= self.decimals <= 36:
            raise ValueError("currency decimals must be a bounded integer")
        if self.policy.base_token != self.address:
            raise ValueError("feed denomination mismatch")
        if self.address == ZERO_ADDRESS and (
            self.decimals != 18
            or self.policy.chain_id not in _NATIVE_ETH_CHAINS
            or self.policy.description != "ETH / USD"
        ):
            raise ValueError(
                "native currency requires a supported ETH chain, 18 decimals and ETH feed"
            )


def currency_feed(chain_id: int, declaration: CurrencyDeclaration) -> CurrencyFeed:
    """Materialize one declared pool currency and its pinned feed policy.

    The declaration and its feed pin are one operator statement, so the policy is
    built here rather than twice in two callers that could drift apart.
    """
    if not isinstance(declaration, CurrencyDeclaration):
        raise TypeError("a typed currency declaration is required")
    pin = declaration.feed
    return CurrencyFeed(
        declaration.address,
        declaration.decimals,
        UsdFeedPolicy(
            chain_id,
            pin.address,
            declaration.address,
            pin.description,
            pin.runtime_keccak,
            pin.max_age_seconds,
        ),
    )


@dataclass(frozen=True, slots=True)
class FeedQuote:
    """One currency's observed USD answer, in that feed's and token's own scales."""

    answer: int
    feed_decimals: int
    token_decimals: int

    def __post_init__(self) -> None:
        if self.answer <= 0:
            raise ValueError("a positive feed answer is required")
        for name in ("feed_decimals", "token_decimals"):
            value = getattr(self, name)
            if type(value) is not int or not 0 <= value <= 36:
                raise ValueError(f"{name} must be a bounded integer")


def reference_price_x192(currency0: FeedQuote, currency1: FeedQuote) -> int:
    """The Q192 price of one ``currency0`` base unit in ``currency1`` base units.

    Each answer is that currency's whole-token USD price scaled by its feed's own
    decimals, and one raw unit is a currency's own ``10 ** -decimals`` of a whole
    token, so the ratio is currency0's USD value per currency1's USD value in the
    currencies' own raw units -- exactly the unit ``sqrt_priceX96 ** 2`` reports and
    the unit ``produce_pool_state`` divides by ``Q192`` to value ``inventory0`` in
    ``inventory1``. The result is floored, as Uniswap's own price is, and refused
    when it rounds to zero.
    """
    for name, quote in (("currency0", currency0), ("currency1", currency1)):
        if not isinstance(quote, FeedQuote):
            raise TypeError(f"{name} must be a typed feed quote")
    numerator: int = currency0.answer * 10 ** (currency1.feed_decimals + currency1.token_decimals)
    denominator: int = currency1.answer * 10 ** (currency0.feed_decimals + currency0.token_decimals)
    value: int = numerator * Q192 // denominator
    if value == 0:
        raise ValueError("the declared feeds imply a zero reference price")
    return value


def reference_deviation(text: str) -> Decimal:
    """Parse the operator's explicit deviation bound; floats and loose forms refused."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("an explicit max_reference_deviation is required")
    try:
        bound = Decimal(text)
    except ArithmeticError as error:  # decimal.InvalidOperation is not a ValueError
        raise ValueError("max_reference_deviation must be an explicit decimal string") from error
    if not bound.is_finite() or not 0 <= bound <= 1:
        raise ValueError("max_reference_deviation must be between zero and one")
    numerator, denominator = bound.as_integer_ratio()
    if numerator * 10**18 % denominator:
        raise ValueError("max_reference_deviation must have at most 18 decimal places")
    return bound


def _within_deviation(bound: Decimal, difference: int, reference: int) -> bool:
    """Integer-exact ``difference / reference <= bound``; no binary float anywhere."""
    if reference <= 0:
        raise ValueError("a positive reference price is required")
    numerator, denominator = bound.as_integer_ratio()
    return difference * denominator <= numerator * reference


def _uint8(result: object) -> int:
    if not isinstance(result, str):
        raise ValueError("a decimals getter must return hex data")
    return int(cast(int, abi_decode(["uint8"], bytes.fromhex(normalize_hex(result)[2:]))[0]))


@dataclass(frozen=True, slots=True)
class CurrencyFeedCapture:
    """One provider's exact bytes for one pool currency's USD feed at one block.

    The block is not supplied: it is read out of this capture's own opening header,
    so the evidence pins its own height, hash and timestamp.
    """

    source: str
    currency: str
    currency_decimals: int
    policy: UsdFeedPolicy
    exchanges: tuple[RpcExchange, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "exchanges", tuple(self.exchanges))
        object.__setattr__(self, "currency", normalize_address(self.currency))
        if not self.source.strip():
            raise ValueError("currency feed provider required")
        if self.policy.base_token != self.currency:
            raise ValueError("feed denomination mismatch")
        if type(self.currency_decimals) is not int or not 0 <= self.currency_decimals <= 36:
            raise ValueError("currency decimals must be a bounded integer")
        CurrencyFeed(self.currency, self.currency_decimals, self.policy)
        self.reconstruct()

    def reconstruct(self) -> tuple[BlockSnapshot, UsdPriceObservation, int]:
        """Every field this capture asserts, re-derived from its own raw bytes."""
        exchanges = self.exchanges
        native = self.currency == ZERO_ADDRESS
        expected_count = _EXPECTED_EXCHANGES - int(native)
        if (
            len(exchanges) != expected_count
            or sum(len(item.request_payload) + len(item.response_payload) for item in exchanges)
            > _MAX_ENVELOPE_BYTES
        ):
            raise ValueError("incomplete or oversized currency feed evidence")
        chain_params, chain_response = decode_exchange(exchanges[_CHAIN])
        if exchanges[_CHAIN].method != "eth_chainId" or chain_params or "error" in chain_response:
            raise ValueError("currency feed evidence must open with a chain id")
        chain_id = rpc_quantity(chain_response["result"])
        opening_params, opening_response = decode_exchange(exchanges[_OPENING_HEADER])
        if (
            exchanges[_OPENING_HEADER].method != "eth_getBlockByNumber"
            or len(opening_params) != 2
            or opening_params[1] is not False
            or "error" in opening_response
        ):
            raise ValueError("currency feed evidence must open with a block header")
        header = decode_header(opening_response["result"], chain_id=chain_id)
        results = successful_results(exchanges, _expected(self, header))
        if decode_header(results[-1], chain_id=chain_id) != header:
            raise ValueError("currency feed block changed during collection")
        feed = UsdPriceCapture(
            header,
            self.policy,
            self.source,
            exchanges if native else tuple(exchanges[index] for index in _FEED_SEQUENCE),
        )
        observation = feed.reconstruct_observation()
        decimals = 18 if native else _uint8(results[_TOKEN_DECIMALS])
        if decimals != self.currency_decimals:
            raise ValueError("currency token decimals mismatch")
        return header, observation, decimals

    @property
    def payload(self) -> bytes:
        header, observation, decimals = self.reconstruct()
        return _currency_feed_payload(self, header, observation, decimals)


def _expected(
    capture: CurrencyFeedCapture, header: BlockSnapshot
) -> list[tuple[str, list[object]]]:
    """The exact exchange sequence one currency feed capture must consist of."""
    block = header.ref
    pin: dict[str, object] = {"blockHash": block.block_hash, "requireCanonical": True}
    policy = capture.policy
    expected: list[tuple[str, list[object]]] = [
        ("eth_chainId", []),
        ("eth_getBlockByNumber", [hex(block.number), False]),
        ("eth_getCode", [policy.address, pin]),
    ]
    expected.extend(
        ("eth_call", [{"to": policy.address, "data": selector(name)}, pin])
        for name in FEED_SIGNATURES
    )
    if capture.currency != ZERO_ADDRESS:
        expected.append(
            ("eth_call", [{"to": capture.currency, "data": selector("decimals()")}, pin])
        )
    expected.append(("eth_getBlockByNumber", [hex(block.number), False]))
    return expected


def collect_currency_feed(
    session: RecordedSession,
    *,
    currency: CurrencyFeed,
    block_number: int,
) -> CurrencyFeedCapture:
    """Read one pool currency's pinned feed at one explicit block through one provider.

    The caller supplies a fresh one-shot session with a wired recorder, so the
    exchanges this returns are the capture's own and cannot be shared with another
    read. The sequence is ``_expected``'s: the currency's own on-chain ``decimals``
    is read beside the feed for ERC-20 currencies. Native ETH has no getter and
    uses the supported chain's protocol scale, never a simulated zero-address call.
    """
    if not isinstance(currency, CurrencyFeed):
        raise TypeError("a typed currency feed is required")
    if type(block_number) is not int or block_number < 0:
        raise ValueError("an explicit nonnegative block number is required")
    transport, recorder, source = session
    client = RpcClient(transport, expected_chain_id=currency.policy.chain_id)
    header = client.get_block_snapshot(block_number)
    client.get_code(currency.policy.address, header.ref)
    for name in FEED_SIGNATURES:
        client.call(currency.policy.address, selector(name), header.ref)
    if currency.address != ZERO_ADDRESS:
        client.call(currency.address, selector("decimals()"), header.ref)
    closing = client.get_block_snapshot(block_number)
    if closing != header:
        raise ValueError("the currency feed block moved during collection")
    return CurrencyFeedCapture(
        source,
        currency.address,
        currency.decimals,
        currency.policy,
        recorder.exchanges,
    )


def currency_feed_from_inventory(
    capture: AccountInventoryCapture, *, currency: CurrencyFeed
) -> CurrencyFeedCapture:
    """Reuse an inventory's exact wire bytes without another price lookup.

    Revalidate the complete inventory first. Selected exchanges preserve IDs
    and bytes; the ERC-20 getter is reordered into the reference envelope, not
    represented as a new network observation. Both valuations use one policy.
    """
    if not isinstance(capture, AccountInventoryCapture) or not isinstance(currency, CurrencyFeed):
        raise TypeError("typed inventory capture and currency required")
    capture.reconstruct()
    asset = capture.entry.asset_by_address(currency.address)
    if (
        asset.decimals != currency.decimals
        or asset.usd_feed(capture.entry.chain_id) != currency.policy
    ):
        raise ValueError("reference currency policy differs from account inventory")
    # Inventory reconstruction verified this exact layout. Select the asset's
    # own four feed exchanges, even when ETH and WETH share a feed address.
    entry = capture.entry
    offset = (
        3 + 3 * len(entry.erc20_assets) + 2 * len(entry.positions) + 4 * entry.assets.index(asset)
    )
    selected = [*capture.exchanges[:2], *capture.exchanges[offset : offset + 4]]
    if not asset.is_native:
        selected.append(capture.exchanges[4 + 3 * entry.erc20_assets.index(asset)])
    selected.append(capture.exchanges[-1])
    return CurrencyFeedCapture(
        capture.source, currency.address, currency.decimals, currency.policy, tuple(selected)
    )


def _currency_feed_payload(
    capture: CurrencyFeedCapture,
    header: BlockSnapshot,
    observation: UsdPriceObservation,
    decimals: int,
) -> bytes:
    return canonical_json(
        {
            "schema": CURRENCY_FEED_SCHEMA,
            "source": capture.source,
            "currency": capture.currency,
            "currency_decimals": decimals,
            "policy": asdict(capture.policy),
            "block": asdict(header.ref),
            "observation": asdict(observation),
        }
    )


def persist_currency_feed_evidence(
    store: EvidenceStore, capture: CurrencyFeedCapture
) -> EvidenceRef:
    """Retain one provider's pinned currency feed observation with its exact bytes."""
    header, _observation, _decimals = capture.reconstruct()
    return store.put(
        CURRENCY_FEED_KIND,
        capture.payload,
        header.ref,
        capture.source,
        request=wire_envelope(capture.exchanges, response=False),
        raw_response=wire_envelope(capture.exchanges, response=True),
        interpretation={
            "schema": CURRENCY_FEED_SCHEMA,
            "validation": "raw_rpc_reconstruction",
            "currency_decimals_source": "native_eth_protocol"
            if capture.currency == ZERO_ADDRESS
            else "erc20_decimals_getter",
            "execution_eligible": False,
        },
    )


def replay_currency_feed_evidence(
    store: EvidenceStore,
    evidence: EvidenceRef,
    *,
    currency: CurrencyFeed,
) -> CurrencyFeedCapture:
    """Rebuild one retained currency feed observation from its envelopes alone."""
    if evidence.kind != CURRENCY_FEED_KIND or evidence.block is None:
        raise ValueError("currency feed evidence scope mismatch")
    try:
        record = store.get_record(evidence)
    except KeyError as error:
        raise ValueError("currency feed evidence is not retained") from error
    if record.request_payload is None:
        raise ValueError("currency feed evidence missing wire requests")
    capture = CurrencyFeedCapture(
        evidence.source,
        currency.address,
        currency.decimals,
        currency.policy,
        restore_exchanges(record.request_payload, record.raw_response_payload),
    )
    header, _observation, _decimals = capture.reconstruct()
    if header.ref != evidence.block:
        raise ValueError("currency feed evidence is not the block it claims")
    if capture.payload != record.payload:
        raise ValueError("currency feed interpretation differs from raw reconstruction")
    return capture


def _pool_price_x192(price: AgreedInteger) -> int:
    """The window's own decision-block price, in the same Q192 unit as the ratio."""
    if price.name != "sqrt_price_x96" or not 0 < price.value < 1 << 160:
        raise ValueError("a positive bounded decision-block pool price is required")
    return price.value**2


def _retain(
    store: EvidenceStore,
    *,
    value: int,
    block: BlockRef,
    record: EvidenceRecord,
    interpretation: dict[str, object],
) -> EvidenceRef:
    """One provider's derived reference price, borrowing that provider's own bytes."""
    return store.put(
        DERIVED_REFERENCE_KIND,
        agreed_value_payload(REFERENCE_NAME, value, block),
        block,
        record.reference.source,
        request=record.request_payload,
        raw_response=record.raw_response_payload,
        interpretation=interpretation,
        retrieved_at=record.reference.retrieved_at,
    )


def agreed_reference_price(
    store: EvidenceStore,
    window: MarketWindow,
    *,
    pool: PoolKey,
    currency0: CurrencyFeed,
    currency1: CurrencyFeed,
    providers: tuple[ReferenceProvider, ReferenceProvider],
    max_deviation: Decimal,
) -> AgreedInteger:
    """Derive and retain the decision-block reference price, or refuse everything.

    Both currencies' feeds are replayed at the window's own decision block from the
    window's own two providers, the two providers must agree to the base unit, and
    the result must be a plausible price of this pool. Every check precedes the
    first write, so a refused input retains no partially derived value.
    """
    if not isinstance(store, EvidenceStore):
        raise TypeError("an evidence store is required")
    if not isinstance(window, MarketWindow):
        raise TypeError("a verified market window is required")
    if not isinstance(pool, PoolKey):
        raise TypeError("a typed pool key is required")
    for name, feed in (("currency0", currency0), ("currency1", currency1)):
        if not isinstance(feed, CurrencyFeed):
            raise TypeError(f"{name} must be a typed currency feed")
    if (
        not isinstance(providers, tuple)
        or len(providers) != 2
        or not all(isinstance(item, ReferenceProvider) for item in providers)
        or providers[0].source == providers[1].source
    ):
        raise ValueError("two distinct reference providers are required")
    if not isinstance(max_deviation, Decimal):
        raise TypeError("an explicit max_reference_deviation is required")
    max_deviation = reference_deviation(str(max_deviation))
    if (pool.currency0, pool.currency1) != (currency0.address, currency1.address):
        raise ValueError("the declared currencies are not the pool's own currencies")
    if pool_id_from_key(pool) != window.pool_id:
        raise ValueError("the declared pool is not the market window's pool")
    block = window.decision_block
    for currency in (currency0, currency1):
        if currency.policy.chain_id != window.chain_id:
            raise ValueError("the declared feeds are not on the market window's chain")
    if not window.prices:
        raise ValueError("the market window carries no pool price")
    price = window.prices[-1]
    if not isinstance(price, AgreedInteger) or price.block != block:
        raise ValueError("the window's own price is not the decision-block price")
    sources = frozenset(item.source for item in price.evidence)
    if len(sources) != 2 or {item.source for item in providers} != sources:
        raise ValueError("the reference price must come from the window's own two providers")
    pool_price = _pool_price_x192(price)
    quotes: list[int] = []
    donors: dict[str, EvidenceRecord] = {}
    parents: list[EvidenceRef] = []
    for provider in sorted(providers, key=lambda item: item.source):
        observations: list[UsdPriceObservation] = []
        for currency, declared in (
            (currency0, provider.currency0),
            (currency1, provider.currency1),
        ):
            if declared.source != provider.source:
                raise ValueError("a currency feed reference is not its declared provider")
            capture = replay_currency_feed_evidence(store, declared, currency=currency)
            header, observation, _decimals = capture.reconstruct()
            if header.ref != block:
                raise ValueError("a currency feed observation is not the decision block")
            observations.append(observation)
            parents.append(declared)
        quotes.append(
            reference_price_x192(
                FeedQuote(observations[0].answer, observations[0].decimals, currency0.decimals),
                FeedQuote(observations[1].answer, observations[1].decimals, currency1.decimals),
            )
        )
        donors[provider.source] = store.get_record(provider.currency0)
    if len(set(quotes)) != 1:
        raise ValueError("the reference price providers disagree on the value")
    value = quotes[0]
    if not _within_deviation(max_deviation, abs(value - pool_price), pool_price):
        raise ValueError("the derived reference price is not a plausible price of this pool")
    interpretation = {
        "schema": REFERENCE_SCHEMA,
        "execution_eligible": False,
        "bytes_kind": "currency_feed_observation_wire_envelope_that_pinned_this_value",
        "pool_id": window.pool_id,
        "name": REFERENCE_NAME,
        "value": value,
        "block": asdict(block),
        "currencies": [currency0.address, currency1.address],
        "feed_pins": [asdict(currency0.policy), asdict(currency1.policy)],
        "currency_decimals": [currency0.decimals, currency1.decimals],
        "pool_price_x192": pool_price,
        "max_reference_deviation": str(max_deviation),
        "parents": [
            asdict(item)
            for item in sorted(parents, key=lambda item: (item.source, item.content_hash))
        ],
    }
    references = tuple(
        _retain(
            store,
            value=value,
            block=block,
            record=donors[source],
            interpretation=interpretation,
        )
        for source in sorted(donors)
    )
    return AgreedInteger(REFERENCE_NAME, value, block, references)


__all__ = [
    "CURRENCY_FEED_KIND",
    "CURRENCY_FEED_SCHEMA",
    "DERIVED_REFERENCE_KIND",
    "FEED_SIGNATURES",
    "REFERENCE_NAME",
    "REFERENCE_SCHEMA",
    "CurrencyDeclaration",
    "CurrencyFeed",
    "CurrencyFeedCapture",
    "FeedPin",
    "FeedQuote",
    "ReferenceDeclaration",
    "ReferenceProvider",
    "ReferenceRequest",
    "agreed_reference_price",
    "collect_currency_feed",
    "currency_feed",
    "persist_currency_feed_evidence",
    "reference_deviation",
    "reference_price_x192",
    "replay_currency_feed_evidence",
]
