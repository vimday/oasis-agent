"""Observe-only cross-chain inventory and loss reconciliation. Never writes the risk ledger.

Captures two-provider pinned account-inventory evidence, replays it into one
complete portfolio frame and reports units, feeds, NAV, movement attribution and
the loss the pure ledger reconciliation would record. With `--state` it chains
the previous ledger state, so successive runs accumulate a real day/cumulative
loss history instead of restarting at inception. It cannot sign, broadcast,
reserve capital or admit a commitment, and it never opens the risk journal:
`execution_eligible` stays false.

Provider names must be exactly the manifest's declared `primary_source` and
`secondary_source`, so retained proofs always satisfy the manifest's own trust
assumption. Native movement is never inferred: gas and non-gas units are
operator declarations in `--native`, and anything undeclared stays unresolved and
blocks the frame.

Reported figures are retained-input reconstructions at one pinned block, not
settled PnL, not complete NAV across time and not independent LP valuation.
"""

import argparse
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from oasis_agent.evidence.exact_capture import RecordedSession
from oasis_agent.evidence.rpc_capture import BoundedExchangeRecorder
from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.execution.account_risk_reader import (
    AccountObservation,
    AssetFlowEvidence,
    EvidenceReplayAccountRiskReader,
)
from oasis_agent.farming.account_flows import (
    FlowScope,
    collect_token_flow_quorum,
    persist_token_flow_evidence,
)
from oasis_agent.farming.account_inventory import (
    AccountInventoryQuorum,
    collect_account_inventory_quorum,
    persist_account_inventory_evidence,
)
from oasis_agent.models.common import canonical_json
from oasis_agent.risk.account_risk import (
    AccountFrame,
    PortfolioFrame,
    RiskState,
    reconcile,
)
from oasis_agent.risk.portfolio_manifest import ChainInventory, PortfolioManifest
from oasis_agent.rpc.budget import BudgetedPoster, BudgetPolicy
from oasis_agent.rpc.transport import JsonRpcTransport, _UrlLibPoster


def _pairs(values: list[str], name: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        key, sep, item = value.partition("=")
        if not sep or not key.strip() or not item.strip() or key.strip() in result:
            raise ValueError(f"invalid {name} entry")
        result[key.strip()] = item.strip()
    return result


def _rpc_map(values: list[str]) -> dict[str, tuple[str, str]]:
    result: dict[str, tuple[str, str]] = {}
    for value in values:
        parts = value.split(":")
        if len(parts) != 3 or not all(part.strip() for part in parts):
            raise ValueError("invalid rpc entry")
        chain, primary, secondary = (part.strip() for part in parts)
        if primary == secondary or chain in result:
            raise ValueError("two distinct RPC environment names per chain required")
        result[chain] = (primary, secondary)
    return result


def _environment(path: Path, names: set[str]) -> dict[str, str]:
    found: dict[str, str] = {}
    with path.open() as stream:
        for line in stream:
            name, sep, value = line.strip().removeprefix("export ").partition("=")
            if sep and name.strip() in names:
                if name.strip() in found:
                    raise ValueError("duplicate RPC configuration")
                found[name.strip()] = value.strip().strip("\"'")
    if set(found) != names or len(set(found.values())) != len(found):
        raise ValueError("two configured RPC endpoints per chain required")
    return found


def _budget_policies(
    names: set[str], *, max_requests: int, interval_ms: int
) -> tuple[BudgetPolicy, ...]:
    """One immutable policy set per budget ledger: `RpcBudget` refuses any change."""
    if type(max_requests) is not int or not 1 <= max_requests <= 1_000_000:
        raise ValueError("RPC request cap must be 1..1000000")
    return tuple(BudgetPolicy(name, max_requests, interval_ms) for name in sorted(names))


def _sessions(
    *,
    sources: tuple[str, str],
    urls: Mapping[str, str],
    policies: tuple[BudgetPolicy, ...],
    budget: Path,
) -> tuple[RecordedSession, RecordedSession]:
    """Fresh one-shot sessions: an exact capture may not reuse a recorder."""
    sessions: list[RecordedSession] = []
    for source in sources:
        recorder = BoundedExchangeRecorder()
        sessions.append(
            (
                JsonRpcTransport(
                    "https://unused.invalid",
                    http=BudgetedPoster(_UrlLibPoster(urls[source], 15), budget, policies, source),
                    retries=0,
                    recorder=recorder,
                ),
                recorder,
                source,
            )
        )
    return sessions[0], sessions[1]


def _inventory(
    entry: ChainInventory,
    *,
    block: int,
    urls: Mapping[str, str],
    policies: tuple[BudgetPolicy, ...],
    budget: Path,
) -> AccountInventoryQuorum:
    sessions = _sessions(
        sources=(entry.primary_source, entry.secondary_source),
        urls=urls,
        policies=policies,
        budget=budget,
    )
    return collect_account_inventory_quorum(
        sessions[0], sessions[1], entry=entry, block_number=block
    )


def _flow_evidence(
    store: EvidenceStore,
    entry: ChainInventory,
    *,
    prior_block: int,
    end_block: int,
    urls: Mapping[str, str],
    policies: tuple[BudgetPolicy, ...],
    budget: Path,
) -> tuple[AssetFlowEvidence, ...]:
    """Attribute every declared ERC-20 delta from complete two-provider logs."""
    collected: list[AssetFlowEvidence] = []
    for asset in entry.erc20_assets:
        scope = FlowScope(
            entry.chain_id,
            entry.account,
            asset.asset_id,
            asset.address,
            asset.decimals,
            prior_block,
            end_block,
        )
        sessions = _sessions(
            sources=(entry.primary_source, entry.secondary_source),
            urls=urls,
            policies=policies,
            budget=budget,
        )
        quorum = collect_token_flow_quorum(sessions[0], sessions[1], scope=scope)
        collected.append(
            AssetFlowEvidence(asset.asset_id, persist_token_flow_evidence(store, quorum))
        )
    return tuple(collected)


def _native_declarations(
    path: Path | None, manifest: PortfolioManifest
) -> dict[str, tuple[int, int]]:
    """Operator-declared (gas, non-gas) native units per account; never inferred."""
    if path is None:
        return dict.fromkeys(manifest.keys, (0, 0))
    data = json.loads(path.read_bytes())
    if not isinstance(data, dict) or set(data) != set(manifest.keys):
        raise ValueError("native declarations must cover exactly the manifest accounts")
    declared: dict[str, tuple[int, int]] = {}
    for key, value in data.items():
        if not isinstance(value, dict) or set(value) != {"gas_units", "non_trading_units"}:
            raise ValueError("native declaration requires gas_units and non_trading_units")
        # Both quantities are unsigned here on purpose. `reconstruct_movements`
        # accepts a signed non-gas attribution, but an observer cannot tell a
        # genuine native withdrawal from a native outflow that bought an
        # investment and lost value on the way. Crediting that outflow as
        # non-trading flow would erase the loss, so the CLI refuses to declare a
        # native outflow and the frame stays refused instead of under-reporting.
        units: list[int] = []
        for name in ("gas_units", "non_trading_units"):
            item = value[name]
            if type(item) is not int or not 0 <= item < 2**256:
                raise ValueError("native declaration units must be bounded integers")
            units.append(item)
        declared[key] = (units[0], units[1])
    return declared


def _prior_state(path: Path | None) -> RiskState:
    return RiskState() if path is None else RiskState.model_validate_json(path.read_bytes())


def _bind_blocks(manifest: PortfolioManifest, blocks: Mapping[int, int]) -> dict[int, int]:
    if set(blocks) != {chain.chain_id for chain in manifest.chains}:
        raise ValueError("one pinned block per manifest chain required")
    return dict(blocks)


def _bind_endpoints(
    manifest: PortfolioManifest, endpoints: Mapping[str, tuple[str, str]]
) -> dict[str, tuple[str, str]]:
    """Each chain's RPC pair must be exactly the manifest's declared providers."""
    if set(endpoints) != {str(chain.chain_id) for chain in manifest.chains}:
        raise ValueError("one RPC pair per manifest chain required")
    for entry in manifest.chains:
        if endpoints[str(entry.chain_id)] != (entry.primary_source, entry.secondary_source):
            raise ValueError("rpc pair must match the declared manifest providers")
    return dict(endpoints)


def _bind_state(manifest: PortfolioManifest, state: RiskState) -> RiskState:
    """A chained state must describe exactly this manifest's accounts."""
    if state.frame is not None and {a.key for a in state.frame.accounts} != set(manifest.keys):
        raise ValueError("previous state must cover exactly the manifest accounts")
    return state


def _account(frame: PortfolioFrame | None, key: str) -> AccountFrame | None:
    if frame is None:
        return None
    return next(account for account in frame.accounts if account.key == key)


def _summary(frame: PortfolioFrame) -> list[dict[str, object]]:
    return [
        {
            "key": account.key,
            "block": account.block_number,
            "block_hash": account.block_hash,
            "nav_usd_micros": account.nav,
            "movements": account.movements,
            "non_trading_flow_usd_micros": account.non_trading_flow_usd_micros,
            "unresolved_operations": account.unresolved_operations,
        }
        for account in frame.accounts
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--rpc", action="append", default=[], metavar="CHAIN:PRIMARY:SECONDARY")
    parser.add_argument("--block", action="append", default=[], metavar="CHAIN=NUMBER")
    parser.add_argument("--state", type=Path, help="previous risk state for continuous observation")
    parser.add_argument(
        "--native", type=Path, help="declared native gas/transfer units per account"
    )
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--interval-ms", type=int, default=3000)
    parser.add_argument("--max-requests", type=int, default=256)
    args = parser.parse_args()
    try:
        manifest = PortfolioManifest.model_validate_json(args.manifest.read_bytes())
        blocks = _bind_blocks(
            manifest, {int(key): int(value) for key, value in _pairs(args.block, "block").items()}
        )
        endpoints = _bind_endpoints(manifest, _rpc_map(args.rpc))
        if not 3000 <= args.interval_ms <= 60000:
            raise ValueError("minimum request interval is three seconds")
        names = {name for pair in endpoints.values() for name in pair}
        urls = _environment(args.env_file, names)
        state = _bind_state(manifest, _prior_state(args.state))
        if state.frame is None and args.native is not None:
            raise ValueError("native declarations require a previous state")
        native = _native_declarations(args.native, manifest)
        policies = _budget_policies(
            names, max_requests=args.max_requests, interval_ms=args.interval_ms
        )
        args.output.mkdir(parents=True, exist_ok=False)
        store = EvidenceStore(args.output / "evidence.db")
    except Exception:  # noqa: BLE001 - never print endpoint values
        parser.exit(1, "Invalid reconciliation configuration; no signing is available.\n")

    observations: dict[str, AccountObservation] = {}
    inventory: list[dict[str, object]] = []
    try:
        for entry in manifest.chains:
            quorum = _inventory(
                entry,
                block=blocks[entry.chain_id],
                urls=urls,
                policies=policies,
                budget=args.output / "budget.db",
            )
            references = persist_account_inventory_evidence(store, quorum)
            prior = _account(state.frame, entry.key)
            observations[entry.key] = (
                AccountObservation(inventory=references)
                if prior is None
                else AccountObservation(
                    inventory=references,
                    flows=_flow_evidence(
                        store,
                        entry,
                        prior_block=prior.block_number,
                        end_block=quorum.header.ref.number,
                        urls=urls,
                        policies=policies,
                        budget=args.output / "budget.db",
                    ),
                    native_gas_units=native[entry.key][0],
                    non_trading_native_units=native[entry.key][1],
                )
            )
            inventory.append(
                {
                    "key": entry.key,
                    "block": quorum.header.ref.number,
                    "block_hash": quorum.header.ref.block_hash,
                    "units": [[unit.asset_id, unit.units] for unit in quorum.units],
                    "feeds": [
                        [feed.asset_id, feed.answer, feed.decimals, feed.round_id]
                        for feed in quorum.feeds
                    ],
                }
            )
    except Exception:  # noqa: BLE001 - RPC errors may echo endpoint URLs
        store.close()
        (args.output / "report.json").write_bytes(
            canonical_json({"status": "capture_failed_redacted", "execution_eligible": False})
            + b"\n"
        )
        parser.exit(2, "Inventory capture failed; retained evidence is incomplete.\n")

    reader = EvidenceReplayAccountRiskReader(store, manifest, observations, previous=state.frame)
    frame: PortfolioFrame | None = None
    ledger: RiskState | None = None
    try:
        frame = reader.reconcile()
        ledger = reconcile(manifest.policy, state, frame, now=int(datetime.now(UTC).timestamp()))
        rejected = ""
    except Exception as error:  # noqa: BLE001 - our own bounded messages only, no network I/O
        rejected = str(error)
    store.close()
    (args.output / "inventory.json").write_bytes(canonical_json(inventory) + b"\n")
    result = {
        "schema": "oasis.account-risk-reconciliation.v1",
        "inventory": inventory,
        "accounts": [] if frame is None else _summary(frame),
        "continuous": state.frame is not None,
        "ledger_accepted": ledger is not None,
        "rejected_reason": rejected,
        "cumulative_loss_usd_micros": None if ledger is None else ledger.cumulative_loss,
        "daily_losses_usd_micros": None if ledger is None else ledger.daily_losses,
        "halted": None if ledger is None else ledger.halted,
        "policy_digest": manifest.policy.digest,
        "manifest_digest": manifest.digest,
        "execution_eligible": False,
        "unresolved": [
            "declared_manifest_is_supplied_policy_not_chain_truth",
            "native_gas_and_non_trading_native_units_are_operator_declared",
            "observer_never_writes_the_risk_journal",
            "no_independent_lp_or_position_valuation",
        ],
    }
    (args.output / "report.json").write_bytes(canonical_json(result) + b"\n")
    if frame is not None and ledger is not None:
        (args.output / "frame.json").write_bytes(
            canonical_json(frame.model_dump(mode="json")) + b"\n"
        )
        (args.output / "state.json").write_bytes(
            canonical_json(ledger.model_dump(mode="json")) + b"\n"
        )
    print(
        canonical_json(
            {"ledger_accepted": ledger is not None, "execution_eligible": False}
        ).decode()
    )
    if ledger is None:
        parser.exit(3, f"Ledger refused the frame: {rejected}\n")


if __name__ == "__main__":
    main()
