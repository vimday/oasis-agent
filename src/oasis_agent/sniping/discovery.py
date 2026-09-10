"""Resumable, block-pinned pool and liquidity-event discovery.

Confirmed-log polling works over both configured HTTPS providers. It is not a
pending-mempool subscriber. An event schedules a fresh quote, never a purchase.
"""

from dataclasses import asdict, dataclass
from typing import Any

from eth_abi.abi import decode
from eth_hash.auto import keccak

from ..models.common import normalize_address, normalize_hash32
from .journal import ExecutionJournal
from .rpc import JsonRpc, ProviderDisagreement
from .venues import AERO_FACTORY, SLIPSTREAM, V4_MANAGER, VenuePool, call_data


def topic(signature: str) -> str:
    return "0x" + keccak(signature.encode()).hex()


AERO_CREATED = topic("PoolCreated(address,address,bool,address,uint256)")
V3_CREATED = topic("PoolCreated(address,address,uint24,int24,address)")
SLIP_CREATED = topic("PoolCreated(address,address,int24,address)")
V4_INITIALIZED = topic("Initialize(bytes32,address,address,uint24,int24,address,uint160,int24)")
V4_MODIFIED = topic("ModifyLiquidity(bytes32,address,int24,int24,int256,bytes32)")
CL_MINT = topic("Mint(address,address,int24,int24,uint128,uint256,uint256)")
CL_BURN = topic("Burn(address,int24,int24,uint128,uint256,uint256)")
AERO_MINT = topic("Mint(address,uint256,uint256)")
AERO_SYNC = topic("Sync(uint256,uint256)")
TRANSFER = topic("Transfer(address,address,uint256)")


def word_address(value: str) -> str:
    raw = bytes.fromhex(value[2:])
    if len(raw) != 32:
        raise ValueError("indexed address requires one word")
    return str(decode(["address"], raw, strict=True)[0])


def target_topic(address: str) -> str:
    return "0x" + "0" * 24 + normalize_address(address)[2:]


def decode_created(log: dict[str, Any], target: str) -> VenuePool:
    address = normalize_address(log["address"])
    topics = log["topics"]
    raw = bytes.fromhex(log["data"][2:])
    if len(topics) != 4 or log.get("removed", False):
        raise ValueError("invalid or removed creation event")
    if address == V4_MANAGER and topics[0] == V4_INITIALIZED:
        currency0, currency1 = word_address(topics[2]), word_address(topics[3])
        if len(raw) != 160:
            raise ValueError("v4 Initialize needs five data words")
        fee, spacing, hooks, _, _ = decode(
            ["uint24", "int24", "address", "uint160", "int24"], raw, strict=True
        )
        pool = VenuePool(
            "uniswap_v4",
            address,
            topics[1],
            currency0,
            currency1,
            fee=fee,
            tick_spacing=spacing,
            hooks=hooks,
        )
    else:
        if not (
            (address == AERO_FACTORY and topics[0] == AERO_CREATED)
            or (address in SLIPSTREAM and topics[0] == SLIP_CREATED)
        ):
            raise ValueError("unapproved factory/event combination")
        currency0, currency1 = word_address(topics[1]), word_address(topics[2])
        if address == AERO_FACTORY and topics[0] == AERO_CREATED:
            if len(raw) != 64:
                raise ValueError("Aerodrome creation data size")
            stable = decode(["bool"], bytes.fromhex(topics[3][2:]), strict=True)[0]
            pool_address, _ = decode(["address", "uint256"], raw, strict=True)
            pool = VenuePool(
                "aerodrome", address, pool_address, currency0, currency1, stable=stable
            )
        elif address in SLIPSTREAM and topics[0] == SLIP_CREATED:
            if len(raw) != 32:
                raise ValueError("Slipstream creation data size")
            spacing = decode(["int24"], bytes.fromhex(topics[3][2:]), strict=True)[0]
            pool_address = decode(["address"], raw, strict=True)[0]
            pool = VenuePool(
                "slipstream", address, pool_address, currency0, currency1, tick_spacing=spacing
            )
        else:
            raise ValueError("unapproved factory/event combination")
    if normalize_address(target) not in (pool.currency0, pool.currency1):
        raise ValueError("unrelated target")
    return pool


@dataclass(frozen=True)
class LiquiditySignal:
    pool: str
    kind: str
    block_number: int
    block_hash: str
    transaction_hash: str
    log_index: int


def decode_liquidity(
    log: dict[str, Any], pools: dict[str, VenuePool], target: str, v3_pools: set[str]
) -> LiquiditySignal:
    if log.get("removed", False):
        raise ValueError("removed liquidity event")
    emitter = normalize_address(log["address"])
    topics = log["topics"]
    data = bytes.fromhex(log["data"][2:])
    kind = ""
    pool_id = emitter
    if emitter == V4_MANAGER and len(topics) == 3 and topics[0] == V4_MODIFIED:
        pool_id = topics[1].lower()
        if pool_id not in pools or pools[pool_id].venue != "uniswap_v4":
            raise ValueError("unknown v4 PoolId")
        if len(data) != 128:
            raise ValueError("v4 liquidity event data size")
        lower, upper, delta, _ = decode(["int24", "int24", "int256", "bytes32"], data)
        if lower >= upper:
            raise ValueError("invalid liquidity tick range")
        kind = (
            "liquidity_added"
            if delta > 0
            else "liquidity_removed"
            if delta < 0
            else "position_update"
        )
    elif emitter == normalize_address(target) and len(topics) == 3 and topics[0] == TRANSFER:
        pool_id = word_address(topics[2])
        if pool_id not in pools and pool_id not in v3_pools:
            raise ValueError("transfer recipient is not a tracked pool")
        if len(data) != 32 or decode(["uint256"], data)[0] == 0:
            raise ValueError("empty transfer")
        kind = "inventory_received"
    elif emitter in pools or emitter in v3_pools:
        venue = pools[emitter].venue if emitter in pools else "uniswap_v3"
        if venue == "aerodrome" and topics[0] == AERO_SYNC and len(topics) == 1 and len(data) == 64:
            decode(["uint256", "uint256"], data)
            kind = "reserves_changed"
        elif (
            venue == "aerodrome" and topics[0] == AERO_MINT and len(topics) == 2 and len(data) == 64
        ):
            decode(["uint256", "uint256"], data)
            kind = "liquidity_added"
        elif (
            venue in ("slipstream", "uniswap_v3")
            and topics[0] == CL_MINT
            and len(topics) == 4
            and len(data) == 128
        ):
            decode(["address", "uint128", "uint256", "uint256"], data)
            kind = "liquidity_added"
        elif (
            venue in ("slipstream", "uniswap_v3")
            and topics[0] == CL_BURN
            and len(topics) == 4
            and len(data) == 96
        ):
            decode(["uint128", "uint256", "uint256"], data)
            kind = "liquidity_removed"
    if not kind:
        raise ValueError("unexpected liquidity event")
    return LiquiditySignal(
        pool_id,
        kind,
        int(log["blockNumber"], 16),
        normalize_hash32(log["blockHash"], "block hash"),
        normalize_hash32(log["transactionHash"], "transaction hash"),
        int(log["logIndex"], 16),
    )


class VenueScanner:
    """Cursor advances only after both sources and all filters complete.

    Every checkpoint retains the complete discovered registry. On reorg the
    operator must rebuild from the declared start block; stale state cannot be
    silently reused. Start block is explicit so partial history is not hidden.
    """

    def __init__(
        self,
        rpc: JsonRpc,
        target: str,
        start_block: int,
        journal: ExecutionJournal,
        chunk_size: int = 1000,
    ):
        if type(start_block) is not int or start_block < 0 or not 1 <= chunk_size <= 2000:
            raise ValueError("valid start block and chunk size required")
        self.rpc = rpc
        self.target = normalize_address(target)
        self.start_block = start_block
        self.cursor = start_block - 1
        self.cursor_hash: str | None = None
        self.chunk_size = chunk_size
        self.journal = journal
        self.pools: dict[str, VenuePool] = {}
        for record in journal.read():
            if record["kind"] == "venue_checkpoint":
                if record["target"] != self.target or record["start_block"] != start_block:
                    raise ValueError("venue discovery policy changed")
                self.pools = {p["pool"]: VenuePool(**p) for p in record["pools"]}
                self.cursor = record["block"]
                self.cursor_hash = record["block_hash"]

    def _logs(self, query: dict[str, Any], start: int, end: int) -> list[dict[str, Any]]:
        value = self.rpc.dual(
            "eth_getLogs", [query | {"fromBlock": hex(start), "toBlock": hex(end)}]
        )

        def canonical(items: Any) -> list[dict[str, Any]]:
            if not isinstance(items, list):
                raise ValueError("log response must be a list")
            output = []
            seen = set()
            for item in items:
                required = (
                    "address",
                    "topics",
                    "data",
                    "blockNumber",
                    "blockHash",
                    "transactionHash",
                    "logIndex",
                )
                clean = {key: item[key] for key in required}
                if item.get("removed", False) or not start <= int(clean["blockNumber"], 16) <= end:
                    raise ValueError("removed or out-of-range log")
                identity = (clean["blockHash"], clean["transactionHash"], clean["logIndex"])
                if identity in seen:
                    raise ValueError("duplicate log")
                seen.add(identity)
                output.append(clean)
            return sorted(output, key=lambda x: (int(x["blockNumber"], 16), int(x["logIndex"], 16)))

        left, right = canonical(value.primary), canonical(value.secondary)
        if left != right:
            raise ProviderDisagreement("venue log disagreement")
        return left

    def bootstrap(self, head: int) -> None:
        """Seed old pools from sparse creation logs, validating each via two receipts.

        Public RPC history-range limits need not force millions of empty-block
        scans. Primary logs are discovery hints only; both providers must verify
        every retained creation receipt and its canonical block. Absence is not
        asserted by two independent full-history searches.
        """
        if self.cursor >= self.start_block:
            return
        if head < self.start_block:
            raise ValueError("discovery start is beyond head")
        header = self.rpc.dual("eth_getBlockByNumber", [hex(head), False]).require_agreement(
            "bootstrap head"
        )
        address_topic = target_topic(self.target)
        queries: list[dict[str, Any]] = []
        for position in (1, 2):
            topics: list[Any] = [[AERO_CREATED, SLIP_CREATED], None, None]
            topics[position] = address_topic
            queries.append({"address": [AERO_FACTORY, *SLIPSTREAM], "topics": topics})
        for position in (2, 3):
            topics = [V4_INITIALIZED, None, None, None]
            topics[position] = address_topic
            queries.append({"address": V4_MANAGER, "topics": topics})
        pools: dict[str, VenuePool] = {}
        verified_events = []
        fields = (
            "address",
            "topics",
            "data",
            "blockNumber",
            "blockHash",
            "transactionHash",
            "logIndex",
        )
        for query in queries:
            logs = self.rpc.get_logs(
                query | {"fromBlock": hex(self.start_block), "toBlock": hex(head)}
            )
            for event in logs:
                number = int(event["blockNumber"], 16)
                if not self.start_block <= number <= head or event.get("removed", False):
                    raise ValueError("invalid bootstrap log")
                candidate = decode_created(event, self.target)
                receipts = self.rpc.dual("eth_getTransactionReceipt", [event["transactionHash"]])
                for receipt in (receipts.primary, receipts.secondary):
                    if (
                        not isinstance(receipt, dict)
                        or receipt.get("status") != "0x1"
                        or receipt.get("transactionHash") != event["transactionHash"]
                        or receipt.get("blockHash") != event["blockHash"]
                    ):
                        raise ProviderDisagreement("bootstrap receipt identity mismatch")
                    match = [
                        {k: log[k] for k in fields}
                        for log in receipt["logs"]
                        if log["logIndex"] == event["logIndex"] and not log.get("removed", False)
                    ]
                    if match != [{k: event[k] for k in fields}]:
                        raise ProviderDisagreement("bootstrap log missing from receipt")
                canonical = self.rpc.dual(
                    "eth_getBlockByNumber", [hex(number), False]
                ).require_agreement("creation block")
                if canonical["hash"] != event["blockHash"]:
                    raise ProviderDisagreement("creation event no longer canonical")
                pools[candidate.pool] = candidate
                verified_events.append({k: event[k] for k in fields})
        recheck = self.rpc.dual("eth_getBlockByNumber", [hex(head), False]).require_agreement(
            "bootstrap recheck"
        )
        if header != recheck:
            raise ProviderDisagreement("bootstrap head reorg")
        self.journal.append(
            "venue_checkpoint",
            {
                "target": self.target,
                "start_block": self.start_block,
                "block": head,
                "block_hash": header["hash"],
                "pools": [asdict(p) for p in pools.values()],
                "signals": [],
                "creation_events": verified_events,
                "history_discovery_source": "primary",
                "creation_verification_sources": ["primary", "secondary"],
                "execution_eligible": False,
            },
        )
        self.pools = pools
        self.cursor = head
        self.cursor_hash = header["hash"]

    def poll(self, head: int, v3_pools: set[str] | None = None) -> list[LiquiditySignal]:
        v3_pools = {normalize_address(p) for p in (v3_pools or set())}
        if head < self.cursor:
            raise ProviderDisagreement("discovery head moved backwards")
        if self.cursor_hash is not None:
            block = self.rpc.dual(
                "eth_getBlockByNumber", [hex(self.cursor), False]
            ).require_agreement("cursor block")
            if block is None or block["hash"] != self.cursor_hash:
                raise ProviderDisagreement("discovery reorg: rebuild required")
        if head == self.cursor:
            return []
        end = min(head, self.cursor + self.chunk_size)
        start = self.cursor + 1
        anchor = self.rpc.dual("eth_getBlockByNumber", [hex(end), False]).require_agreement(
            "scan end"
        )
        if not isinstance(anchor, dict) or int(anchor["number"], 16) != end:
            raise ProviderDisagreement("invalid scan anchor")
        address_topic = target_topic(self.target)
        creates = []
        for position in (1, 2):
            topics: list[Any] = [[AERO_CREATED, SLIP_CREATED], None, None]
            topics[position] = address_topic
            creates.extend(
                self._logs({"address": [AERO_FACTORY, *SLIPSTREAM], "topics": topics}, start, end)
            )
        for position in (2, 3):
            topics = [V4_INITIALIZED, None, None, None]
            topics[position] = address_topic
            creates.extend(self._logs({"address": V4_MANAGER, "topics": topics}, start, end))
        pending = dict(self.pools)
        signals = []
        for log in creates:
            pool = decode_created(log, self.target)
            pending[pool.pool] = pool
            signals.append(
                LiquiditySignal(
                    pool.pool,
                    "created",
                    int(log["blockNumber"], 16),
                    log["blockHash"],
                    log["transactionHash"],
                    int(log["logIndex"], 16),
                )
            )
        addresses = sorted({p.pool for p in pending.values() if p.venue != "uniswap_v4"} | v3_pools)
        event_logs = []
        if addresses:
            event_logs.extend(
                self._logs(
                    {"address": addresses, "topics": [[CL_MINT, CL_BURN, AERO_MINT, AERO_SYNC]]},
                    start,
                    end,
                )
            )
            event_logs.extend(
                self._logs(
                    {
                        "address": self.target,
                        "topics": [TRANSFER, None, [target_topic(a) for a in addresses]],
                    },
                    start,
                    end,
                )
            )
        ids = [p.pool for p in pending.values() if p.venue == "uniswap_v4"]
        if ids:
            event_logs.extend(
                self._logs({"address": V4_MANAGER, "topics": [V4_MODIFIED, ids]}, start, end)
            )
        signals.extend(decode_liquidity(log, pending, self.target, v3_pools) for log in event_logs)
        verify = self.rpc.dual("eth_getBlockByNumber", [hex(end), False]).require_agreement(
            "scan end recheck"
        )
        if verify != anchor:
            raise ProviderDisagreement("reorg during event scan")
        self.journal.append(
            "venue_checkpoint",
            {
                "target": self.target,
                "start_block": self.start_block,
                "block": end,
                "block_hash": anchor["hash"],
                "pools": [asdict(p) for p in pending.values()],
                "signals": [asdict(s) for s in signals],
                "execution_eligible": False,
            },
        )
        self.pools = pending
        self.cursor = end
        self.cursor_hash = anchor["hash"]
        return signals

    def verify_pool(self, pool: VenuePool) -> None:
        """Verify the discovered route still binds to its factory at decision state."""
        if pool.venue == "uniswap_v4":
            # PoolId/PoolKey binding was verified at construction; initialization
            # and liquidity are checked by StateView and by the executable quote.
            return
        if pool.venue == "aerodrome":
            data = call_data(
                "getPool(address,address,bool)",
                ["address", "address", "bool"],
                [pool.currency0, pool.currency1, pool.stable],
            )
        else:
            data = call_data(
                "getPool(address,address,int24)",
                ["address", "address", "int24"],
                [pool.currency0, pool.currency1, pool.tick_spacing],
            )
        raw = self.rpc.dual_eth_call(pool.factory, data).require_agreement("pool factory binding")
        if decode(["address"], bytes.fromhex(raw[2:]))[0] != pool.pool:
            raise ProviderDisagreement("pool factory binding changed")
