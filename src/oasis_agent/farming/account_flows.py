"""Per-asset movement and non-trading-flow attribution between two pinned frames.

Balance deltas come from two agreeing inventory providers. ERC-20 deltas are
additionally explained from complete, two-provider `Transfer` logs; a declared LP
position is explained from the four-topic ERC-721 `Transfer` logs of its own token
id, where a mint from or a burn to the zero address is a legitimate movement of
one unit. The native delta is only explained by an explicit retained gas/transfer
attestation.

`non_trading_flow_usd_micros` is what the risk ledger needs: the value of the
flow that explains a NAV change without being a trading result. Declared LP
currencies are unclassified until the reader derives exact internal transfers
from raw settlement proofs; those transfers explain balances but receive zero
external-flow credit. Other token deltas retain the legacy transfer-credit path,
including transfers between monitored accounts. Gas is never credited. Anything
still unexplained is counted and the ledger refuses the frame.

`TokenFlow.external_*` is reporting metadata only: it separates counterparties
outside the monitored set, which is what a bridge or withdrawal attribution
needs. Using those figures as the ledger credit would charge every transfer
between monitored accounts as a loss. No pricing, signing or execution.
"""

import re
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import cast

from eth_hash.auto import keccak

from ..evidence.exact_capture import (
    RecordedSession,
    decode_header,
    successful_results,
    validate_session_pair,
    wire_envelope,
)
from ..evidence.rpc_validation import (
    decode_exchange,
    restore_exchanges,
    rpc_quantity,
    strict_json_object,
)
from ..evidence.store import EvidenceStore
from ..models.chain import BlockSnapshot
from ..models.common import (
    EvidenceRef,
    canonical_json,
    normalize_address,
    normalize_hash32,
    normalize_hex,
)
from ..rpc.transport import RpcExchange
from .price_evidence import value_usd_micros

TRANSFER_TOPIC = "0x" + keccak(b"Transfer(address,address,uint256)").hex()
_SCHEMA = "oasis.account-token-flow.v1"


def _topic_address(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("transfer topic must be a hex string")
    topic = normalize_hex(value)
    if len(topic) != 66 or topic[2:26] != "0" * 24:
        raise ValueError("transfer topic is not a canonical address word")
    return normalize_address("0x" + topic[26:])


def _topic_word(address: str) -> str:
    return "0x" + address[2:].rjust(64, "0")


def _topic_token(token_id: int) -> str:
    return "0x" + f"{token_id:064x}"


@dataclass(frozen=True, slots=True)
class TransferLog:
    """One observed `Transfer` touching the reconciled account.

    An ERC-20 log carries three topics and its amount in `data`, so both sides
    must be real addresses. An ERC-721 log carries the token id as a fourth
    topic, has no data, and moves exactly one unit of its position asset; its
    mint and burn sides are the zero address.
    """

    token: str
    from_address: str
    to_address: str
    units: int
    block_number: int
    log_index: int
    transaction_hash: str
    token_id: int | None = None

    def __post_init__(self) -> None:
        for name in ("token", "from_address", "to_address"):
            address = normalize_address(getattr(self, name))
            if int(address, 16) == 0 and (name == "token" or self.token_id is None):
                raise ValueError(f"transfer {name} must be nonzero")
            object.__setattr__(self, name, address)
        if self.token_id is not None and self.units != 1:
            raise ValueError("an ERC-721 transfer moves exactly one position unit")
        if type(self.units) is not int or not 0 <= self.units < 2**256:
            raise ValueError("transfer units must be uint256")
        if type(self.block_number) is not int or self.block_number < 0:
            raise ValueError("transfer block must be nonnegative")
        if type(self.log_index) is not int or self.log_index < 0:
            raise ValueError("transfer log index must be nonnegative")
        object.__setattr__(
            self, "transaction_hash", normalize_hash32(self.transaction_hash, "transaction hash")
        )

    @property
    def order_key(self) -> tuple[int, int, str]:
        return (self.block_number, self.log_index, self.transaction_hash)


def _log_from_result(value: object, *, token: str, token_id: int | None = None) -> TransferLog:
    if not isinstance(value, dict):
        raise TypeError("transfer log must be an object")
    topics = value.get("topics")
    expected_topics = 3 if token_id is None else 4
    if not isinstance(topics, list) or len(topics) != expected_topics:
        raise ValueError(f"transfer log must carry exactly {expected_topics} topics")
    if normalize_hex(cast(str, topics[0])) != TRANSFER_TOPIC:
        kind = "ERC-20" if token_id is None else "ERC-721"
        raise ValueError(f"transfer log is not a canonical {kind} Transfer")
    data = value.get("data")
    if not isinstance(data, str):
        raise TypeError("transfer log data must be hex")
    normalized = normalize_hex(data)
    if token_id is None:
        if len(normalized) != 66:
            raise ValueError("transfer log value must be one ABI word")
        units = int(normalized, 16)
    else:
        if normalized != "0x":
            raise ValueError("an ERC-721 transfer log must carry no data")
        if normalize_hex(cast(str, topics[3])) != _topic_token(token_id):
            raise ValueError("transfer log token id differs from the declared position")
        units = 1
    address = value.get("address")
    if not isinstance(address, str) or normalize_address(address) != token:
        raise ValueError("transfer log token address mismatch")
    if value.get("removed") not in (False, None):
        raise ValueError("removed transfer log is not final")
    return TransferLog(
        token,
        _topic_address(topics[1]),
        _topic_address(topics[2]),
        units,
        rpc_quantity(value.get("blockNumber")),
        rpc_quantity(value.get("logIndex")),
        normalize_hash32(cast(str, value.get("transactionHash")), "transaction hash"),
        token_id,
    )


def _logs_from_result(
    value: object, *, token: str, token_id: int | None = None
) -> tuple[TransferLog, ...]:
    if not isinstance(value, list):
        raise TypeError("eth_getLogs result must be a list")
    return tuple(_log_from_result(item, token=token, token_id=token_id) for item in value)


def _log_json(log: TransferLog) -> dict[str, object]:
    """The persisted ERC-20 shape is unchanged; only a position log adds its id."""
    data: dict[str, object] = {
        "token": log.token,
        "from_address": log.from_address,
        "to_address": log.to_address,
        "units": log.units,
        "block_number": log.block_number,
        "log_index": log.log_index,
        "transaction_hash": log.transaction_hash,
    }
    if log.token_id is not None:
        data["token_id"] = log.token_id
    return data


@dataclass(frozen=True, slots=True)
class FlowScope:
    """Identity of one attributable movement window (exclusive of start).

    `token_id` selects the ERC-721 shape: the scope then owns exactly one token
    of `token` and its movements are single units, not scaled amounts.
    """

    chain_id: int
    account: str
    asset_id: str
    token: str
    decimals: int
    start_block: int
    end_block: int
    token_id: int | None = None
    start_block_hash: str | None = None
    end_block_hash: str | None = None

    def __post_init__(self) -> None:
        if type(self.chain_id) is not int or self.chain_id <= 0:
            raise ValueError("token flow requires a positive chain")
        account = normalize_address(self.account)
        token = normalize_address(self.token)
        if int(account, 16) == 0 or int(token, 16) == 0:
            raise ValueError("token flow requires nonzero account and token")
        object.__setattr__(self, "account", account)
        object.__setattr__(self, "token", token)
        if not self.asset_id:
            raise ValueError("token flow requires an asset identifier")
        if type(self.decimals) is not int or not 0 <= self.decimals <= 36:
            raise ValueError("token flow decimals must be bounded")
        if type(self.start_block) is not int or self.start_block < 0:
            raise ValueError("token flow start block must be nonnegative")
        if type(self.end_block) is not int or self.end_block <= self.start_block:
            raise ValueError("token flow window must span at least one block")
        if (self.start_block_hash is None) != (self.end_block_hash is None):
            raise ValueError("flow requires both boundary hashes or neither")
        if self.end_block_hash is not None:
            if self.end_block - self.start_block > 1000:
                raise ValueError("hash-bound flow window exceeds 1000 blocks")
            for name in ("start_block_hash", "end_block_hash"):
                object.__setattr__(self, name, normalize_hash32(getattr(self, name)))
        if self.token_id is not None:
            if type(self.token_id) is not int or not 0 < self.token_id < 2**256:
                raise ValueError("an NFT position flow requires a bounded token id")
            if self.decimals != 0:
                raise ValueError("an NFT position flow requires zero decimals")


def _scope_json(scope: FlowScope) -> dict[str, object]:
    """The persisted ERC-20 scope shape is unchanged; a position scope adds its id."""
    data: dict[str, object] = {
        "chain_id": scope.chain_id,
        "account": scope.account,
        "asset_id": scope.asset_id,
        "token": scope.token,
        "decimals": scope.decimals,
        "start_block": scope.start_block,
        "end_block": scope.end_block,
    }
    if scope.token_id is not None:
        data["token_id"] = scope.token_id
    if scope.end_block_hash is not None:
        data.update(start_block_hash=scope.start_block_hash, end_block_hash=scope.end_block_hash)
    return data


@dataclass(frozen=True, slots=True)
class TokenFlow:
    """Net and external units attributable to one window."""

    asset_id: str
    decimals: int
    net_units: int
    external_net_units: int
    external_in_units: int
    external_out_units: int
    logs: tuple[TransferLog, ...]


def _expected(
    scope: FlowScope, headers: tuple[BlockSnapshot, ...] = ()
) -> list[tuple[str, list[object]]]:
    window: dict[str, object] = {
        "fromBlock": hex(scope.start_block + 1),
        "toBlock": hex(scope.end_block),
    }
    owned: list[str] = [] if scope.token_id is None else [_topic_token(scope.token_id)]
    if scope.end_block_hash is not None:
        expected: list[tuple[str, list[object]]] = [("eth_chainId", [])]
        expected.extend(
            ("eth_getBlockByNumber", [hex(n), False])
            for n in range(scope.start_block, scope.end_block + 1)
        )
        for header in headers[1:]:
            topic_pairs = (
                [TRANSFER_TOPIC, None, _topic_word(scope.account), *owned],
                [TRANSFER_TOPIC, _topic_word(scope.account), None, *owned],
            )
            expected.extend(
                (
                    "eth_getLogs",
                    [
                        {
                            "address": scope.token,
                            "blockHash": header.ref.block_hash,
                            "topics": topics,
                        }
                    ],
                )
                for topics in topic_pairs
            )
        expected.append(("eth_getBlockByNumber", [hex(scope.end_block), False]))
        return expected
    return [
        ("eth_chainId", []),
        (
            "eth_getLogs",
            [
                {
                    "address": scope.token,
                    **window,
                    "topics": [TRANSFER_TOPIC, None, _topic_word(scope.account), *owned],
                }
            ],
        ),
        (
            "eth_getLogs",
            [
                {
                    "address": scope.token,
                    **window,
                    "topics": [TRANSFER_TOPIC, _topic_word(scope.account), None, *owned],
                }
            ],
        ),
    ]


@dataclass(frozen=True, slots=True)
class TokenFlowCapture:
    """One provider's exact bytes for a scope."""

    source: str
    scope: FlowScope
    exchanges: tuple[RpcExchange, ...]
    logs: tuple[TransferLog, ...] = field(init=False, repr=False, default=())

    def __post_init__(self) -> None:
        object.__setattr__(self, "exchanges", tuple(self.exchanges))
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", self.source):
            raise ValueError("token flow provider name is invalid")
        object.__setattr__(self, "logs", self.reconstruct())

    @property
    def payload(self) -> bytes:
        return canonical_json(
            {
                "schema": _SCHEMA
                if self.scope.end_block_hash is None
                else "oasis.account-token-flow.v2",
                "source": self.source,
                "scope": _scope_json(self.scope),
                "logs": [_log_json(log) for log in self.logs],
            }
        )

    def bound_headers(self) -> tuple[BlockSnapshot, ...]:
        if self.scope.end_block_hash is None:
            return ()
        count = self.scope.end_block - self.scope.start_block + 1
        if len(self.exchanges) != 3 * (count - 1) + 3:
            raise ValueError("incomplete hash-bound flow evidence")
        headers = tuple(
            decode_header(decode_exchange(item)[1].get("result"), chain_id=self.scope.chain_id)
            for item in self.exchanges[1 : 1 + count]
        )
        if (headers[0].ref.block_hash, headers[-1].ref.block_hash) != (
            self.scope.start_block_hash,
            self.scope.end_block_hash,
        ):
            raise ValueError("flow boundaries differ from reconciled snapshots")
        for index, header in enumerate(headers):
            if header.ref.number != self.scope.start_block + index:
                raise ValueError("flow headers do not cover the exact window")
            if index and header.parent_hash != headers[index - 1].ref.block_hash:
                raise ValueError("flow header ancestry is discontinuous")
            if index:
                current_time, prior_time = header.ref.timestamp, headers[index - 1].ref.timestamp
                if current_time is None or prior_time is None or current_time <= prior_time:
                    raise ValueError("flow header time did not advance")
        return headers

    def reconstruct(self) -> tuple[TransferLog, ...]:
        headers = self.bound_headers()
        results = successful_results(self.exchanges, _expected(self.scope, headers))
        if rpc_quantity(results[0]) != self.scope.chain_id:
            raise ValueError("token flow provider chain mismatch")
        if headers:
            if decode_header(results[-1], chain_id=self.scope.chain_id) != headers[-1]:
                raise ValueError("flow block changed during capture")
            collected: list[TransferLog] = []
            cursor = 1 + len(headers)
            for header in headers[1:]:
                for direction, raw in enumerate(results[cursor : cursor + 2]):
                    if not isinstance(raw, list) or any(
                        not isinstance(item, dict)
                        or item.get("blockHash") != header.ref.block_hash
                        or item.get("blockNumber") != hex(header.ref.number)
                        for item in raw
                    ):
                        raise ValueError("flow log does not belong to its pinned block")
                    logs = _logs_from_result(
                        raw, token=self.scope.token, token_id=self.scope.token_id
                    )
                    if len({log.order_key for log in logs}) != len(logs):
                        raise ValueError("token flow provider returned a duplicate log")
                    if any(
                        (log.to_address if direction == 0 else log.from_address)
                        != self.scope.account
                        for log in logs
                    ):
                        raise ValueError("flow log does not match its direction filter")
                    collected.extend(logs)
                cursor += 2
        else:
            collected = [
                *_logs_from_result(
                    results[1], token=self.scope.token, token_id=self.scope.token_id
                ),
                *_logs_from_result(
                    results[2], token=self.scope.token, token_id=self.scope.token_id
                ),
            ]
        if any(
            not self.scope.start_block < log.block_number <= self.scope.end_block
            for log in collected
        ):
            raise ValueError("token flow log lies outside its declared window")
        unique: dict[tuple[int, int, str], TransferLog] = {}
        for log in collected:
            if log.order_key in unique and (
                not headers
                or unique[log.order_key] != log
                or not (log.from_address == log.to_address == self.scope.account)
            ):
                raise ValueError("token flow provider returned a duplicate log")
            unique[log.order_key] = log
        return tuple(sorted(unique.values(), key=lambda log: log.order_key))

    def flow(self, *, tracked_accounts: frozenset[str]) -> TokenFlow:
        external_in = external_out = net = 0
        for log in self.logs:
            if self.scope.account not in (log.from_address, log.to_address):
                raise ValueError("transfer log does not involve the reconciled account")
            if log.from_address == log.to_address:
                continue
            if log.to_address == self.scope.account:
                net += log.units
                if log.from_address not in tracked_accounts:
                    external_in += log.units
            else:
                net -= log.units
                if log.to_address not in tracked_accounts:
                    external_out += log.units
        return TokenFlow(
            self.scope.asset_id,
            self.scope.decimals,
            net,
            external_in - external_out,
            external_in,
            external_out,
            self.logs,
        )


@dataclass(frozen=True, slots=True)
class TokenFlowQuorum:
    left: TokenFlowCapture
    right: TokenFlowCapture

    def __post_init__(self) -> None:
        if self.left.source == self.right.source or self.left.scope != self.right.scope:
            raise ValueError("token flow quorum requires distinct providers of one scope")
        if self.left.logs != self.right.logs:
            raise ValueError("token flow providers disagree on the transfer set")

    def flow(self, *, tracked_accounts: frozenset[str]) -> TokenFlow:
        return self.left.flow(tracked_accounts=tracked_accounts)


def _collect_one(session: RecordedSession, *, scope: FlowScope) -> TokenFlowCapture:
    transport, recorder, source = session
    transport.request("eth_chainId", [])
    headers: tuple[BlockSnapshot, ...] = ()
    if scope.end_block_hash is not None:
        headers = tuple(
            decode_header(
                transport.request("eth_getBlockByNumber", [hex(n), False]), chain_id=scope.chain_id
            )
            for n in range(scope.start_block, scope.end_block + 1)
        )
    for method, params in _expected(scope, headers)[1 + len(headers) :]:
        transport.request(method, params)
    return TokenFlowCapture(source, scope, recorder.exchanges)


def collect_token_flow_quorum(
    primary: RecordedSession,
    secondary: RecordedSession,
    *,
    scope: FlowScope,
) -> TokenFlowQuorum:
    validate_session_pair(primary, secondary)
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="account-token-flow") as workers:
        left = workers.submit(_collect_one, primary, scope=scope)
        right = workers.submit(_collect_one, secondary, scope=scope)
        return TokenFlowQuorum(left.result(), right.result())


def persist_token_flow_evidence(
    store: EvidenceStore,
    quorum: TokenFlowQuorum,
    *,
    retrieved_at: str | None = None,
) -> tuple[EvidenceRef, EvidenceRef]:
    if quorum.left.logs != quorum.right.logs or quorum.left.scope != quorum.right.scope:
        raise ValueError("token flow quorum changed before persistence")
    left, right = quorum.left, quorum.right
    return (
        _persist_capture(store, left, retrieved_at=retrieved_at),
        _persist_capture(store, right, retrieved_at=retrieved_at),
    )


def _persist_capture(
    store: EvidenceStore, capture: TokenFlowCapture, *, retrieved_at: str | None
) -> EvidenceRef:
    return store.put(
        "account_token_flow",
        capture.payload,
        capture.bound_headers()[-1].ref if capture.scope.end_block_hash is not None else None,
        capture.source,
        request=wire_envelope(capture.exchanges, response=False),
        raw_response=wire_envelope(capture.exchanges, response=True),
        interpretation={
            "schema": "oasis.account-token-flow-evidence.v2"
            if capture.scope.end_block_hash is not None
            else "oasis.account-token-flow-evidence.v1",
            "block_window": [capture.scope.start_block, capture.scope.end_block],
            "execution_eligible": False,
        },
        retrieved_at=retrieved_at,
        schema_version=2 if capture.scope.end_block_hash is not None else 1,
    )


def replay_token_flow_evidence(
    store: EvidenceStore,
    reference: EvidenceRef,
    *,
    scope: FlowScope,
    tracked_accounts: frozenset[str],
) -> TokenFlow:
    if (
        reference.kind != "account_token_flow"
        or reference.schema_version != (2 if scope.end_block_hash is not None else 1)
        or (scope.end_block_hash is None and reference.block is not None)
    ):
        raise ValueError("unsupported token flow evidence")
    record = store.get_record(reference)
    if record.request_payload is None:
        raise ValueError("token flow evidence is missing raw requests")
    data = strict_json_object(record.payload)
    if (
        set(data) != {"schema", "source", "scope", "logs"}
        or data["schema"]
        != (_SCHEMA if scope.end_block_hash is None else "oasis.account-token-flow.v2")
        or data["scope"] != _scope_json(scope)
    ):
        raise ValueError("token flow evidence does not match the requested scope")
    capture = TokenFlowCapture(
        reference.source,
        scope,
        restore_exchanges(record.request_payload, record.raw_response_payload),
    )
    if capture.payload != record.payload:
        raise ValueError("token flow interpretation differs from raw reconstruction")
    if scope.end_block_hash is not None and reference.block != capture.bound_headers()[-1].ref:
        raise ValueError("flow evidence is not pinned to its closing snapshot")
    return capture.flow(tracked_accounts=tracked_accounts)


@dataclass(frozen=True, slots=True)
class MovementReconstruction:
    """Ledger inputs for one account: exact deltas, non-trading flow, unresolved count."""

    movements: dict[str, int]
    non_trading_flow_usd_micros: int
    unresolved_operations: int
    evidence: tuple[EvidenceRef, ...]


def reconstruct_movements(
    *,
    previous_units: Mapping[str, int],
    current_units: Mapping[str, int],
    prices: Mapping[str, tuple[int, int, int]],
    flows: Mapping[str, TokenFlow],
    native_asset_id: str,
    native_gas_units: int,
    non_trading_native_units: int = 0,
    unclassified_assets: frozenset[str] = frozenset(),
    internal_transfers: frozenset[TransferLog] = frozenset(),
    evidence: tuple[EvidenceRef, ...] = (),
) -> MovementReconstruction:
    """Explain every asset delta; count, never guess, what cannot be explained.

    `movements` is the exact two-source observed delta the ledger re-checks. A
    missing or disagreeing movement explanation increments
    `unresolved_operations`, which makes the ledger refuse the whole frame.
    `non_trading_native_units` is the declared native inflow/outflow that is not
    gas; any native delta different from declared inflow less gas is unresolved,
    including an unchanged balance that hides an unexplained offsetting inflow.

    A Transfer proves movement, not its economic purpose. For assets whose
    trading/non-trading cash attribution is missing, retain the observed delta
    but count nonzero activity as unresolved and credit no external flow. The
    account reader derives this set from its manifest's owned LP currencies:
    principal moved into an already counted LP asset is not a capital withdrawal.
    The account reader may supply individual internal transfers only after raw
    owned-position settlement replay. Uncovered nonzero activity stays unresolved;
    a net amount or an arbitrary counterparty is never a purpose proof.
    """
    if set(previous_units) != set(current_units) or set(prices) != set(previous_units):
        raise ValueError("movement reconstruction requires one complete asset set")
    if native_asset_id not in prices:
        raise ValueError("movement reconstruction requires the native gas asset")
    if type(unclassified_assets) is not frozenset or not unclassified_assets <= prices.keys():
        raise ValueError("unclassified assets must be a frozen subset of the inventory")
    if type(internal_transfers) is not frozenset or not internal_transfers <= {
        log
        for asset_id, flow in flows.items()
        if asset_id in unclassified_assets
        for log in flow.logs
    }:
        raise ValueError("internal transfers must belong to the unclassified movement set")
    for name, value in (
        ("native_gas_units", native_gas_units),
        ("non_trading_native_units", non_trading_native_units),
    ):
        if type(value) is not int:
            raise TypeError(f"{name} must be an integer")
    if native_gas_units < 0:
        raise ValueError("native gas units must be nonnegative")

    movements: dict[str, int] = {}
    flow_usd_micros = 0
    unresolved = 0
    for asset_id, (numerator, denominator, decimals) in prices.items():
        delta = current_units[asset_id] - previous_units[asset_id]
        movements[asset_id] = delta
        if asset_id == native_asset_id:
            if asset_id in flows:
                raise ValueError("native gas flow cannot be reconstructed from Transfer logs")
            if asset_id in unclassified_assets and non_trading_native_units:
                unresolved += 1
                continue
            if delta == non_trading_native_units - native_gas_units:
                flow_usd_micros += value_usd_micros(
                    non_trading_native_units,
                    numerator=numerator,
                    denominator=denominator,
                    decimals=decimals,
                )
            else:
                unresolved += 1
            continue
        flow = flows.get(asset_id)
        if flow is None:
            if delta:
                unresolved += 1
            continue
        if flow.decimals != decimals or flow.net_units != delta:
            unresolved += 1
            continue
        if asset_id in unclassified_assets and (
            flow.net_units or any(log.units for log in flow.logs)
        ):
            if not any(log.units for log in flow.logs) or any(
                log.units and log not in internal_transfers for log in flow.logs
            ):
                unresolved += 1
            continue
        flow_usd_micros += value_usd_micros(
            flow.net_units,
            numerator=numerator,
            denominator=denominator,
            decimals=decimals,
        )
    return MovementReconstruction(movements, flow_usd_micros, unresolved, evidence)


__all__ = [
    "FlowScope",
    "MovementReconstruction",
    "TokenFlow",
    "TokenFlowCapture",
    "TokenFlowQuorum",
    "TransferLog",
    "collect_token_flow_quorum",
    "persist_token_flow_evidence",
    "reconstruct_movements",
    "replay_token_flow_evidence",
]
