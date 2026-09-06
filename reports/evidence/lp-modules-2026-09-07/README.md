# 独立审计证据包：LP 模块 / 归属 / shadow 决策日志

日期：2026-09-07（Asia/Shanghai）
Git 基线：`afd39df`（HEAD，见下）。本目录为**实现侧**自查证据，供第三节指定复核人独立复现；
本包任何内容都不构成独立验收、上线收益或已审计声明。

## 复现步骤

```bash
git checkout afd39df
make python-check                 # 预期：All checks passed! 499 passed + 96 subtests
make security-check               # 预期：pip-audit 无已知漏洞；bandit 严重度 Medium/High 0；secret gate 通过
make contract-check               # 预期：4 套件 32 passed，0 failed，0 skipped
make contract-security-check      # 预期：slither 0 result
```

门禁原始输出见 `gates/`。

## 本包覆盖的代码与测试

| 模块 | 文件 | 复核重点 |
|---|---|---|
| 多仓位 owner 现金归属 | `src/oasis_agent/farming/batch_settlement.py`（`resolve_batch_owner_cash` / `batch_owner_cash_payload`）、`tests/unit/test_batch_settlement.py` | 币种唯一物理归属；共享币种/第三方/原生币失败闭合；三重交叉验证是否与收据一致 |
| 原生代币付款归属 | `src/oasis_agent/farming/native_payments.py`、`tests/unit/test_native_payments.py` | 原生余额变化 = 结算额 + gas；Ethereum/OP Stack 分链 gas 口径；WRAP/UNWRAP 与第三方流失败闭合 |
| shadow LP 决策日志 | `src/oasis_agent/shadow/lp_runner.py`、`src/oasis_agent/shadow/journal.py`、`tests/unit/test_shadow_lp_runner.py`、`tests/unit/test_shadow.py` | 决策链哈希链接与篡改检测；HALT 跨重启锁存；`execution_eligible` 恒 False；异常文本不入库 |
| LP 规划模块（前置） | `src/oasis_agent/farming/strategy.py`、`src/oasis_agent/execution/lp_state_machine.py`、`src/oasis_agent/execution/range_controller.py`、`src/oasis_agent/risk/lp_policy.py` | 带表单调性/边界；状态机迁移合法性；marginal value 门限；canary 四上限取 min |
| 执行器合约（前置） | `contracts/src/OasisPositionExecutor.sol`、`contracts/test/OasisPositionExecutor.t.sol` | 原子 RECENTER/REMOVE；nonce/deadline/pool 绑定；guardian 仅可停机；lint 豁免注释的理由 |
| 收付款与成本（前置基线） | `src/oasis_agent/farming/transaction_payments.py`、`cost_allocation.py`、`full_cost.py` | 与 `docs/verification/2026-09-06-closure-acceptance-checklist.md` 一致性 |

## 审计背景（供复核人核对，不替代复核）

- 决策记录 schema：`oasis.shadow-lp-decision.v1`；健康链 schema：`oasis.shadow-health.v1`。
- 多仓位归属只在“物理唯一”时输出，否则抛 `UnsupportedBatchAllocation`；原生币归属要求
  settle/take 与 root 付款逐帧匹配，任何第三方价值流即失败。
- 本轮验证文档：`docs/verification/2026-09-07-lp-modules-shadow-closure.md`。
- 未放行项清单（canary、独立审计委托、长期 shadow 观察、动态费超额收益证明）同见该文档第四节。

## 文件清单与 SHA256

见 `MANIFEST.sha256`（含本目录全部文件；复核后建议将复核报告与本清单一并不可变归档）。
