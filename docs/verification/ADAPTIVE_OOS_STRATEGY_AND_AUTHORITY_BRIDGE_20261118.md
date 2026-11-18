# Wave 13–14 自适应做市对冲、样本外持久化执行、策略-权威桥接与容量雷达核验（2026-11-18）

2026-11-18（Asia/Shanghai）；在 `d2e613e`（Wave 12 已推送基线）之上并行完成 **Wave 13** 三路子代理开发与 **Wave 14** 联调实测，直接针对 Wave 12 暴露的三项核心瓶颈进行工程闭环：**微额碎单全额 gas 与盘间波动率逆向选择（自适应 `effective_adverse_bps` + `$10` 起批合并对冲与双向自然净额抵消）**、**无前视偏差的 `t -> t+1` 样本外持续性归因与 Gate 5 双模准入**，以及 **`profit-strategy` 到 `profit_authority.sock` 的无密钥桥接守护进程与 `/api/v1/radar/capacity` 只读容量 API**。

全程严格保持无真实私钥、`execution_eligible = false`、`release_permitted = false`、无真实链上广播与付费 RPC 消耗。本文档衔接并保留先前核验记录（[DYNAMIC_CAPACITY_RISK_AND_WS_MAKER_20261118.md](DYNAMIC_CAPACITY_RISK_AND_WS_MAKER_20261118.md)、[ECOSYSTEM_MAKER_LIQUIDATION_AND_AUTHORITY_20261118.md](ECOSYSTEM_MAKER_LIQUIDATION_AND_AUTHORITY_20261118.md) 与 [BASE_100TICK_MULTICALL_CAMPAIGN_20261118.md](BASE_100TICK_MULTICALL_CAMPAIGN_20261118.md)）。

## 1. Wave 13–14 核心架构与工程交付

- **分支 1 — 波动率自适应做市与有界批量库存对冲（`4e677df`，[maker_hedge_shadow.py](../../src/oasis_agent/cross_venue/maker_hedge_shadow.py)、[单测 1](../../tests/unit/test_maker_hedge_shadow.py)、[单测 2](../../tests/unit/test_coinbase_trade_flow_and_fifo_maker.py)）**：
  - `MakerHedgeConfig` 与 `MakerQuoteCandidate` 新增可回退的 `adaptive_volatility_enabled`、`volatility_multiplier`（`1.5`）、`volatility_window_steps`（`16`）与 `max_adaptive_adverse_bps`（`60.0`），基于按产品维护的最近跨 tick mid 价格波动序列动态计算 `effective_adverse_bps`，在高波时自动拉宽被动报价缓冲而不影响静态基线（默认为 `False`）。
  - `MakerHedgeLifecycleTracker` 新增 `UnhedgedMakerLot` 与有界批量对冲（`min_hedge_notional_usdc`、`max_acceptable_gas_drag_bps=5.0`、`max_unhedged_steps=8`、`max_unhedged_drift_bps=15.0`）：当同产品出现反向被动成交时优先零链上 gas 自然净额抵消（`natural_netted` / `natural_netted_fills`）；未抵消微额成交暂存为 `UnhedgedMakerLot` 并计入实时 `unhedged_exposure_usd_micros()` 与 `unrealized_drawdown_usd_micros()`；当累积名义规模达到动态 gas 拖累下界、超期 `8` 步、逆向漂移达到 `15 bps` 止损或结束前显式调用 `flush_all_unhedged(...)` 时合并单笔 DEX gas 对冲清零（`batched_fifo_hedges`）。
- **分支 2 — 真实事件时间 `t -> t+1` 样本外持续性跟踪与 Gate 5 双模检验（`8ed7ba1`，[oos_execution_tracker.py](../../src/oasis_agent/cross_venue/oos_execution_tracker.py)、[canary_readiness.py](../../src/oasis_agent/live/canary_readiness.py)、[check_profit_engine_readiness.py](../../scripts/check_profit_engine_readiness.py)、[OOS 单测](../../tests/unit/test_oos_execution_tracker.py)、[就绪与 Anvil 单测](../../tests/unit/test_profit_engine_readiness_and_anvil.py)）**：
  - `OosStrategyExecutionTracker` 对每笔 Taker 信号执行严格连续 `taker_persistence_ticks=2` 同向持续性过滤（拒斥单 tick 脉冲信号并计入 `rejected_one_tick_flickers`），于 `t` 入队 `PendingTakerIntent`，于 `t+1` 使用 `t+1` 新鲜链上 DEX 报价与 Coinbase 盘口结算实现盈亏（零前视偏差，超时信号自动作废）；同时对跨快照传入的已对冲 Maker `strict_cross` / `trade_flow_fifo` 成交按组合键去重归因。
  - `canary_readiness.py` Gate 5（`positive_cost_adjusted_lcb_edge`）与 `check_profit_engine_readiness.py` 新增 `--min-oos-trades`（预设门槛不变：`DEFAULT_MIN_OOS_TRADES = 5`、`DEFAULT_MIN_LCB_NET_EDGE_BPS = 5`），保持传统 `shadow_attribution` 验证完整并允许严格整数类型的样本外统计通过（要求 `oos_trades_count >= 5`、`oos_positive_trades > 0`、`oos_total_net_profit_usd > 0` 且 `type(oos_lcb_net_edge_bps) is int` 与 `>= 5`），同时始终在 `report.evidence["oos_execution"]` 输出明细。
- **分支 3 — 无密钥策略-权威桥接守护进程、Compose 硬化与容量雷达 API（`ec78402`，[profit_strategy_daemon.py](../../src/oasis_agent/live/profit_strategy_daemon.py)、[run_profit_strategy.py](../../scripts/run_profit_strategy.py)、[compose.yaml](../../deploy/profit-engine/compose.yaml)、[verify_profit_compose.py](../../scripts/verify_profit_compose.py)、[Socket 与 Compose 单测](../../tests/unit/test_profit_authority_socket_and_compose.py)、[routers_radar.py](../../src/oasis_agent/server/routers_radar.py)、[console_contract.py](../../src/oasis_agent/server/console_contract.py)、[雷达 API 单测](../../tests/unit/test_server_radar_api.py)）**：
  - `ProfitStrategyBridge` 通过 `/run/oasis/profit_authority.sock` 与 `ProfitAuthoritySocketClient` 同步实时 `status()`（停机、熔断、过期、回滚预算与 `pending_count`），按 `--risk-config` 自动构建 `AccountCapitalSnapshot` 与 `HeadroomAllocation`；严格故障闭锁（STOP、HALT、未决残差或可用额度归零立即拒绝提案），默认 `shadow` 只读状态轮询，支持 `dry_run` 与受控双仿真-单次广播-异常即停-双源对账流程；完全不含私钥或 `eth_account`。
  - [compose.yaml](../../deploy/profit-engine/compose.yaml) 将 `profit-strategy` 与 `profit-strategy-recovery` 切换为 `python -m scripts.run_profit_strategy --mode shadow` 并仅挂载只读 `/etc/oasis/profit-engine-risk.json:ro` 与共享 `/run/oasis/profit_authority.sock`，禁止任何密钥卷挂载；[verify_profit_compose.py](../../scripts/verify_profit_compose.py) 新增 `--compose-file` 校验并全部通过。
  - 新增只读端点 `GET /api/v1/radar/capacity` 并注册于 `CONSOLE_VIEWS`（`"research"` 视图），严格通过 `_load_verified_radar` 与 `_RADAR_JSON_LIMIT` 防篡改/故障闭锁输出配置本金/日亏策略、实时可用额度、最新 OOS 统计、各资产盘口深度最优手数以及永久锁定 `execution_eligible: false` / `release_permitted: false`。
- **Wave 14 联调（[run_multicall_100tick_campaign.py](../../scripts/run_multicall_100tick_campaign.py)、[联调单测](../../tests/unit/test_dual_rpc_and_cex_dex_shadow.py)）**：
  - 当 `--dynamic-sizing` 开启时自动启用 `adaptive_volatility_enabled=True` 与 `min_hedge_notional_usdc >= $10`，将 `maker_tracker.unhedged_exposure_usd_micros()` 与 `unrealized_drawdown_usd_micros()` 实时接入每 tick `AccountCapitalSnapshot`，并在最终 tick 执行 `flush_all_unhedged`（确保 `open_unhedged_lots = 0`、`unresolved_residuals = 0`）；对每次挂起 Taker 意图均使用 `t+1` 同名义规模精确 DEX 报价与实时 BBO 结算，并将 `oos_step` 写入逐 tick 哈希链记录、`oos_execution_summary` 写入 `shadow_summary.json` 及标准输出。

## 2. Wave 14 未触碰实测结果（归档于 `reports/research/wave14-20261118/`）

### 2.1 部署基线、Anvil 结算与双源 Base RPC 仲裁

| 核验项 | 核心实测结果 | 证据 SHA256 / 块锚点 |
| --- | --- | --- |
| Compose 策略隔离开关（`profit_compose_verification.json`） | `passed = true`，`services = 6`，`network_calls = 0`，`execution_eligible = false` | `compose_hash = b3821a8f...`，`evidence_hash = 625b6077...` |
| 无密钥双配置隔离签名演练（`authority_rehearsal.json`） | `$500/$200` 与 `$5,000/$750` 双配置均 `passed = true`，`ephemeral_dirs_cleaned = true` | `report_sha256 = 141b5fe7778ecff12d45865daf4c3cbe7a973cd22a643a2d5cf6f87c2c18f79e` |
| 回环 Anvil 字节码全结算（`anvil_e2e_report.json`） | `all_drills_passed = true`（`4/4` 演练通过，`early_abort = 58,204 gas`，`0` 授权残差） | `report_sha256 = d4a7f361c89bddde1adaa90a8e25f55d5128fc81f83790c5cf149f921c105218` |
| 双源 Base RPC 仲裁（`dual_rpc_quorum.json`） | `publicnode_base` 与 `base_official_mainnet` 于固定块 **`59099492`** 一致，`secondary_rpc_verified = true` | `block_hash = 0x7b586ccb...`，`report_sha256 = a6ba88f645bc...` |
| 只读雷达与 BaseTime（`radar/radar_report.json`） | 公开只读采集通过，BaseTime 预部署于块 **`59099792`** 一致 | `report_sha256 = 03a459315f7776379b2ba0a5dab6a8a6055cb4f95e2cbadeb218da3f55ad1f31` |
| 2-Tick 实盘流联调（`smoke-2/`） | `ticks_completed = 2`，WS `matches` 实收 `239` 笔、`0` 断流、`44` 池、`7` RPC | `summary_sha256 = 1c62a40fff01c5bc56662bc6607f6b690687a893095241454e9344a58e64a192` |

### 2.2 100-Tick 连续主网 + 实时 Coinbase WS 自适应与样本外执行实测（`shadow-100-adaptive-oos/`）

- **稳定度与规模指标**：使用 `--risk-config configs/profit-engine-risk.example.json --dynamic-sizing --enable-coinbase-ws --include-ecosystem-tokens --ticks 100 --interval-ms 2000 --max-rpc-calls 600` 连续覆盖 Base 主网区块 `59099892..59104859`（**`100` 个唯一区块**、`ticks_completed = 100`、`44` 座池、`203 / 600` RPC 预算），Coinbase WebSocket `matches` 实时流 **`100 / 100` tick 零断流、零重启、零 REST 回退**，累计实时处理 **`18,283`** 笔成交（`BTC-USD: 15,667`、`ETH-USD: 1,895`、`VVV-USD: 467`、`AERO-USD: 223`、`MORPHO-USD: 10`、`WELL-USD: 7`、`CBETH-USD: 6`、`DEGEN-USD: 4`、`VIRTUAL-USD: 4`）；哈希链尾 `0ae03ba93c275c33de58b7cce07be4e966449e4ab5b9188cb51142e37b36b884`，汇总自哈希 **`9052a964e38568f4201b84d8c134abdc05d03d6638e56010e142928cf7335979`**。
- **自适应做市与批量对冲效果（对比 Wave 12 微负 FIFO）**：
  - `maker_hedge_summary` 启用 `adaptive_volatility_enabled = true` 与 `$10` 批量门槛后，完成 **`5` 次批量对冲**，共产生 **`2` 笔 FIFO 成交（`2 / 2` 全正、`+48.511838 bps` 均值、总净盈利 `+$0.145091` / `$68.442074` 成交额）** 与 **`3` 笔 `strict_cross`（`2 / 3` 正、`+$0.011054`、`+0.081808 bps` 均值）**；从 Wave 12 的 `-$0.177101` 显著反转为净正盈利，`open_unhedged_lots = 0`、`unhedged_exposure_usd_micros = 0`、`unhedged_fill_warnings = 0`、`unresolved_residuals = 0`。
- **样本外 `t -> t+1` 实盘执行统计（`oos_execution_summary`，零前视偏差）**：
  - 共实现 **`12` 笔完整样本外成交（Taker `7` 笔 + Maker `5` 笔）**，其中 **`10 / 12` 为正净盈利（胜率 `83.33%`）**，总净盈利 **`+$0.232804`**（扣除 Base 链上 gas 与 Coinbase 手续费后），平均样本外净边际 **`+9.541982 bps`**，最大回撤仅 **`$0.027576`**，5 分位尾损 `-1.795034 bps`，`pending_unsettled_intents = 0`、`expired_pending_intents = 0`。
  - **Taker 2-Tick 持续性过滤**：`7` 笔结算 Taker 交易 **6/7（`85.7%`）为正盈利**（`WELL-USD` `$17.93` `+3.24 bps`、`$17.23` `+1.75 bps`；`AERO-USD` `$53.32` `+0.43 bps`、`$43.49` `+3.54 bps`、`$51.73` `+6.52 bps`、`$79.57` `+2.96 bps`，仅收尾第 71 tick `$59.10` 小亏 `-$0.007140` / `-1.21 bps`），成功过滤未延续单脉冲。
  - **Maker OOS 成交**：`WELL-USD` ask_maker FIFO `$63.02` 净赚 `+$0.101751`（`+16.19 bps`）、`CBETH-USD` `$5.42` 经 8-step 超时批量平仓净赚 `+$0.043341`（`+80.84 bps`）、`AERO-USD` 两笔 `strict_cross` 分别净赚 `+$0.022907`（`+1.02 bps`）与 `+$0.008583`（`+1.02 bps`），仅一笔 `AERO-USD` 小亏 `-$0.020436`（`-1.80 bps`）。
- **统计门禁诚实诊断（绝不事后放宽阈值）**：
  - 按预注册 Gate 5 高斯置信下界公式计算：`9.541982 - 1.96 * 22.939311 / sqrt(12) = -3.437156`（即 `oos_lcb_net_edge_bps = -4 < 5`）。尽管 12 笔样本外交易 **胜率高达 `83.33%`、均值 `+9.54 bps`、总净赚 `+$0.2328`、最大回撤不到 `$0.03`**，但 `CBETH-USD` 单笔正尾部离群收益（`+80.84 bps`）大幅拉高了双边样本标准差（`22.939311 bps`），使正态 95% LCB 转为负值。
  - 依据严格工程原则，Wave 14 保持 Gate 5 阈值（`min_oos_trades=5`、`min_lcb_net_edge_bps=5`）毫不动摇，如实保留阻断状态，并指明后续长窗口积累更充分 `N`（例如 400–1,000 tick 多时段窗口以降低 `1/sqrt(n)` 方差项）的推进方向。

### 2.3 9 门禁就绪评估（`readiness_report.json`）

搭配 `--risk-config configs/profit-engine-risk.example.json`（`risk_config_sha256 = f9840fe3306efbaaadc35e0c3b246f899d12c039af5f1aee21c9ea6036c7c9a6`）与 Wave 14 `dual_rpc_quorum.json` 生成 **`6 / 9` 通过**（`report_sha256 = 505227242c8c249ca03e9e16c3ab5498cf600376f755959a1e0755cabcf3f853`），`release_permitted = false`、`execution_eligible = false`：
- 通过门禁：`compiled_contracts_pinned`、`anvil_e2e_settlement_reconciled`（`58,204 gas`）、`base_time_predeploy_verified`（块 `59099792`）、`untouched_shadow_sample_size`（`100`）、`revert_budget_healthy`（`unresolved_residuals=0`）、`dual_rpc_quorum_verified`（块 `59099492`）。
- 阻断门禁：`positive_cost_adjusted_lcb_edge`（`oos_trades=12`, `oos_pos=10`, `oos_profit_usd='0.232804'`, `oos_lcb_bps=-4 < 5`）、`isolated_signer_authority_ready`（需实盘隔离签名卷与 socket 证明）、`owner_scope_release_manifest_present`（需 owner 签字发布范围清单）。

## 3. 全仓库 CI 与安全门禁

- 合并后及 Wave 14 终版执行全套 CI 与安全检查（详见交付报告真实输出与计数）：`make rust-check`、`make profit-deployment-check`、`make security-check`（`pip_audit`、`bandit -r src scripts -ll`、`scripts.check_secrets`）、`make contract-security-check`（`slither`）、`make check`（`ruff`、`mypy src`、`pytest`、`forge test`、`forge lint`）、`git diff --check` 与相对路径文档链接核验。
