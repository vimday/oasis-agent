"""Separate receipt Swap counts from evidenced funding depth in historical transactions."""

from collections import Counter, defaultdict, deque
from collections.abc import Iterable
from dataclasses import dataclass

from ..arbitrage.cycle_plan import address, hash32
from ..arbitrage.cycle_protocols import data_bytes, topic
from .public_receipts import ObservedReceipt

BANCOR_CONVERSION_TOPIC = topic("Conversion(address,address,address,uint256,uint256,int256)")

# These describe event shapes. Pool or protocol identity still requires independent evidence.
_LAYOUT = {
    topic("Swap(address,uint256,uint256,uint256,uint256,address)"): ("v2", 4, False),
    topic("Swap(address,address,uint256,uint256,uint256,uint256)"): ("aero", 4, False),
    topic("Swap(address,address,int256,int256,uint160,uint128,int24)"): ("v3", 5, False),
    topic("Swap(address,address,int256,int256,uint160,uint128,int24,uint128,uint128)"): (
        "pancake_v3",
        7,
        False,
    ),
    topic("Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)"): ("v4", 6, True),
    topic("Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24,uint16)"): (
        "infinity_cl",
        7,
        True,
    ),
    BANCOR_CONVERSION_TOPIC: ("bancor_conversion", 3, False),
}
TRANSFER = topic("Transfer(address,address,uint256)")
MAX_SWAP_EVENTS = 4096
MAX_FUNDING_LINKS = 16384
PoolIdentity = tuple[str, str | None]


@dataclass(frozen=True, slots=True)
class ObservedSwap:
    log_index: int
    pool: PoolIdentity
    shape: str


@dataclass(frozen=True, slots=True)
class SwapReceiptProfile:
    events: tuple[ObservedSwap, ...]
    distinct_pools: int
    distinct_emitters: int
    repeated_pool_events: int


def profile_swap_receipt(receipt: ObservedReceipt) -> SwapReceiptProfile:
    """Count compatible event shapes; manager pools retain the indexed PoolId."""
    if receipt.status != 1:
        if receipt.logs:
            raise ValueError("failed receipt contains logs")
        return SwapReceiptProfile((), 0, 0, 0)
    events: list[ObservedSwap] = []
    for log in receipt.logs:
        if not log.topics or log.topics[0] not in _LAYOUT:
            continue
        shape, words, singleton = _LAYOUT[log.topics[0]]
        topics = 4 if shape == "bancor_conversion" else 3
        if len(log.topics) != topics or len(data_bytes(log.data)) != 32 * words:
            raise ValueError("malformed recognized swap event")
        for item in log.topics[1:]:
            hash32(item)
        events.append(
            ObservedSwap(log.log_index, (log.address, log.topics[1] if singleton else None), shape)
        )
        if len(events) > MAX_SWAP_EVENTS:
            raise ValueError("swap event count exceeds budget")
    count = len({event.pool for event in events})
    if len({event.log_index for event in events}) != len(events):
        raise ValueError("duplicate swap log index")
    return SwapReceiptProfile(
        tuple(sorted(events, key=lambda event: event.log_index)),
        count,
        len({event.pool[0] for event in events}),
        len(events) - count,
    )


def _uint(value: int, description: str, *, minimum: int = 0) -> None:
    if type(value) is not int or not minimum <= value < 1 << 256:
        raise ValueError(f"invalid {description}")


@dataclass(frozen=True, slots=True)
class SwapLeg:
    """A swap whose pool and directed asset amounts were resolved by the caller."""

    event: ObservedSwap
    token_in: str
    token_out: str
    amount_in: int
    amount_out: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "token_in", address(self.token_in))
        object.__setattr__(self, "token_out", address(self.token_out))
        if self.token_in == self.token_out:
            raise ValueError("swap input and output assets match")
        _uint(self.amount_in, "input amount", minimum=1)
        _uint(self.amount_out, "output amount", minimum=1)


@dataclass(frozen=True, slots=True)
class FundingLink:
    """A transfer allocation in base units backed by a receipt or trace reference."""

    source: int
    target: int
    token: str
    amount: int
    evidence_id: str

    def __post_init__(self) -> None:
        _uint(self.source, "source swap index")
        _uint(self.target, "target swap index")
        object.__setattr__(self, "token", address(self.token))
        _uint(self.amount, "allocation amount", minimum=1)
        if not isinstance(self.evidence_id, str) or not self.evidence_id.strip():
            raise ValueError("missing funding evidence reference")


@dataclass(frozen=True, slots=True)
class SwapFundingProfile:
    swaps_with_amounts: int
    links: int
    linked_swaps: int
    branch_swaps: int
    join_swaps: int
    witnessed_depth: int | None
    longest_witnessed_path: tuple[int, ...]
    cyclic_allocations: bool


def analyze_swap_funding(
    legs: Iterable[SwapLeg], links: Iterable[FundingLink]
) -> SwapFundingProfile:
    """Return the longest evidenced serial path. Unlinked flows remain unknown.

    Edges use actual funding evidence, so a source can emit its Swap after its
    destination's Swap during callbacks. Cyclic allocations need financing or
    call-order reconciliation before assigning a serial depth.
    """
    by_id: dict[int, SwapLeg] = {}
    for leg in legs:
        idx = leg.event.log_index
        _uint(idx, "swap event index")
        if idx in by_id or len(by_id) >= MAX_SWAP_EVENTS:
            raise ValueError("duplicate or excess swap legs")
        by_id[idx] = leg
    sent: Counter[int] = Counter()
    received: Counter[int] = Counter()
    parents: dict[int, set[int]] = defaultdict(set)
    children: dict[int, set[int]] = defaultdict(set)
    proofs: set[str] = set()
    for link in links:
        src, dst = by_id.get(link.source), by_id.get(link.target)
        if (
            src is None
            or dst is None
            or link.source == link.target
            or src.token_out != link.token
            or dst.token_in != link.token
            or link.evidence_id in proofs
            or len(proofs) >= MAX_FUNDING_LINKS
        ):
            raise ValueError("invalid or duplicate swap funding allocation")
        proofs.add(link.evidence_id)
        sent[link.source] += link.amount
        received[link.target] += link.amount
        if sent[link.source] > src.amount_out or received[link.target] > dst.amount_in:
            raise ValueError("swap funding overallocated")
        parents[link.target].add(link.source)
        children[link.source].add(link.target)

    indegrees = {idx: len(parents[idx]) for idx in by_id}
    queue = deque(sorted(idx for idx, degree in indegrees.items() if degree == 0))
    depth = dict.fromkeys(by_id, 1)
    previous: dict[int, int] = {}
    processed = 0
    while queue:
        source = queue.popleft()
        processed += 1
        for destination in sorted(children[source]):
            if depth[source] + 1 > depth[destination]:
                depth[destination] = depth[source] + 1
                previous[destination] = source
            indegrees[destination] -= 1
            if indegrees[destination] == 0:
                queue.append(destination)
    cyclic = processed != len(by_id)
    path = []
    if depth and not cyclic:
        path.append(max(sorted(depth), key=depth.__getitem__))
        while path[-1] in previous:
            path.append(previous[path[-1]])
    return SwapFundingProfile(
        len(by_id),
        len(proofs),
        len(sent.keys() | received.keys()),
        sum(len(x) > 1 for x in children.values()),
        sum(len(x) > 1 for x in parents.values()),
        len(path) if not cyclic else None,
        tuple(reversed(path)),
        cyclic,
    )


def direct_pool_funding(
    receipt: ObservedReceipt, legs: Iterable[SwapLeg]
) -> tuple[FundingLink, ...]:
    """Infer only unique exact ERC-20 transfers between resolved pool contracts.

    Shared manager addresses, intermediaries, and ambiguous repeated amounts
    stay out of this conservative subset. More complete trace allocations can
    be passed explicitly to the analyzer with their evidence references.
    """
    expected = {event.log_index: event for event in profile_swap_receipt(receipt).events}
    outgoing: dict[tuple[str, str, int], list[int]] = defaultdict(list)
    incoming: dict[tuple[str, str, int], list[int]] = defaultdict(list)
    registered: set[int] = set()
    singleton_accounts = {event.pool[0] for event in expected.values() if event.pool[1] is not None}
    for leg in legs:
        idx = leg.event.log_index
        if idx in registered or expected.get(idx) != leg.event:
            raise ValueError("swap leg does not bind to receipt")
        registered.add(idx)
        account = leg.event.pool[0]
        if account not in singleton_accounts:
            outgoing[account, leg.token_out, leg.amount_out].append(idx)
            incoming[account, leg.token_in, leg.amount_in].append(idx)

    links: list[FundingLink] = []
    for log in receipt.logs:
        if len(log.topics) != 3 or log.topics[0] != TRANSFER:
            continue
        raw = data_bytes(log.data)
        if len(raw) != 32 or any(int(hash32(t)[2:26], 16) for t in log.topics[1:]):
            continue
        token = log.address
        amount = int.from_bytes(raw, "big")
        a, b = ("0x" + t[-40:] for t in log.topics[1:])
        if a == b:
            continue
        sources = outgoing.get((a, token, amount), [])
        targets = incoming.get((b, token, amount), [])
        if len(sources) == len(targets) == 1:
            links.append(FundingLink(sources[0], targets[0], token, amount, f"log:{log.log_index}"))
    sent = Counter(link.source for link in links)
    received = Counter(link.target for link in links)
    return tuple(link for link in links if sent[link.source] == received[link.target] == 1)


@dataclass(frozen=True, slots=True)
class SwapCallBundles:
    observed_frames: int
    matched_address_pool_swap_calls: int
    complete_for_address_pool_events: bool
    outer_branch_swap_counts: tuple[tuple[int, int], ...]


_ADDRESS_SWAP_SELECTOR = {
    "v2": topic("swap(uint256,uint256,address,bytes)")[:10],
    "aero": topic("swap(uint256,uint256,address,bytes)")[:10],
    "v3": topic("swap(address,bool,int256,uint160,bytes)")[:10],
    "pancake_v3": topic("swap(address,bool,int256,uint160,bytes)")[:10],
    "bancor_conversion": topic("convert(address,address,uint256,address,address)")[:10],
}


def profile_swap_call_bundles(
    receipt: ObservedReceipt, frames: Iterable[tuple[tuple[int, ...], str, str, bool]]
) -> SwapCallBundles:
    """Count successful address-pool swap CALLs by the root's immediate child.

    Inputs contain path, recipient, selector, and local failure flag. The
    caller binds the observed trace to the receipt. Singleton-manager calls
    need pool-key decoding and have separate receipt counts.
    """
    rows: dict[tuple[int, ...], tuple[str, str, bool]] = {}
    for path, recipient, selector, failed in frames:
        if (
            path in rows
            or len(rows) >= 16_384
            or len(path) > 64
            or any(type(idx) is not int or not 0 <= idx < 16_384 for idx in path)
            or type(failed) is not bool
        ):
            raise ValueError("invalid or duplicate swap trace path")
        rows[path] = address(recipient), selector, failed
    if () not in rows or any(path and path[:-1] not in rows for path in rows):
        raise ValueError("incomplete swap trace ancestry")

    profile = profile_swap_receipt(receipt)
    expected = Counter(
        (event.pool[0], _ADDRESS_SWAP_SELECTOR[event.shape])
        for event in profile.events
        if event.shape in _ADDRESS_SWAP_SELECTOR
    )
    committed: dict[tuple[int, ...], bool] = {}
    matching: Counter[tuple[str, str]] = Counter()
    branches: Counter[int] = Counter()
    for path in sorted(rows):
        recipient, selector, failed = rows[path]
        committed[path] = not failed and (not path or committed[path[:-1]])
        if path and committed[path] and (recipient, selector) in expected:
            matching[recipient, selector] += 1
            branches[path[0]] += 1
    return SwapCallBundles(
        len(rows),
        sum(matching.values()),
        matching == expected,
        tuple(sorted(branches.items())),
    )
