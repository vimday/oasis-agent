# Phase 1 研究闭合验收清单与独立复核指定

日期：2026-09-06（Asia/Shanghai）
范围：多仓位真交易收付款、全成本与价格估值、shadow 健康门禁、密钥扫描 CI 的本地研究闭合。
结论：**研究证据链可复核验收；自动 LP、canary 与任何资金执行一律不放行。**

本轮无部署、签名、广播、修改线上配置或资金使用。Git 基线 `6db4d22`；所有门禁在本机复跑通过。

## 一、本轮闭合的验收项

| 验收项 | 结论 | 证据 | 复核动作 |
|---|---|---|---|
| 整笔交易全部 ERC-20 参与人收付款 | verified | `base-payments-resumed/`：3 币、3 账户（含第三方收款与销毁），按块前后余额与总供应量核对，残差为零 | 用 `transaction_payments` 复放，独立重算每账户 paid/received/net 与块余额差 |
| 6 个 NFT 核心结算对账 | verified | `batch-settlement.json`：PoolManager 收付款逐币对账，3 币残差均为 0；`pool_manager_residual_by_token` 全零 | 逐仓位重放核心 delta，独立求和并与总账比对 |
| 整笔原生成本（执行费+L1 数据费） | verified | `base-cost-retry/`：`total_native=20,092,985,809,493`，`execution_native=20,085,342,000,000`，`l1_native=7,643,809,493`，`operator_native=0`（钉块链上读取） | 复放 receipt gas/price 与 L1 fee 相乘，核对求和与分链口径 |
| 可靠价格接入成本估值 | verified | `base-price/`：ETH/USD `248,035,894,741`（8dp，≈$2,480.36），USDC/USD `99,989,680`（8dp，≈$0.9998968），同块同运行时哈希 `0xbd6f…c966c2`；估值 49,843 quote 单位（≈0.049843 USDC），指示性 USD 49,838 | 用钉块 `eth_call` 独立读取 `latestRoundData`/`description`/`decimals` 与 `eth_getCode` 运行时哈希比对；确认无 1:1 稳定币假设 |
| shadow 健康门禁 | verified（短窗口） | `closure-2026-09-06/*shadow*/`：Base 3 次 HOLD，Robinhood 连续 3 块 HOLD；HALT 跨重启锁存 | 复放哈希链；注意短时健康观察≠策略 shadow 验收 |
| 密钥扫描 CI 门禁 | verified | `.github/workflows/ci.yml` + `scripts/check_secrets.py` + `configs/secret-scan-baseline.json`；本轮 `make security-check` 通过，无未复核候选 | 独立运行 `detect_secrets scan --all-files` 与新基线比对；CI 触发一次确认 |

### 全成本口径

`full_cost.py` 十类成本（gas、失败执行、再平衡、对冲、融资、机会成本、协议/hook、IL、LVR、库存风险）缺项不得静默归零；`failed_tx_cost`/`opportunity_cost` 非负；IL/LVR/协议扣款不得重复计入。`cost_allocation.py` 只是声明式记账分配，不能建立受益所有权；实际付款人以收据为准。

### 价格估值口径

`value_native_cost` 向上取整，需要 ETH/USD 与报价币/USD 两个喂价且与成本同一钉块快照；喂价策略是研究输入，运行时哈希、描述、新鲜度在链上核对，**不是独立认证**。USDC/USD 观察值 `0.9998968` 直接用于 USD 折算，未假设 1:1 钉住。

## 二、可重复验收命令

```bash
git checkout 6db4d22
make python-check                    # 422 passed + 70 subtests; mypy 75 源文件; ruff 全过
make security-check                  # pip-audit 无已知漏洞; bandit Medium/High 0; secret gate 通过
make contract-check                  # Foundry 18 项/组，零失败零跳过
make contract-security-check         # slither --fail-low 通过
PYTHONPATH=src:. .venv/bin/python -m pytest \
  tests/unit/test_recorded_transaction_closure.py -q   # 真证据复放（收付款/成本/价格）共 3 项
git diff --check
```

新证据 SHA256（`reports/evidence/transaction-closure-2026-09-06/base-price/`）：

| 文件 | SHA256 |
|---|---|
| `evidence.db` | `a3059261e3ab296274e0a10a58344b7a73ffeb53617733fc0974dd09cccbb4a7` |
| `price-native-wire.jsonl` | `04e16116b33757a337f461966c9c88d6ad2bfe0bb8583455ebca9cb395e7020f` |
| `price-quote-wire.jsonl` | `033f5d18f35ac6508a4d34e009fb3c7798fc112a20143ca922faae5743f18254` |
| `summary.json` | `071d4ffafbf2ba078fbb3165102fb1873f2e435a1d2be2b93e3315e98e3c6738` |

## 三、独立复核指定（实现与验收分离）

本清单的“verified”是实现侧自查结论，**不构成独立验收**。放行前必须由以下复核人之一独立复现并签署：

1. **独立复核 AI 实例（默认指定）**：由非本实现会话的另一个 AI 会话执行。仅凭本清单、`REVIEW.md` 严重度框架（P0–P3）、源码与证据包复现；禁止引用实现会话结论、禁止以“测试通过”代替语义正确性。复核必须覆盖：ABI/事件/回放方向、恶意代币与回调、重入、权限撤销、数值边界、真实 Permit2、逐仓位归属与计价、成本不重复、secret 泄漏、测试可信度。每条结论给出最小触发场景、资金/安全后果、精确文件/行号与违反的不变式。
2. **外部安全审计团队（候选，未委托前不冒充）**：Trail of Bits、OpenZeppelin、Consensys Diligence 或 CertiK 任选其一，范围限定 `contracts/` 研究 hook 与 signer 策略。委托开始时间与报告编号须记录于本文件；在此之前不得宣称已审计。
3. **签署要求**：复核人出具书面结论，逐项核对第一节表格并列出未解决 P0/P1；任一未解决 P0/P1 即阻止放行。复核结论以不可变形式归档（本仓库 `docs/verification/` + 外部锚点）。

## 四、今天不能签字放行的项目（保持真实状态）

| 项 | 状态 | 缺少的证据/实现 |
|---|---|---|
| 策略 shadow（长期观察窗口） | 未验收 | 固定策略、预先声明时长/风险门槛的持续 shadow，及 RPC quorum/finality、运行代码变化、限损/安全退出演练 |
| canary 部署 | 未开始 | shadow 通过后的小额可撤销账户；硬资金与亏损上限；自动停机与安全退出 |
| 独立安全审计 | 未委托 | 第三节复核人签署结论 |
| 逐仓位 owner 现金归属 | 未解析 | 6 NFT 核心结算已对账，但 owner 现金分配仍 `unresolved`；不得把整笔净额分给任意 NFT |
| 动态手续费超额收益 | 未证明 | 样本外被动 LP/静态费基准、路由弹性、全成本后净收益 |
| 真实 Permit2 完整生命周期 | 未验证（happy-path 已 fork 验证） | `OasisForkLifecycle.t.sol` 已用真实链上 Permit2（`OASIS_FORK_PERMIT2` + codehash 断言）走 `permit2.transferFrom` 结算完成 mint→swap→collect→revoke→unwind；仍缺恶意代币/重入/回调战役与多账户并发 |

短时健康观察、合成用例与单提供者测试网证据均**不替代**上述验收。任何报告不得把本轮结果描述为上线收益或已审计系统。
