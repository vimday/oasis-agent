"""Block-pinned transaction fees, including failures and chain-specific extras.

Ethereum blob gas is additional; OP DA footprint is not Ethereum blob gas.
Nitro L1 gas is already included in gasUsed. Unknown chains fail closed.
"""

from dataclasses import asdict, dataclass
from typing import cast

from eth_abi.abi import encode
from eth_hash.auto import keccak

from ..evidence.rpc_validation import decode_exchange, restore_exchanges, rpc_quantity
from ..evidence.store import EvidenceStore
from ..models.chain import BlockSnapshot
from ..models.common import (
    BlockRef,
    EvidenceRef,
    canonical_json,
    normalize_address,
    normalize_hash32,
)
from ..rpc.transport import RpcExchange
from .owner_payments import _abi
from .position_settlement import _mapping, _text

OP_ORACLE = "0x420000000000000000000000000000000000000f"
# Explicit researched families; additions require protocol-specific evidence/tests.
FAMILIES = {
    1: "ethereum",
    11155111: "ethereum",
    8453: "op",
    84532: "op",
    4663: "nitro",
    46630: "nitro",  # Robinhood testnet; retained E2E receipts include gasUsedForL1.
    42161: "nitro",
}
OP_SELECTOR = "0x" + keccak(b"getOperatorFee(uint256)")[:4].hex()


@dataclass(frozen=True, slots=True)
class TransactionCost:
    chain_id: int
    transaction_hash: str
    payer: str
    status: int
    execution_native: int
    l1_native: int
    blob_native: int
    operator_native: int
    total_native: int
    native_decimals: int = 18


def transaction_cost_head(block: BlockRef, transaction_hash: str) -> list[tuple[str, list[object]]]:
    """The exact pre-receipt requests: chain, pinned header, then the receipt itself."""
    return [
        ("eth_chainId", []),
        ("eth_getBlockByNumber", [hex(block.number), False]),
        ("eth_getTransactionReceipt", [transaction_hash]),
    ]


def transaction_cost_tail(block: BlockRef, *, gas: int) -> list[tuple[str, list[object]]]:
    """The exact post-receipt requests, ending in a second read of the same header.

    The OP family adds a pinned oracle-code read and the versioned on-chain operator
    fee for *this* transaction's gas, so a tail cannot be built before the receipt has
    been read. A writer and the verifier share this one definition on purpose: a gate
    whose own requests differ from the ones it checks is not checking what it sent.
    """
    family = FAMILIES.get(block.chain_id)
    if family is None:
        raise ValueError("unknown chain cost policy")
    pin = {"blockHash": block.block_hash, "requireCanonical": True}
    tail: list[tuple[str, list[object]]] = []
    if family == "op":
        tail += [
            ("eth_getCode", [OP_ORACLE, pin]),
            (
                "eth_call",
                [{"to": OP_ORACLE, "data": OP_SELECTOR + encode(["uint256"], [gas]).hex()}, pin],
            ),
        ]
    tail.append(("eth_getBlockByNumber", [hex(block.number), False]))
    return tail


@dataclass(frozen=True, slots=True)
class TransactionCostCapture:
    header: BlockSnapshot
    transaction_hash: str
    source: str
    exchanges: tuple[RpcExchange, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "transaction_hash", normalize_hash32(self.transaction_hash))
        object.__setattr__(self, "exchanges", tuple(self.exchanges))
        if not self.source.strip():
            raise ValueError("cost RPC source required")
        self.reconstruct()

    def reconstruct(self) -> TransactionCost:
        b = self.header.ref
        family = FAMILIES.get(b.chain_id)
        if family is None:
            raise ValueError("unknown chain cost policy")
        if (
            len(self.exchanges) != (6 if family == "op" else 4)
            or sum(len(x.request_payload) + len(x.response_payload) for x in self.exchanges)
            > 5_000_000
        ):
            raise ValueError("incomplete or oversized cost evidence")
        receipt = _mapping(decode_exchange(self.exchanges[2])[1].get("result"))
        gas = rpc_quantity(receipt.get("gasUsed"))
        price = rpc_quantity(receipt.get("effectiveGasPrice"))
        if gas >= 1 << 64 or price >= 1 << 256:
            raise ValueError("gas/price width overflow")
        expected = transaction_cost_head(b, self.transaction_hash) + transaction_cost_tail(
            b, gas=gas
        )
        results = []
        ids = set()
        for x, (m, p) in zip(self.exchanges, expected, strict=True):
            actual, response = decode_exchange(x)
            if (
                x.method != m
                or canonical_json(actual) != canonical_json(p)
                or x.request_id in ids
                or "error" in response
            ):
                raise ValueError("cost request/response mismatch")
            ids.add(x.request_id)
            results.append(response["result"])
        if rpc_quantity(results[0]) != b.chain_id:
            raise ValueError("cost chain mismatch")
        for i in (1, len(results) - 1):
            h = _mapping(results[i])
            actual_header = BlockSnapshot(
                BlockRef(
                    b.chain_id,
                    rpc_quantity(h.get("number")),
                    _text(h.get("hash")),
                    rpc_quantity(h.get("timestamp")),
                ),
                _text(h.get("parentHash")),
            )
            if actual_header != self.header:
                raise ValueError("cost block changed")
        if (
            receipt.get("transactionHash") != self.transaction_hash
            or receipt.get("blockHash") != b.block_hash
            or rpc_quantity(receipt.get("blockNumber")) != b.number
        ):
            raise ValueError("cost receipt transaction/block mismatch")
        status = rpc_quantity(receipt.get("status"))
        if status not in (0, 1):
            raise ValueError("invalid receipt status")
        payer = normalize_address(_text(receipt.get("from")))
        tx_type = rpc_quantity(receipt.get("type"))
        if tx_type not in (0, 1, 2, 3, 4) or (family != "ethereum" and tx_type == 3):
            raise ValueError("unsupported deposit/system/blob transaction")
        execution = gas * price
        l1 = blob = operator = 0
        if family == "op":
            code = _text(results[3])
            if not code.startswith("0x") or not bytes.fromhex(code[2:]):
                raise ValueError("operator oracle code missing")
            l1 = rpc_quantity(receipt.get("l1Fee"))
            operator = cast(int, _abi(["uint256"], _text(results[4]))[0])
            # Explicitly use the pinned on-chain versioned operator fee. Receipt
            # blobGasUsed on Jovian is DA footprint, never another ETH blob debit.
        elif family == "nitro":
            gas_l1 = rpc_quantity(receipt.get("gasUsedForL1"))
            if gas_l1 > gas:
                raise ValueError("L1 gas exceeds total Nitro gas")
            l1 = gas_l1 * price
            execution -= l1
        elif tx_type == 3:
            blob_gas = rpc_quantity(receipt.get("blobGasUsed"))
            blob_price = rpc_quantity(receipt.get("blobGasPrice"))
            if blob_gas == 0 or blob_gas % 131072 or blob_price == 0:
                raise ValueError("invalid Ethereum blob gas/price")
            blob = blob_gas * blob_price
        total = execution + l1 + blob + operator
        if total >= 1 << 256:
            raise ValueError("total transaction cost overflows uint256")
        return TransactionCost(
            b.chain_id, self.transaction_hash, payer, status, execution, l1, blob, operator, total
        )

    @property
    def payload(self) -> bytes:
        return canonical_json(
            {
                "schema": "oasis.transaction-cost.v1",
                "block": asdict(self.header.ref),
                "cost": asdict(self.reconstruct()),
                "source": self.source,
                "execution_eligible": False,
                "position_allocation": "unresolved",
                "quote_valuation": "unresolved",
            }
        )


def persist_transaction_cost(
    store: EvidenceStore,
    capture: TransactionCostCapture,
    *,
    origin: dict[str, object] | None = None,
) -> EvidenceRef:
    """Retain one cost capture. `origin`, when given, is kept inside the hash-bound
    interpretation (its hash is part of the evidence key), so it travels with the
    record; without it the interpretation is exactly the historical one."""

    def envelope(response: bool) -> dict[str, object]:
        return {
            "encoding": "hex_exact_bytes",
            "exchanges": [
                {
                    "method": x.method,
                    "request_id": x.request_id,
                    "attempt": x.attempt,
                    "payload": (x.response_payload if response else x.request_payload).hex(),
                }
                for x in capture.exchanges
            ],
        }

    return store.put(
        "transaction_cost",
        capture.payload,
        capture.header.ref,
        capture.source,
        request=envelope(False),
        raw_response=envelope(True),
        interpretation={
            "scope": "whole_transaction_native_gas_cost",
            "execution_eligible": False,
            **({} if origin is None else {"origin": origin}),
        },
    )


def replay_transaction_cost(
    store: EvidenceStore, evidence: EvidenceRef, *, header: BlockSnapshot, transaction_hash: str
) -> TransactionCostCapture:
    if evidence.kind != "transaction_cost" or evidence.block != header.ref:
        raise ValueError("cost evidence scope mismatch")
    record = store.get_record(evidence)
    if record.request_payload is None:
        raise ValueError("cost evidence missing wire requests")
    capture = TransactionCostCapture(
        header,
        transaction_hash,
        evidence.source,
        restore_exchanges(record.request_payload, record.raw_response_payload),
    )
    if capture.payload != record.payload:
        raise ValueError("cost interpretation differs from raw reconstruction")
    return capture
