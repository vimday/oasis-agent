"""Read-only, hash-pinned token identity reconnaissance.

This verifies basic identity only. Owner, peers, Endpoint delegate, DVNs,
source-code review, holders and executable venues still require separate proof.
"""

from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from eth_hash.auto import keccak
from pydantic import TypeAdapter

from ..evidence.rpc_capture import BoundedExchangeRecorder
from ..evidence.store import EvidenceStore
from ..models.common import BlockRef, EvidenceRef, canonical_json
from ..rpc.client import RpcClient
from ..rpc.transport import JsonRpcTransport, RpcExchange
from .models import Source, Target, TargetPolicy


@dataclass(frozen=True)
class IdentitySample:
    source: str
    block: BlockRef
    retrieved_at: str
    runtime_hash: str
    decimals: int
    local_supply_base_units: int
    exchanges: tuple[RpcExchange, ...]
    target_ca: str
    reference_supply: int


def _word(value: str) -> int:
    if len(value) != 66:
        raise ValueError("noncanonical uint256 returndata")
    return int(value, 16)


def acquire_identity(
    transport: JsonRpcTransport,
    recorder: BoundedExchangeRecorder,
    *,
    source: str,
    block_number: int,
    target: TargetPolicy | None = None,
) -> IdentitySample:
    """Caller must wire the recorder into this transport before construction."""
    TypeAdapter(Source).validate_python(source, strict=True)
    target = Target() if target is None else target
    if type(block_number) is not int or block_number < 0:
        raise ValueError("explicit block number required")
    client = RpcClient(transport, expected_chain_id=target.chain_id)
    block = client.get_block(block_number)
    address = target.target_ca
    code = client.get_code(address, block)
    if code == "0x":
        raise ValueError("target runtime missing")
    decimals = _word(client.call(address, "0x" + keccak(b"decimals()")[:4].hex(), block))
    if decimals > 77:
        raise ValueError("unsupported token decimals")
    supply = _word(client.call(address, "0x" + keccak(b"totalSupply()")[:4].hex(), block))
    if client.get_block(block_number) != block:
        raise ValueError("reorg during identity acquisition")
    if len(recorder.exchanges) < 6:
        raise ValueError("raw RPC evidence missing")
    return IdentitySample(
        source,
        block,
        datetime.now(UTC).isoformat(),
        "0x" + keccak(bytes.fromhex(code[2:])).hex(),
        decimals,
        supply,
        recorder.exchanges,
        target.target_ca,
        target.reference_supply,
    )


def retain_identity(store: EvidenceStore, sample: IdentitySample) -> EvidenceRef:
    fact = {
        "schema": "oasis.launch-token-identity.v1",
        "target_ca": sample.target_ca,
        "runtime_keccak256": sample.runtime_hash,
        "token_decimals": sample.decimals,
        "local_supply_base_units": str(sample.local_supply_base_units),
        "reference_supply_tokens": sample.reference_supply,
        "execution_eligible": False,
        "control_plane": "unresolved",
    }
    return store.put(
        "launch_token_identity",
        canonical_json(fact),
        sample.block,
        sample.source,
        request=[
            {"method": e.method, "payload_hex": e.request_payload.hex()} for e in sample.exchanges
        ],
        raw_response=[
            {"method": e.method, "payload_hex": e.response_payload.hex()} for e in sample.exchanges
        ],
        interpretation=fact,
        retrieved_at=sample.retrieved_at,
    )


def identity_report(
    samples: tuple[IdentitySample, IdentitySample], references: tuple[EvidenceRef, EvidenceRef]
) -> dict[str, object]:
    left, right = samples
    agreement = (
        left.source != right.source
        and left.block == right.block
        and left.target_ca == right.target_ca
        and left.reference_supply == right.reference_supply
        and left.runtime_hash == right.runtime_hash
        and left.decimals == right.decimals
        and left.local_supply_base_units == right.local_supply_base_units
    )
    return {
        "schema": "oasis.launch-recon-report.v1",
        "execution_eligible": False,
        "identity_agreement": agreement,
        "decision": "observe" if agreement else "stand_down",
        "evidence": [asdict(ref) for ref in references],
        "unresolved": [
            "control_plane",
            "source_runtime_match",
            "holder_topology",
            "pool_and_route",
            "continuous_simulation",
            "economic_viability",
        ],
    }
