from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from eth_abi.abi import encode

from oasis_agent.deploy.checklist import (
    EXPECTED_HOOK_PERMISSION_BITS,
    REQUIRED_DEPLOY_CHECKS,
    CheckStatus,
    DeployCheck,
    DeploymentChecklist,
)
from oasis_agent.evidence.pool_access import CheckStatus as PoolCheckStatus
from oasis_agent.models.common import BlockRef, EvidenceRef
from scripts.run_deploy_checklist import (
    IdentityConfig,
    PublicPoster,
    RpcEndpoint,
    _decode_address,
    _decode_bool,
    _decode_hook_fee,
    _decode_hook_permission_bits,
    _decode_slot0,
    _decode_uint8,
    load_identity_config,
)


def block() -> BlockRef:
    return BlockRef(4663, 56_905_986, "0x" + "aa" * 32, 1_700_000_000)


def evidence(source: str = "provider-a", raw_hash: str = "33" * 32) -> EvidenceRef:
    return EvidenceRef(
        kind="deployment_identity",
        content_hash="11" * 32,
        block=block(),
        source=source,
        retrieved_at="2026-09-08T00:00:00+00:00",
        request_hash="22" * 32,
        raw_response_hash=raw_hash,
        interpretation_hash="44" * 32,
    )


def check(name: str, status: CheckStatus = CheckStatus.PASS) -> DeployCheck:
    refs = () if status is CheckStatus.UNKNOWN else (evidence(), evidence("provider-b", "55" * 32))
    return DeployCheck(name=name, status=status, evidence=refs, detail=f"{name} detail")


def passing_checks() -> tuple[DeployCheck, ...]:
    return tuple(check(name) for name in sorted(REQUIRED_DEPLOY_CHECKS))


class DeployCheckValidationTests(unittest.TestCase):
    def test_requires_name_and_detail(self) -> None:
        with self.assertRaisesRegex(ValueError, "name and detail"):
            DeployCheck(name="  ", status=CheckStatus.UNKNOWN, evidence=(), detail="x")
        with self.assertRaisesRegex(ValueError, "name and detail"):
            DeployCheck(name="chain_id", status=CheckStatus.UNKNOWN, evidence=(), detail=" ")
        with self.assertRaisesRegex(ValueError, "name and detail"):
            DeployCheck(name="chain_id", status=CheckStatus.UNKNOWN, evidence=(), detail="")

    def test_requires_typed_status(self) -> None:
        with self.assertRaises(TypeError):
            DeployCheck(name="chain_id", status="pass", evidence=(), detail="x")  # type: ignore[arg-type]

    def test_confirmed_checks_require_evidence(self) -> None:
        for status in (CheckStatus.PASS, CheckStatus.FAIL):
            with (
                self.subTest(status=status),
                self.assertRaisesRegex(ValueError, "retained evidence"),
            ):
                DeployCheck(name="chain_id", status=status, evidence=(), detail="x")

    def test_confirmed_checks_require_complete_block_pinned_evidence(self) -> None:
        ref = evidence()
        mutations = {
            "missing block": replace(ref, block=None),
            "missing request hash": replace(ref, request_hash=""),
            "missing raw response hash": replace(ref, raw_response_hash=""),
            "missing interpretation hash": replace(ref, interpretation_hash=""),
        }
        for name, mutated in mutations.items():
            with (
                self.subTest(case=name),
                self.assertRaisesRegex(ValueError, "complete and block-pinned"),
            ):
                DeployCheck(
                    name="chain_id",
                    status=CheckStatus.PASS,
                    evidence=(mutated,),
                    detail="x",
                )

    def test_unknown_may_carry_evidence_or_none(self) -> None:
        DeployCheck(name="chain_id", status=CheckStatus.UNKNOWN, evidence=(), detail="x")
        DeployCheck(name="chain_id", status=CheckStatus.UNKNOWN, evidence=(evidence(),), detail="x")

    def test_confirmed_check_rejects_single_provider_or_disagreement(self) -> None:
        with self.assertRaisesRegex(ValueError, "two providers"):
            DeployCheck(
                name="chain_id",
                status=CheckStatus.PASS,
                evidence=(evidence(),),
                detail="x",
            )
        with self.assertRaisesRegex(ValueError, "one interpretation"):
            DeployCheck(
                name="chain_id",
                status=CheckStatus.PASS,
                evidence=(
                    evidence(),
                    replace(
                        evidence("provider-b", "55" * 32),
                        interpretation_hash="66" * 32,
                    ),
                ),
                detail="x",
            )

    def test_expected_permission_bits_match_hook_flags(self) -> None:
        # beforeInitialize | beforeAddLiquidity | beforeSwap per OasisHookFactory.
        self.assertEqual(EXPECTED_HOOK_PERMISSION_BITS, 0x2880)

    def test_hook_permission_struct_is_abi_decoded_to_address_bits(self) -> None:
        permissions = (True, False, True, False, False, False, True, *([False] * 7))
        encoded = "0x" + encode(["bool"] * 14, permissions).hex()
        self.assertEqual(_decode_hook_permission_bits(encoded), EXPECTED_HOOK_PERMISSION_BITS)
        with self.assertRaisesRegex(ValueError, "ABI length"):
            _decode_hook_permission_bits("0x00")

    def test_token_and_slot0_results_are_strictly_abi_decoded(self) -> None:
        self.assertEqual(_decode_uint8("0x" + encode(["uint8"], [18]).hex(), "decimals"), 18)
        with self.assertRaisesRegex(ValueError, "one ABI word"):
            _decode_uint8("0x12", "decimals")
        with self.assertRaisesRegex(ValueError, "fit uint8"):
            _decode_uint8("0x" + encode(["uint256"], [256]).hex(), "decimals")

        slot0 = "0x" + encode(["uint160", "int24", "uint24", "uint24"], [2**96, -1, 0, 3_000]).hex()
        self.assertEqual(
            _decode_slot0(slot0),
            {"sqrt_price_x96": 2**96, "tick": -1, "protocol_fee": 0, "lp_fee": 3_000},
        )
        with self.assertRaisesRegex(ValueError, "ABI length"):
            _decode_slot0("0x" + encode(["uint160"], [2**96]).hex())

    def test_contract_state_results_require_canonical_abi(self) -> None:
        address = "0x" + "12" * 20
        self.assertEqual(
            _decode_address("0x" + encode(["address"], [address]).hex(), "owner"), address
        )
        self.assertTrue(_decode_bool("0x" + encode(["bool"], [True]).hex(), "paused"))
        with self.assertRaisesRegex(ValueError, "padding"):
            _decode_address("0x" + "01" * 12 + "12" * 20, "owner")
        with self.assertRaisesRegex(ValueError, "canonical ABI bool"):
            _decode_bool("0x" + encode(["uint256"], [2]).hex(), "paused")

    def test_hook_fee_config_is_strictly_decoded(self) -> None:
        values = [500, 1_500, 3_000, 10, 100, 20, 200, 7, True]
        encoded = (
            "0x"
            + encode(
                [
                    "uint24",
                    "uint24",
                    "uint24",
                    "uint128",
                    "uint128",
                    "uint128",
                    "uint128",
                    "uint64",
                    "bool",
                ],
                values,
            ).hex()
        )
        self.assertEqual(
            _decode_hook_fee(encoded),
            {
                "base_fee": 500,
                "medium_fee": 1_500,
                "high_fee": 3_000,
                "medium_amount0_threshold": 10,
                "high_amount0_threshold": 100,
                "medium_amount1_threshold": 20,
                "high_amount1_threshold": 200,
                "rule_version": 7,
            },
        )
        unconfigured = (
            "0x"
            + encode(
                [
                    "uint24",
                    "uint24",
                    "uint24",
                    "uint128",
                    "uint128",
                    "uint128",
                    "uint128",
                    "uint64",
                    "bool",
                ],
                [*values[:-1], False],
            ).hex()
        )
        with self.assertRaisesRegex(ValueError, "not configured"):
            _decode_hook_fee(unconfigured)


class IdentityConfigTests(unittest.TestCase):
    def test_resolves_named_rpc_from_environment_without_putting_secret_in_config(self) -> None:
        payload = {
            "hook": "0x" + "11" * 20,
            "token0": "0x" + "00" * 20,
            "token1": "0x" + "22" * 20,
            "expected_hashes": {"hook": "0x" + "33" * 32},
            "rpc_extra": [{"name": "provider-b", "url_env": "ROBINHOOD_RPC_B"}],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "identity.json"
            path.write_text(json.dumps(payload))
            self.assertNotIn("secret-value", path.read_text())
            config = load_identity_config(
                path, {"ROBINHOOD_RPC_B": "https://provider.example/v2/secret-value"}
            )
        self.assertEqual(
            config.rpc_extra,
            (RpcEndpoint("provider-b", "https://provider.example/v2/secret-value"),),
        )

    def test_rejects_inline_rpc_urls_and_missing_environment_variable(self) -> None:
        payload = {"rpc_extra": ["https://provider.example/secret"]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "identity.json"
            path.write_text(json.dumps(payload))
            with self.assertRaisesRegex(ValueError, "exactly name and url_env"):
                load_identity_config(path, {})
            path.write_text(json.dumps({"rpc_extra": [{"name": "provider-b", "url_env": "RPC_B"}]}))
            with self.assertRaisesRegex(ValueError, "environment variable is missing: RPC_B"):
                load_identity_config(path, {})

    def test_config_requires_dynamic_fee_and_normalizes_expected_hashes(self) -> None:
        config = IdentityConfig(expected_hashes={"HOOK": "0x" + "AB" * 32})
        self.assertEqual(config.expected_hashes["hook"], "0x" + "ab" * 32)
        with self.assertRaisesRegex(ValueError, "dynamic fee"):
            IdentityConfig(fee=3_000)
        with self.assertRaisesRegex(ValueError, "unknown expected hash role"):
            IdentityConfig(expected_hashes={"router": "0x" + "11" * 32})

    def test_config_requires_separate_control_roles(self) -> None:
        role = "0x" + "11" * 20
        with self.assertRaisesRegex(ValueError, "must be separate"):
            IdentityConfig(governor=role, guardian=role)

    def test_rpc_transport_error_redacts_endpoint(self) -> None:
        endpoint = "https://provider.example/v2/sensitive-api-key"
        error = subprocess.CalledProcessError(22, ["curl", endpoint])
        with (
            patch("scripts.run_deploy_checklist.subprocess.run", side_effect=error),
            self.assertRaisesRegex(OSError, "RPC HTTPS request failed") as caught,
        ):
            PublicPoster(endpoint).post(b"{}")
        self.assertNotIn("sensitive-api-key", str(caught.exception))


class DeploymentChecklistTests(unittest.TestCase):
    def test_requires_exactly_the_canonical_checks(self) -> None:
        with self.assertRaisesRegex(ValueError, "exactly the canonical checks"):
            DeploymentChecklist(checks=passing_checks()[:-1])
        with self.assertRaisesRegex(ValueError, "exactly the canonical checks"):
            DeploymentChecklist(checks=(*passing_checks(), check("invented_check")))
        duplicate = passing_checks() + (check("chain_id"),)
        with self.assertRaisesRegex(ValueError, "exactly the canonical checks"):
            DeploymentChecklist(checks=duplicate)

    def test_identities_verified_only_when_every_check_passes(self) -> None:
        checklist = DeploymentChecklist(checks=passing_checks())
        self.assertTrue(checklist.identities_verified)
        self.assertEqual(checklist.reasons, ())
        self.assertFalse(checklist.execution_eligible)

    def test_any_non_pass_fails_closed_with_reasons(self) -> None:
        names = sorted(REQUIRED_DEPLOY_CHECKS)
        for index in range(len(names)):
            mutated = list(passing_checks())
            mutated[index] = check(names[index], CheckStatus.UNKNOWN)
            checklist = DeploymentChecklist(checks=tuple(mutated))
            self.assertFalse(checklist.identities_verified, msg=names[index])
            self.assertEqual(len(checklist.reasons), 1)
            self.assertIn(names[index], checklist.reasons[0])

    def test_fail_status_also_fails_closed(self) -> None:
        mutated = list(passing_checks())
        mutated[0] = check("chain_id", CheckStatus.FAIL)
        checklist = DeploymentChecklist(checks=tuple(mutated))
        self.assertFalse(checklist.identities_verified)
        self.assertIn("chain_id", checklist.reasons[0])

    def test_execution_eligible_is_always_false(self) -> None:
        self.assertFalse(DeploymentChecklist(checks=passing_checks()).execution_eligible)
        mutated = list(passing_checks())
        mutated[0] = check("chain_id", CheckStatus.UNKNOWN)
        self.assertFalse(DeploymentChecklist(checks=tuple(mutated)).execution_eligible)

    def test_check_status_is_the_pool_access_taxonomy(self) -> None:
        # The deployment checklist must share the evidence taxonomy with pool access.
        self.assertIs(CheckStatus, PoolCheckStatus)
        self.assertEqual({item.value for item in CheckStatus}, {"pass", "fail", "unknown"})


if __name__ == "__main__":
    unittest.main()
