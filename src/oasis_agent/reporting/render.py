from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def benchmark_payload() -> dict[str, Any]:
    hook = "0x1191Ac2561817686EFE4296B37BA5a5419Eaa880"
    return {
        "title": "Robinhood Chain Uni v4 Benchmark Hook Analysis",
        "chain_id": 4663,
        "pool_id": "0x374531f25cfb1b92f6d03b946598d53dd934fba6e138bcece153a52113aa73a5",
        "pool_manager": "0x8366a39cc670b4001a1121b8f6a443a643e40951",
        "hook": hook,
        "base_fee": 60,
        "permissions": {
            "before_initialize": True,
            "before_add_liquidity": True,
            "before_swap": True,
            "after_swap": False,
            "before_swap_returns_delta": False,
            "after_swap_returns_delta": False,
        },
        "default_fee_config": [10000, 20000, 30000, 200, 3400, 3300, 3300, 16777216],
        "findings": [
            {"level": "confirmed", "text": "`beforeSwap` is enabled by the hook address permissions."},
            {"level": "confirmed", "text": "No swap-return-delta permission is enabled by the address mask."},
            {"level": "inferred", "text": "Bytecode contains a fee override path and likely dynamic fee configuration."},
            {"level": "unresolved", "text": "The exact fee-selection algorithm requires pinned fork execution or verified source."},
        ],
        "limitations": [
            "Source code is not verified.",
            "This report is generated from deterministic local fixture data.",
            "No mainnet transaction is sent by the report command.",
            "Dynamic fee claims require block-pinned settlement reconciliation.",
        ],
    }


def render_markdown(payload: dict[str, Any]) -> str:
    fee_percent = payload["base_fee"] / 1_000_000
    lines = [
        f"# {payload['title']}", "", "## Identity", "",
        f"- Chain ID: `{payload['chain_id']}`",
        f"- Pool ID: `{payload['pool_id']}`",
        f"- PoolManager: `{payload['pool_manager']}`",
        f"- Hook: `{payload['hook']}`",
        f"- Pool initialization fee: `{payload['base_fee']}` (`{fee_percent:.6%}`)",
        "", "## Hook permissions", "",
    ]
    lines.extend(f"- `{name}`: {'enabled' if enabled else 'disabled'}" for name, enabled in payload["permissions"].items())
    lines += ["", "## Default fee configuration", "", "```text", json.dumps(payload["default_fee_config"]), "```", "", "## Findings", ""]
    lines.extend(f"- **{finding['level']}** — {finding['text']}" for finding in payload["findings"])
    lines += ["", "## Limitations and safety", ""]
    lines.extend(f"- {item}" for item in payload["limitations"])
    lines.append("")
    return "\n".join(lines)


def write_report(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_markdown(benchmark_payload()), encoding="utf-8")
