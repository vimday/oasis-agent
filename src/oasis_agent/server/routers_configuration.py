"""Authenticated configuration validation without altering active campaigns.

JSON documents cross the browser boundary as strings: wei and uint256 values
must never round-trip through JavaScript Number. The CLI parsers remain the
source of truth. Validation does not authorize execution or overwrite a policy.
"""

import asyncio
import json
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException
from pydantic import Field

from ..farming.strategy import BandTableFeeStrategy
from ..launch.models import Address, Positive, StrictModel
from ..models.common import sha256_hex
from ..shadow.market_archive import ArchiveConfig
from ..shadow.observer_config import Config as ObserverConfig
from ..shadow.service import parse_policy, serializable
from ..sniping.config import ExecutionConfig
from .deps import State
from .routers_ops import Bearer, _actor_from_token

router = APIRouter(prefix="/api/v1/configuration", tags=["configuration"])


class ConfigurationRequest(StrictModel):
    kind: Literal["launch", "lp", "rpc", "market", "archive"]
    document: Annotated[str, Field(min_length=2, max_length=65_536)]


class RpcConfiguration(StrictModel):
    chain_id: Positive
    name: Annotated[str, Field(min_length=1, max_length=80)]
    rpc_url_env: Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]{0,127}$")]
    pool_manager: Address
    cache_seconds: Annotated[int, Field(ge=1, le=3600)] = 15


def _json_document(value: object) -> str:
    return json.dumps(value, sort_keys=True, indent=2, ensure_ascii=True, allow_nan=False) + "\n"


_MARKET_TEMPLATE = (
    Path(__file__).resolve().parents[3] / "configs/research.base-eth-usdc.example.json"
)


def _market_template() -> dict[str, object]:
    # The committed example is parsed by the same model the campaign runner
    # uses, so a stale template fails here rather than in the operator's editor.
    observer = ObserverConfig.from_json(_MARKET_TEMPLATE.read_text())
    if observer.market is None:
        raise ValueError("market template requires a market window configuration")
    return observer.model_dump(mode="json")


@router.get("")
async def configuration(state: State) -> dict[str, object]:
    lp = {
        "chain_id": 46630,
        "max_age_seconds": 10,
        "min_benefit": "0",
        "canary_limits": None,
        "strategy": serializable(
            BandTableFeeStrategy(
                min_fee=100,
                base_fee=500,
                max_fee=10000,
                volatility_bands=(),
                impact_bands0=(),
                impact_bands1=(),
                toxicity_bands=(),
                inventory_bands=(),
            )
        ),
    }
    market = await asyncio.to_thread(_market_template)
    return {
        "schema": "oasis.server.configuration.v1",
        "execution_eligible": False,
        "operations_enabled": state.authorizer.enabled,
        "shadow_start_available": state.runner is not None and state.authorizer.enabled,
        "shadow_stop_available": state.runner is not None and state.authorizer.enabled,
        "launch_live_control_available": False,
        "configuration_apply_available": state.campaigns is not None,
        "templates": {
            "market": _json_document(market),
            "archive": _json_document(
                {
                    "observer": market,
                    "start_block": 51907410,
                    "end_block": 51907413,
                    "max_requests_per_provider": 36,
                }
            ),
            "launch": _json_document(
                ExecutionConfig.from_json(
                    '{"schema":"oasis.launch-execution-config.v2"}'
                ).model_dump(mode="json", by_alias=True)
            ),
            "lp": _json_document(lp),
            "rpc": _json_document(
                {
                    "chain_id": 8453,
                    "name": "Base",
                    "rpc_url_env": "BASE_RPC_PRIMARY",
                    "pool_manager": "0x" + "0" * 40,
                    "cache_seconds": 15,
                }
            ),
        },
    }


@router.post("/validate")
async def validate_configuration(
    body: ConfigurationRequest,
    state: State,
    authorization: Bearer = None,
) -> dict[str, object]:
    actor = _actor_from_token(authorization, state)
    try:
        if body.kind == "archive":
            document = _json_document(
                ArchiveConfig.from_json(body.document).model_dump(mode="json")
            )
        elif body.kind == "market":
            observer = ObserverConfig.from_json(body.document)
            if observer.market is None:
                raise ValueError("market window configuration required")
            document = _json_document(observer.model_dump(mode="json"))
        elif body.kind == "launch":
            parsed = ExecutionConfig.from_json(body.document)
            document = _json_document(parsed.model_dump(mode="json", by_alias=True))
        elif body.kind == "lp":
            document = _json_document(serializable(parse_policy(body.document.encode())))
        else:
            rpc = RpcConfiguration.from_json(body.document)
            # JSON string quoting is compatible with these restricted TOML values.
            document = (
                "[[chains]]\n"
                + "\n".join(
                    f"{key} = {json.dumps(value)}"
                    for key, value in rpc.model_dump(mode="json").items()
                )
                + "\n"
            )
    except (ValueError, TypeError, KeyError, ArithmeticError, RecursionError) as exc:
        # Pydantic and Decimal errors can include raw inputs. Never echo them.
        raise HTTPException(
            status_code=422, detail="invalid configuration; check schema and bounds"
        ) from exc
    digest = sha256_hex(document.encode())
    state.journal.record(
        action="validate_configuration",
        actor=actor,
        params={"kind": body.kind, "digest": digest},
        result="validated",
        detail="validated export only; active configuration unchanged",
    )
    return {
        "schema": "oasis.server.configuration-validation.v1",
        "kind": body.kind,
        "document": document,
        "digest": digest,
        "applied": False,
        "execution_eligible": False,
        "filename": {
            "market": "lp-market-research.json",
            "archive": "lp-market-archive.json",
            "launch": "launch_execution.json",
            "lp": "lp-shadow.json",
            "rpc": "server-rpc.toml",
        }[body.kind],
    }
