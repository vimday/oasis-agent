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


class AcceptancePermit(BaseModel):
    """One owner-authorized zero-principal transaction that produces the first real receipt.

    The release blockers all require a real dedicated-account receipt, which no closed
    release can produce. This permit admits exactly one action kind at exactly one account
    nonce. Once any transaction with that nonce lands, the permit can never match again.
    """

    kind: str
    account_nonce: int
    authorized_by: str


class LiveRelease(BaseModel):
    schema_version: str = "oasis.live-release.v1"
    new_actions_allowed: bool = False
    owner_canary_authorized: bool = True
    capital_limit_usd: int = 500
    daily_loss_limit_usd: int = 200
    cumulative_loss_limit_usd: int = 500
    acceptance_permit: AcceptancePermit | None = None
    blockers: list[ReleaseBlocker]


# Owner choice 2026-10-28: "放行一笔零本金授权". Nonce 3 was observed on two providers at
# Base block 53204765 for 0xe653df20a8c525edf381942ffc47704b7bdfe97d.
ACCEPTANCE_PERMIT: AcceptancePermit | None = AcceptancePermit(
    kind="approve_usdc", account_nonce=3, authorized_by="owner 2026-10-28"
)


def live_release() -> LiveRelease:
    return LiveRelease(
        acceptance_permit=ACCEPTANCE_PERMIT,
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
                required=(
                    "签名价格 NAV 候选支持双源只读复核及当前时效检查; 尚未接入生产签名门禁。"
                    "仍需新鲜 USD 喂价、生产 NAV 验收、历史资金基准迁移及完整持仓估值。"
                ),
            ),
            ReleaseBlocker(
                code="durable_execution_recovery_incomplete",
                title="双进程及历史承诺恢复已验证, 待生产双服务联调",
                required=(
                    "已支持结算后按原 nonce 找回承诺; 仍需部署双服务的真实回执对账。"
                    "未知签名仍禁止自动重试。"
                ),
            ),
        ],
    )


def require_live_release() -> None:
    report = live_release()
    if not report.new_actions_allowed or report.blockers:
        raise ValueError("live release blocked: " + ", ".join(b.code for b in report.blockers))


def acceptance_allows(kind: object, nonce: object) -> bool:
    """True only for the permitted action kind at the permitted account nonce."""
    permit = ACCEPTANCE_PERMIT
    return (
        permit is not None
        and kind == permit.kind
        and type(nonce) is int
        and nonce == permit.account_nonce
    )


def require_live_action(kind: object, nonce: object) -> None:
    """Gate one signing decision: the acceptance permit or a complete release."""
    if not acceptance_allows(kind, nonce):
        require_live_release()


def require_live_start() -> None:
    """Gate service startup; per-action checks still apply to every signature."""
    if ACCEPTANCE_PERMIT is None:
        require_live_release()
