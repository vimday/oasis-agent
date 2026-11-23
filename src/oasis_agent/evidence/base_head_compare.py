"""Strict Base header bindings and common-window coverage for two observation arms."""

from dataclasses import asdict, dataclass

from ..models.common import normalize_hash32
from .base_denim import BASE_CHAIN_ID, validate_header_timestamp_ms
from .rpc_validation import rpc_quantity, strict_object, strict_string

NS_MS = 1_000_000
WS_ARM = "newHeads"
POLL_ARM = "poll"
ARMS = (WS_ARM, POLL_ARM)


@dataclass(frozen=True, slots=True)
class HeadPlan:
    poll_slots: int
    interval_ms: int
    witness_cap: int

    def __post_init__(self) -> None:
        if (
            type(self.poll_slots) is not int
            or not 2 <= self.poll_slots <= 16
            or type(self.interval_ms) is not int
            or not 500 <= self.interval_ms <= 5_000
            or self.poll_slots * self.interval_ms > 12_000
            or type(self.witness_cap) is not int
            or not 2 <= self.witness_cap <= 64
        ):
            raise ValueError("invalid Base head comparison budget or window")

    @property
    def window_ns(self) -> int:
        return self.poll_slots * self.interval_ms * NS_MS


@dataclass(frozen=True, slots=True)
class BaseHead:
    height: int
    block_hash: str
    parent_hash: str
    state_root: str
    timestamp_s: int
    timestamp_ms: int | None

    def agrees(self, other: BaseHead) -> bool:
        return (
            self.height == other.height
            and self.block_hash == other.block_hash
            and self.parent_hash == other.parent_hash
            and self.state_root == other.state_root
            and self.timestamp_s == other.timestamp_s
            and (
                self.timestamp_ms is None
                or other.timestamp_ms is None
                or self.timestamp_ms == other.timestamp_ms
            )
        )


def decode_head(value: object, *, hint: bool = False) -> BaseHead:
    raw = strict_object(value, "Base header")
    exact_ms = None
    if not hint or "timestampMs" in raw:
        exact_ms = validate_header_timestamp_ms(raw, chain_id=BASE_CHAIN_ID, require_denim=True)
    head = BaseHead(
        rpc_quantity(raw.get("number")),
        normalize_hash32(strict_string(raw.get("hash"), "hash")),
        normalize_hash32(strict_string(raw.get("parentHash"), "parentHash")),
        normalize_hash32(strict_string(raw.get("stateRoot"), "stateRoot")),
        rpc_quantity(raw.get("timestamp")),
        exact_ms,
    )
    if head.block_hash == head.parent_hash or not 0 <= head.height < 1 << 64:
        raise ValueError("invalid_base_header_identity")
    return head


def decode_notification(value: object, subscription: str) -> BaseHead:
    data = strict_object(value, "newHeads notification")
    params = strict_object(data.get("params"), "subscription params")
    if (
        set(data) != {"jsonrpc", "method", "params"}
        or data["jsonrpc"] != "2.0"
        or data["method"] != "eth_subscription"
        or set(params) != {"subscription", "result"}
        or params["subscription"] != subscription
    ):
        raise ValueError("invalid_base_new_heads_subscription")
    return decode_head(params["result"], hint=True)


@dataclass(frozen=True, slots=True)
class Arrival:
    head: BaseHead
    received_ns: int


class HeadWindow:
    """Deduplicate each source, retaining a single consistent header per height."""

    def __init__(self) -> None:
        self.arrivals: dict[str, dict[int, Arrival]] = {arm: {} for arm in ARMS}
        self.duplicates = dict.fromkeys(ARMS, 0)
        self.known: dict[int, BaseHead] = {}

    def observe(self, arm: str, head: BaseHead, received_ns: int) -> None:
        n = head.height
        same = self.known.get(n)
        prior, after = self.known.get(n - 1), self.known.get(n + 1)
        if same is not None and not same.agrees(head):
            raise ValueError("same_height_header_conflict")
        if (prior is not None and prior.block_hash != head.parent_hash) or (
            after is not None and after.parent_hash != head.block_hash
        ):
            raise ValueError("adjacent_parent_conflict")
        observed = self.arrivals[arm]
        if observed and n < next(reversed(observed)):
            raise ValueError("arm_height_regression")
        if same is None or same.timestamp_ms is None:
            self.known[n] = head
        if n in observed:
            self.duplicates[arm] += 1
        else:
            observed[n] = Arrival(head, received_ns)

    def span(self, cap: int) -> range:
        by_arm = tuple(self.arrivals.values())
        if not all(by_arm):
            return range(0)
        first = max(min(rows) for rows in by_arm)
        end = min(max(rows) for rows in by_arm) + 1
        length = end - first
        return range(first, end) if 0 < length <= cap else range(0)

    def check_witness(self, head: BaseHead, height: int, prior: BaseHead | None) -> None:
        observed = self.known.get(height)
        if (
            head.height != height
            or (observed is not None and not observed.agrees(head))
            or (prior is not None and prior.block_hash != head.parent_hash)
        ):
            raise ValueError("official_witness_disagrees_with_observed_chain")

    def report(self, plan: HeadPlan, witness: dict[int, BaseHead]) -> dict[str, object]:
        span = self.span(plan.witness_cap)
        if not span or tuple(witness) != tuple(span):
            return {
                "status": "no_qualified_common_window",
                "observed_counts": {arm: len(self.arrivals[arm]) for arm in ARMS},
                "duplicates": self.duplicates,
                "witnessed_heights": list(witness),
            }
        rows = {}
        for arm in ARMS:
            observed_arm = self.arrivals[arm]
            rows[arm] = {
                "observed": len(observed_arm),
                "missing": [index for index in span if index not in observed_arm],
                "present": sum(index in observed_arm for index in span),
                "direct_children": sum(
                    index in observed_arm and index - 1 in observed_arm
                    for index in range(span.start + 1, span.stop)
                ),
                "exact_timestamp_ms": sum(
                    item.head.timestamp_ms is not None for item in observed_arm.values()
                ),
            }
        stream, poll = (self.arrivals[arm] for arm in ARMS)
        paired = [
            {
                "height": index,
                "hash": witness[index].block_hash,
                "new_heads_receive_ns": stream[index].received_ns,
                "poll_receive_ns": poll[index].received_ns,
                "poll_minus_new_heads_ns": poll[index].received_ns - stream[index].received_ns,
            }
            for index in span
            if index in stream and index in poll
        ]
        return {
            "status": "witnessed_common_window",
            "start_height": span.start,
            "end_height": span.stop - 1,
            "height_denominator": len(span),
            "direct_child_denominator": len(span) - 1,
            "opening_header": asdict(witness[span.start]),
            "closing_header": asdict(witness[span.stop - 1]),
            "arms": rows,
            "duplicates": self.duplicates,
            "paired": paired,
        }
