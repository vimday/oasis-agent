"""Multi-chain and alert routes (P3).

Chain endpoints only perform whitelisted read-only JSON-RPC calls with
bounded timeouts, a short cache and concurrent fan-out across independent
calls; the server never submits transactions. RPC transport failures are
translated to 503/502 by the centralized exception handlers. Alerts are
evaluated on demand from verified data only.
"""

import asyncio

from fastapi import APIRouter, HTTPException

from ..models.common import ZERO_ADDRESS
from .alerts import evaluate_alerts
from .chains import decode_rpc_quantity
from .deps import State
from .schemas import AlertItem, AlertsResponse, ChainItem, ChainsResponse, ChainStatusResponse

router = APIRouter(prefix="/api/v1", tags=["p3"])


@router.get("/chains", response_model=ChainsResponse)
async def list_chains(state: State) -> ChainsResponse:
    return ChainsResponse(
        schema="oasis.server.chains.v1",
        items=[ChainItem(**item) for item in state.registry.summary()],
    )


@router.get("/chains/{chain_id}/chain", response_model=ChainStatusResponse)
async def chain_status(state: State, chain_id: int) -> ChainStatusResponse:
    client = state.registry.client(chain_id)
    if not client.available:
        raise HTTPException(status_code=503, detail="chain RPC not configured")
    chain_call, block_call, balance_call = await asyncio.gather(
        client.call("eth_chainId", []),
        client.call("eth_blockNumber", []),
        client.call("eth_getBalance", [ZERO_ADDRESS, "latest"]),
    )
    return ChainStatusResponse(
        schema="oasis.server.chain-status.v1",
        chain_id=chain_id,
        rpc_chain_id=decode_rpc_quantity(chain_call.result, "chain id"),
        block_number=decode_rpc_quantity(block_call.result, "block number"),
        sample_balance_wei=decode_rpc_quantity(balance_call.result, "sample balance"),
    )


@router.get("/alerts", response_model=AlertsResponse)
async def alerts(state: State) -> AlertsResponse:
    runs = tuple(state.journal.list_runs(limit=5))
    items = await state.reader.run(
        lambda databases: evaluate_alerts(
            config=state.config,
            databases=databases,
            runs=runs,
        )
    )
    return AlertsResponse(
        schema="oasis.server.alerts.v1",
        total=len(items),
        items=[
            AlertItem(
                level=alert.level,
                rule=alert.rule,
                message=alert.message,
                observed_at=alert.observed_at,
            )
            for alert in items
        ],
    )
