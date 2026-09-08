import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from oasis_agent.evidence.deployment_identity import (
    ContractTarget,
    collect_v4_deployment_identity,
    persist_v4_deployment_identity,
    require_deployment_identity_agreement,
    validate_deployment_identity_exchanges,
)
from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.models.chain import BlockSnapshot
from oasis_agent.models.common import BlockRef
from oasis_agent.models.v4 import DYNAMIC_FEE_FLAG, PoolKey
from oasis_agent.rpc.transport import RpcExchange


class FakeIdentityRpc:
    def __init__(
        self, *, mutate_closing_block: bool = False, empty_role: str | None = None
    ) -> None:
        self.snapshot = BlockSnapshot(
            BlockRef(4663, 42, "0x" + "aa" * 32, 1_000),
            "0x" + "bb" * 32,
        )
        self.mutate_closing_block = mutate_closing_block
        self.empty_role = empty_role
        self.block_reads = 0
        self.calls: list[tuple[str, BlockRef]] = []

    def chain_id(self) -> int:
        return 4663

    def get_block_snapshot(self, block: int | str = "latest") -> BlockSnapshot:
        self.block_reads += 1
        if self.mutate_closing_block and self.block_reads == 2:
            return BlockSnapshot(
                replace(self.snapshot.ref, block_hash="0x" + "cc" * 32),
                self.snapshot.parent_hash,
            )
        return self.snapshot

    def get_code(self, address: str, block: int | str | BlockRef = "latest") -> str:
        assert isinstance(block, BlockRef)
        self.calls.append((address, block))
        if self.empty_role is not None and address == ADDRESSES[self.empty_role]:
            return "0x"
        return "0x60" + address[-2:]


ADDRESSES = {
    "pool_manager": "0x" + "11" * 20,
    "position_manager": "0x" + "22" * 20,
    "hook": "0x" + "33" * 20,
    "state_view": "0x" + "44" * 20,
}


def pool_key() -> PoolKey:
    return PoolKey(
        "0x" + "00" * 20,
        "0x" + "55" * 20,
        DYNAMIC_FEE_FLAG,
        60,
        ADDRESSES["hook"],
    )


def targets() -> tuple[ContractTarget, ...]:
    return tuple(ContractTarget(role, address) for role, address in reversed(ADDRESSES.items()))


def exchange(
    method: str, request_id: int, params: list[object] | None = None, result: object = "0x"
) -> RpcExchange:
    request = json.dumps(
        {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or []}
    ).encode()
    response = json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}).encode()
    return RpcExchange(method, request_id, request, response, 1)


def valid_exchanges() -> tuple[RpcExchange, ...]:
    snapshot = FakeIdentityRpc().snapshot
    header = {
        "number": hex(snapshot.ref.number),
        "hash": snapshot.ref.block_hash,
        "parentHash": snapshot.parent_hash,
        "timestamp": hex(snapshot.ref.timestamp or 0),
    }
    return (
        exchange("eth_chainId", 1, [], "0x1237"),
        exchange("eth_getBlockByNumber", 2, ["0x2a", False], header),
        *(
            exchange(
                "eth_getCode",
                i,
                [address, {"blockHash": snapshot.ref.block_hash, "requireCanonical": True}],
                "0x60" + address[-2:],
            )
            for i, address in enumerate(sorted(ADDRESSES.values()), 3)
        ),
        exchange("eth_getBlockByNumber", 7, ["0x2a", False], header),
    )


class DeploymentIdentityTests(unittest.TestCase):
    def test_collects_sorted_hashes_at_one_stable_block(self) -> None:
        rpc = FakeIdentityRpc()
        dossier = collect_v4_deployment_identity(
            rpc,
            source="provider-a",
            block=42,
            pool_key=pool_key(),
            targets=targets(),
        )
        self.assertEqual(tuple(item.role for item in dossier.contracts), tuple(sorted(ADDRESSES)))
        self.assertEqual(len(rpc.calls), 4)
        self.assertTrue(all(block == dossier.snapshot.ref for _, block in rpc.calls))
        self.assertTrue(all(item.byte_length == 2 for item in dossier.contracts))
        self.assertEqual(len(dossier.content_hash), 64)
        self.assertNotIn(b"provider-a", dossier.payload)

    def test_rejects_empty_code_and_mid_collection_reorg(self) -> None:
        with self.assertRaisesRegex(ValueError, "empty runtime"):
            collect_v4_deployment_identity(
                FakeIdentityRpc(empty_role="hook"),
                source="provider-a",
                block=42,
                pool_key=pool_key(),
                targets=targets(),
            )
        with self.assertRaisesRegex(ValueError, "changed during collection"):
            collect_v4_deployment_identity(
                FakeIdentityRpc(mutate_closing_block=True),
                source="provider-a",
                block=42,
                pool_key=pool_key(),
                targets=targets(),
            )

    def test_requires_core_roles_unique_roles_and_matching_hook(self) -> None:
        incomplete = tuple(item for item in targets() if item.role != "position_manager")
        with self.assertRaisesRegex(ValueError, "missing required roles"):
            collect_v4_deployment_identity(
                FakeIdentityRpc(),
                source="provider-a",
                block=42,
                pool_key=pool_key(),
                targets=incomplete,
            )
        duplicate = (*targets(), ContractTarget("hook", ADDRESSES["hook"]))
        with self.assertRaisesRegex(ValueError, "unique roles"):
            collect_v4_deployment_identity(
                FakeIdentityRpc(),
                source="provider-a",
                block=42,
                pool_key=pool_key(),
                targets=duplicate,
            )
        wrong_key = replace(pool_key(), hooks="0x" + "66" * 20)
        with self.assertRaisesRegex(ValueError, "hook identity"):
            collect_v4_deployment_identity(
                FakeIdentityRpc(),
                source="provider-a",
                block=42,
                pool_key=wrong_key,
                targets=targets(),
            )

    def test_provider_agreement_excludes_provider_name_but_detects_code_drift(self) -> None:
        first = collect_v4_deployment_identity(
            FakeIdentityRpc(),
            source="provider-a",
            block=42,
            pool_key=pool_key(),
            targets=targets(),
        )
        second = replace(first, source="provider-b")
        agreement = require_deployment_identity_agreement((first, second))
        self.assertEqual(agreement.sources, ("provider-a", "provider-b"))

        changed_contract = replace(first.contracts[0], runtime_sha256="1" * 64)
        conflicting = replace(second, contracts=(changed_contract, *second.contracts[1:]))
        with self.assertRaisesRegex(ValueError, "disagree"):
            require_deployment_identity_agreement((first, conflicting))

    def test_persists_manifest_and_exact_rpc_envelopes(self) -> None:
        dossier = collect_v4_deployment_identity(
            FakeIdentityRpc(),
            source="provider-a",
            block=42,
            pool_key=pool_key(),
            targets=targets(),
        )
        exchanges = valid_exchanges()
        with tempfile.TemporaryDirectory() as directory:
            store = EvidenceStore(Path(directory) / "evidence.db")
            reference = persist_v4_deployment_identity(
                store,
                dossier,
                exchanges,
                retrieved_at="2026-09-05T00:00:00+00:00",
            )
            record = store.get_record(reference)
            self.assertEqual(record.payload, dossier.payload)
            self.assertEqual(reference.block, dossier.snapshot.ref)
            self.assertEqual(reference.kind, "v4_deployment_identity")
            assert record.raw_response_payload is not None
            self.assertEqual(len(json.loads(record.raw_response_payload)["exchanges"]), 7)
            store.close()

    def test_method_counts_cannot_attest_unrelated_rpc_payloads(self) -> None:
        dossier = collect_v4_deployment_identity(
            FakeIdentityRpc(),
            source="provider-a",
            block=42,
            pool_key=pool_key(),
            targets=targets(),
        )
        fabricated = (
            exchange("eth_getBlockByNumber", 1),
            *(exchange("eth_getCode", i) for i in range(2, 6)),
            exchange("eth_getBlockByNumber", 6),
        )
        with tempfile.TemporaryDirectory() as directory:
            store = EvidenceStore(Path(directory) / "evidence.db")
            with self.assertRaises(ValueError):
                persist_v4_deployment_identity(store, dossier, fabricated)
            store.close()

    def test_refuses_incomplete_rpc_evidence(self) -> None:
        dossier = collect_v4_deployment_identity(
            FakeIdentityRpc(),
            source="provider-a",
            block=42,
            pool_key=pool_key(),
            targets=targets(),
        )
        with tempfile.TemporaryDirectory() as directory:
            store = EvidenceStore(Path(directory) / "evidence.db")
            with self.assertRaisesRegex(ValueError, "every contract"):
                persist_v4_deployment_identity(
                    store,
                    dossier,
                    (
                        exchange("eth_getBlockByNumber", 1),
                        exchange("eth_getBlockByNumber", 2),
                    ),
                )
            store.close()

    def test_rejects_wrong_contract_block_code_and_ambiguous_envelopes(self) -> None:
        dossier = collect_v4_deployment_identity(
            FakeIdentityRpc(),
            source="provider-a",
            block=42,
            pool_key=pool_key(),
            targets=targets(),
        )
        original = valid_exchanges()
        code = original[2]
        request = json.loads(code.request_payload)
        response = json.loads(code.response_payload)

        def req(**fields: object) -> RpcExchange:
            return replace(code, request_payload=json.dumps({**request, **fields}).encode())

        def res(**fields: object) -> RpcExchange:
            return replace(code, response_payload=json.dumps({**response, **fields}).encode())

        mutations = {
            "wrong address": req(params=["0x" + "99" * 20, request["params"][1]]),
            "latest code": req(params=[request["params"][0], "latest"]),
            "other block": req(
                params=[
                    request["params"][0],
                    {"blockHash": "0x" + "dd" * 32, "requireCanonical": True},
                ]
            ),
            "noncanonical allowed": req(
                params=[
                    request["params"][0],
                    {"blockHash": dossier.snapshot.ref.block_hash, "requireCanonical": False},
                ]
            ),
            "truthy canonical flag": req(
                params=[
                    request["params"][0],
                    {"blockHash": dossier.snapshot.ref.block_hash, "requireCanonical": 1},
                ]
            ),
            "wrong method label": replace(code, method="eth_chainId"),
            "wrong response id": res(id=999),
            "boolean response id": res(id=True),
            "changed code": res(result="0x6000"),
            "empty code": res(result="0x"),
            "nonstring code": res(result=0),
            "result and error": res(error={"code": -32005, "message": "retry"}),
            "duplicate JSON field": replace(
                code,
                response_payload=(b'{"jsonrpc":"2.0","id":3,"result":"0x6000","result":"0x6011"}'),
            ),
        }
        for name, mutated in mutations.items():
            with self.subTest(case=name), self.assertRaises(ValueError):
                validate_deployment_identity_exchanges(
                    dossier, (*original[:2], mutated, *original[3:])
                )

        variants = {
            "missing chain": original[1:],
            "wrong chain": (exchange("eth_chainId", 1, [], "0x1"), *original[1:]),
            "duplicate request": (*original[:3], code, *original[4:]),
            "closing before code": (*original[:2], original[-1], *original[2:-1]),
            "same contract repeated": (
                *original[:3],
                replace(
                    code,
                    request_id=4,
                    request_payload=json.dumps({**request, "id": 4}).encode(),
                    response_payload=json.dumps({**response, "id": 4}).encode(),
                ),
                *original[4:],
            ),
        }
        for name, variant in variants.items():
            with self.subTest(case=name), self.assertRaises(ValueError):
                validate_deployment_identity_exchanges(dossier, variant)

    def test_retry_errors_are_retained_but_never_substitute_for_success(self) -> None:
        dossier = collect_v4_deployment_identity(
            FakeIdentityRpc(),
            source="provider-a",
            block=42,
            pool_key=pool_key(),
            targets=targets(),
        )
        original = valid_exchanges()
        request = json.loads(original[2].request_payload)
        error = replace(
            original[2],
            request_id=20,
            request_payload=json.dumps({**request, "id": 20}).encode(),
            response_payload=json.dumps(
                {"jsonrpc": "2.0", "id": 20, "error": {"code": -32005, "message": "rate limited"}}
            ).encode(),
        )
        validate_deployment_identity_exchanges(dossier, (*original[:2], error, *original[2:]))
        with self.assertRaisesRegex(ValueError, "incomplete successful"):
            validate_deployment_identity_exchanges(dossier, (*original[:2], error, *original[3:]))


if __name__ == "__main__":
    unittest.main()
