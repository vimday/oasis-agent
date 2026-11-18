# Wave 17–18 Coinbase Limit IOC 优先成交、并发亚块级采集与 300-Tick 主网复测核验（2026-11-19）

2026-11-19（Asia/Shanghai）；在 `4484369`（Wave 16 已推送基线）之上通过并行合入 **Wave 17**（分支 `66b3743`、`ce8389e`）并执行 **Wave 18** 全套前置/WSL Docker/300-tick 连续主网实测，直击 Wave 16 暴露的三项微结构延迟与逆向选择病因（**CEX Limit IOC 优先执行 + 保守 DEX 缺失平仓**、**线程局部 TLS 连接池与 9 并发 L2 盘口预拉取将每 tick 压缩至 `2.040s`**、**动态做市门槛上调至 `10.0 bps` 并新增 `--allowed-products` / `--min-taker-edge-bps` / `--maker-min-edge-bps` 显式约束**）。

全程严格保持无真实私钥落盘或入库、预注册 Gate 5 阈值毫不动摇（`min_oos_trades = 5`、`min_lcb_net_edge_bps = 5`）、`execution_eligible = false`、`release_permitted = false`。本文档衔接并保留先前核验记录（[SOLO_EOA_CUSTODY_AND_WSL_DOCKER_20261118.md](SOLO_EOA_CUSTODY_AND_WSL_DOCKER_20261118.md)、[ADAPTIVE_OOS_STRATEGY_AND_AUTHORITY_BRIDGE_20261118.md](ADAPTIVE_OOS_STRATEGY_AND_AUTHORITY_BRIDGE_20261118.md) 与 [准实盘操作手册](../operations/PROFIT_ENGINE_PRELIVE_RUNBOOK.md)）。

## 1. Wave 17–18 核心架构与工程交付

- **分支 1 — 样本外 CEX Limit IOC 优先执行与保守平仓（`66b3743`，[oos_execution_tracker.py](../../src/oasis_agent/cross_venue/oos_execution_tracker.py)、[OOS 单测](../../tests/unit/test_oos_execution_tracker.py)）**：
  - `OosTrackerConfig` 新增可回退字段 `cex_ioc_limit_enabled: bool = False`，`PendingTakerIntent` 新增队列决策时刻限价 `limit_cex_price_usd: Decimal | None = None`（`BUY_DEX_SELL_CEX` 取 `t` 时刻 `best_bid`，`SELL_DEX_BUY_CEX` 取 `best_ask`），`OosStepResult` 与 `summary()` 新增 `cancelled_ioc_intents` 与 `cex_ioc_limit_enabled`。
  - 开启 `cex_ioc_limit_enabled=True` 后，于 `t+1` 先检 Coinbase Limit IOC 成交条件（`best_bid_{t+1} >= limit` 或 `best_ask_{t+1} <= limit`）：若 CEX 盘口劣变则 `$0` 手续费 / `$0` gas 撤单；若 IOC 成交则无条件在 `t+1` Base DEX 对冲结算（忠实保留 DEX 滑点与亏损），若 `t+1` DEX 报价意外缺失则执行按反向 BBO 承担双边 Taker 手续费的保守平仓 `_unwind_filled_cex_leg(...)`，绝不漏记已成交风险头寸。
- **分支 2 — 线程安全 HTTP 池、并发盘口预拉取与风控选择性（`ce8389e` + Wave 18 接线，[run_multicall_100tick_campaign.py](../../scripts/run_multicall_100tick_campaign.py)、[联调单测](../../tests/unit/test_dual_rpc_and_cex_dex_shadow.py)）**：
  - `_KeepAliveHttpsPool` 升级为按 `threading.local()` 隔离每线程 HTTP/1.1 Keep-Alive TLS 套接字，配合 `threading.Lock()` 统一登记并在 `close()` 时全量释放。
  - 新增 `max_book_workers`（`--max-book-workers`，默认 `9`）：在 `public-shadow` 下对各资产 L2 盘口使用 `ThreadPoolExecutor` 并发预拉取并仍按 `CEX_DEX_TOKEN_ORDER` 确定性顺序归因与构链。
  - 新增可配置过滤与门槛：`--allowed-products`（按产品白名单约束 WS 与每 tick 评估，写入 `summary["allowed_products"]`）、`--min-taker-edge-bps`（`dynamic_sizing` 默认由 `1` 提升至 `3` 并自动开启 `cex_ioc_limit_enabled=True`）及 `--maker-min-edge-bps`（`dynamic_sizing` 默认由 `3.0` 提升至 **`10.0 bps`** 且 `volatility_multiplier = 2.0`）。

## 2. Wave 18 跨机与 300-Tick 未触碰实测结果（归档于 `reports/research/wave18-20261119/`）

### 2.1 前置基线与 `aleph` WSL 真实 Linux Docker 复验

- `profit_compose_verification.json`：`passed = true`、`compose_hash = b3821a8f...`、`execution_eligible = false`。
- `authority_rehearsal.json`：双配置无密钥演练 `passed = true`（`report_sha256 = 141b5fe7...`）。
- `anvil_e2e_report.json`：回环 Anvil 4/4 全结算通过（`early_abort = 58,204 gas`，0 残差，`report_sha256 = a4d181e0...`）。
- `dual_rpc_quorum.json`：`publicnode_base` 与 `base_official_mainnet` 于固定块 **`59157946`** 一致（`secondary_rpc_verified = true`、`report_sha256 = ff19a9e0...`）。
- `radar/radar_report.json`：块 **`59157964`** BaseTime 交叉验证通过（`report_sha256 = 02731038...`）。
- `aleph` WSL 真实 Docker 复验（基于最新 Wave 17 代码流重建并全清临时卷）：`docker_confinement_report.json` `passed = true`、`rehearsal_cleaned = true`、`uid/gid = 10001`，`wsl_authority_status_proof.json` 产出有效 Gate 8 证明（`isolated_key_volume = oasis-rehearsal-66e618f28545-canary-keys`、`authority_socket_path = /run/oasis/profit_authority.sock`）。

### 2.2 并发 300-Tick 主网 + Live Coinbase WS 实测对照（`shadow-300-ioc-fast/` vs Wave 16）

- **采样吞吐提速近 4×、零限流零断流**：使用 `--ticks 300 --interval-ms 1500 --max-rpc-calls 900 --include-ecosystem-tokens --risk-config configs/profit-engine-risk.example.json --dynamic-sizing --enable-coinbase-ws`，300 tick 总耗时仅 **`612.08 秒`（均速 `2.040 秒/tick`，对比 Wave 16 `~8.0 秒/tick`）**；连续覆盖 Base 区块 **`59157950..59160987`**（`300` 个唯一区块、`44` 座池、`603 / 900` RPC 预算），`2,700 / 2,700` Coinbase L2 评估 **`0` 次失败 / `0` 次 HTTP 429**，Coinbase WS **`300 / 300` 零断流、`21,358`** 笔成交。链尾 `d72a849a...`，汇总自哈希 **`9901cb3475867b591fcdf5943c2a35646729350e1d8697d37a075e0c1b4f9465`**。
- **高纪律性零回撤实证**：在该平稳 10 分钟窗口内，市场最优原始价差峰值仅 `+5.297134 bps`（低于 Wave 16 峰值 `+27.41 bps`）：
  - Taker 触发 **`0`** 笔低质量下单与 `0` 笔 `strict_cross` 逆向穿价毒流量（Wave 16 为 `10` 笔亏损 `-$0.1975`）；
  - 唯一达到 `10.0 bps` 被动做市过滤条件的 **`DEGEN-USD` `ask_maker` FIFO 成交（Tick 97，`$19.968646`）净赚 `+$0.023090`（`+11.586679 bps` 扣 Base gas 后、`latency_drift = -0.72 bps`）**，组合总 **`oos_trades_count = 1`、`100%` 胜率、`oos_max_drawdown_usd = 0.000000`、`final_remaining_daily_loss_usd_micros = 200,000,000`、`open_unhedged_lots = 0`**。

### 2.3 9 门禁联合总验（`readiness_report.json`：`7 / 9` 通过）

搭配 `rehearsal_software_signoff.json`（`--rehearsal`）、Wave 18 `dual_rpc_quorum.json` 与 `wsl_authority_status_proof.json`，[check_profit_engine_readiness.py](../../scripts/check_profit_engine_readiness.py) 输出 **`7 / 9` 通过**（`report_sha256 = 410e48a69d69fd55e3ac0c655f1072458d7cfcdae099922275eef1f6034c351f`，`release_permitted = false`、`execution_eligible = false`）：

- **通过 7 项**：`compiled_contracts_pinned`、`anvil_e2e_settlement_reconciled`（`58,204 gas`）、`base_time_predeploy_verified`（块 `59157964`）、`untouched_shadow_sample_size`（`300`）、`revert_budget_healthy`（`unresolved_residuals=0`）、`dual_rpc_quorum_verified`（块 `59157946`）、`isolated_signer_authority_ready`（WSL Docker 容器 `oasis-rehearsal-66e618f28545-canary-keys`）。
- **阻断 2 项**：
  1. `positive_cost_adjusted_lcb_edge`：由于 10 分钟低波时段仅成交 `1` 笔样本外交易（`oos_trades_count = 1 < 5`、`oos_lcb_net_edge_bps = 0 < 5`），Gate 5 如实阻断；待在活跃高波窗口（例如 `--ticks 1000 --interval-ms 1500` 或 `--allowed-products AERO-USD,ETH-USD,DEGEN-USD,WELL-USD` 连续覆盖 ~30–60 分钟）累积 `>= 5` 笔稳定正收益成交即可冲击 Gate 5；
  2. `owner_scope_release_manifest_present`：演练 Manifest 故意锁定 `rehearsal_only=True`，待 Gate 5 通过后由运营者以双平面软件 EOA 执行 `--approve-software-scope` 签署。
