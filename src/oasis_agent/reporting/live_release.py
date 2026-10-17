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
    implementation_status: str = "implemented_pending_integration_validation"


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
                title="资金对账及本地 fork 已通过, 待生产双源验收",
                required=("本地 fork 已通过; 仍需生产双源 trace 与完整 OP 费用验收。"),
            ),
            ReleaseBlocker(
                code="transaction_policy_not_isolated",
                title="完整交易签名边界已实现, 待进程隔离验收",
                required=(
                    "验证私有 socket、独立风险日志、原始双源模拟及部署身份隔离; 禁止摘要签名回退。"
                ),
            ),
            ReleaseBlocker(
                code="exact_nav_accounting_incomplete",
                title="整数净值及未收手续费已接通, 待真实数据验收",
                required="验证两种货币的有效 USD 喂价、历史资金基准迁移与完整持仓估值。",
            ),
            ReleaseBlocker(
                code="durable_execution_recovery_incomplete",
                title="持久化及重组阻断已接通, 待跨进程恢复验收",
                required="验证签名前后崩溃、两份执行日志恢复及链重组; 未知签名不可自动重试。",
            ),
        ]
    )


def require_live_release() -> None:
    report = live_release()
    if not report.new_actions_allowed or report.blockers:
        raise ValueError("live release blocked: " + ", ".join(b.code for b in report.blockers))
