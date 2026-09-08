from __future__ import annotations

from eth_hash.auto import keccak

from ..models.chain import LogRecord
from ..models.v4 import ModifyLiquidityObservation, PoolInitialized, PoolKey, SwapObservation

INITIALIZE_EVENT_SIGNATURE = (
    "Initialize(bytes32,address,address,uint24,int24,address,uint160,int24)"
)
SWAP_EVENT_SIGNATURE = "Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)"
MODIFY_LIQUIDITY_EVENT_SIGNATURE = "ModifyLiquidity(bytes32,address,int24,int24,int256,bytes32)"
MODIFY_POSITION_EVENT_SIGNATURE = "ModifyPosition(bytes32,address,int24,int24,int256,bytes32)"
POOL_INITIALIZED_TOPIC = "0x" + keccak(INITIALIZE_EVENT_SIGNATURE.encode()).hex()
SWAP_TOPIC = "0x" + keccak(SWAP_EVENT_SIGNATURE.encode()).hex()
MODIFY_LIQUIDITY_TOPIC = "0x" + keccak(MODIFY_LIQUIDITY_EVENT_SIGNATURE.encode()).hex()
MODIFY_POSITION_TOPIC = "0x" + keccak(MODIFY_POSITION_EVENT_SIGNATURE.encode()).hex()


def _words(data: str) -> list[int]:
    raw = bytes.fromhex(data[2:])
    if len(raw) % 32:
        raise ValueError("event data is not word aligned")
    return [int.from_bytes(raw[i : i + 32], "big") for i in range(0, len(raw), 32)]


def _signed(value: int, bits: int) -> int:
    if not 0 <= value < 1 << 256:
        raise ValueError("ABI word must fit uint256")
    mask = (1 << bits) - 1
    sign = 1 << (bits - 1)
    lower = value & mask
    upper = value >> bits
    expected_upper = (1 << (256 - bits)) - 1 if lower & sign else 0
    if upper != expected_upper:
        raise ValueError("signed ABI word is not canonically sign-extended")
    return lower - (1 << bits) if lower & sign else lower


def _topic_address(topic: str) -> str:
    raw = bytes.fromhex(topic[2:])
    if len(raw) != 32 or raw[:12] != b"\x00" * 12:
        raise ValueError("indexed address is not ABI padded")
    return "0x" + raw[-20:].hex()


def _topic_id(topic: str) -> str:
    if len(bytes.fromhex(topic[2:])) != 32:
        raise ValueError("indexed pool id must be bytes32")
    return topic


def decode_pool_initialized(log: LogRecord) -> PoolInitialized:
    if len(log.topics) != 4 or log.topics[0] != POOL_INITIALIZED_TOPIC:
        raise ValueError("invalid Initialize event topics")
    words = _words(log.data)
    if len(words) != 5:
        raise ValueError("Initialize requires five data words")
    fee, tick_spacing, hooks, sqrt_price, tick = words
    if fee > 0xFFFFFF or hooks > (1 << 160) - 1 or sqrt_price > (1 << 160) - 1:
        raise ValueError("Initialize contains an out-of-range field")
    key = PoolKey(
        _topic_address(log.topics[2]),
        _topic_address(log.topics[3]),
        fee,
        _signed(tick_spacing, 24),
        # hooks was range-checked to 160 bits, so the high 12 bytes are zero
        # and a fixed-width 20-byte rendering cannot drop or invent bytes.
        "0x" + f"{hooks:040x}",
    )
    return PoolInitialized(
        _topic_id(log.topics[1]), key, sqrt_price, _signed(tick, 24), log.block_number
    )


def decode_swap(log: LogRecord) -> SwapObservation:
    if len(log.topics) != 3 or log.topics[0] != SWAP_TOPIC:
        raise ValueError("invalid Swap event topics")
    words = _words(log.data)
    if len(words) != 6:
        raise ValueError("Swap requires six data words")
    amount0 = _signed(words[0], 128)
    amount1 = _signed(words[1], 128)
    sqrt_price = words[2]
    liquidity = words[3]
    tick = _signed(words[4], 24)
    fee = words[5]
    if sqrt_price > (1 << 160) - 1 or liquidity > (1 << 128) - 1 or fee > 0xFFFFFF:
        raise ValueError("Swap contains an out-of-range field")
    return SwapObservation(
        _topic_id(log.topics[1]),
        _topic_address(log.topics[2]),
        amount0,
        amount1,
        sqrt_price,
        liquidity,
        tick,
        fee,
        log.block_number,
    )


def _decode_position_modification(
    log: LogRecord, expected_topic: str, event_name: str
) -> ModifyLiquidityObservation:
    if len(log.topics) != 3 or log.topics[0] != expected_topic:
        raise ValueError(f"invalid {event_name} event topics")
    words = _words(log.data)
    if len(words) != 4:
        raise ValueError(f"{event_name} requires four data words")
    tick_lower, tick_upper, liquidity_delta, salt = words
    lower = _signed(tick_lower, 24)
    upper = _signed(tick_upper, 24)
    if lower >= upper:
        raise ValueError("ModifyLiquidity tick range is invalid")
    delta = _signed(liquidity_delta, 256)
    return ModifyLiquidityObservation(
        _topic_id(log.topics[1]),
        _topic_address(log.topics[2]),
        lower,
        upper,
        delta,
        "0x" + salt.to_bytes(32, "big").hex(),
        log.block_number,
    )


def decode_modify_liquidity(log: LogRecord) -> ModifyLiquidityObservation:
    return _decode_position_modification(log, MODIFY_LIQUIDITY_TOPIC, "ModifyLiquidity")


def decode_modify_position(log: LogRecord) -> ModifyLiquidityObservation:
    return _decode_position_modification(log, MODIFY_POSITION_TOPIC, "ModifyPosition")
