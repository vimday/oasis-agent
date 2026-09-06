from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from eth_hash.auto import keccak

from ..models.chain import BlockSnapshot
from ..models.common import (
    BlockRef,
    EvidenceRef,
    canonical_json,
    normalize_address,
    normalize_hash32,
    normalize_hex,
    sha256_hex,
)
from ..models.v4 import PoolKey
from ..rpc.transport import RpcExchange
from ..uniswap_v4.pool_id import pool_id_from_key
from .rpc_validation import decode_exchange, rpc_quantity
from .store import EvidenceStore

_ROLE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_REQUIRED_ROLES = frozenset({"pool_manager", "position_manager", "hook"})


def _validate_role(role: str) -> None:
    if not isinstance(role, str) or _ROLE_PATTERN.fullmatch(role) is None:
        raise ValueError("contract role must be lowercase snake_case and at most 64 characters")


@dataclass(frozen=True, slots=True)
class ContractTarget:
    role: str
    address: str

    def __post_init__(self) -> None:
        _validate_role(self.role)
        object.__setattr__(self, "address", normalize_address(self.address))


@dataclass(frozen=True, slots=True)
class RuntimeContractIdentity:
    role: str
    address: str
    byte_length: int
    runtime_keccak256: str
    runtime_sha256: str

    def __post_init__(self) -> None:
        _validate_role(self.role)
        object.__setattr__(self, "address", normalize_address(self.address))
        if (
            isinstance(self.byte_length, bool)
            or not isinstance(self.byte_length, int)
            or self.byte_length <= 0
        ):
            raise ValueError("runtime contract byte length must be positive")
        object.__setattr__(
            self,
            "runtime_keccak256",
            normalize_hash32(self.runtime_keccak256, "runtime keccak256"),
        )
        normalize_hash32("0x" + self.runtime_sha256, "runtime sha256")


@dataclass(frozen=True, slots=True)
class V4DeploymentIdentityDossier:
    source: str
    snapshot: BlockSnapshot
    pool_key: PoolKey
    pool_id: str
    contracts: tuple[RuntimeContractIdentity, ...]

    def __post_init__(self) -> None:
        if not self.source:
            raise ValueError("deployment identity source is required")
        normalized_pool_id = normalize_hash32(self.pool_id, "pool id")
        object.__setattr__(self, "pool_id", normalized_pool_id)
        if normalized_pool_id != pool_id_from_key(self.pool_key):
            raise ValueError("pool id does not match the PoolKey")
        if not self.contracts:
            raise ValueError("deployment identity requires contracts")
        roles = tuple(item.role for item in self.contracts)
        if roles != tuple(sorted(roles)) or len(set(roles)) != len(roles):
            raise ValueError("contract identities must have unique sorted roles")
        missing = _REQUIRED_ROLES.difference(roles)
        if missing:
            raise ValueError(f"deployment identity is missing required roles: {sorted(missing)}")
        hook = next(item for item in self.contracts if item.role == "hook")
        if hook.address != self.pool_key.hooks:
            raise ValueError("hook identity does not match the PoolKey")
        if any(item.address == "0x" + "0" * 40 for item in self.contracts):
            raise ValueError("contract identity cannot use the zero address")

    @property
    def payload(self) -> bytes:
        ref = self.snapshot.ref
        return canonical_json(
            {
                "schema": "oasis.v4-deployment-identity.v1",
                "chain": {
                    "chain_id": ref.chain_id,
                    "block_number": ref.number,
                    "block_hash": ref.block_hash,
                    "parent_hash": self.snapshot.parent_hash,
                    "timestamp": ref.timestamp,
                },
                "pool_id": self.pool_id,
                "pool_key": {
                    "currency0": self.pool_key.currency0,
                    "currency1": self.pool_key.currency1,
                    "fee": self.pool_key.fee,
                    "tick_spacing": self.pool_key.tick_spacing,
                    "hooks": self.pool_key.hooks,
                },
                "contracts": [
                    {
                        "role": item.role,
                        "address": item.address,
                        "byte_length": item.byte_length,
                        "runtime_keccak256": item.runtime_keccak256,
                        "runtime_sha256": item.runtime_sha256,
                    }
                    for item in self.contracts
                ],
            }
        )

    @property
    def content_hash(self) -> str:
        return sha256_hex(self.payload)


@dataclass(frozen=True, slots=True)
class DeploymentIdentityAgreement:
    sources: tuple[str, ...]
    content_hash: str

    def __post_init__(self) -> None:
        if len(self.sources) < 2 or len(set(self.sources)) != len(self.sources):
            raise ValueError("deployment identity agreement requires distinct providers")
        if self.sources != tuple(sorted(self.sources)) or any(not item for item in self.sources):
            raise ValueError("deployment identity agreement sources must be nonempty and sorted")
        normalize_hash32("0x" + self.content_hash, "deployment identity content hash")


class DeploymentIdentityRpc(Protocol):
    def chain_id(self) -> int: ...

    def get_block_snapshot(self, block: int | str = "latest") -> BlockSnapshot: ...

    def get_code(self, address: str, block: int | str | BlockRef = "latest") -> str: ...


def collect_v4_deployment_identity(
    rpc: DeploymentIdentityRpc,
    *,
    source: str,
    block: int | str,
    pool_key: PoolKey,
    targets: tuple[ContractTarget, ...],
) -> V4DeploymentIdentityDossier:
    if not source:
        raise ValueError("deployment identity source is required")
    ordered_targets = tuple(sorted(targets, key=lambda item: item.role))
    roles = tuple(item.role for item in ordered_targets)
    if len(set(roles)) != len(roles):
        raise ValueError("contract targets must have unique roles")
    missing = _REQUIRED_ROLES.difference(roles)
    if missing:
        raise ValueError(f"deployment identity is missing required roles: {sorted(missing)}")

    opening = rpc.get_block_snapshot(block)
    if opening.ref.chain_id != rpc.chain_id():
        raise ValueError("deployment identity block and RPC chain IDs disagree")
    identities: list[RuntimeContractIdentity] = []
    for target in ordered_targets:
        raw = normalize_hex(rpc.get_code(target.address, opening.ref))
        bytecode = bytes.fromhex(raw[2:])
        if not bytecode:
            raise ValueError(f"contract role {target.role} has empty runtime bytecode")
        identities.append(
            RuntimeContractIdentity(
                target.role,
                target.address,
                len(bytecode),
                "0x" + keccak(bytecode).hex(),
                sha256_hex(bytecode),
            )
        )

    closing = rpc.get_block_snapshot(opening.ref.number)
    if closing != opening:
        raise ValueError("deployment identity block changed during collection")
    return V4DeploymentIdentityDossier(
        source,
        opening,
        pool_key,
        pool_id_from_key(pool_key),
        tuple(identities),
    )


def require_deployment_identity_agreement(
    dossiers: tuple[V4DeploymentIdentityDossier, ...],
) -> DeploymentIdentityAgreement:
    if len(dossiers) < 2:
        raise ValueError("deployment identity agreement requires at least two dossiers")
    sources = tuple(sorted(item.source for item in dossiers))
    if len(set(sources)) != len(sources):
        raise ValueError("deployment identity agreement requires distinct providers")
    expected = dossiers[0].content_hash
    if any(item.content_hash != expected for item in dossiers[1:]):
        raise ValueError("providers disagree on the deployment identity")
    return DeploymentIdentityAgreement(sources, expected)


def persist_v4_deployment_identity(
    store: EvidenceStore,
    dossier: V4DeploymentIdentityDossier,
    exchanges: tuple[RpcExchange, ...],
    *,
    retrieved_at: str | None = None,
) -> EvidenceRef:
    methods = tuple(item.method for item in exchanges)
    if not exchanges or methods.count("eth_getBlockByNumber") < 2:
        raise ValueError("deployment identity evidence requires opening and closing block reads")
    if methods.count("eth_getCode") < len(dossier.contracts):
        raise ValueError("deployment identity evidence does not cover every contract")
    validate_deployment_identity_exchanges(dossier, exchanges)
    timestamp = retrieved_at or datetime.now(UTC).isoformat()
    request_envelope = {
        "encoding": "hex_exact_bytes",
        "exchanges": [
            {
                "method": item.method,
                "request_id": item.request_id,
                "attempt": item.attempt,
                "payload": item.request_payload.hex(),
            }
            for item in exchanges
        ],
    }
    response_envelope = {
        "encoding": "hex_exact_bytes",
        "exchanges": [
            {
                "method": item.method,
                "request_id": item.request_id,
                "attempt": item.attempt,
                "payload": item.response_payload.hex(),
            }
            for item in exchanges
        ],
    }
    return store.put(
        "v4_deployment_identity",
        dossier.payload,
        dossier.snapshot.ref,
        dossier.source,
        request=request_envelope,
        raw_response=response_envelope,
        interpretation={
            "schema": "oasis.v4-deployment-identity-evidence.v1",
            "dossier_content_hash": dossier.content_hash,
            "contract_count": len(dossier.contracts),
        },
        retrieved_at=timestamp,
        schema_version=1,
    )


def validate_deployment_identity_exchanges(
    dossier: V4DeploymentIdentityDossier, exchanges: tuple[RpcExchange, ...]
) -> None:
    """Bind runtime hashes to captured results before storing the interpretation.

    This establishes envelope/interpretation consistency, not RPC honesty, source
    verification, proof of pool existence, or independence of provider operators.
    """
    ref = dossier.snapshot.ref
    by_address: dict[str, list[RuntimeContractIdentity]] = {}
    for contract in dossier.contracts:
        by_address.setdefault(contract.address, []).append(contract)
    seen_ids: set[int] = set()
    code_positions: dict[str, int] = {}
    header_positions: list[int] = []
    chain_position: int | None = None
    last_header_tag: object = None
    for index, exchange in enumerate(exchanges):
        params, response = decode_exchange(exchange)
        if exchange.request_id in seen_ids:
            raise ValueError("deployment identity evidence repeats an RPC request ID")
        seen_ids.add(exchange.request_id)
        if exchange.method not in {"eth_chainId", "eth_getBlockByNumber", "eth_getCode"}:
            raise ValueError("unexpected RPC method in deployment identity evidence")
        # Valid failed attempts are retained for audit, but never count as coverage.
        if "error" in response:
            continue
        result = response["result"]
        if exchange.method == "eth_chainId":
            if params or rpc_quantity(result) != ref.chain_id:
                raise ValueError("deployment identity evidence chain ID mismatch")
            if chain_position is None:
                chain_position = index
        elif exchange.method == "eth_getBlockByNumber":
            if len(params) != 2 or params[1] is not False:
                raise ValueError("deployment identity evidence has invalid block request")
            if params[0] != "latest" and rpc_quantity(params[0]) != ref.number:
                raise ValueError("deployment identity evidence requests another block")
            if (
                not isinstance(result, dict)
                or rpc_quantity(result.get("number")) != ref.number
                or result.get("hash") != ref.block_hash
                or result.get("parentHash") != dossier.snapshot.parent_hash
                or rpc_quantity(result.get("timestamp")) != ref.timestamp
            ):
                raise ValueError("deployment identity evidence block header mismatch")
            header_positions.append(index)
            last_header_tag = params[0]
        else:
            if len(params) != 2 or not isinstance(params[0], str):
                raise ValueError("deployment identity evidence has invalid code request")
            address = normalize_address(params[0])
            if address not in by_address:
                raise ValueError("deployment identity evidence requests an unrelated contract")
            selector = params[1]
            if (
                not isinstance(selector, dict)
                or set(selector) != {"blockHash", "requireCanonical"}
                or selector["blockHash"] != ref.block_hash
                or selector["requireCanonical"] is not True
            ):
                raise ValueError("deployment identity code evidence is not canonical hash-pinned")
            if not isinstance(result, str):
                raise ValueError("deployment identity code result must be hex bytes")
            raw = bytes.fromhex(normalize_hex(result)[2:])
            for contract in by_address[address]:
                if (
                    len(raw) != contract.byte_length
                    or "0x" + keccak(raw).hex() != contract.runtime_keccak256
                    or sha256_hex(raw) != contract.runtime_sha256
                ):
                    raise ValueError("deployment identity runtime bytecode does not match evidence")
            if not header_positions or (chain_position is None or chain_position > index):
                raise ValueError("deployment identity code precedes chain/header evidence")
            code_positions[address] = index

    if (
        chain_position is None
        or len(header_positions) < 2
        or set(code_positions) != set(by_address)
    ):
        raise ValueError("deployment identity evidence has incomplete successful read coverage")
    if (
        chain_position > header_positions[0]
        or header_positions[-1] < max(code_positions.values())
        or last_header_tag == "latest"
    ):
        raise ValueError("deployment identity evidence lacks an ordered pinned closing header")


__all__ = [
    "ContractTarget",
    "DeploymentIdentityAgreement",
    "DeploymentIdentityRpc",
    "RuntimeContractIdentity",
    "V4DeploymentIdentityDossier",
    "collect_v4_deployment_identity",
    "persist_v4_deployment_identity",
    "require_deployment_identity_agreement",
    "validate_deployment_identity_exchanges",
]
