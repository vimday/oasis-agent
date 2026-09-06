# LP 模块、归属与 shadow 决策日志闭合说明（实现侧）

日期：2026-09-07（Asia/Shanghai）
范围：把此前“未解决项”中可由代码与测试交付的部分全部实现并接线；独立审计证据包归档。
结论：**实现侧自查门禁全部通过；自动 LP、canary 与任何资金执行依旧不放行。**

本轮无部署、签名、广播、修改线上配置或资金使用。所有门禁在本机复跑通过。本文件是
实现侧自查记录，**不构成独立验收**；放行前仍须按
`docs/verification/2026-09-06-closure-acceptance-checklist.md` 第三节指定复核人签署。

## 一、本轮交付的模块与测试

| 模块 | 实现 | 语义要点 | 测试 |
|---|---|---|---|
| 多仓位 owner 现金归属 | `farming/batch_settlement.py` `resolve_batch_owner_cash` | 三重交叉验证（调用轨迹核心 delta + 收据/全块 Transfer + 块余额差）；币种被多个仓位共用、出现第三方收付、池用原生币时一律 `UnsupportedBatchAllocation` 失败闭合，绝不按比例臆分 | `tests/unit/test_batch_settlement.py`（含双池物理归属与全部失败路径） |
| 原生代币付款归属 | `farming/native_payments.py` `NativeOwnerPaymentCapture` | owner 原生余额变化 = 结算额 + 执行 gas 扣减；gas 口径分链（Ethereum 本地区块 / OP Stack L1 数据费）；WRAP/UNWRAP、第三方、重复 settle/take 全部失败闭合；配对 ERC-20 用全块 Transfer 证据交叉核对 | `tests/unit/test_native_payments.py` |
| shadow LP 决策日志 | `shadow/lp_runner.py` `LpShadowRunner` + `shadow/journal.py` decisions 链 | 观测专用，无签名无广播；每条决策 `execution_eligible=False`；健康 HALT 跨重启锁存；评估失败落 HALT 记录且不留异常文本；决策链哈希链接、可查篡改；Decimal 规范化序列化保证同义决策同哈希 | `tests/unit/test_shadow_lp_runner.py`（11 例，含链完整性/篡改检测） |
| 合约 lint 豁免 | `contracts/src/OasisPositionExecutor.sol` 等 3 文件 | 每个 slither/forge-lint 豁免注释绑定精确语句并写明理由（deadline 到期保护、弃用 position-info 返回值、memory-safe 打包 tick 汇编、immutable 常量命名、CREATE2 init-code 哈希）；使用两工具各自的块级语法避免互相遮蔽 | `make contract-check` + `make contract-security-check` 复跑通过 |

前置已交付（上一轮提交）：`farming/strategy.py` 离散单调带表手续费、`execution/lp_state_machine.py`
状态机、`execution/range_controller.py` 区间/库存控制器、`risk/lp_policy.py` canary 上限与失败矩阵、
`contracts/src/OasisPositionExecutor.sol` 有界原子 RECENTER/REMOVE 执行器。

## 二、门禁复跑结果（2026-09-07）

| 门禁 | 结果 |
|---|---|
| `make python-check` | `All checks passed!`；499 passed + 96 subtests；ruff/mypy 全过 |
| `make security-check` | pip-audit 无已知漏洞；bandit 严重度 Medium/High 为 0；secret gate 通过 |
| `make contract-check` | Foundry 4 套件 32 passed，0 failed，0 skipped |
| `make contract-security-check` | slither（80 contracts, 102 detectors）0 result |

## 三、Git 基线

```text
afd39df chore: document deliberate lint exceptions in contracts
3fc7742 feat: shadow LP decision runner journals hash-linked decisions
e4b67cf feat: native token payment attribution with gas reconciliation
5c6a5fa feat: multi-position owner cash allocation with unique physical attribution
4453867 feat: bounded atomic RECENTER/REMOVE position executor contract (blueprint 4.3/6.5)
b1ea312 feat: LP risk policy with canary sizing and failure matrix (blueprint 9.1/9.2)
6d42d8e feat: LP rebalance state machine and range/inventory controller (blueprint 6.2-6.5)
b0291c9 feat: discrete monotone band-table fee strategy (blueprint 5.3)
```

## 四、仍未放行（保持真实状态）

| 项 | 状态 | 缺失 |
|---|---|---|
| 策略 shadow 长期观察窗口 | 有实现，未验收 | runner 已就绪，但尚未按预先声明的时长/风险门槛持续运行，也未经 RPC quorum/finality、运行代码变化、限损/安全退出演练 |
| canary 部署 | 未开始 | shadow 观察通过后的小额可撤销账户；硬资金与亏损上限；自动停机与安全退出 |
| 独立安全审计 | 未委托 | 复核人签署结论；外部审计候选未委托前不得宣称已审计 |
| 动态手续费超额收益 | 未证明 | 样本外被动 LP/静态费基准、路由弹性、全成本后净收益 |
| 真实 Permit2 完整生命周期 | happy-path 已 fork 验证 | 恶意代币/重入/回调战役与多账户并发 |

短时健康观察、合成用例与单提供者测试网证据均**不替代**上述验收。任何报告不得把本轮
结果描述为上线收益或已审计系统。
