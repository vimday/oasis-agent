"""The operator CLI that retains one position's decision-block proof.

`assemble_lp_cycle.py` cannot run without the position's own two retained snapshots,
and the simulation plan that also retains them needs the cycle first. These tests
assert the producer that breaks that loop: the emitted scope is derived from the two
agreeing captures, the emitted object is exactly the shape the cycle request accepts,
and every identity the request declares is checked before the store is written to.
"""

import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.farming.position_capture import replay_position_snapshot_evidence
from oasis_agent.farming.position_quorum import PositionQuorum
from oasis_agent.models.common import canonical_json
from oasis_agent.models.position_scope import LpPositionScope
from scripts.assemble_lp_cycle import PositionRequest
from scripts.capture_lp_position import CaptureRequest, capture, endpoint_bindings
from tests.unit.test_lp_cycle_assembly import evidence_rows
from tests.unit.test_position_capture import snapshot_capture

ALIASES = ("public", "paid")


def other_address(address: str) -> str:
    """An address that is certainly not the declared one."""
    return "0x" + ("2" * 40 if address.lower() != "0x" + "2" * 40 else "3" * 40)


def sample() -> PositionQuorum:
    """A real two-provider quorum from the synthetic canonical position wire."""
    capture_a = snapshot_capture()
    return PositionQuorum(
        replace(capture_a, source=ALIASES[0]), replace(capture_a, source=ALIASES[1])
    )


def request_for(quorum: PositionQuorum, **overrides: object) -> CaptureRequest:
    snapshot = quorum.left.snapshot
    values: dict[str, object] = {
        "chain_id": snapshot.block.chain_id,
        "position_manager": snapshot.position_manager,
        "state_view": quorum.left.state_view,
        "token_id": snapshot.token_id,
        "block_number": snapshot.block.number,
        "sources": [
            {"alias": ALIASES[0], "env": "PRIMARY_RPC_URL"},
            {"alias": ALIASES[1], "env": "SECONDARY_RPC_URL"},
        ],
    }
    values.update(overrides)
    return CaptureRequest.model_validate_json(json.dumps(values))


def evidence(tmp_path: Path) -> Path:
    """An existing evidence database, as a window capture would have left behind."""
    path = tmp_path / "evidence.db"
    store = EvidenceStore(path)
    store.close()
    return path


def test_the_capture_derives_the_scope_and_emits_a_cycle_request_object(tmp_path: Path) -> None:
    quorum = sample()
    output = tmp_path / "position.json"
    path = evidence(tmp_path)
    capture(
        request_for(quorum),
        evidence_path=path,
        output_path=output,
        collect=lambda: quorum,
    )
    emitted = json.loads(output.read_bytes())
    assert emitted["schema"] == "oasis.lp-position-capture.v1"
    assert emitted["execution_eligible"] is False
    snapshot = quorum.left.snapshot
    assert emitted["block"]["block_hash"] == snapshot.block.block_hash
    assert emitted["position"]["scope"] == {
        "chain_id": snapshot.block.chain_id,
        "position_manager": snapshot.position_manager,
        "token_id": snapshot.token_id,
        "pool_id": snapshot.pool_id,
        "owner": snapshot.nft_owner,
    }
    # The emitted object is what the cycle assembler consumes, verbatim.
    accepted = PositionRequest.from_json(canonical_json(emitted["position"]))
    assert accepted.scope == LpPositionScope(**emitted["position"]["scope"])
    assert [reference.source for reference in accepted.references] == list(ALIASES)
    store = EvidenceStore(path)
    try:
        assert [
            replay_position_snapshot_evidence(store, reference).snapshot
            for reference in accepted.references
        ] == [snapshot, snapshot]
    finally:
        store.close()


@pytest.mark.parametrize(
    "mutate,expected",
    [
        pytest.param(
            lambda quorum: {"chain_id": quorum.left.snapshot.block.chain_id + 1},
            "requested chain and height",
            id="foreign-chain",
        ),
        pytest.param(
            lambda quorum: {"block_number": quorum.left.snapshot.block.number + 1},
            "requested chain and height",
            id="other-height",
        ),
        pytest.param(
            lambda quorum: {
                "position_manager": other_address(quorum.left.snapshot.position_manager)
            },
            "requested manager",
            id="other-manager",
        ),
        pytest.param(
            lambda quorum: {"token_id": quorum.left.snapshot.token_id + 1},
            "requested token id",
            id="other-token",
        ),
        pytest.param(
            lambda quorum: {"state_view": other_address(quorum.left.state_view)},
            "requested state view",
            id="other-state-view",
        ),
        pytest.param(
            lambda quorum: {
                "sources": [
                    {"alias": "other", "env": "PRIMARY_RPC_URL"},
                    {"alias": ALIASES[1], "env": "SECONDARY_RPC_URL"},
                ]
            },
            "declared aliases",
            id="other-aliases",
        ),
    ],
)
def test_a_capture_that_is_not_the_declared_request_retains_nothing(
    tmp_path: Path, mutate: Callable[[PositionQuorum], dict[str, object]], expected: str
) -> None:
    """A proof of some other NFT, block, view or provider pair is not this request."""
    quorum = sample()
    output = tmp_path / "position.json"
    path = evidence(tmp_path)
    with pytest.raises(ValueError, match=expected):
        capture(
            request_for(quorum, **mutate(quorum)),
            evidence_path=path,
            output_path=output,
            collect=lambda: quorum,
        )
    assert not output.exists()
    assert evidence_rows(path) == 0


def test_a_capture_refuses_to_overwrite_or_to_invent_its_store(tmp_path: Path) -> None:
    quorum = sample()
    path = evidence(tmp_path)
    output = tmp_path / "position.json"
    output.write_bytes(b"{}\n")
    with pytest.raises(ValueError, match="overwrite a retained capture"):
        capture(
            request_for(quorum),
            evidence_path=path,
            output_path=output,
            collect=lambda: quorum,
        )
    assert output.read_bytes() == b"{}\n"
    absent = tmp_path / "absent" / "position.json"
    with pytest.raises(ValueError, match="existing evidence database"):
        capture(
            request_for(quorum),
            evidence_path=tmp_path / "absent.db",
            output_path=absent,
            collect=lambda: quorum,
        )
    assert not absent.exists()


def test_the_request_refuses_a_shared_alias_or_a_key_shaped_endpoint(tmp_path: Path) -> None:
    quorum = sample()
    for sources in (
        [
            {"alias": ALIASES[0], "env": "PRIMARY_RPC_URL"},
            {"alias": ALIASES[0], "env": "SECONDARY_RPC_URL"},
        ],
        [
            {"alias": ALIASES[0], "env": "PRIMARY_RPC_URL"},
            {"alias": ALIASES[1], "env": "PRIMARY_RPC_URL"},
        ],
        [
            {"alias": ALIASES[0], "env": "PRIMARY_RPC_URL"},
            {"alias": ALIASES[1], "env": "SECONDARY_RPC_KEY"},
        ],
        [
            {"alias": ALIASES[0], "env": "PRIMARY_RPC_URL"},
            {"alias": ALIASES[1], "env": "secondary_endpoint"},
        ],
    ):
        with pytest.raises(ValueError):
            request_for(quorum, sources=sources)


def test_the_alias_binding_reads_only_the_declared_endpoints(tmp_path: Path) -> None:
    """An alias is paired with a declared variable, and both must be distinct."""
    quorum = sample()
    env = tmp_path / ".env"
    env.write_text(
        "PRIMARY_RPC_URL=https://primary.invalid\n"
        "SECONDARY_RPC_URL=https://secondary.invalid\n"
        "UNRELATED_KEY=unused\n"
    )
    assert endpoint_bindings(request_for(quorum).sources, env) == {
        ALIASES[0]: "https://primary.invalid",
        ALIASES[1]: "https://secondary.invalid",
    }
    env.write_text("PRIMARY_RPC_URL=https://same.invalid\nSECONDARY_RPC_URL=https://same.invalid\n")
    with pytest.raises(ValueError, match="two configured RPC endpoints"):
        endpoint_bindings(request_for(quorum).sources, env)
    env.write_text("PRIMARY_RPC_URL=https://primary.invalid\n")
    with pytest.raises(ValueError, match="two configured RPC endpoints"):
        endpoint_bindings(request_for(quorum).sources, env)
