"""Atomic flash-loan liquidation scanner over synchronized pool graphs (research/shadow only)."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal

from ..models.common import normalize_address, normalize_hash32
from .bridge import FastpathCallError, require_int
from .pool_graph import FastpathPoolGraph, PoolNode

LiquidationProtocol = Literal["morpho_blue", "aave_v3", "moonwell"]

_SUPPORTED_PROTOCOLS: Final[frozenset[str]] = frozenset({"morpho_blue", "aave_v3", "moonwell"})
_WAD: Final[int] = 10**18
_BPS_DENOM: Final[int] = 10_000
_REPAY_FRACTIONS_PCT: Final[tuple[int, ...]] = (25, 50, 75, 100)


@dataclass(frozen=True, slots=True)
class BorrowerPositionSnapshot:
    protocol: LiquidationProtocol
    market_id: str
    borrower: str
    debt_token: str
    collateral_token: str
    debt_amount_raw: int
    collateral_amount_raw: int
    health_factor_wad: int
    close_factor_bps: int
    liquidation_bonus_bps: int
    oracle_price_collateral_in_debt_wad: int
    block_number: int
    block_hash: str

    def __post_init__(self) -> None:
        if self.protocol not in _SUPPORTED_PROTOCOLS:
            raise ValueError(f"unsupported lending protocol: {self.protocol!r}")
        if not isinstance(self.market_id, str) or not self.market_id.strip():
            raise ValueError("market_id must be a non-empty string")
        object.__setattr__(self, "borrower", normalize_address(self.borrower))
        object.__setattr__(self, "debt_token", normalize_address(self.debt_token))
        object.__setattr__(self, "collateral_token", normalize_address(self.collateral_token))
        object.__setattr__(self, "block_hash", normalize_hash32(self.block_hash, "block_hash"))
        require_int(self.debt_amount_raw, "debt_amount_raw", non_negative=True)
        require_int(self.collateral_amount_raw, "collateral_amount_raw", non_negative=True)
        require_int(self.health_factor_wad, "health_factor_wad", non_negative=True)
        require_int(self.close_factor_bps, "close_factor_bps", non_negative=True)
        require_int(self.liquidation_bonus_bps, "liquidation_bonus_bps", non_negative=True)
        require_int(
            self.oracle_price_collateral_in_debt_wad,
            "oracle_price_collateral_in_debt_wad",
            non_negative=True,
        )
        require_int(self.block_number, "block_number", non_negative=True)


@dataclass(frozen=True, slots=True)
class FlashLiquidationConfig:
    flash_fee_bps: int = 0
    l2_gas_units: int = 380_000
    gas_cost_in_debt_raw: int = 0
    min_net_profit_debt_raw: int = 1
    max_repay_debt_raw: int = 0

    def __post_init__(self) -> None:
        flash_fee = require_int(self.flash_fee_bps, "flash_fee_bps", non_negative=True)
        if flash_fee > _BPS_DENOM:
            raise ValueError("flash_fee_bps must be <= 10_000")
        require_int(self.l2_gas_units, "l2_gas_units", non_negative=True)
        require_int(self.gas_cost_in_debt_raw, "gas_cost_in_debt_raw", non_negative=True)
        require_int(self.min_net_profit_debt_raw, "min_net_profit_debt_raw", non_negative=True)
        require_int(self.max_repay_debt_raw, "max_repay_debt_raw", non_negative=True)


@dataclass(frozen=True, slots=True)
class LiquidationCandidate:
    protocol: LiquidationProtocol
    market_id: str
    borrower: str
    debt_token: str
    collateral_token: str
    repay_debt_raw: int
    seized_collateral_raw: int
    swap_pool_id: str
    swap_out_debt_raw: int
    flash_fee_debt_raw: int
    gas_cost_in_debt_raw: int
    net_profit_debt_raw: int
    health_factor_wad: int
    block_number: int
    block_hash: str
    execution_eligible: Literal[False] = False

    def __post_init__(self) -> None:
        if self.execution_eligible is not False:
            raise ValueError("LiquidationCandidate is research-only (execution_eligible=False)")


def _graph_pools(pool_graph: FastpathPoolGraph) -> tuple[PoolNode, ...]:
    public_pools = getattr(pool_graph, "pools", None)
    if isinstance(public_pools, Mapping):
        return tuple(public_pools.values())
    if isinstance(public_pools, Sequence):
        return tuple(public_pools)
    return tuple(pool_graph._pools.values())


def _is_candidate_better(
    candidate: LiquidationCandidate,
    incumbent: LiquidationCandidate | None,
) -> bool:
    if incumbent is None:
        return True
    if candidate.net_profit_debt_raw != incumbent.net_profit_debt_raw:
        return candidate.net_profit_debt_raw > incumbent.net_profit_debt_raw
    if candidate.repay_debt_raw != incumbent.repay_debt_raw:
        return candidate.repay_debt_raw < incumbent.repay_debt_raw
    return candidate.swap_pool_id < incumbent.swap_pool_id


class FlashLiquidationScanner:
    """Stateless atomic flash-loan liquidation opportunity scanner."""

    @staticmethod
    def scan(
        positions: Sequence[BorrowerPositionSnapshot],
        pool_graph: FastpathPoolGraph,
        config: FlashLiquidationConfig | None = None,
    ) -> list[LiquidationCandidate]:
        active_config = config if config is not None else FlashLiquidationConfig()
        all_pools = _graph_pools(pool_graph)
        best_by_position: dict[tuple[str, str, str], LiquidationCandidate] = {}

        for pos in positions:
            if not 0 < pos.health_factor_wad < _WAD:
                continue
            if pos.debt_amount_raw <= 0 or pos.collateral_amount_raw <= 0:
                continue
            if pos.oracle_price_collateral_in_debt_wad <= 0:
                continue
            if not 1 <= pos.close_factor_bps <= _BPS_DENOM:
                continue
            if pos.collateral_token == pos.debt_token:
                continue

            max_repay_by_debt = (pos.debt_amount_raw * pos.close_factor_bps) // _BPS_DENOM
            max_repay_by_collateral = (
                pos.collateral_amount_raw * pos.oracle_price_collateral_in_debt_wad * _BPS_DENOM
            ) // ((_BPS_DENOM + pos.liquidation_bonus_bps) * _WAD)
            effective_max_repay = min(max_repay_by_debt, max_repay_by_collateral)
            if active_config.max_repay_debt_raw > 0:
                effective_max_repay = min(effective_max_repay, active_config.max_repay_debt_raw)
            if effective_max_repay <= 0:
                continue

            pair = {pos.collateral_token, pos.debt_token}
            matching_pools = [
                pool
                for pool in all_pools
                if {pool.token0.strip().lower(), pool.token1.strip().lower()} == pair
            ]
            if not matching_pools:
                continue

            pos_key = (pos.protocol, pos.market_id, pos.borrower)
            for pct in _REPAY_FRACTIONS_PCT:
                repay_debt_raw = (effective_max_repay * pct) // 100
                if repay_debt_raw <= 0:
                    continue
                seized_collateral_raw = min(
                    pos.collateral_amount_raw,
                    (repay_debt_raw * (_BPS_DENOM + pos.liquidation_bonus_bps) * _WAD)
                    // (_BPS_DENOM * pos.oracle_price_collateral_in_debt_wad),
                )
                if seized_collateral_raw <= 0:
                    continue

                flash_fee_debt_raw = (
                    repay_debt_raw * active_config.flash_fee_bps + _BPS_DENOM - 1
                ) // _BPS_DENOM

                for pool in matching_pools:
                    try:
                        quote = pool_graph.quote_pool(
                            pool.chain_id,
                            pool.pool_id,
                            pos.collateral_token,
                            seized_collateral_raw,
                        )
                    except FastpathCallError:
                        continue

                    swap_out_debt_raw = quote.amount_out
                    net_profit_debt_raw = (
                        swap_out_debt_raw
                        - repay_debt_raw
                        - flash_fee_debt_raw
                        - active_config.gas_cost_in_debt_raw
                    )
                    if net_profit_debt_raw < active_config.min_net_profit_debt_raw:
                        continue

                    candidate = LiquidationCandidate(
                        protocol=pos.protocol,
                        market_id=pos.market_id,
                        borrower=pos.borrower,
                        debt_token=pos.debt_token,
                        collateral_token=pos.collateral_token,
                        repay_debt_raw=repay_debt_raw,
                        seized_collateral_raw=seized_collateral_raw,
                        swap_pool_id=pool.pool_id.strip().lower(),
                        swap_out_debt_raw=swap_out_debt_raw,
                        flash_fee_debt_raw=flash_fee_debt_raw,
                        gas_cost_in_debt_raw=active_config.gas_cost_in_debt_raw,
                        net_profit_debt_raw=net_profit_debt_raw,
                        health_factor_wad=pos.health_factor_wad,
                        block_number=pos.block_number,
                        block_hash=pos.block_hash,
                        execution_eligible=False,
                    )
                    if _is_candidate_better(candidate, best_by_position.get(pos_key)):
                        best_by_position[pos_key] = candidate

        return sorted(
            best_by_position.values(),
            key=lambda c: (
                -c.net_profit_debt_raw,
                c.protocol,
                c.market_id,
                c.borrower,
                c.swap_pool_id,
            ),
        )
