"""Frozen read-only Swap experiment design and evidence interpretation."""

from dataclasses import dataclass

from eth_hash.auto import keccak

from ..evidence.public_receipts import decode_receipt, quantity, receipt_header
from ..evidence.rpc_validation import strict_list, strict_object
from ..models.chain import BlockSnapshot, LogRecord
from ..models.common import ZERO_ADDRESS, BlockRef
from ..models.v4 import PoolKey
from ..uniswap_v4.events import SWAP_TOPIC, decode_swap
from ..uniswap_v4.liquidity_math import MAX_SQRT_PRICE, MIN_SQRT_PRICE
from ..uniswap_v4.pool_id import pool_id_from_key
from .cycle_case import V3_SWAP
from .cycle_evidence import header, log_record
from .cycle_plan import Pool, hash32
from .cycle_protocols import abi, calldata, data_bytes, quote_request, quote_result

WINDOW_SECONDS = 90
POLL_SECONDS = (5, 30, 55, 80)
DEADLINE_NS = 750_000_000
BASE_AMOUNT = 10**16
GAS_LIMIT_MODEL = 500_000
GAS_TIP_MODEL = 10**6
BYTES_MODEL = 600
MAX_RANGE = 128
MAX_WINDOW_BLOCKS = 1024
ORACLE = "0x420000000000000000000000000000000000000f"
WETH = "0x4200000000000000000000000000000000000006"
USDC = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
FACTORY = "0x33128a8fc17869897dce68ed026d694621f6fdfd"
V3 = Pool(
    name="base-weth-usdc-500",
    protocol="v3",
    address="0xd0b53d9277642d899df5c87a3966a349a798f224",
    token0=WETH,
    token1=USDC,
    runtime_hash="0xcd06f61c6db6a1d8317548aaaa0aa83254624aec741534c51815810e977587ae",
    factory=FACTORY,
    factory_hash="0x95707a4ac71f20181a63ef7d180e3c625be5d20fc8f6f980befa966bad568132",
    quoter="0x3d4e44eb1374240ce5f1b871ab261cd16335b76a",
    fee=500,
)
V4_KEY = PoolKey(ZERO_ADDRESS, USDC, 500, 10, ZERO_ADDRESS)
V4 = Pool(
    name="base-eth-usdc-500",
    protocol="v4",
    address="0x498581ff718922c3f8e6a244956af099b2652b2b",
    token0=ZERO_ADDRESS,
    token1=USDC,
    runtime_hash="0x83b2af6e9f3158defc2811cbcb0db71ecf8b2ba2abea39c39e370ac5c6f43eb6",
    quoter="0x0d5e0f971ed27fbff6c2837bf31316121532048d",
    quoter_hash="0x9a5c0cdd56325bef0e48cdab071a4b6a7f877e1271c2e08510998d724a038bb3",
    fee=500,
    tick_spacing=10,
    hooks=ZERO_ADDRESS,
    pool_id=pool_id_from_key(V4_KEY),
)
BASE_LEGS = ((V4, ZERO_ADDRESS), (V3, USDC))
ELYSIUM_POOL_ID = "0xf9b8a9101438145d62c9f03156b203c6c573931167e2d36f9061a32575706c8a"
KNTQ = "0xe45a355d9e9cad2ad85f2d03e3ee3102a036acf0"
ELYSIUM_USDC = "0xb47c4c8f73f009cc0c0d3f92b388d13e293f0b65"
WHYPE = "0xba876237b0f021a6c6d852d6c26e107997cc5e81"
AERO = "0x1ddfec5058180c207f55bb77d95acdd0d8227868"
AERO_REGISTRY = "0xdd1705e23762f4d6e44d8c31bd8ea80c54612239"
AERO_FACTORY = "0xefd7a089d3057f05189b05f74ffcf30610c42891"
ELYSIUM_V4 = Pool(
    name="elysium-hype-kntq-v4",
    protocol="v4",
    address="0xe2e105d3f7209a9da11f83ccf3e7398a753823f1",
    token0=ZERO_ADDRESS,
    token1=KNTQ,
    runtime_hash="0xbb5fb2091cfd38e71f56d14a2b6b5e71a22ac2b8e5881faa422ccf92b40df7a1",
    quoter="0x379f556c5a748bd7cc9baf0f7a344094ac1814f5",
    quoter_hash="0x469aa0c619f9e32d2e75aaf4e6d86c2d3ddeb43349936a912b4e0bd49fef7e9f",
    fee=3000,
    tick_spacing=60,
    hooks=ZERO_ADDRESS,
    pool_id=ELYSIUM_POOL_ID,
)
AERO_CL = "0x4d5cca6bf05af4fd6c59714b2f7caa6dbfeeca79"
ELYSIUM_VOL = "0x1b4f02f7881958c9a02d3e1906dcec56005885fd"
ELYSIUM_VOL_FACTORY = "0x8b3f6eb2c9bfbee74cfc5372eb88a6353ea0a887"
ELYSIUM_CODE = {
    ELYSIUM_V4.address: ELYSIUM_V4.runtime_hash,
    ELYSIUM_V4.quoter: ELYSIUM_V4.quoter_hash,
    AERO: "0xd9510141cf24d31ae70e1c9426de78ca6afb53cf2331888c5a6404d8ec72287a",
    AERO_REGISTRY: "0xbd051312cb39406292f6cbd74764c3eebc89cd9bb547963f32ec32739dd5b264",
    AERO_CL: "0x55e3735acd886d76160bfd959db31dd476e73e46e8a0d70ac36e4c53ef5a2a45",
    AERO_FACTORY: "0x7de82d9f6768b404a4f63916578f5c4f37a80ffc135f62633128012f760df2a0",
    ELYSIUM_VOL: "0x4f9e134070d24a8c45871c1b2a55b2de7d8483313e18ed581bae3330e86cb5c8",
    ELYSIUM_VOL_FACTORY: "0xb8ef37490937dbb9790ca7193353a848877281d82fa21fe02e7e52a8d09c32da",
}
ELYSIUM_GETTERS = (
    (ELYSIUM_V4.quoter, "poolManager()", "address", ELYSIUM_V4.address),
    (AERO, "factoryRegistry()", "address", AERO_REGISTRY),
    (AERO_CL, "factory()", "address", AERO_FACTORY),
    (AERO_CL, "token0()", "address", ELYSIUM_USDC),
    (AERO_CL, "token1()", "address", KNTQ),
    (ELYSIUM_VOL, "factory()", "address", ELYSIUM_VOL_FACTORY),
    (ELYSIUM_VOL, "token0()", "address", ELYSIUM_USDC),
    (ELYSIUM_VOL, "token1()", "address", WHYPE),
    (ELYSIUM_VOL, "stable()", "bool", False),
    (KNTQ, "decimals()", "uint8", 18),
    (ELYSIUM_USDC, "decimals()", "uint8", 6),
    (WHYPE, "decimals()", "uint8", 18),
)


@dataclass(frozen=True, slots=True)
class Chain:
    chain_id: int
    primary: str
    witness: str
    address: str
    topics: tuple[str, ...]
    quote_enabled: bool

    @property
    def log_filter(self) -> dict[str, object]:
        return {"address": self.address, "topics": list(self.topics)}


CHAINS = {
    "base": Chain(
        8453, "base-rpc.publicnode.com", "mainnet.base.org", V3.address, (V3_SWAP,), True
    ),
    "elysium": Chain(
        99901,
        "elysium-rpc.publicnode.com",
        "rpc.elysium.kinetiq.xyz",
        ELYSIUM_V4.address,
        (SWAP_TOPIC, ELYSIUM_POOL_ID),
        False,
    ),
    "elysium-live": Chain(
        99901,
        "elysium-rpc.publicnode.com",
        "rpc.elysium.kinetiq.xyz",
        ELYSIUM_V4.address,
        (SWAP_TOPIC, ELYSIUM_POOL_ID),
        True,
    ),
}


def route_request(chain: Chain, leg: int, amount: int) -> tuple[str, str]:
    if chain.chain_id == 8453:
        pool, token = BASE_LEGS[leg]
        return quote_request(pool, token, amount, None)
    if leg == 0:
        return quote_request(ELYSIUM_V4, ZERO_ADDRESS, amount, None)
    if leg == 1:
        return AERO, calldata(
            "quoteExactInputSingleV3(address,address,uint256,uint160)",
            ("address", "address", "uint256", "uint160"),
            (AERO_CL, KNTQ, amount, 0),
        )
    return ELYSIUM_VOL, calldata(
        "getAmountOut(uint256,address)", ("uint256", "address"), (amount, ELYSIUM_USDC)
    )


def route_result(chain: Chain, leg: int, raw: bytes) -> tuple[int, int | None]:
    if chain.chain_id == 8453:
        pool, token = BASE_LEGS[leg]
        return quote_result(pool, token, raw, None)
    if leg == 0:
        return quote_result(ELYSIUM_V4, ZERO_ADDRESS, raw, None)
    if leg == 1:
        amount, sqrt, _ = abi(("uint256", "uint160", "uint32"), raw)
        if not MIN_SQRT_PRICE + 1 < sqrt < MAX_SQRT_PRICE - 1:
            raise ValueError("aero_partial_input_possible")
    else:
        (amount,) = abi(("uint256",), raw)
    if not 0 < amount < 1 << 255:
        raise ValueError("elysium_quote_amount_outside_bounds")
    return amount, None


def snap(value: object, chain: Chain) -> BlockSnapshot:
    block = strict_object(value, "block")
    return header(block, chain.chain_id, quantity(block.get("number"), 64))


def swap(value: object, chain: Chain, *, stream: bool) -> LogRecord:
    obj = strict_object(value, "Swap log")
    if stream and obj.get("removed") is not False:
        raise ValueError("stream_log_removed_or_missing_flag")
    snap_ref = BlockRef(
        chain.chain_id, quantity(obj.get("blockNumber"), 64), hash32(obj.get("blockHash"))
    )
    log = log_record(
        obj, snap_ref, hash32(obj.get("transactionHash")), quantity(obj.get("transactionIndex"), 32)
    )
    if log.address != chain.address or log.topics[: len(chain.topics)] != chain.topics:
        raise ValueError("unexpected_swap_filter")
    if quantity(obj.get("logIndex"), 32) != log.log_index:
        raise ValueError("unexpected_log_position")
    if chain.topics[0] == V3_SWAP:
        if len(log.topics) != 3:
            raise ValueError("unexpected_v3_swap_topics")
        for word in log.topics[1:]:
            abi(("address",), data_bytes(word))
        abi(("int256", "int256", "uint160", "uint128", "int24"), data_bytes(log.data))
    else:
        decode_swap(log)
    return log


def receipt_match(chain: Chain, log: LogRecord, block: object, raw_receipt: object) -> None:
    inclusion = receipt_header(block, chain.chain_id, log.block_number, (log.transaction_hash,))
    if inclusion.ref.block_hash != log.block_hash:
        raise ValueError("receipt_block_reorg")
    index = dict(inclusion.receipt_positions)[log.transaction_hash]
    receipt = decode_receipt(raw_receipt, inclusion.ref, log.transaction_hash, index)
    if receipt.status != 1 or receipt.logs.count(log) != 1:
        raise ValueError("swap_not_in_successful_receipt")


def runtime_hash(value: object) -> str:
    raw = data_bytes(value)
    if not raw:
        raise ValueError("contract_code_missing")
    return "0x" + keccak(raw).hex()


def signaled_net(output: int, base_fee: int, l1_fee: int) -> int:
    return output - BASE_AMOUNT - GAS_LIMIT_MODEL * (base_fee + GAS_TIP_MODEL) - l1_fee


def covered_logs(
    values: object, chain: Chain, start: int, end: int
) -> dict[tuple[int, int], LogRecord]:
    logs: dict[tuple[int, int], LogRecord] = {}
    for value in strict_list(values, "backfilled logs"):
        log = swap(value, chain, stream=False)
        key = (log.block_number, log.log_index)
        if not start <= log.block_number <= end or key in logs:
            raise ValueError("backfill_position_conflict")
        logs[key] = log
    return logs


def same_opportunity(event: dict[str, int], poll: dict[str, int]) -> bool:
    return (
        event["block"] <= poll["block"]
        and 0 <= poll["dispatch_ns"] - event["start_ns"] <= DEADLINE_NS
    )
