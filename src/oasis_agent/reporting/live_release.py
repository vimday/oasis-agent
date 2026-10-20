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
                title="历史双源 trace 与完整费用已验证, 待专用账户验收",
                required=(
                    "真实历史交易的 169 帧 trace 及回执双源一致; 完整 OP 费用已另行重放。"
                    "仍需专用账户资金对账及生产数据源可靠性验收。"
                ),
            ),
            ReleaseBlocker(
                code="transaction_policy_not_isolated",
                title="隔离配置及密钥导入已验证, 待生产服务验收",
                required=(
                    "核对部署主机的密钥配置并完成生产双源模拟及时效验收; 密钥导入不代表交易授权。"
                ),
            ),
            ReleaseBlocker(
                code="exact_nav_accounting_incomplete",
                title="整数净值及未收手续费已接通, 待真实数据验收",
                required="验证两种货币的有效 USD 喂价、历史资金基准迁移与完整持仓估值。",
            ),
            ReleaseBlocker(
                code="durable_execution_recovery_incomplete",
                title="双进程回执恢复模拟已验证, 待生产双服务联调",
                required="完成部署双服务的状态恢复与真实回执对账; 未知签名仍禁止自动重试。",
            ),
        ]
    )


def require_live_release() -> None:
    report = live_release()
    if not report.new_actions_allowed or report.blockers:
        raise ValueError("live release blocked: " + ", ".join(b.code for b in report.blockers))
