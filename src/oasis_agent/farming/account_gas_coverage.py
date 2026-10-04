"""Offline direct-account gas coverage, including reverted and unrelated transactions.

The interval is (opening, closing]. Complete RPC transaction lists are quorum
claims, not trie/consensus proofs. Internal/AA reimbursements and LP attribution
remain unresolved. No zero-cost disclosure or execution permit is produced.
"""

from dataclasses import asdict, dataclass, field

from ..evidence.exact_capture import decode_header, successful_results, wire_envelope
from ..evidence.rpc_validation import (
    decode_exchange,
    restore_exchanges,
    rpc_quantity,
    strict_integer,
    strict_list,
    strict_object,
    strict_string,
)
from ..evidence.store import EvidenceStore
from ..models.chain import BlockSnapshot
from ..models.common import (
    ZERO_ADDRESS,
    EvidenceRef,
    canonical_json,
    normalize_address,
    normalize_hash32,
    normalize_hex,
)
from ..rpc.transport import RpcExchange
from .price_evidence import (
    UsdFeedPolicy,
    replay_price_observation_evidence,
    value_usd_micros,
)
from .transaction_costs import FAMILIES, replay_transaction_cost


def _address(value: object) -> str:
    return normalize_address(strict_string(value, "transaction address"))


def _hash(value: object) -> str:
    return normalize_hash32(strict_string(value, "transaction hash"))


@dataclass(frozen=True, slots=True)
class AccountGasPlan:
    header: BlockSnapshot
    account: str
    providers: tuple[str, str]
    max_account_transactions: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "account", normalize_address(self.account))
        if (
            self.account == ZERO_ADDRESS
            or self.header.ref.chain_id not in FAMILIES
            or len(self.providers) != 2
            or len(set(self.providers)) != 2
            or any(not p.strip() for p in self.providers)
            or type(self.max_account_transactions) is not int
            or not 1 <= self.max_account_transactions <= 128
        ):
            raise ValueError("bounded direct account and two distinct providers required")

    def requests(self) -> list[tuple[str, list[object]]]:
        return [
            ("eth_chainId", []),
            ("eth_getBlockByHash", [self.header.ref.block_hash, True]),
            ("eth_getBlockByNumber", [hex(self.header.ref.number), False]),
        ]


@dataclass(frozen=True, slots=True)
class AccountGasCapture:
    plan: AccountGasPlan
    source: str
    exchanges: tuple[RpcExchange, ...]

    def facts(self) -> dict[str, object]:
        if (
            self.source not in self.plan.providers
            or sum(len(x.request_payload) + len(x.response_payload) for x in self.exchanges)
            > 16_000_000
        ):
            raise ValueError("declared bounded account block proof required")
        results = successful_results(self.exchanges, self.plan.requests())
        if rpc_quantity(results[0]) != self.plan.header.ref.chain_id:
            raise ValueError("account gas chain differs")
        full = strict_object(results[1], "full block")
        close = strict_object(results[2], "closing block")
        for block in (full, close):
            if decode_header(block, chain_id=self.plan.header.ref.chain_id) != self.plan.header:
                raise ValueError("account gas header changed")
        raw = strict_list(full.get("transactions"), "full transactions")
        if len(raw) > 4096:
            raise ValueError("full transaction list exceeds structural bound")
        transactions = []
        hashes: list[str] = []
        for index, item in enumerate(raw):
            tx = strict_object(item, "full transaction")
            digest = _hash(tx.get("hash"))
            if (
                digest in hashes
                or "to" not in tx
                or _hash(tx.get("blockHash")) != self.plan.header.ref.block_hash
                or rpc_quantity(tx.get("blockNumber")) != self.plan.header.ref.number
                or rpc_quantity(tx.get("transactionIndex")) != index
            ):
                raise ValueError("full transaction enumeration or block binding differs")
            hashes.append(digest)
            transactions.append(
                {
                    "hash": digest,
                    "index": index,
                    "from": _address(tx.get("from")),
                    "to": None if tx.get("to") is None else _address(tx.get("to")),
                    "type": rpc_quantity(tx.get("type")),
                    "input": normalize_hex(strict_string(tx.get("input"), "input")),
                    "value": rpc_quantity(tx.get("value")),
                    "gas": rpc_quantity(tx.get("gas")),
                    "nonce": rpc_quantity(tx.get("nonce")),
                }
            )
        if strict_list(close.get("transactions"), "closing hashes") != hashes:
            raise ValueError("closing transaction enumeration differs")
        selected = [tx for tx in transactions if tx["from"] == self.plan.account]
        if len(selected) > self.plan.max_account_transactions:
            raise ValueError("all account transactions exceed frozen bound; no truncation")
        return {
            "schema": "oasis.account-gas-block.v1",
            "plan": asdict(self.plan),
            "transactions": transactions,
            "account_transactions": selected,
        }


def retain_account_gas_block(store: EvidenceStore, capture: AccountGasCapture) -> EvidenceRef:
    facts = capture.facts()
    return store.put(
        "account_gas_block",
        canonical_json(facts),
        capture.plan.header.ref,
        capture.source,
        request=wire_envelope(capture.exchanges, response=False),
        raw_response=wire_envelope(capture.exchanges, response=True),
        interpretation=facts,
        schema_version=1,
    )


def replay_account_gas_block(
    store: EvidenceStore, ref: EvidenceRef, plan: AccountGasPlan
) -> dict[str, object]:
    if ref.kind != "account_gas_block" or ref.schema_version != 1 or ref.block != plan.header.ref:
        raise ValueError("account gas block evidence identity differs")
    record = store.get_record(ref)
    if record.request_payload is None:
        raise ValueError("account gas wire proof missing")
    facts = AccountGasCapture(
        plan, ref.source, restore_exchanges(record.request_payload, record.raw_response_payload)
    ).facts()
    if record.payload != canonical_json(facts) or record.interpretation_payload != canonical_json(
        facts
    ):
        raise ValueError("account gas facts differ from wire proof")
    return facts


@dataclass(frozen=True, slots=True)
class AccountGasBlockInput:
    plan: AccountGasPlan
    blocks: tuple[EvidenceRef, EvidenceRef]
    costs: tuple[tuple[EvidenceRef, EvidenceRef], ...]
    native_prices: tuple[EvidenceRef, EvidenceRef]


@dataclass(frozen=True, slots=True)
class AccountGasRequest:
    opening: BlockSnapshot
    closing: BlockSnapshot
    account: str
    providers: tuple[str, str]
    native_policy: UsdFeedPolicy
    blocks: tuple[AccountGasBlockInput, ...]


@dataclass(frozen=True, slots=True)
class AccountGasReplay:
    opening: BlockSnapshot
    closing: BlockSnapshot
    account: str
    transactions: tuple[dict[str, object], ...]
    successful_native: int
    reverted_native: int
    total_native: int
    total_usd_micros: int
    evidence: tuple[EvidenceRef, ...]
    scope: str = field(
        default="all outer transactions from account in (opening, closing]", init=False
    )
    position_allocation: str = field(default="unresolved", init=False)
    net_pnl: None = field(default=None, init=False)
    execution_eligible: bool = field(default=False, init=False)


def replay_account_gas(store: EvidenceStore, request: AccountGasRequest) -> AccountGasReplay:
    account = normalize_address(request.account)
    if (
        not 1 <= len(request.blocks) <= 1024
        or request.closing.ref.chain_id != request.opening.ref.chain_id
        or request.native_policy.chain_id != request.opening.ref.chain_id
        or request.native_policy.base_token != ZERO_ADDRESS
        or request.blocks[-1].plan.header != request.closing
    ):
        raise ValueError("bounded complete direct-account interval and native USD policy required")
    previous = request.opening
    rows: list[dict[str, object]] = []
    refs: list[EvidenceRef] = []
    seen: set[str] = set()
    success = reverted = total_usd = 0
    for block in request.blocks:
        plan = block.plan
        if (
            plan.account != account
            or plan.providers != request.providers
            or plan.header.ref.chain_id != previous.ref.chain_id
            or plan.header.ref.number != previous.ref.number + 1
            or plan.header.parent_hash != previous.ref.block_hash
            or plan.header.ref.timestamp is None
            or previous.ref.timestamp is None
            or plan.header.ref.timestamp <= previous.ref.timestamp
        ):
            raise ValueError("account gas interval has a gap, reorg or foreign identity")
        for pair in (block.blocks, block.native_prices, *block.costs):
            if len(pair) != 2 or tuple(ref.source for ref in pair) != request.providers:
                raise ValueError("complete declared two-provider pair required")
        facts = tuple(replay_account_gas_block(store, ref, plan) for ref in block.blocks)
        if facts[0] != facts[1]:
            raise ValueError("providers disagree on full block transactions")
        selected = strict_list(facts[0]["account_transactions"], "account transactions")
        if len(selected) != len(block.costs):
            raise ValueError("every account transaction requires its own complete cost pair")
        observations = tuple(
            replay_price_observation_evidence(
                store, ref, header=plan.header, policy=request.native_policy
            ).reconstruct_observation()
            for ref in block.native_prices
        )
        if observations[0] != observations[1]:
            raise ValueError("native price providers disagree on full round")
        price = observations[0]
        refs.extend((*block.blocks, *block.native_prices))
        for item, pair in zip(selected, block.costs, strict=True):
            tx = strict_object(item, "selected transaction")
            digest = strict_string(tx["hash"], "selected hash")
            if digest in seen:
                raise ValueError("account gas repeats a transaction")
            seen.add(digest)
            captures = tuple(
                replay_transaction_cost(store, ref, header=plan.header, transaction_hash=digest)
                for ref in pair
            )
            costs = tuple(c.reconstruct() for c in captures)
            receipts = tuple(
                strict_object(decode_exchange(c.exchanges[2])[1].get("result"), "receipt")
                for c in captures
            )
            if canonical_json(receipts[0]) != canonical_json(receipts[1]) or costs[0] != costs[1]:
                raise ValueError("providers disagree on complete account receipt")
            cost, receipt = costs[0], receipts[0]
            target = None if receipt.get("to") is None else _address(receipt.get("to"))
            logs = strict_list(receipt.get("logs"), "receipt logs")
            if (
                cost.payer != account
                or "to" not in receipt
                or rpc_quantity(receipt.get("transactionIndex")) != tx["index"]
                or target != tx["to"]
                or rpc_quantity(receipt.get("type")) != tx["type"]
                or not 0 < rpc_quantity(receipt.get("gasUsed")) <= strict_integer(tx["gas"], "gas")
                or (cost.status == 0 and logs)
            ):
                raise ValueError("account receipt sender/index/type/gas/logs differ")
            usd = value_usd_micros(
                cost.total_native,
                numerator=price.answer * 1_000_000,
                denominator=10**price.decimals,
                decimals=18,
                round_up=True,
            )
            if cost.status == 0:
                reverted += cost.total_native
            else:
                success += cost.total_native
            total_usd += usd
            rows.append({"block": asdict(plan.header.ref), **asdict(cost), "usd_micros": usd})
            refs.extend(pair)
        previous = plan.header
    return AccountGasReplay(
        request.opening,
        request.closing,
        account,
        tuple(rows),
        success,
        reverted,
        success + reverted,
        total_usd,
        tuple(refs),
    )
