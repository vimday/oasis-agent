"""Frozen anonymous read scopes; no endpoint, credential or transaction injection."""

from typing import Annotated, Literal, Self

from pydantic import Field, StringConstraints, model_validator

from ..arbitrage.cycle_plan import Address, BlockNumber, Frozen, Hash, Label, utc
from ..models.common import ZERO_ADDRESS, canonical_json

ChainSource = Literal[
    "publicnode_ethereum", "publicnode_base", "publicnode_robinhood", "publicnode_elysium"
]
# Official public pages verified 2026-11-14. These are one operator, not independent providers.
CHAIN_SOURCES = {
    "publicnode_ethereum": (1, "https://ethereum-rpc.publicnode.com"),
    "publicnode_base": (8453, "https://base-rpc.publicnode.com"),
    "publicnode_robinhood": (4663, "https://robinhood-rpc.publicnode.com"),
    "publicnode_elysium": (99901, "https://elysium-rpc.publicnode.com"),
}
Source = ChainSource | Literal["titan_ap"]
SOURCES = {**CHAIN_SOURCES, "titan_ap": (1, "https://ap.data.titanbuilder.xyz/data")}
HexData = Annotated[str, StringConstraints(pattern=r"^0x(?:[0-9a-f]{2}){4,8192}$")]


class SourceLimit(Frozen):
    source: Source
    max_requests: Annotated[int, Field(ge=1, le=256)]


class PublicScope(Frozen):
    schema_version: Literal["oasis-public-rpc-scope-v1"] = Field(alias="schema")
    purpose: Label
    expires_utc: str
    sources: Annotated[tuple[SourceLimit, ...], Field(min_length=1, max_length=5)]
    max_capture_seconds: Annotated[int, Field(ge=1, le=120)]

    @model_validator(mode="after")
    def valid(self) -> Self:
        utc(self.expires_utc)
        if len({s.source for s in self.sources}) != len(self.sources):
            raise ValueError("duplicate_source")
        return self


class PublicRead(Frozen):
    method: Literal["eth_getCode", "eth_getStorageAt", "eth_call"]
    target: Address
    data: HexData | None = None
    slot: Hash | None = None
    sender: Address | None = None

    @model_validator(mode="after")
    def valid(self) -> Self:
        if self.target == ZERO_ADDRESS:
            raise ValueError("zero_read_target")
        if (self.method == "eth_call") != (self.data is not None):
            raise ValueError("call_data_required_exactly_for_call")
        if (self.method == "eth_getStorageAt") != (self.slot is not None):
            raise ValueError("slot_required_exactly_for_storage")
        if self.method != "eth_call" and self.sender is not None:
            raise ValueError("sender_belongs_to_call")
        return self


class PublicHeadPlan(Frozen):
    """An observation to choose a later pinned plan, never a settled-state claim."""

    schema_version: Literal["oasis-public-rpc-head-plan-v1"] = Field(alias="schema")
    source: ChainSource
    chain_id: Annotated[int, Field(gt=0, lt=1 << 64)]
    block_tag: Literal["latest", "safe", "finalized"] | BlockNumber

    @model_validator(mode="after")
    def valid(self) -> Self:
        if SOURCES[self.source][0] != self.chain_id:
            raise ValueError("source_chain_mismatch")
        return self

    def requests(self) -> tuple[tuple[str, list[object]], ...]:
        tag = hex(self.block_tag) if isinstance(self.block_tag, int) else self.block_tag
        return (("eth_chainId", []), ("eth_getBlockByNumber", [tag, False]))


class PublicReadPlan(Frozen):
    schema_version: Literal["oasis-public-rpc-read-plan-v1"] = Field(alias="schema")
    source: ChainSource
    chain_id: Annotated[int, Field(gt=0, lt=1 << 64)]
    block_number: BlockNumber
    block_hash: Hash
    reads: Annotated[tuple[PublicRead, ...], Field(min_length=1, max_length=125)]

    @model_validator(mode="after")
    def valid(self) -> Self:
        if SOURCES[self.source][0] != self.chain_id:
            raise ValueError("source_chain_mismatch")
        if len({canonical_json(v.model_dump()) for v in self.reads}) != len(self.reads):
            raise ValueError("duplicate_read")
        return self

    def requests(self) -> tuple[tuple[str, list[object]], ...]:
        pin = {"blockHash": self.block_hash, "requireCanonical": True}
        header: tuple[str, list[object]] = (
            "eth_getBlockByNumber",
            [hex(self.block_number), False],
        )
        result: list[tuple[str, list[object]]] = [("eth_chainId", []), header]
        for read in self.reads:
            if read.method == "eth_call":
                tx = {"to": read.target, "data": read.data}
                if read.sender is not None:
                    tx["from"] = read.sender
                params: list[object] = [tx, pin]
            elif read.method == "eth_getStorageAt":
                params = [read.target, read.slot, pin]
            else:
                params = [read.target, pin]
            result.append((read.method, params))
        return (*result, header)


class PublicTitanPlan(Frozen):
    """Builder declarations only. No chain proof, state application or submission."""

    schema_version: Literal["oasis-titan-state-capture-plan-v1"] = Field(alias="schema")
    source: Literal["titan_ap"]
    chain_id: Annotated[int, Field(ge=1, le=1)]
    samples: Annotated[int, Field(ge=1, le=3)]

    def requests(self) -> tuple[tuple[str, list[object]], ...]:
        return tuple(("titan_getPammVenues", []) for _ in range(self.samples))


class PublicReceiptPlan(Frozen):
    schema_version: Literal["oasis-public-receipt-plan-v1"] = Field(alias="schema")
    source: ChainSource
    chain_id: Annotated[int, Field(gt=0, lt=1 << 64)]
    block_number: BlockNumber
    block_hash: Hash
    transactions: Annotated[tuple[Hash, ...], Field(min_length=1, max_length=8)]

    @model_validator(mode="after")
    def valid(self) -> Self:
        if SOURCES[self.source][0] != self.chain_id:
            raise ValueError("source_chain_mismatch")
        if len(set(self.transactions)) != len(self.transactions):
            raise ValueError("duplicate_receipt")
        return self

    def requests(self) -> tuple[tuple[str, list[object]], ...]:
        head: tuple[str, list[object]] = ("eth_getBlockByNumber", [hex(self.block_number), False])
        receipts: list[tuple[str, list[object]]] = [
            ("eth_getTransactionReceipt", [tx]) for tx in self.transactions
        ]
        return (
            ("eth_chainId", []),
            head,
            *receipts,
            head,
        )
