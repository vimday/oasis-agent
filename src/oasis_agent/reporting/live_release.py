"""Current direct-loop release boundary, shared by CLI, execution and read-only UI.

This describes missing implemented controls, not operator permission. No file,
flag, environment variable, dashboard approval or passing unit test can turn a
missing implementation into an authorization. Remove an item only alongside its
end-to-end implementation and validation.
"""

from pydantic import BaseModel


class ReleaseBlocker(BaseModel):
    code: str
    title: str
    required: str


class LiveRelease(BaseModel):
    schema_version: str = "oasis.live-release.v1"
    new_actions_allowed: bool = False
    owner_canary_authorized: bool = True
    capital_limit_usd: int = 500
    daily_loss_limit_usd: int = 200
    cumulative_loss_limit_usd: int = 500
    blockers: list[ReleaseBlocker]


def live_release() -> LiveRelease:
    return LiveRelease(
        blockers=[
            ReleaseBlocker(
                code="successful_action_cash_incomplete",
                title="兑换、建仓、撤仓资金对账未接通",
                required="重放双源完整资金流、原生币退款与持仓变化, 零残差后保留余额基线。",
            ),
            ReleaseBlocker(
                code="transaction_policy_not_isolated",
                title="交易策略约束尚未进入独立签名边界",
                required="由独立进程校验实际交易、限额与损失预算; 摘要签名本身不能完成校验。",
            ),
            ReleaseBlocker(
                code="exact_nav_accounting_incomplete",
                title="精确净值与未收手续费估值未接通",
                required="以整数或 Decimal 核算本金、费用与亏损, 绑定原始证据并拒绝未解释变化。",
            ),
            ReleaseBlocker(
                code="durable_execution_recovery_incomplete",
                title="执行记录与重组恢复未形成完整闭环",
                required="追加式执行日志、配置绑定、签名崩溃恢复与已结算交易的重组处置。",
            ),
        ]
    )


def require_live_release() -> None:
    report = live_release()
    if not report.new_actions_allowed or report.blockers:
        raise ValueError("live release blocked: " + ", ".join(b.code for b in report.blockers))
