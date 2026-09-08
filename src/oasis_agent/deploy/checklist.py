"""Deployment identity checklist: observe-only pre-deployment identity verification.

This module classifies already-retrieved, block-pinned identity facts for an
explicitly selected EVM chain. It mirrors the :mod:`oasis_agent.evidence.pool_access`
taxonomy (PASS / FAIL / UNKNOWN) and requires every confirmed fact to carry
complete, block-pinned :class:`~oasis_agent.models.common.EvidenceRef` provenance
(chain id, block number and hash, address, runtime bytecode keccak, RPC source,
retrieval time, request/raw/interpretation hashes).

A :class:`DeploymentChecklist` fails closed: ``identities_verified`` is True only
when every canonical check passes. It never authorizes execution: the checklist is
observe-only tooling whose output is evidence for the operator and the authorized
release review process, and ``execution_eligible`` is always False.

RPC I/O lives in ``scripts/run_deploy_checklist.py``; this module is pure and
deterministic.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..evidence.pool_access import CheckStatus
from ..models.common import EvidenceRef

# Canonical pre-release deployment identity checks. The names are the assertion itself;
# the set must match exactly, so a forgotten or invented check cannot pass.
REQUIRED_DEPLOY_CHECKS = frozenset(
    {
        "chain_id",
        "pool_manager_identity",
        "position_manager_identity",
        "state_view_identity",
        "permit2_identity",
        "hook_identity",
        "hook_permission_bits",
        "hook_bindings_and_state",
        "executor_identity",
        "executor_bindings_and_state",
        "pool_id_and_initialization",
        "token0_identity",
        "token1_identity",
        "provider_diversity",
    }
)

# beforeInitialize | beforeAddLiquidity | beforeSwap, mirroring
# OasisHookFactory.REQUIRED_HOOK_FLAGS (Hooks library bit positions 13/11/7).
EXPECTED_HOOK_PERMISSION_BITS = 0x2880


@dataclass(frozen=True, slots=True)
class DeployCheck:
    name: str
    status: CheckStatus
    evidence: tuple[EvidenceRef, ...]
    detail: str

    def __post_init__(self) -> None:
        if not self.name.strip() or not self.detail.strip():
            raise ValueError("deploy checks require a name and detail")
        if not isinstance(self.status, CheckStatus):
            raise TypeError("typed deploy check status required")
        if self.status is not CheckStatus.UNKNOWN and not self.evidence:
            raise ValueError("confirmed deploy checks require retained evidence")
        if self.status is not CheckStatus.UNKNOWN:
            sources: set[str] = set()
            interpretations: set[tuple[str, str]] = set()
            for reference in self.evidence:
                if (
                    reference.block is None
                    or not reference.request_hash
                    or not reference.raw_response_hash
                    or not reference.interpretation_hash
                ):
                    raise ValueError("deploy evidence must be complete and block-pinned")
                sources.add(reference.source)
                interpretations.add((reference.content_hash, reference.interpretation_hash))
            if len(sources) < 2 or len(interpretations) != 1:
                raise ValueError(
                    "confirmed deploy checks require two providers with one interpretation"
                )


@dataclass(frozen=True, slots=True)
class DeploymentChecklist:
    checks: tuple[DeployCheck, ...]
    identities_verified: bool = field(init=False)
    reasons: tuple[str, ...] = field(init=False)
    execution_eligible: bool = field(init=False)

    def __post_init__(self) -> None:
        names = tuple(item.name for item in self.checks)
        if len(names) != len(set(names)) or set(names) != REQUIRED_DEPLOY_CHECKS:
            raise ValueError("deployment checklist requires exactly the canonical checks")
        reasons = tuple(
            f"{item.name}: {item.detail}"
            for item in self.checks
            if item.status is not CheckStatus.PASS
        )
        object.__setattr__(self, "reasons", reasons)
        # Fail closed: any non-pass check means identities are not verified.
        object.__setattr__(self, "identities_verified", not reasons)
        # Observe-only identity verification; it never authorizes execution.
        object.__setattr__(self, "execution_eligible", False)


__all__ = [
    "EXPECTED_HOOK_PERMISSION_BITS",
    "REQUIRED_DEPLOY_CHECKS",
    "CheckStatus",
    "DeployCheck",
    "DeploymentChecklist",
]
