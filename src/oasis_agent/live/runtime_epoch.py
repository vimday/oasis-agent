"""Pure, exact five-to-eight-pin journal epoch; never approves or acquires runtime.

The explicit caller must first re-derive the retained global review from raw
proofs. Journal replay checks its binding and the sole permitted state delta.
"""

from dataclasses import asdict
from typing import Any

from ..models.common import canonical_json, evidence_ref_from_json, sha256_hex
from .config import LiveConfig, PublicUsdConfig
from .config_epochs import retained_config
from .feed_epoch import checkpoint_snapshot
from .gas_oracle import GAS_PIN_NAMES, LEGACY_PIN_NAMES, require_gas_oracle_pins

SCHEMA = "oasis.runtime-epoch.v1"
SOURCES = ("live_primary", "live_secondary")


def transition_snapshot(
    prior: dict[str, Any], record: dict[str, Any], *, binding: str, parent: str
) -> tuple[str, dict[str, Any]]:
    if (
        set(record)
        != {
            "schema",
            "source_tip",
            "source_state_hash",
            "target_config",
            "service",
            "references",
            "review",
            "approved_review_hash",
        }
        or record["schema"] != SCHEMA
    ):
        raise ValueError("exact runtime epoch record required")
    if (
        record["source_tip"] != parent
        or record["source_state_hash"] != sha256_hex(canonical_json(prior))
        or record["service"] not in {"strategy", "authority"}
        or prior.get("mode") != "live"
        or not isinstance(prior.get("halted"), str)
        or not prior["halted"]
        or prior.get("pending") is not None
        or prior.get("signing_attempt") is not None
        or prior.get("acknowledge_flow") is not False
        or prior.get("legacy_gas_cost_incomplete") is not False
    ):
        raise ValueError("runtime epoch requires its exact halted source snapshot")
    target = LiveConfig.from_json(canonical_json(record["target_config"]))
    require_gas_oracle_pins(target.expected_pins)
    previous = retained_config(target, binding)
    if (
        set(previous.expected_pins) != LEGACY_PIN_NAMES
        or (target.oracle_max_age_seconds, target.usdc_oracle_max_age_seconds) != (1800, 1800)
        or (target.max_capital_usd, target.max_daily_loss_usd, target.max_cumulative_loss_usd)
        != (500, 200, 500)
    ):
        raise ValueError("runtime epoch may only add gas pins and tighten historical feed age")
    target_hash = sha256_hex(canonical_json(asdict(target)))
    review = record["review"]
    commitment = {key: value for key, value in review.items() if key != "evidence_hash"}
    if (
        review["schema"] != "oasis.runtime-transition-review.v1"
        or review["review_passed"] is not True
        or any(
            review[key] is not False
            for key in (
                "execution_eligible",
                "runtime_approved",
                "migration_applied",
                "state_write_supported",
            )
        )
        or type(review["rpc_requests"]) is not int
        or review["rpc_requests"] != 0
        or review["target_config_hash"] != target_hash
        or review["evidence_hash"] != sha256_hex(canonical_json(commitment))
        or record["approved_review_hash"] != review["evidence_hash"]
        or set(review["services"]) != {"strategy", "authority"}
        or review["gas_observed_pins"] != {k: target.expected_pins[k] for k in GAS_PIN_NAMES}
        or review["references_hash"] != sha256_hex(canonical_json(record["references"]))
    ):
        raise ValueError("runtime epoch lacks the exact explicit reviewed proposal")
    service = review["services"][record["service"]]
    views = [
        (
            row["nav_usd_micros"],
            row["nav_raw_results_hash"],
            row["risk_preview"]["basis"],
            row["risk_preview"]["external"],
        )
        for row in review["services"].values()
    ]
    if (
        service["journal_tip"] != parent
        or service["source_config_hash"] != binding
        or service["state_hash"] != record["source_state_hash"]
        or service["halt_preserved"] is not True
        or views[0] != views[1]
    ):
        raise ValueError("runtime review differs from the source service")
    references, block = record["references"], review["block"]
    if (
        set(references) != {"schema", "nav", "gas"}
        or references["schema"] != "oasis.runtime-transition-inputs.v1"
        or set(references["nav"]) != {"strategy", "authority"}
        or type(block["timestamp"]) is not int
        or type(review["valid_until"]) is not int
        or type(review["reviewed_at"]) is not int
        or not block["timestamp"]
        <= review["reviewed_at"]
        <= review["valid_until"]
        <= block["timestamp"] + 120
    ):
        raise ValueError("bounded same-block runtime review required")
    for raw_refs, kind in (
        (references["gas"], "live_gas_oracle_discovery"),
        *(
            (
                references["nav"][name],
                "live_public_usd_nav" if isinstance(target, PublicUsdConfig) else "live_nav",
            )
            for name in ("strategy", "authority")
        ),
    ):
        refs = tuple(evidence_ref_from_json(ref) for ref in raw_refs)
        if (
            len(refs) != 2
            or tuple(ref.source for ref in refs) != SOURCES
            or any(
                ref.kind != kind
                or ref.block is None
                or asdict(ref.block) != block
                or ref.block.chain_id != target.chain_id
                for ref in refs
            )
        ):
            raise ValueError("runtime epoch proof scope differs")
    fresh = {
        "block": block,
        "nav_usd_micros": service["nav_usd_micros"],
        "valid_until": review["valid_until"],
        "evidence": references["nav"][record["service"]],
    }
    return target_hash, checkpoint_snapshot(
        prior, target, fresh, service["risk_preview"], halt_reason=prior["halted"]
    )
