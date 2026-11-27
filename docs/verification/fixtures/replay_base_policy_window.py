"""Export the verified prospective Base capture as normalized policy observations."""

import argparse
from pathlib import Path

from run_base_prior_head_policy import ANCHORS, run

from oasis_agent.fastpath.base_policy import (
    BasePolicyCase,
    BasePolicySimulation,
    BasePolicyWindow,
    PreviousQuote,
)
from oasis_agent.fastpath.batch_shadow import BatchShadowHead
from oasis_agent.models.common import canonical_json, sha256_hex


def export(root: Path) -> BasePolicyWindow:
    panel = run(root, False)
    heads = [
        BatchShadowHead(
            height=row["head"]["height"],
            block_hash=row["head"]["block_hash"],
            parent_hash=row["head"]["parent_hash"],
            observed_ns=row["received_monotonic_ns"],
        )
        for row in panel["heads"]
    ]
    observations = {(row["state"], row["route"]): row for row in panel["rows"]}
    if len(observations) != len(panel["rows"]):
        raise ValueError("duplicate simulation evidence")
    cases = []
    for anchor in ANCHORS:
        if anchor >= len(heads):
            break
        previous, head = heads[anchor - 1 : anchor + 1]
        child = heads[anchor + 1] if anchor + 1 < len(heads) else None
        for name, envelope in panel["scope"]["envelopes"].items():
            digest = sha256_hex(canonical_json(envelope))
            quote = observations.get((previous.height, name))
            reference = (
                PreviousQuote(
                    head=previous,
                    envelope_sha256=digest,
                    available_ns=quote.get("ready_ns", quote["simulation_reply_ns"]),
                    meets_minimum_output=quote["success"],
                    modeled_net_wei=quote.get("net_wei"),
                )
                if quote is not None
                else None
            )
            cases.append(
                BasePolicyCase(
                    kind="control" if name == "minimum_output_control" else "fixed_route",
                    envelope_sha256=digest,
                    principal_wei=int(envelope["value"], 16),
                    head=head,
                    child=child,
                    previous_quote=reference,
                    head_simulation=_simulation(
                        head, observations.get((head.height, name)), digest
                    ),
                    child_simulation=(
                        _simulation(child, observations.get((child.height, name)), digest)
                        if child is not None
                        else None
                    ),
                )
            )
    return BasePolicyWindow(schema_version="oasis.base-shadow-policy-input.v1", cases=tuple(cases))


def _simulation(head, row, digest):
    if row is None:
        return None
    if (row["state_hash"], row["head_receive_ns"]) != (head.block_hash, head.observed_ns):
        raise ValueError("simulation and independent head observation differ")
    return BasePolicySimulation(
        head=head,
        envelope_sha256=digest,
        available_ns=(
            row.get("ready_ns", row["simulation_reply_ns"])
            if row["success"]
            else row["simulation_reply_ns"]
        ),
        disposition="settled" if row["success"] else "reverted",
        settlement_delta_wei=row["received_weth_wei"] - row["paid_wei"],
        modeled_network_fee_wei=row.get("network_fee_scenario_wei"),
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture_dir", type=Path)
    args = parser.parse_args()
    print(canonical_json(export(args.capture_dir).model_dump(mode="json")).decode())
