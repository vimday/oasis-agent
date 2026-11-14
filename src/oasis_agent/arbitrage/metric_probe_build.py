"""Bind an explicitly pinned local build artifact to the probe's complete immutable runtime."""

from eth_abi.abi import encode
from eth_hash.auto import keccak

from ..evidence.rpc_validation import strict_json_object, strict_list, strict_object
from ..models.common import sha256_hex
from .cycle_protocols import data_bytes, topic
from .metric_probe import PROBE_SIGNATURE, Plan, immutable_values

SOURCE = "src/research/MetricV3RevertingProbe.sol"
CONTRACT = "MetricV3RevertingProbe"
SAFECAST_SOURCE = "node_modules/@openzeppelin/contracts/utils/math/SafeCast.sol"
# Reviewed sources at 3983de1; these are provenance constraints, not a proof of compiler honesty.
SOURCE_HASHES = {
    SOURCE: "0xf2652275d024746e26902f90c4b6d113228dbee0374dcc180fa8cf776babfdd1",
    SAFECAST_SOURCE: "0xc8cae21c9ae4a46e5162ff9bf5b351d6fa6a6eba72d515f3bc1bdfeda7fdf083",
}


def materialize(plan: Plan, artifact_raw: bytes) -> bytes:
    runtime = build_runtime(plan, artifact_raw)
    if "0x" + keccak(runtime).hex() != plan.probe_runtime_hash:
        raise ValueError("probe_build_materialized_hash_mismatch")
    return runtime


def build_runtime(plan: Plan, artifact_raw: bytes) -> bytes:
    """Build the immutable runtime before its digest is fixed in a research plan."""
    if sha256_hex(artifact_raw) != plan.build_artifact_sha256:
        raise ValueError("probe_build_artifact_hash_mismatch")
    artifact = strict_json_object(artifact_raw)
    metadata = strict_object(artifact.get("metadata"), "build metadata")
    compiler = strict_object(metadata.get("compiler"), "compiler")
    settings = strict_object(metadata.get("settings"), "settings")
    sources = strict_object(metadata.get("sources"), "sources")
    if (
        compiler.get("version") != "0.8.26+commit.8a97fa7a"
        or settings.get("compilationTarget") != {SOURCE: CONTRACT}
        or settings.get("optimizer") != {"enabled": True, "runs": 200}
        or settings.get("evmVersion") != "cancun"
        or settings.get("metadata") != {"bytecodeHash": "none", "appendCBOR": False}
        or settings.get("libraries") != {}
        or set(sources) != set(SOURCE_HASHES)
        or any(
            strict_object(sources[n], "source").get("keccak256") != h
            for n, h in SOURCE_HASHES.items()
        )
    ):
        raise ValueError("probe_build_settings_or_sources_mismatch")
    methods = strict_object(artifact.get("methodIdentifiers"), "methods")
    if methods.get(PROBE_SIGNATURE) != topic(PROBE_SIGNATURE)[2:10]:
        raise ValueError("probe_build_method_mismatch")
    ast = strict_object(artifact.get("ast"), "AST")
    contracts = [
        n
        for n in strict_list(ast.get("nodes"), "AST nodes")
        if isinstance(n, dict)
        and n.get("nodeType") == "ContractDefinition"
        and n.get("name") == CONTRACT
    ]
    if ast.get("absolutePath") != SOURCE or len(contracts) != 1:
        raise ValueError("probe_build_ast_mismatch")
    values = immutable_values(plan)
    slots = {}
    for n in contracts[0].get("nodes", []):
        if not isinstance(n, dict) or n.get("mutability") != "immutable":
            continue
        name, identity = n.get("name"), n.get("id")
        if name not in values or type(identity) is not int or str(identity) in slots:
            raise ValueError("probe_build_immutable_declaration")
        if (
            strict_object(n.get("typeDescriptions"), "immutable type").get("typeString")
            != values[name][0]
        ):
            raise ValueError("probe_build_immutable_type")
        slots[str(identity)] = name
    if len(slots) != len(values) or set(slots.values()) != set(values):
        raise ValueError("probe_build_missing_immutable")
    deployed = strict_object(artifact.get("deployedBytecode"), "deployed bytecode")
    refs = strict_object(deployed.get("immutableReferences"), "immutable references")
    runtime = bytearray(data_bytes(deployed.get("object")))
    if (
        deployed.get("linkReferences") != {}
        or not 0 < len(runtime) <= 24576
        or set(refs) != set(slots)
    ):
        raise ValueError("probe_build_runtime_shape")
    occupied: set[int] = set()
    for identity, name in slots.items():
        spans = refs[identity]
        if not isinstance(spans, list) or not spans:
            raise ValueError("probe_build_missing_immutable_span")
        for span in spans:
            if not isinstance(span, dict) or set(span) != {"start", "length"}:
                raise ValueError("probe_build_immutable_span")
            start, size = span["start"], span["length"]
            if (
                type(start) is not int
                or type(size) is not int
                or size != 32
                or not 0 <= start <= len(runtime) - size
            ):
                raise ValueError("probe_build_immutable_span")
            offsets = set(range(start, start + size))
            if occupied & offsets or runtime[start : start + size] != bytes(32):
                raise ValueError("probe_build_overlapping_or_nonzero_placeholder")
            occupied |= offsets
            kind, value = values[name]
            runtime[start : start + size] = encode((kind,), (value,))
    return bytes(runtime)
