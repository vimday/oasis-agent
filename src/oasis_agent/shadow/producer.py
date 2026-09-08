"""Bounded live shadow observation producer (WP6).

Acquires block-pinned pool activity from one RPC source, reconstructs swap
state, quotes the configured fee strategy, and compares expected vs observed
fees. Research single-provider only: full AgreedInteger decision features
require a second agreeing provider, so this producer never emits an
execution-eligible decision. No signing interface exists here.
"""

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, localcontext
from itertools import pairwise

from ..evidence.rpc_capture import (
    BoundedExchangeRecorder,
    persist_block_range_evidence,
)
from ..evidence.store import EvidenceStore
from ..farming.strategy import BandTableFeeStrategy, OnchainAmountBandFeePolicy
from ..models.common import (
    BlockRef,
    EvidenceRef,
    canonical_json,
    normalize_address,
    normalize_hash32,
    sha256_hex,
)
from ..models.finance import PoolState
from ..models.v4 import SwapDirection, SwapObservation
from ..rpc.client import RpcClient
from ..rpc.collector import BlockRangeDataset, collect_block_range
from ..rpc.transport import HttpPoster, JsonRpcTransport, RpcExchange
from ..uniswap_v4.events import SWAP_TOPIC, decode_swap

Q96 = 1 << 96


@dataclass(frozen=True, slots=True)
class ObservedPoolKey:
    chain_id: int
    token0: str
    token1: str
    hook: str
    pool_id: str

    def __post_init__(self) -> None:
        if type(self.chain_id) is not int or self.chain_id <= 0:
            raise ValueError("positive observation chain required")
        for name in ("token0", "token1", "hook"):
            address = normalize_address(getattr(self, name))
            if int(address, 16) == 0:
                raise ValueError(f"{name} must be nonzero")
            object.__setattr__(self, name, address)
        if self.token0 >= self.token1:
            raise ValueError("canonical token ordering requires token0 < token1")
        object.__setattr__(self, "pool_id", normalize_hash32(self.pool_id, "pool id").lower())


@dataclass(frozen=True, slots=True)
class SwapQuoteComparison:
    block_number: int
    block_hash: str
    transaction_hash: str
    log_index: int
    pool_id: str
    zero_for_one: bool
    direction: str
    amount0: int
    amount1: int
    sqrt_price_x96: int
    tick: int
    liquidity: int
    observed_fee_pips: int | None
    quote_exact_input_fee_pips: int | None
    quote_exact_output_fee_pips: int | None
    deployed_hook_exact_input_fee_pips: int | None
    deployed_hook_exact_output_fee_pips: int | None
    volatility: str
    inventory_skew: str
    adverse_selection: str | None
    oracle_deviation: str
    exact_fee_match: bool | None

    def __post_init__(self) -> None:
        if type(self.block_number) is not int or type(self.log_index) is not int:
            raise TypeError("block number and log index must be integers")
        if self.amount0 == 0 or self.amount1 == 0 or self.amount0 * self.amount1 >= 0:
            raise ValueError("swap delta signs must be opposite and nonzero")
        for name in ("volatility", "inventory_skew", "oracle_deviation"):
            value = getattr(self, name)
            if not isinstance(value, str):
                raise TypeError(f"{name} must be a decimal string")
            Decimal(value)
        if self.adverse_selection is not None:
            if not isinstance(self.adverse_selection, str):
                raise TypeError("adverse_selection must be a decimal string or None")
            Decimal(self.adverse_selection)
        if self.observed_fee_pips is not None and (
            type(self.observed_fee_pips) is not int or self.observed_fee_pips < 0
        ):
            raise ValueError("observed fee must be a nonnegative integer or None")
        for name in (
            "quote_exact_input_fee_pips",
            "quote_exact_output_fee_pips",
            "deployed_hook_exact_input_fee_pips",
            "deployed_hook_exact_output_fee_pips",
        ):
            value = getattr(self, name)
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError(f"{name} must be a nonnegative integer or None")
        exact = self.exact_fee_match
        if exact is not None and type(exact) is not bool:
            raise TypeError("exact_fee_match must be a bool or None")


@dataclass(frozen=True, slots=True)
class ObservationReport:
    schema: str = field(default="oasis.lp-live-shadow-observation.v2", init=False)
    chain_id: int
    source: str
    start_block: int
    end_block: int
    pool_id: str
    window_evidence: EvidenceRef
    block_count: int
    swap_count: int
    comparisons: tuple[SwapQuoteComparison, ...]
    observed_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    execution_eligible: bool = field(default=False, init=False)
    trust: str = field(default="single_provider_research", init=False)

    def __post_init__(self) -> None:
        if type(self.chain_id) is not int or self.chain_id <= 0:
            raise ValueError("positive observation chain required")
        if self.end_block < self.start_block:
            raise ValueError("observation block range is invalid")
        if len(self.comparisons) != self.swap_count:
            raise ValueError("comparison count must equal swap count")
        if self.execution_eligible:
            raise ValueError("observation reports are never execution-eligible")

    @property
    def payload(self) -> bytes:
        data = asdict(self)
        # Hard invariant: no observation report can ever be hashed with
        # execution_eligible true, even if the field were tampered with.
        data["execution_eligible"] = False
        return canonical_json(data)

    @property
    def digest(self) -> str:
        return sha256_hex(self.payload)


def acquire_observation(
    rpc: RpcClient,
    recorder: BoundedExchangeRecorder,
    *,
    source: str,
    start_block: int,
    end_block: int,
    address: str,
    topics: tuple[str, ...],
    max_blocks: int = 1_000,
) -> tuple[BlockRangeDataset, tuple[RpcExchange, ...]]:
    """Fetch a block-pinned log window through a recording transport."""
    dataset = collect_block_range(
        rpc,
        source=source,
        start_block=start_block,
        end_block=end_block,
        address=address,
        topics=topics,
        max_blocks=max_blocks,
    )
    return dataset, recorder.exchanges


def persist_observation(
    store: EvidenceStore,
    dataset: BlockRangeDataset,
    exchanges: tuple[RpcExchange, ...],
    *,
    retrieved_at: str | None = None,
) -> EvidenceRef:
    return persist_block_range_evidence(store, dataset, exchanges, retrieved_at=retrieved_at)


def _decimal_str(value: Decimal) -> str:
    return format(value, "f")


def swap_state(
    *,
    decision_block: BlockRef,
    price_history: tuple[SwapObservation, ...],
    inventory0: int = 0,
    inventory1: int = 0,
    reference_sqrt_price_x96: int | None = None,
) -> PoolState:
    """Reconstruct observation PoolState from acquired swap prices.

    Research path only: no external oracle is assumed, so oracle deviation is
    measured against the last pool price and explicitly flagged unpriced by the
    caller's report trust label. Inventory defaults to zero (no owned position
    in observation mode) which yields zero skew.
    """
    if type(inventory0) is not int or type(inventory1) is not int:
        raise TypeError("inventory must be integers")
    if inventory0 < 0 or inventory1 < 0:
        raise ValueError("inventory must be nonnegative")
    if decision_block.timestamp is None:
        raise ValueError("decision block timestamp is required")
    # Research path: no external oracle is assumed. When no independent
    # reference price is supplied the pool's own last price is used, which by
    # construction yields zero deviation; the caller's report trust label must
    # flag this single-provider research path. An explicit reference is
    # validated rather than silently replaced (no `or` fallback masking).
    if reference_sqrt_price_x96 is not None:
        if type(reference_sqrt_price_x96) is not int or not 0 < reference_sqrt_price_x96 < (
            1 << 160
        ):
            raise ValueError("reference sqrt price must be a positive int bounded by 2^160")
    else:
        reference_sqrt_price_x96 = price_history[-1].sqrt_price_x96
    with localcontext() as context:
        context.prec = 80  # matches produce_pool_state so research and feature paths agree
        prices = tuple(Decimal(item.sqrt_price_x96) ** 2 for item in price_history)
        if not prices:
            raise ValueError("price history requires at least one swap")
        if len(prices) == 1:
            volatility = Decimal(0)
        else:
            terms = tuple(
                abs(current - previous) / previous for previous, current in pairwise(prices)
            )
            volatility = sum(terms, Decimal(0)) / Decimal(len(terms))
        reference = Decimal(reference_sqrt_price_x96) ** 2
        final_pool = prices[-1]
        oracle_deviation = abs(final_pool - reference) / reference
        value0 = Decimal(inventory0) * reference / Decimal(1 << 192)
        value1 = Decimal(inventory1)
        total = value0 + value1
        inventory_skew = Decimal(0) if total == 0 else (value0 - value1) / total
    timestamp = decision_block.timestamp
    return PoolState(
        volatility=volatility,
        inventory_imbalance=inventory_skew,
        adverse_selection=Decimal(0),
        rebalance_incentive=abs(inventory_skew),
        observed_at_timestamp=timestamp,
        oracle_timestamp=timestamp,
        oracle_deviation=oracle_deviation,
        block=decision_block,
    )


def _realized_adverse_selection(
    current: SwapObservation, following: SwapObservation | None
) -> Decimal | None:
    """Realized markout against the next observed pool price, when available."""
    if following is None or following.sqrt_price_x96 == 0:
        return None
    execution = Decimal(current.sqrt_price_x96) ** 2
    future = Decimal(following.sqrt_price_x96) ** 2
    if execution == 0:
        return None
    if current.amount0 > 0:  # pool received token0: zero-for-one swap
        return max(Decimal(0), (future - execution) / future)
    return max(Decimal(0), (execution - future) / future)


def build_observation(
    *,
    dataset: BlockRangeDataset,
    window_evidence: EvidenceRef,
    pool: ObservedPoolKey,
    strategy: BandTableFeeStrategy,
    min_benefit: Decimal,
    source: str,
    deployed_hook_policy: OnchainAmountBandFeePolicy | None = None,
    max_window_swaps: int = 256,
) -> ObservationReport:
    """Decode Swap events and quote the strategy against each observed swap."""
    if not isinstance(strategy, BandTableFeeStrategy):
        raise TypeError("typed band-table fee strategy required")
    if deployed_hook_policy is not None and not isinstance(
        deployed_hook_policy, OnchainAmountBandFeePolicy
    ):
        raise TypeError("typed deployed hook fee policy required")
    if not isinstance(min_benefit, Decimal) or not min_benefit.is_finite():
        raise ValueError("finite Decimal min_benefit required")
    blocks_by_number = {item.ref.number: item.ref for item in dataset.blocks}
    swaps: list[tuple[SwapObservation, str, int]] = []
    for log in dataset.logs:
        if log.topics and log.topics[0] == SWAP_TOPIC:
            decoded = decode_swap(log)
            if decoded.pool_id != pool.pool_id:
                raise ValueError("observed swap does not belong to the pinned pool")
            swaps.append((decoded, log.transaction_hash, log.log_index))
    ordered = tuple(sorted(swaps, key=lambda item: item[0].block_number))
    if len(ordered) > max_window_swaps:
        ordered = ordered[-max_window_swaps:]

    comparisons: list[SwapQuoteComparison] = []
    for index, (swap, transaction_hash, log_index) in enumerate(ordered):
        block = blocks_by_number.get(swap.block_number)
        if block is None or block.block_hash is None:
            raise ValueError("swap block missing from acquired window")
        history = tuple(item[0] for item in ordered if item[0].block_number <= swap.block_number)
        state = swap_state(decision_block=block, price_history=history)
        direction = SwapDirection.ZERO_FOR_ONE if swap.amount0 > 0 else SwapDirection.ONE_FOR_ZERO
        exact_input_amount = swap.amount0 if swap.amount0 > 0 else swap.amount1
        exact_output_amount = -swap.amount1 if swap.amount1 < 0 else -swap.amount0
        quote_in = strategy.quote(
            state, direction, exact_input_amount, exact_input=True, benign_flow=False
        )
        quote_out = strategy.quote(
            state, direction, exact_output_amount, exact_input=False, benign_flow=False
        )
        hook_quote_in = (
            None
            if deployed_hook_policy is None
            else deployed_hook_policy.quote(direction, exact_input_amount, exact_input=True)
        )
        hook_quote_out = (
            None
            if deployed_hook_policy is None
            else deployed_hook_policy.quote(direction, exact_output_amount, exact_input=False)
        )
        following = ordered[index + 1][0] if index + 1 < len(ordered) else None
        adverse = _realized_adverse_selection(swap, following)
        observed = swap.fee
        comparisons.append(
            SwapQuoteComparison(
                block_number=swap.block_number,
                block_hash=block.block_hash,
                transaction_hash=transaction_hash,
                log_index=log_index,
                pool_id=swap.pool_id,
                zero_for_one=swap.amount0 > 0,
                direction=direction.value,
                amount0=swap.amount0,
                amount1=swap.amount1,
                sqrt_price_x96=swap.sqrt_price_x96,
                tick=swap.tick,
                liquidity=swap.liquidity,
                observed_fee_pips=observed,
                quote_exact_input_fee_pips=quote_in.effective_fee,
                quote_exact_output_fee_pips=quote_out.effective_fee,
                deployed_hook_exact_input_fee_pips=hook_quote_in,
                deployed_hook_exact_output_fee_pips=hook_quote_out,
                volatility=_decimal_str(state.volatility),
                inventory_skew=_decimal_str(state.inventory_imbalance),
                adverse_selection=None if adverse is None else _decimal_str(adverse),
                oracle_deviation=_decimal_str(state.oracle_deviation),
                exact_fee_match=(
                    None
                    if observed is None or hook_quote_in is None or hook_quote_in != hook_quote_out
                    else observed == hook_quote_in
                ),
            )
        )
    return ObservationReport(
        chain_id=pool.chain_id,
        source=source,
        start_block=dataset.start_block,
        end_block=dataset.end_block,
        pool_id=pool.pool_id,
        window_evidence=window_evidence,
        block_count=len(dataset.blocks),
        swap_count=len(comparisons),
        comparisons=tuple(comparisons),
    )


def open_observation_rpc(
    *,
    url: str,
    chain_id: int,
    recorder: BoundedExchangeRecorder,
    poster: HttpPoster | None = None,
) -> RpcClient:
    """Open a recording RPC client; an explicit poster may be injected for
    endpoints whose certificate chain the sandboxed urllib cannot verify.
    No credential material is ever added; verification is never disabled."""
    transport = JsonRpcTransport(url, recorder=recorder, http=poster)
    return RpcClient(transport, expected_chain_id=chain_id)


__all__ = [
    "ObservationReport",
    "ObservedPoolKey",
    "SwapQuoteComparison",
    "acquire_observation",
    "build_observation",
    "open_observation_rpc",
    "persist_observation",
    "swap_state",
]
