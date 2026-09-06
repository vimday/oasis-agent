from __future__ import annotations

from ..errors import ExecutionBlockedError
from ..models.execution import ArbitrageOpportunity, ExecutionMode, ExecutionRecord


class ExecutionStateMachine:
    def __init__(self, mode: ExecutionMode) -> None:
        self.mode = mode

    def observe(self, opportunity_id: str) -> ExecutionRecord:
        return ExecutionRecord(opportunity_id, self.mode, "observed")

    def approve(
        self, opportunity: ArbitrageOpportunity, current_block: int, risk_allowed: bool
    ) -> ExecutionRecord:
        if self.mode is not ExecutionMode.APPROVE:
            raise ExecutionBlockedError("approval is disabled in the current execution mode")
        if opportunity.expiry_block < current_block:
            raise ExecutionBlockedError("opportunity expired")
        if not risk_allowed:
            raise ExecutionBlockedError("risk policy denied opportunity")
        return ExecutionRecord(opportunity.opportunity_id, self.mode, "approved")
