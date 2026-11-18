# 准实盘盈利闭环（Wave 5 / Wave 6）实测与阻断验证记录（2026-11-18）

2026-11-18（Asia/Shanghai）；在 `main` 已推送基线 `6897e30` 上完成 Wave 5 三路并行工程交付与 Wave 6 集成实测。所有主网读取保持零密钥、只读、限额执行；交易签名仅在隔离的回环 `anvil`（`127.0.0.1`，内存临时密钥）中执行，`execution_eligible = false`。

## 1. Wave 5 核心工程交付

- **独立原子/意图签名权威准入与结算对账**（[atomic_authority.py](../../src/oasis_agent/live/atomic_authority.py)、[单测](../../tests/unit/test_atomic_intent_authority.py)）：
  实现 `admit_atomic_or_intent_proposal`、`verify_signed_bytes_match_admission` 与 `reconcile_atomic_or_intent_receipt`。强制校验证实双独立状态源同块一致模拟无回滚且 `profit > L1 + L2 gas`、`target` 代码哈希与内部 calldata 解码精确一致、白名单池/路由/反应器严格校验、广播前对 EIP-1559 原生签名包做逐字段密码学比对，以及结算后原生燃气残差为 `0`、ERC-20 净增量与回滚分级预算严格闭合。
- **Base 主网 4 池实时影子归因器**（[live_shadow_runner.py](../../src/oasis_agent/fastpath/live_shadow_runner.py)、[CLI](../../scripts/run_base_arb_shadow.py)、[单测](../../tests/unit/test_base_arb_shadow.py)）：
  单次 JSON-RPC 批处理固定同一 Base 区块，采集 `Uniswap V3 500`（`0xd0b5...224`）、`Aerodrome Volatile`（`0xcdac...c43` + `Factory.getFee` 动态费率）与两座 `Slipstream`（`0xdbc6...0f1`、`0x4e39...16dd`）状态，联合 `BaseTime`（`0x4200...0030`）及 Base `GasPriceOracle`（`0x4200...000f`）精确 `getL1Fee`，通过 Rust `FastpathPoolGraph` 测算实时毛价差与扣除 L1+L2 燃气后的净价差并生成哈希链式日志 `shadow_attribution.jsonl`。
- **回环 Anvil 全链结算验证与 8 门禁发布评估器**（[verify_atomic_vault_anvil.py](../../scripts/verify_atomic_vault_anvil.py)、[canary_readiness.py](../../src/oasis_agent/live/canary_readiness.py)、[CLI](../../scripts/check_profit_engine_readiness.py)、[单测](../../tests/unit/test_profit_engine_readiness_and_anvil.py)）：
  提供实测真实 EVM 字节码的 4 项端到端 EIP-1559 演练，以及 fail-closed 8 项发布前门禁判定。

## 2. 本机实测数据摘要（归档于 gitignore 目录 `reports/research/prelive-20261118/`）

### 2.1 回环 Anvil 真实 Solidity 字节码结算（4/4 全通过）

报告哈希：`86ff7e2464afa113163560c07557a7f78890cf37c20d11bfb94644a12c86e044`：

| 演练项目 | 实测 Gas 与链上结果 | 资金与权限闭合校验 |
| --- | --- | --- |
| Drill 1：`OasisAtomicExecutor.executeCycle` + `sweepProfit` | `executeCycle` **191,519 gas**；`sweepProfit` **66,622 gas** | `CycleExecuted` & `ProfitSwept` 匹配，`net_profit_raw = 9,550,169,617,820,656,117` 全额归集 `REVENUE_SINK`，原生燃气残差 = `0` |
| Drill 2：`EarlyAborted` 滑点保护探测 | **`58,180 gas`**（< `65,000 gas` 阈值） | `status == 1`、发射 `EarlyAborted`、0 代币损失、`RollingRevertBudgetTracker` 保持 `active` |
| Drill 3：`OasisIntentVault` ERC-1271 域签名与 Epoch 作废 | `rotateQuoteSigner` **36,536 gas** | Epoch 1 签名返回 `0x1626ba7e`；轮换至 Epoch 2 后旧签名即时返回 `0xffffffff`，新签名通过 |
| Drill 4：`OasisIntentVault.executeReactorFill`（`IReactorCallback`） | **`271,931 gas`** | 金库净增 `+100,000,000`（>= `90,000,000` 下限），全部 4 组 ERC-20 router/reactor 残留授权严格为 **`0`** |

### 2.2 Base 主网 4 池真实块影子测量（ honest negative spread 实证拒绝）

于 `https://base-rpc.publicnode.com` 完成 3 tick 有界读取（消耗 `48/48` 请求，零密钥、零重试），串联历史哈希头 `d8445cd0fa8154c7bd76a2b4a46fd15d6943c95f0a241ec8e6cebc04dcf05778`：

| Tick | Base 区块号 | `BaseTime` 毫秒 | L2 Gas 模型（500k） | L1 数据费（`GasPriceOracle`） | 0.1 WETH 最优实测路线与净价差 |
| --- | --- | --- | --- | --- | --- |
| 1 | `58990391` | `1794988453800` | `3,500,000,000,000 wei` | `1,050,896,114 wei` | V3 500bps -> Aero V2：`181.555031 USDC` -> `0.09985918 WETH`，毛差 **`-14.081582 bps`** / 净差 **`-14.431687 bps`** |
| 2 | `58990410` | `1794988457600` | `3,500,000,000,000 wei` | `1,050,668,730 wei` | 同上：毛差 `-14.081582 bps` / 净差 **`-14.431687 bps`** |
| 3 | `58990433` | `1794988462200` | `3,500,000,000,000 wei` | `1,050,668,730 wei` | 同上：净差 **`-14.431742 bps`**（L1+L2 均有效计入；Rust 平均扫描 `7.89 ms`） |

汇总结果：`positive_gross_ticks = 0`、`positive_net_ticks = 0`、`lcb_net_edge_bps = -14.431741 bps`。数据诚实证明：在常规无冲击的 Base 主网公共区块上，高流动 WETH/USDC 静态 2-hop 价差显著为负，不应直接开通实盘资金。

### 2.3 8 项发布门禁评估 verdict

`readiness_report.json`（自哈希 `8ecf0096364b09924eb6805a5f1c65c50e626b7f41e5f785fbd3ee94ee63e909`）输出 **`4/8` 通过、`release_permitted = false`**：

- **已通过（4）**：`compiled_contracts_pinned`、`anvil_e2e_settlement_reconciled`（`58,180` gas early-abort，0 授权残留）、`base_time_predeploy_verified`（Base 块 `58978574`）、`revert_budget_healthy`。
- **仍阻断（4）**：`untouched_shadow_sample_size`（3 < 100）、`positive_cost_adjusted_lcb_edge`（`-14.431741 bps < 5 bps`）、`dual_provider_and_isolated_signer_ready`（缺双独立 RPC 与隔离签名器证明）、`owner_scope_release_manifest_present`（缺 owner 当次授权范围清单）。

## 3. 安全边界结论

技术闭环（合约不变量、EIP-1559 字节校验、Anvil 真实结算、实时 Base 读盘与 8 门禁总闸）已经完整就位；而安全护栏按设计成功拦截了负预期行情下贸然上实盘的行为。进入实盘的唯一合法路径是按 [准实盘盈利手册](../operations/PROFIT_ENGINE_PRELIVE_RUNBOOK.md) 依次消解剩余 4 个阻断门。
