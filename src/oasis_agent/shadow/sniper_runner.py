"""Retained launch-input diagnostics with a restart-stable, append-only journal.

No protocol adapter or raw-proof verifier is implied by an imported observation.
Unsupported proof gates always reject; an operator cannot supply `passed` flags.
"""

import json
import sqlite3
from pathlib import Path
from typing import Any

from ..launch.holders import assess_holders
from ..launch.models import Cycle, HolderTarget, TargetPolicy, parse_target
from ..launch.quotes import matrix_reasons
from ..launch.watcher import coherent
from ..models.common import canonical_json, sha256_hex


def code_digest() -> str:
    package = Path(__file__).resolve().parents[1]
    paths = [
        *sorted((package / "launch").glob("*.py")),
        Path(__file__).resolve(),
        package / "models" / "common.py",
    ]
    return sha256_hex(
        canonical_json({str(p.relative_to(package)): sha256_hex(p.read_bytes()) for p in paths})
    )


def evaluate(cycle: Cycle, target: TargetPolicy) -> dict[str, Any]:
    observations = cycle.observations
    gates: dict[str, list[str]] = {f"G{i}": [] for i in range(1, 12)}
    stand_down: list[str] = []
    holder_assessment = None
    if isinstance(target, HolderTarget):
        holder_assessment = assess_holders(cycle, target)
        gates["HOLDERS"] = holder_assessment["reasons"]
        if holder_assessment["stand_down"]:
            stand_down.extend(holder_assessment["reasons"])
    elif cycle.holder_observations is not None:
        stand_down.append("holder_policy_v3_required")
    if not coherent(observations):
        stand_down.append("two_source_snapshot_required")
    if not observations:
        gates["G1"].append("observations_missing")
        gates["G2"].append("control_observations_missing")
        gates["G4"].append("asset_observations_missing")
    if target.runtime_hash is None or target.token_decimals is None:
        gates["G1"].append("token_baseline_unverified")
    if target.control_baseline is None:
        gates["G2"].append("control_baseline_unverified")
    if target.max_age_seconds is None or target.max_age_blocks is None:
        stand_down.append("freshness_thresholds_unfrozen")
    for observation in observations:
        if (
            observation.target_ca != target.target_ca
            or observation.runtime_hash != target.runtime_hash
            or observation.token_decimals != target.token_decimals
        ):
            gates["G1"].append("token_identity_mismatch")
        if observation.control_digest != target.control_baseline:
            gates["G2"].append("control_baseline_mismatch")
            stand_down.append("control_baseline_mismatch")
        if (
            target.target_ca not in (observation.token0, observation.token1)
            or observation.token0 >= observation.token1
        ):
            gates["G4"].append("invalid_pool_assets")
        block = observation.snapshot
        if block.chain_id != target.chain_id:
            stand_down.append("chain_mismatch")
        if not block.timestamp <= observation.retrieved_at <= cycle.evaluated_at:
            stand_down.append("future_or_incoherent_time")
        if (
            target.max_age_seconds is not None
            and not 0 <= cycle.evaluated_at - block.timestamp <= target.max_age_seconds
        ):
            stand_down.append("stale_or_future_snapshot")
        if (
            target.max_age_blocks is not None
            and not 0 <= cycle.current_block - block.number <= target.max_age_blocks
        ):
            stand_down.append("stale_or_future_block")
    quote_reasons = matrix_reasons(observations, target)
    if any("disagreement" in reason for reason in quote_reasons):
        stand_down.append("provider_disagreement")
    gates["G7"].extend(quote_reasons)
    # Interpretation digests are useful for reproducible diagnostics, but not
    # proofs of the underlying RPC calls, runtime code or protocol semantics.
    gates["G1"].append("raw_identity_proof_verifier_unavailable")
    gates["G2"].append("complete_control_plane_verifier_unavailable")
    gates["G3"].append("versioned_factory_router_quoter_adapter_unavailable")
    gates["G4"].append("quote_asset_and_pool_binding_unverified")
    gates["G5"].append("executable_liquidity_unverified")
    gates["G6"].append("owner_cash_and_withdrawal_authority_unverified")
    gates["G7"].append("native_usd_valuation_and_raw_quote_proofs_unverified")
    gates["G8"].append("continuous_simulation_and_calldata_protection_unverified")
    gates["G9"].append("daily_token_budget_and_kill_price_unverified")
    gates["G10"].append("execution_state_machine_design_only")
    gates["G11"].append("bounded_sell_authorization_unverified")
    for name in (
        "slippage_bps",
        "min_liquidity_usd_micros",
        "max_stress_loss_bps",
        "kill_price_usd_micros",
        "buy_tranches_wei",
    ):
        if getattr(target, name) is None:
            gate = "G9" if name in {"kill_price_usd_micros", "buy_tranches_wei"} else "G8"
            gates[gate].append(f"{name}_unfrozen")
    return {
        **({"holders": holder_assessment} if holder_assessment is not None else {}),
        **({"chain_id": target.chain_id} if hasattr(target, "schema_") else {}),
        "schema": "oasis.shadow-launch-decision.v1",
        "target_ca": target.target_ca,
        "decision": "stand_down" if stand_down else "reject",
        "execution_eligible": False,
        "strategy_qualification": "unresolved",
        "evidence_level": "unresolved",
        "stand_down_reasons": sorted(set(stand_down)),
        "gates": {
            key: {"passed": not reasons, "reasons": sorted(set(reasons))}
            for key, reasons in gates.items()
        },
        "input": cycle.model_dump(mode="json"),
    }


def evaluate_transition(
    cycle: Cycle, target: TargetPolicy, records: list[dict[str, Any]]
) -> dict[str, Any]:
    record = evaluate(cycle, target)
    if records:
        prior = records[-1]
        prior_cycle = Cycle.from_json(canonical_json(prior["input"]))
        if cycle.evaluated_at < prior_cycle.evaluated_at:
            record["stand_down_reasons"].append("clock_regression")
        if coherent(cycle.observations) and coherent(prior_cycle.observations):
            before = prior_cycle.observations[0].snapshot
            after = cycle.observations[0].snapshot
            if (
                after.number < before.number
                or (after.number == before.number and after != before)
                or (
                    after.number > before.number
                    and (
                        after.number != before.number + 1 or after.parent_hash != before.block_hash
                    )
                )
            ):
                record["stand_down_reasons"].append("lineage_gap_or_reorg")
        if prior["decision"] == "stand_down":
            record["stand_down_reasons"].append("stand_down_latched")
    if record["stand_down_reasons"]:
        record["decision"] = "stand_down"
    return record


def read_records(db: sqlite3.Connection) -> list[dict[str, Any]]:
    policy = db.execute("SELECT payload FROM launch_policy WHERE id=1").fetchone()
    if policy is None:
        raise ValueError("launch policy missing")
    policy_digest = sha256_hex(bytes(policy[0]))
    target = parse_target(canonical_json(json.loads(policy[0])["target"]))
    previous = "0" * 64
    records: list[dict[str, Any]] = []
    for seq, input_digest, raw, digest in db.execute(
        "SELECT seq, input_digest, payload, digest FROM launch_decisions ORDER BY seq"
    ):
        record = json.loads(raw)
        if not isinstance(record, dict):
            raise ValueError("launch audit record must be an object")
        if (
            seq != len(records) + 1
            or record.get("seq") != seq
            or record.get("previous") != previous
            or sha256_hex(raw) != digest
            or canonical_json(record) != raw
            or record.get("input_digest") != input_digest
            or sha256_hex(canonical_json(record.get("input"))) != input_digest
            or record.get("policy_digest") != policy_digest
            or record.get("schema") != "oasis.shadow-launch-decision.v1"
            or record.get("execution_eligible") is not False
            or record.get("strategy_qualification") != "unresolved"
            or record.get("decision") not in {"reject", "stand_down"}
        ):
            raise ValueError("launch audit chain corrupted")
        cycle = Cycle.from_json(canonical_json(record["input"]))
        expected = evaluate_transition(cycle, target, records)
        if any(record.get(key) != value for key, value in expected.items()):
            raise ValueError("launch decision does not match retained inputs")
        records.append(record | {"digest": digest})
        previous = digest
    return records


class SniperJournal:
    def __init__(self, path: Path, target: TargetPolicy):
        self.target = target
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS launch_policy "
            "(id INTEGER PRIMARY KEY CHECK(id=1), payload BLOB NOT NULL)"
        )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS launch_decisions "
            "(seq INTEGER PRIMARY KEY, input_digest TEXT UNIQUE NOT NULL, "
            "payload BLOB NOT NULL, digest TEXT NOT NULL)"
        )
        policy = canonical_json({"target": target.model_dump(mode="json"), "code": code_digest()})
        try:
            with self.db:
                self.db.execute("INSERT OR IGNORE INTO launch_policy VALUES(1, ?)", (policy,))
                if self.db.execute("SELECT payload FROM launch_policy").fetchone() != (policy,):
                    raise ValueError("launch code/config cannot change on restart")
                read_records(self.db)
        except ValueError, sqlite3.Error:
            self.db.close()
            raise
        self.policy_digest = sha256_hex(policy)

    def close(self) -> None:
        self.db.close()

    def append(self, cycle: Cycle) -> dict[str, Any]:
        input_digest = sha256_hex(canonical_json(cycle.model_dump(mode="json")))
        self.db.execute("BEGIN IMMEDIATE")
        try:
            records = read_records(self.db)
            duplicate = self.db.execute(
                "SELECT seq FROM launch_decisions WHERE input_digest=?", (input_digest,)
            ).fetchone()
            if duplicate is not None:
                self.db.commit()
                return records[int(duplicate[0]) - 1]
            record = evaluate_transition(cycle, self.target, records)
            record.update(
                seq=len(records) + 1,
                previous=records[-1]["digest"] if records else "0" * 64,
                input_digest=input_digest,
                policy_digest=self.policy_digest,
            )
            raw = canonical_json(record)
            digest = sha256_hex(raw)
            self.db.execute(
                "INSERT INTO launch_decisions VALUES (?, ?, ?, ?)",
                (record["seq"], input_digest, raw, digest),
            )
            self.db.commit()
            return record | {"digest": digest}
        except ValueError, TypeError, KeyError, sqlite3.Error:
            self.db.rollback()
            raise
