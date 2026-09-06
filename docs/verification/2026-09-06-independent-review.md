# Phase 1 研究闭合独立复核报告

日期：2026-09-06（Asia/Shanghai）
复核人：独立复核 AI 会话（实现会话之外，按验收清单第三节第 1 项指定）
复核对象：`docs/verification/2026-09-06-closure-acceptance-checklist.md` 全部验收项
复核依据：`REVIEW.md` 严重度框架（P0–P3）、仓库源码、`reports/evidence/` 证据包、v4-core/v4-periphery 规范源码

## 一、复核结论

**未发现 P0/P1 问题。** 验收清单各 "verified" 项经独立源码审计、规范对照、门禁复跑与证据校验后成立；实现与 v4 规范语义一致，证据自洽，未发现伪造、重复扣减、方向颠倒或 fail-open 行为。

**仍不放行**：自动 LP、canary 部署与任何资金执行。所有输出保持 `execution_eligible=false`、`trust=single_provider_research`，与验收清单第四节一致。

## 二、逐项复核

| 验收项 | 独立复核动作 | 结论 |
|---|---|---|
| 整笔交易全部 ERC-20 参与人收付款 | 审计 `transaction_payments.py`：EIP-1898 钉块、前/后块头双路径校验、每账户 before/after 与全块 Transfer 净额核对、总供应量残差核对、receipt 与全块日志一致性、log_index 有序唯一、代币运行时码前后块一致、请求严格配对（canonical JSON）、尺寸上限 | verified（见 §三.1） |
| 6 个 NFT 核心结算对账 | 审计 `batch_settlement.py`：trace 全帧递归收集 modifyLiquidity 调用、receipt 事件数与调用数相等、逐仓位 caller_delta 求和与 PoolManager 实际净现金流逐币对账；独立重算 `batch-settlement.json`：USDC 6,555,495 / 0xa6a5 150,044,763,288,615,473,218,646 / 0xdb2b 12,643,693，三币残差全零 | verified（见 §三.2） |
| 整笔原生成本（执行费+L1 数据费） | 审计 `transaction_costs.py` 链家族分派；独立验证 `execution_native + l1_native = total_native`（20,085,342,000,000 + 7,643,809,493 = 20,092,985,809,493）；operator/blob 路径分支正确 | verified |
| 可靠价格接入成本估值 | 审计 `price_evidence.py`：同一钉块快照、运行时哈希/描述/decimals/round 有效性/新鲜度链上校验；独立重算 `value_native_cost` 公式（`amount*native*10^(qd+quote_decimals) / (10^(18+nd)*quote)` 向上取整）与 `usd_nominal`（49,843 × 0.9998968 → 49,838），均与证据吻合；无 1:1 钉住假设 | verified |
| shadow 健康门禁 | 审计 `shadow/journal.py` 与 `test_shadow.py`：SHA256 哈希链、HALT 跨重启锁存、policy 不可变、篡改检测、时钟回归/重组/断链检测、故障不保留异常文本；核对 `robinhood-shadow-contiguous` 3 条连续 HOLD 的 previous 链接 | verified（短窗口；不等于策略 shadow 验收） |
| 密钥扫描 CI 门禁 | 审计 `check_secrets.py` 与 `ci.yml`：scanner 版本锁定、baseline 审核字段强制、候选值 redacted；CI actions 全部 pin SHA、`contents: read`、hash-locked 依赖 | verified |

## 三、关键语义验证（源码级对照）

1. **BalanceDelta 方向语义**：对照 v4-core `PoolManager._close`（`currencyDelta < 0 → settle`，即负 delta = 用户支付），确认 `batch_settlement` 的 `actual == -claims` 检查、`owner_payments` 的 `amounts == caller_delta` 检查与 v4 规范一致，非"碰巧通过"。`decode_balance_delta` 字节序（高 128 位 = amount0）与 Solidity `toBalanceDelta` 一致。
2. **Actions 编码**：`0x00`=INCREASE、`0x01`=DECREASE、`0x12`=CLOSE_CURRENCY 与 v4-periphery `Actions.sol` 一致；`owner_payments` 的 `actions in (0x001212, 0x011212)` 与 delta 符号推导正确。
3. **Hook 手续费方向**：`previewFee` 对 exact-input/exact-output × zeroForOne 四象限的"指定币"判断全部正确（与 v4 amountSpecified 语义逐项验证）；PositionInfo tick 偏移（8/32）与 `PositionInfoLibrary` 一致；阈值按币种显式配置，未跨币种比较。
4. **成本不重复**：OP 家族用 receipt `l1Fee` 直接读取、Nitro 拆分 `gasUsedForL1` 并从 execution 中扣除、Ethereum blob 单独核算且非 Ethereum 拒绝 type-3；`full_cost.py` 对 IL/LVR/protocol_hook 强制 EMBEDDED 防二次扣减，同一证据 content_hash 不得重复扣减。
5. **owner 归属诚实性**：整笔净额从不分给任意 NFT；`position_cash_allocation`/`individual_owner_cash_allocation` 保持 `unresolved`，`net_pnl`/`owner_amount` 保持 None。

## 四、发现

### P2（生产前需处理）

- **P2-1 验收清单表述与 fork 证据不符（已修复）**：清单第四节称真实 Permit2 "现为 transfer-only mock"，但 `contracts/fork/OasisForkLifecycle.t.sol` 已通过 `OASIS_FORK_PERMIT2`（codehash 断言）使用真实链上 Permit2 完成 mint→swap→collect→revoke→unwind。已核实 v4-periphery `_settle` 在 allowance 场景走 `permit2.transferFrom(payer, address(poolManager), ...)`，fork 测试真实覆盖该路径。清单已按此修正为"happy-path 已 fork 验证；仍缺恶意代币/重入/回调战役与多账户并发"。
- **P2-2（更正为误报）**：初判"CI 未覆盖真证据重放"不成立。已核实 `reports/evidence/` 全部被 git 跟踪（含 `evidence.db`），且 `ci.yml` python job 运行全量 `make python-check`（pytest 含 `test_recorded_transaction_closure.py`），真证据重放在 CI 中已覆盖。无需变更。

### P3（改进）

- **P3-1** `full_cost.reconcile_full_cost` 中 `blocks` 按块号索引，若 `canonical_blocks` 含同号异块（重组场景）会静默覆盖；建议调用方显式拒绝同号异块。
- **P3-2** slither 3 类 informational 提示（assembly/naming-convention/too-many-digits）为有意设计，无风险；forge lint advisory notes 已文档化。
- **P3-3** `transaction_payments` 对区块内非标准 Transfer（topic0 匹配但 topic 数 ≠ 3）整笔拒绝，属 fail-closed 保守行为，建议文档明示该边界。

## 五、独立复现记录

| 门禁 | 结果 |
|---|---|
| `make python-check`（HEAD c75db01） | 422 passed + 70 subtests；mypy 75 源文件；ruff 全过 |
| `make security-check` | pip-audit 无已知漏洞；bandit Medium/High 0；secret gate 通过 |
| `make contract-check`（FOUNDRY_PROFILE=ci） | 18 passed，0 failed，0 skipped（16 unit + 1 integration + 1 invariant 512 轮） |
| `make contract-security-check` | slither `--fail-low` exit 0（仅 informational） |
| 真证据重放 `test_recorded_transaction_closure.py` | 3 passed（6 NFT 对账 / OP 成本 / 价格估值） |
| 证据 SHA256（base-price 4 文件） | 4/4 与验收清单一致 |
| 批量结算独立重算 | 3 币残差全零，与 `batch-settlement.json` 一致 |
| Robinhood/Base pinned fork 复跑 | **未能独立复现**：公共 RPC 端点 2026-09-06 复跑时不可达（robinhood 报 `metadata is not found`，两端点均返回 transport failure，细节被安全 redact）。已有证据（`result.json` exit 0、`postflight_matches=true`、完整 wire 记录、三合约 codehash）完整，但独立复现需端点恢复后重跑 |

## 六、未解决 P0/P1

无。

## 七、复核签署

复核人（独立 AI 会话）签署：**研究证据链可复核验收；不放行自动 LP、canary 与任何资金执行。** 第四节"今天不能签字放行的项目"维持原状，其中"真实 Permit2 完整生命周期"按 P2-1 修正表述后维持未验收。

复核签署时间：2026-09-06（Asia/Shanghai）
复核 Git 基线：`c75db01`
