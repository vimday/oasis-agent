# Wave 11–12 可配置本金风控、动态套利容量调节与实时 Coinbase WebSocket FIFO 做市核验（2026-11-18）

2026-11-18（Asia/Shanghai）；在 `bde95d3`（Wave 10 已推送基线）之上通过三路并行合入 Wave 11 并完成 Wave 12 联调与实测，消除系统中的静态本金/亏损硬编码（`500`/`200`）与静态下单规模（`1,000` / `250 USDC`），落地：**可配置本金与日亏/累积亏损预算引擎**、**按盘口/DEX 滑点曲线与剩余可用额度动态寻优的容量调节器**、**Coinbase `wss://ws-feed.exchange.coinbase.com` (`matches`) 实时成交流 FIFO 排队损耗做市仿真**、**双配置无密钥隔离签名演练**，以及 **macOS 系统代理直连 SSRF 误杀修复**。

全程严格保持无真实私钥、`execution_eligible = false`、`release_permitted = false`、无真实链上广播与付费 RPC 消耗。本文档衔接并保留先前核验记录（[PRELIVE_PROFIT_BRIDGE_20261118.md](PRELIVE_PROFIT_BRIDGE_20261118.md)、[BASE_100TICK_MULTICALL_CAMPAIGN_20261118.md](BASE_100TICK_MULTICALL_CAMPAIGN_20261118.md) 与 [ECOSYSTEM_MAKER_LIQUIDATION_AND_AUTHORITY_20261118.md](ECOSYSTEM_MAKER_LIQUIDATION_AND_AUTHORITY_20261118.md)）。

## 1. Wave 11–12 核心架构与工程交付

- **可配置本金/日亏风控策略与实时剩余额度引擎**（[capital_policy.py](../../src/oasis_agent/risk/capital_policy.py)、[示例配置](../../configs/profit-engine-risk.example.json)、[canary_readiness.py](../../src/oasis_agent/live/canary_readiness.py)、[check_profit_engine_readiness.py](../../scripts/check_profit_engine_readiness.py)、[单测](../../tests/unit/test_capital_policy_and_dynamic_sizing.py)）：
  - 以整数微美元（`USD micros`）和基点（`bps`）定义可校验、带 `config_sha256` 承诺的 `CapitalRiskConfig`（本金 `total_capital_usd_micros`、日亏上限 `max_daily_loss_usd_micros`、累积亏损上限 `max_cumulative_loss_usd_micros`、安全储备 `reserve_buffer_bps`、单笔比例上限 `max_single_clip_share_bps`、最小手数 `min_clip_usd_micros`、尾部压力系数 `assumed_tail_stress_bps`、行情超时 `max_feed_age_seconds`），默认兼容既有 USD `500 / 200 / 500` 基线且可被任意配置文件或 CLI 参数（`--risk-config`、`--total-capital-usd`、`--max-daily-loss-usd`）动态覆盖。
  - `compute_deployable_headroom` 绝不按现金总额推导仓位：严格从 `min(cash_available, total_capital)` 中先扣除 `reserve_floor`、锁定库存与未决承诺，再按当日剩余亏损预算与累积剩余亏损预算执行尾部压力缩放（`min(remaining_daily, remaining_cum) * 10_000 // assumed_tail_stress_bps`），一旦停机、存在未决残差或亏损耗尽立即归零阻断。
  - `canary_readiness.py` 与第 9 门禁从静态阈值升级为对照 `--risk-config` 验证 owner 签字边界，并将 `risk_config_sha256` 写入核验摘要。
- **Coinbase 实时成交流（WebSocket `matches` + REST `/trades` 水位回退）与 FIFO 队列做市**（[coinbase_trade_flow.py](../../src/oasis_agent/cross_venue/coinbase_trade_flow.py)、[maker_hedge_shadow.py](../../src/oasis_agent/cross_venue/maker_hedge_shadow.py)、[单测](../../tests/unit/test_coinbase_trade_flow_and_fifo_maker.py)）：
  - 纯标准库 RFC-6455 TLS 客户端（`CoinbaseWebSocketFeed`）通过 IPv4 优先直连 `wss://ws-feed.exchange.coinbase.com`，严格校验证书、`101 Switching Protocols` 与 `Sec-WebSocket-Accept`，非阻塞排空真实 `matches` 成交流（`Ping`/`Pong` 保活、去重 `trade_id` 水位、分片重组、`gap_detected` 断线自检与 REST `/products/<id>/trades` 增量回退）。
  - `MakerHedgeLifecycleTracker` 新增 FIFO 排队模拟（报价优于最优买卖盘时 `queue_ahead = 0`，平齐挂靠一档时先扣减一档既有队列深度再按 `maker_side` 语义消耗成交量，部分成交亦全额扣除真实 Base DEX 对冲 gas 成本），独立产出 `trade_flow_fifo` 成交归因与实时亏损扣减反馈。
- **跨策略动态容量最优求解、借贷清算额度截断与代理直连修复**（[live_cex_dex_shadow.py](../../src/oasis_agent/cross_venue/live_cex_dex_shadow.py)、[lending_reader.py](../../src/oasis_agent/fastpath/lending_reader.py)、[multicall_universe.py](../../src/oasis_agent/fastpath/multicall_universe.py)、[dual_rpc_probe.py](../../src/oasis_agent/live/dual_rpc_probe.py)、[run_multicall_100tick_campaign.py](../../scripts/run_multicall_100tick_campaign.py)、[动态与演练单测](../../tests/unit/test_dynamic_cex_dex_and_authority_rehearsal.py)）：
  - 在 4 个直连 `urllib.request.build_opener(...)` 中显式注入首序 `urllib.request.ProxyHandler({})`，彻底解决 macOS 开启系统代理（`127.0.0.1:7890`）时触发自身 SSRF 拦截且无需依赖 `no_proxy='*'` 环境变量。
  - 新增 `compute_coinbase_book_capacity_usdc`、`evaluate_dynamic_cex_dex_opportunity` 与 `_select_dynamic_maker_quotes`（调用 `solve_optimal_notional`），按 `[min_clip, min(headroom.max_clip, 盘口容量, DEX 滑点曲线)]` 动态寻优净美元利润最大点 `q*`；同步使 Rust 原子扫盘区间随 `headroom` 缩放，并为 `lending_reader.py` 增加 `filter_and_cap_liquidation_candidates` 按剩余风险额度等比截断清算还款额。
- **双配置无密钥隔离签名权威演练 CLI**（[verify_profit_authority_rehearsal.py](../../scripts/verify_profit_authority_rehearsal.py)）：
  - 内存中生成临时测试私钥置于 `0700`/`0600` 临时目录，先后演练 `charter_default_500_200`（`$500 / $200`）与 `scaled_config_5000_750`（`$5,000 / $750`）两套独立配置下的 `run_profit_authority --status-only --out-proof` 门禁 7/8 证明、Unix socket 有界签名、超日亏拒签与 2 笔结算失败持久化 `HALT` 跨重启封锁，结束自动销毁临时目录。

## 2. Wave 12 实测结果（归档于 `reports/research/wave12-20261118/`）

### 2.1 双配置隔离签名演练、无环境变量代理直连与 Aave V3 / 双源仲裁

- **隔离签名权威与可配置策略演练（`authority_rehearsal.json`）**：`passed = true`、`execution_eligible = false`、`keyless_ephemeral = true`、`network_calls = 0`、`ephemeral_dirs_cleaned = true`，自哈希 `141b5fe7778ecff12d45865daf4c3cbe7a973cd22a643a2d5cf6f87c2c18f79e`。验证 `$500/$200` 下 `$150` 签名放行、`$250` 日亏拒绝及 2 败重启持久熔断；在 `$5,000/$750` 配置下 `$1,500` 本金 / `$250` 日亏顺利签名通过而 `$800` 超日亏拒签。
- **不依赖 `no_proxy` 的 Base 主网直连校验**：
  - `aave_v3_base_health.json`（5,000 块滑窗）：首次偶发单次传输超时后，重跑于固定区块 **`59063947`** 完成，`6` 名借款人全部持有活跃债务，最低健康因子 `1.5591`（`0xc237...dab6`，抵押 USD `70,048.60` / 债务 USD `37,740.09`），可清算 `0`（`report_sha256 = ab6440eb125d7db9260db10fe79579e9ad2796bcab118b4c09071b5ba99b501b`）。
  - `dual_rpc_quorum.json`：`publicnode_base` 与 `base_official_mainnet` 于固定区块 **`59064043`**（`block_hash = 0x01ed0ceb285ab63ab4625fe480f4b4ee79c9379fc0ee4aadefe6491767c18614`、`state_root = 0x904987fe926a...`、`base_time_ms = 1795003184200`）交叉验证通过（`report_sha256 = 44de35a4d1da6d83dcde0d3b226e63c30323c4b86681116f6a14bc7062641941`）。

### 2.2 动态配置缩放对照（`smoke-2/` vs `smoke-3-scaled-5000/`）

| 实测样本 | 本金 / 日亏配置 | 实时 WS 成交数 | 动态单项范围（Maker / Taker） | 关键实测发现 |
| --- | --- | --- | --- | --- |
| `smoke-2/`（区块 `59064074..59064140`） | `$500 / $200`（最大单笔 `$225`） | `195`（0 断流） | Maker `$10.00–$225.00`；Taker `$10.00` | 首次实现主网实时 Coinbase WebSocket FIFO Maker-Hedge 正盈利成交：**`+10.41381 bps`**（`+$0.009498` 扣 gas 后净赚，`summary_sha256 = 46cde75b...`） |
| `smoke-3-scaled-5000/`（区块 `59064240..59064359`） | `--total-capital-usd 5000 --max-daily-loss-usd 750`（最大单笔 `$2,250`） | `428`（0 断流） | Maker `$10.00–$2,250.00`；Taker `$10.00–$425.39` | 深池 `ETH-USD`/`CBETH-USD` Maker 自动扩容至 **`$2,250`**、`VIRTUAL`/`VVV` 扩至 `$1,080–$1,168`，浅池 `WELL-USD` 自限在 **`$94.18`**；3 tick 中 **2 tick 为正 Taker 净盈利**（`summary_sha256 = 7ec19cd3...`） |

### 2.3 100-Tick 11 资产动态容量 + 实时 Coinbase WebSocket FIFO 连续战役（`shadow-100-dynamic-ws/`）

- **稳定度与规模指标**：连续覆盖 Base 区块 `59064548..59072466`（**`100` 个唯一区块**、`ticks_completed = 100`、`44` 座深池、`203 / 350` RPC），实时 WebSocket `matches` 订阅 **全 9 个产品、`100 / 100` tick 零断流、零重启、零 REST 回退**，累计接收 **`19,829`** 笔真实成交（`BTC-USD: 15,904`、`ETH-USD: 3,143`、`VVV-USD: 474`、`AERO-USD: 236`、`WELL-USD: 30`、`MORPHO-USD: 15`、`VIRTUAL-USD: 12`、`DEGEN-USD: 11`、`CBETH-USD: 4`）；哈希链尾 `120eb14b3e2a352dfabd6ac5db65eb10a9f7fabb3004df9d73c1d0bb90efb583`，汇总自哈希 **`122077d3867d7d21b8b8074ea1e068fa01b7a5858ff0ef68a5bad77d36c31bd7`**。
- **跨策略收益对比（对比 [Wave 10 固定 USD 250 基线](ECOSYSTEM_MAKER_LIQUIDATION_AND_AUTHORITY_20261118.md)）**：
  - **Taker CEX-DEX 由 `1/100` 跃升至 `14/100` 正净收益 tick**，峰值净边际由 `+6.39 bps` 升至 **`+38.076262 bps`**（Tick 68 `VVV-USD` @ `$225`，毛差 `+43.378602 bps`），总体 Taker 均值由 `-2.718 bps` 收窄至 **`-1.807906 bps`**（当前样本标准差增大至 `4.339218 bps`，`lcb_net_edge_bps = -3` 仍未过 `+5` 门禁）。
  - 最具代表性的容量发现出现在 **`WELL-USD`**：在 Wave 10 固定 `$250` 下因 DEX 滑点录得 `-116.90` 至 `-174.10 bps` 全面亏损，而在 `dynamic_sizing` 下自动将 Taker 收缩至 **`$15.12–$19.34`** 匹配薄池容量后，连续 **10 个 tick 转为正净盈利（`+0.69` ~ `+2.90 bps`）**；`VIRTUAL-USD` 在 `$37.97` 与 `$225.00` 亦录得 `2` 次正盈利（峰值 `+2.944230 bps`）。
  - **实时剩余亏损额度反馈验证**：`shadow` 累计实现负向波动 USD `0.310059`，`headroom_summary.final_remaining_daily_loss_usd_micros` 从 `200,000,000` 动态下降为 **`199,689,941`**，单笔边界精确跟踪剩余风险额度。

| 产品 | 有效 / 降级评估 | 动态 Taker 单笔区间 (USD) | 动态 Maker 单笔区间 (USD) | Taker 峰值净边际 (bps) | FIFO 成交数 / 正盈利 | 严格穿价 `strict` / 诊断 `touch_lift` |
| --- | --- | --- | --- | --- | --- | --- |
| `VVV-USD` | `96 / 4` | `$10–$225` | `$10–$225` | **`+38.076`**（2 正） | `1 / 0`（`-2.09 bps`） | `0 / 1` |
| `VIRTUAL-USD` | `96 / 4` | `$10–$225` | `$10–$225` | **`+2.944`**（2 正） | `1 / 1`（`+0.33 bps`） | **`1` 正（`+5.34 bps` / `+$0.120`）/ `18`** |
| `WELL-USD` | `97 / 3` | **`$10–$19`** | `$29–$82` | **`+2.903`**（10 正） | **`1 / 1`（`+5.53 bps` / `+$0.0022`）** | `0 / 7` |
| `AERO-USD` | `95 / 5` | `$10–$16` | `$10–$104` | `-2.762` | **`1 / 1`（`+0.17 bps` / `+$0.0012`）** | `0 / 1` |
| `BTC-USD` | `93 / 7` | `$10–$78` | `$10–$225` | `-0.719` | `0 / 0` | `0 / 0` |
| `ETH-USD` | `98 / 2` | `$10–$89` | `$10–$225` | `-0.526` | `2 / 0`（`-2.00`、`-3.73 bps`） | `2 / 0` |
| `MORPHO-USD` | `96 / 4` | `$10–$10` | `$10–$225` | `-6.843` | `1 / 0`（`$1.63` 小碎单扣全额 gas `-34.57 bps`） | `0 / 8` |
| `CBETH-USD` | `97 / 3` | `$10–$10` | `$225–$225` | `-14.197` | `0 / 0` | `0 / 64` |
| `DEGEN-USD` | `97 / 3` | `$10–$10` | `$10–$225` | `-16.716` | `0 / 0` | `0 / 1` |

- **实盘前最关键的量化诊断结论（为什么 FIFO 与 `strict` 总体仍为微负且如何转正）**：
  1. `maker_hedge_summary` 在 `865` 步中共产生 `447` 条有效挂单腿、`100` 次诊断 `touch_lift`、`3` 次 `strict_cross`（`1` 正、`strict_total_net_profit_usd = -$0.00891`）与 **`7` 次 `trade_flow_fifo`（`3` 正：`WELL-USD +5.53 bps`、`VIRTUAL-USD +0.33 bps`、`AERO-USD +0.17 bps`；总净额 `-$0.177101`）**。
  2. 两处可量化归因的亏损源清晰指向下一轮优化参数：`MORPHO-USD` Tick 62 仅成交 `0.73%`（**USD `1.63` 微小碎片成交**）却立即模拟单笔全额 Base gas，拉低均值 `-34.57 bps` —— 实盘/回测须设置**最小对冲成交通道阈值（`min_hedge_notional >= $10`）**；`ETH-USD`（`3 bps` 起报门槛）在块间价格单边漂移 `5.3–7.0 bps` 时被逆向选单，需为不同波动率分层提高 `min_quote_edge_bps`（如长尾高波资产保持宽松点差、主流深币提高逆向选择缓冲 `adverse_selection_bps >= 6–8 bps`）。

### 2.4 9 门禁就绪报告（`readiness_report.json`）

- 搭配 `--risk-config configs/profit-engine-risk.example.json`（`risk_config_sha256 = f9840fe3306efbaaadc35e0c3b246f899d12c039af5f1aee21c9ea6036c7c9a6`）评估仍为 **`6 / 9` 通过**（`report_sha256 = b33ead017e09569bf3d6a3fd4c18887a75f873bebecda55fa4c76c2918b95b9c`），`release_permitted = false`、`execution_eligible = false`，仅余 `positive_cost_adjusted_lcb_edge`（需经上述防碎单 + 波动率自适应报价阈值证明样本外正 LCB）、`isolated_signer_authority_ready` 与 `owner_scope_release_manifest_present`。
