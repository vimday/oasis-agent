# 当前实现与验证边界

更新：2026-11-22。这里记录已实现能力、已验证结果和待解决问题。发布资格以具体代码、
配置、当前证据和授权为准。历史验收记录适用于其原始范围。

## 当前工作

| 路线 | 已实现 | 发布或研究边界 |
| --- | --- | --- |
| Base ETH/USDC v4 LP canary | 本机 Docker 一键有界入口，独立策略与签名服务，1–3 轮上限，完整配置范围内的 NAV、双源模拟、收据对账、双 STOP 恢复及只读仪表盘 | 已记录两轮真实持有验收。最近三轮尝试未启动。每次运行单独核定鲜度、费率、原预算、双账本和当次授权。 |
| Base 自主现货 | OKX 热榜、信号、风险与报价，GMGN 可选交叉检查，FOMO 提名接口，回调入场及止损、止盈、跟踪和时间退出的纸面账簿，输入回放及历史收据复核 | 影子模式。原 OKX 组单与广播路径已移除。FOMO 未接入已授权数据。独立 NAV 与风险、交易绑定、双源模拟和结算闭环仍是发布前置条件。 |
| Robinhood v4 LP | 固定区块头寸和报价验证、费用与库存特征、范围决策、现金归因、v4 合约生命周期与原子执行器 | Phase 1 经济研究基准。PONS/USDG 的外部 LP 权限、目标池真实路由与样本外净超额收益仍需证明。 |
| 证据、账本和控制台 | 区块绑定、原始 RPC 回放、SQLite 请求预算和持久风险状态、只读财务状态和受鉴权的观察任务 | 控制台无法授权交易。日志匹配、显示估值和链上资金对账分别呈现，缺失证据保持未知。 |
| 同链闭环套利 | 固定块末报价、真实收据与块末整笔调用离线复核、V4 新池发现与事件触发的定向报价回放 | Elysium 两块全部 50 笔交易与收据通过[根承诺核验](verification/ELYSIUM_NITRO_COMMITMENTS_20261114.md)，单源块末调用见[案例](verification/ELYSIUM_CYCLE_RESEARCH_20261111.md)。赢家精确 prestate、真实前瞻报价和结算仍缺。 |
| Metric 事件型套利研究 | 有界公开 metadata/depth 采集、哈希绑定的离线回放、整数深度与双池闭环筛选 | 单提供者估算，不是链上报价或执行准入。本轮真实两池六个候选因空深度全部拒绝；成本和资本保持未知。见[本轮验证](verification/METRIC_RESEARCH_20261114.md)和[下一阶段目标](strategies/METRIC_EVENT_RESEARCH.md)。 |
| 多链发现与借贷压力研究 | 按 chain ID 隔离的 Metric 目录；合成抵押/借款循环、HF、cap、深度、利息与先偿还再撤款核算 | Ethereum/Robinhood 目录有响应；Base/Elysium 公开 API 报未知 chain ID，各采集保留为失败。没有部署/收益证明；Binance 事件已纠错。见[后续验证](verification/MULTICHAIN_LENDING_20261114.md)。 |
| Metric / V3 跨 venue 报价 | 固定块、双起点与双顺序的离线报价组合；身份、实际输入、成本、资金和来源校验 | Ethereum 单源观察共 22 次免费读取，首笔 Metric quote 回滚后停止；选择器匹配 `FeedStalled()`，具体原因未证实。0/4 完整报价；无原子模拟或收益证明。见[报价验证](verification/METRIC_V3_QUOTES_20261114.md)。 |
| 公共链观察及 Metric oracle 诊断 | 四链固定来源、原预算、字节回放与 STOP；Base Denim 头部 `timestampMs` 严格校验；immutable provider/runtime/config 和 reference guards 的离线核验 | Ethereum/Base 五合约身份已观察，Robinhood 时间边界缺陷已修正并保留原失败，Elysium 固定哈希调用被拒。原块 15 秒配置已知，feed 仍缺；全为单运营商。见[验证记录](verification/PUBLIC_RPC_AND_METRIC_IDENTITY_20261114.md)与[Denim 观察](verification/BASE_DENIM_HEADER_20261115.md)。 |
| Titan 条件状态观察 | 复用有界公共采集，严格整数时间/序列、状态摘要与相邻快照比较 | 三次真实匿名读取覆盖目标 Metric oracle 和 ETH/USDC 池；属于构块者声明，未应用 override、未验证 canonical base 或可执行收益。见[首次观察](verification/TITAN_STATE_20261114.md)。 |
| Metric 构块 feed 解释 | 源码布局固定、目录字节绑定、17 槽解码、毫秒/秒转换与 feed 冲突检查；接入原离线命令 | 原三份快照的 ETH/USDC 声明在目标时刻均为 4 秒旧；仅声明诊断。源码执行代码排除 immutable/CBOR 后匹配，metadata 不同；未验证目标配置或两腿执行。见[feed 验证](verification/METRIC_BUILDER_FEEDS_20261114.md)。 |
| Metric 父块关联与整笔探针 | 原始 header/Titan 关联；始终回滚的两池完整 swap/回调/还款探针及合成余额验证 | 单源父子块已观察，第二来源传输失败后原范围 6/96 STOP；probe 本地 10 项测试、10,000 fuzz 通过，无真实 pinned fork 或可执行收益证明。见[探针验证](verification/METRIC_ATOMIC_PROBE_20261114.md)。 |
| Metric 探针调用与回滚解析 | 固定计划、完整 immutable runtime 重建、四项调用包、原始字节与 request hash/数量校验 | 四种路线与 Solidity 实际字节一致；67 项新增 Python 拒绝回归，RPC 包装和池仍为合成。无网络入口或真实历史状态证明。见[编解码验证](verification/METRIC_PROBE_CODEC_20261114.md)。 |
| Metric 自有本地节点执行 | 合成 genesis 全账户比对、空 probe 的单次受限 override、实际 IPC、前后 state dump 与子进程退出核对 | 四方向真实本地执行及负例通过，历史输入仍拒绝；另在原部署档案范围补得目标块五 runtime，原账本 18/32。见[本地状态验证](verification/METRIC_LOCAL_STATE_20261114.md)。 |
| Metric 历史状态证明 | Prague 块头哈希、账户/槽 MPT 路径与 runtime 核验，接入只读采集和离线回放 | 两个原块锚重算一致，Anvil 合成证明通过。早期两次公开尝试分别因 429 和证明窗口 STOP。新固定父块的空账户证明见下文。见[状态证明验证](verification/METRIC_STATE_PROOFS_20261114.md)。 |
| Metric 两腿有无 Titan 对照 | 固定真实父状态和目标块参数、探针地址的空账户证明、仅两槽 oracle 覆盖 | [真实模拟对照](verification/METRIC_TITAN_PAIRED_20261114.md) 0/8 完成；回读确认两 feed 从 42/48 秒变为 4 秒，池级两组仍报 `FeedStalled()`。[调用树验证](verification/ETHEREUM_AAVE_METRIC_TRACES_20261114.md)显示覆盖后卡在要求与区块同秒的压缩报价源。加第三槽后四种路径均到第二腿，返回均低于本金。 |
| Aave 历史事件与收据 | 历史索引候选、固定块/hash/交易位置、V3 角色解释、底层转账匹配及块末账户读数 | 62 笔实际收据，60 笔 Pool 事件都匹配底层转账；18 笔借款 11,701 WETH，12 笔还款；起止块末债务均为零。完整资金及收益继续对账。见[历史借还款](verification/AAVE_HISTORICAL_FLOWS_20261114.md)。 |
| Aave 缩放及钱包账本 | 固定指数逐步复核、余额/索引残差与收据转移匹配 | 62 步缩放轨迹期末残差为零，ETH 差额定位到四段。原钱包报告缺 131 笔候选的完整收据，最新缺口见下文。见[钱包对账](verification/AAVE_WALLET_LEDGER_20261114.md)。 |
| Aave Core 冻结与实际 oracle | 带交易位置的冻结收据、父块价格及轮次、两笔买回与解包收据 | USDC/WETH 在 26301183 同时冻结，买回与解包的 WETH 和原生余额差核对通过。净收益仍未知。见[冻结与价格验证](verification/AAVE_FREEZE_ORACLE_20261114.md)。 |
| Aave 借卖再抵押 | 固定交易 value、封闭的余额区段、费用与借贷收据 | 一次 500 ETH 卖出及同额 USDC 再抵押已复核，估值价差 19.11%，包含失败 gas。见[卖出回放](verification/AAVE_SALE_CYCLE_20261114.md)。 |
| Aave 五笔余额约束 | 五笔成功收据和十个固定块末余额、同源索引计数复核 | [五笔优先核验](verification/AAVE_FIVE_PRIORITY_TRACES_20261114.md)覆盖 422.0038 ETH 差额，逐块支持每组只计一次，[独立调用树](verification/ETHEREUM_AAVE_METRIC_TRACES_20261114.md)确认六组各有一条随祖先回滚，五块余额对平。该阶段剩 87.1996 ETH 差额和 126 笔收据。最新对账见下行。 |
| Ethereum 前瞻流与 Aave 钱包对账 | 公共 Titan 流首收、同秒槽与最终块证明、冻结候选收据回放 | [前瞻与对账](verification/METRIC_STREAM_AAVE_WALLET_20261114.md)六个预选 slot 有声明，最终区块同秒 0/6，24 条模拟本金不足。Aave 冻结候选收据 204/204，校正后原生差额为零，完整净 PnL 待归因。 |
| Titan 构块归因与 Aave 结项 | Relay 与官方公钥核对，固定规模双腿；可从仓库重放收据、余额与 trace | Titan 确认块 3，区块末同秒 0/3。18 条规模路线本金不足。Aave 原生余额对平，固定估值有数值，策略净 PnL 保留未知。见[构块归因与案例结项](verification/METRIC_TITAN_BUILDERS_AAVE_CLOSE_20261114.md)。 |
| 事件采集与逐过滤器覆盖 | 原始日志范围导入、持久空块/缺口、重组撤销、批次原子性；按所报高度及固定偏移选择未来块 | [Base 12 块](verification/PROSPECTIVE_EVENT_WINDOWS_20261114.md)三个过滤器观察到 1 Swap、0 Mint、0 Burn；Elysium 8 块六个过滤器为空，原 Base 14/32 停止记录保留。 |
| 持久事件 shadow 队列 | 冻结代码/窗口、覆盖与候选原子提交；租约重投、旧 worker 拒绝、正常 CLI 定向报价、重组同步失效 | [合成演练](verification/CYCLE_OUTBOX_20261114.md)覆盖定向报价和重组；Elysium 真实空窗已进入队列，覆盖完整、无报价任务。实时唤醒另见下一行。 |
| 实时 Swap 定向报价 | 冻结触发、费用模型、等预算轮询、原始证据与独立收据复核 | [双链实测](verification/EVENT_QUOTE_RESULTS_20261114.md)。Base 事件组 2/4 两腿在 750 ms 内，轮询 0/4，八条毛差全负，按门槛关闭固定路线。Elysium 改用匹配池的 Aero MetaQuoter，三腿报价两组各 4/4，均超时限且毛差全负，重复收尾块读取超时。 |
| Rust 快路径内核与增量同步/清算引擎 | `crates/oasis-core` 多 DEX 状态图（V2/V3/Slipstream/V4/AerodromeStable）经索引零分配热循环优化至 2-hop 6 池 `53 us/scan`、`max_rate_upper` Spot 上界预剪枝下 34 池 4-hop 1,868 条环路 **`2.37 ms/scan`**；Wave 32–33 合入 [链上多跳套利调研](strategies/MULTIHOP_ARBITRAGE_RESEARCH.md) 提出的 P0/P1 内核增强（`ScanCyclesOptions`：按跳与池类型累加 gas、`deduplicate_cycles` 跨基币与旋转规范去重、`changed_pools` 增量热循环过滤）、[incremental_scanner.py](../src/oasis_agent/fastpath/incremental_scanner.py)（`IncrementalCycleScanner` + `run_multihop_ab_experiment`）与 [run_multihop_ab_experiment.py](../scripts/run_multihop_ab_experiment.py)；[multicall_universe.py](../src/oasis_agent/fastpath/multicall_universe.py) 实现五资产 `Multicall3` 发现、ERC-20 余额防幻觉限制与 `QuoterV2` 复验；[state_sync.py](../src/oasis_agent/fastpath/state_sync.py) 实现日志增量同步与 64 块重组回滚；[liquidation.py](../src/oasis_agent/fastpath/liquidation.py) 实现 Morpho/Aave V3 闪电贷清算扫描；[OasisAtomicExecutor.sol](../contracts/src/OasisAtomicExecutor.sol) 与分级回滚风控 | 离线差分 0 误差、Foundry 早期中止 62.4k–64.6k gas、Rust 21 测全过；34 池 4 跳 120 个合成块的 A/B 扫描器对照达到 **`exact_bit_for_bit_blocks = 120/120`、相对全扫 `recall = 1.0`、进入评估的环路 `-88.23%`、扫描阶段 `p99 -52.99%`**。真实状态至整笔复核的延迟与经济召回待测。`Multicall3` 25 池 Discovery、增量同步与闪电贷清算已在主网只读 `public-shadow` 中验证；实盘链上广播仍无执行授权。见[升级核验](verification/AUTONOMOUS_OPPORTUNITY_ENGINE_20261118.md)、[CTO 全路线核验](verification/CTO_FULL_ROADMAP_COMPLETION_20261118.md)、[100-Tick 核验](verification/BASE_100TICK_MULTICALL_CAMPAIGN_20261118.md)与[多跳套利调研](strategies/MULTIHOP_ARBITRAGE_RESEARCH.md)。 |
| `eqn.news` 情报、发射台安全门与 `BaseTime` | 14 频道 `eqn.news` 只读客户端与事件冻结安全发现桥；Base Denim 毫秒级 `LaunchpadSafetyGate` 并直接接入 [spot_agent.py](../src/oasis_agent/launch/spot_agent.py) 入场前安全门；[base_time_probe.py](../src/oasis_agent/evidence/base_time_probe.py) 实现 `BaseTime` 预部署同块校验 | `public-shadow` 实测获取 20 条快讯（自动冻结 APT）并在 Base 块 `58978574` 完成 `BaseTime`（`0x4200...0030`）实读交叉核验（`1794986090400 ms` 一致，3/4 预算）。仅影子/只读证明，`execution_eligible=false`。见[CTO 全路线核验](verification/CTO_FULL_ROADMAP_COMPLETION_20261118.md)。 |
| 意图金库、`UniswapX`、跨市场与 Stage 7 组合分配 | [OasisIntentVault.sol](../contracts/src/OasisIntentVault.sol)（双平面托管 + 域隔离 ERC-1271 + `IReactorCallback` 结算）；`UniswapX` `Dutch_V3` 只读订单簿客户端；`ALEPHTX_SHM_V12` / 只读 `ssh aleph` / `HedgeGroup`；[Stage 7 组合分配器](../src/oasis_agent/portfolio/allocator.py)（独立储备隔离、失效立即归零、相关风险域上限）与只读控制台 `/api/v1/radar/*` | Foundry 3 套件 13 测全过（512 轮 65,536 次不变量 0 违例）、Slither `--fail-low` 0 告警；`UniswapX` Base `/v2/orders` 公开轮询返回 200（0 挂单快照）。尚未部署金库或批准实盘资金权重。见[CTO 全路线核验](verification/CTO_FULL_ROADMAP_COMPLETION_20261118.md)。 |
| 准实盘盈利闭环（Anvil 全结算 + Aero Slipstream 2 + Bybit Spot V5 + 双源预签名/主循环接线 + 防毒流冷却 + 审查加固） | [capital_policy.py](../src/oasis_agent/risk/capital_policy.py) 与 [profit-engine-risk.example.json](../configs/profit-engine-risk.example.json)（可配置本金/日亏/累积亏损预算与动态容量求解）；[multicall_universe.py](../src/oasis_agent/fastpath/multicall_universe.py)（可选 `--include-aero-cl2` 接入 Aero Slipstream 2 `CLFactory`，Base 活跃池扩至 56–57 池）；[bybit_spot_feed.py](../src/oasis_agent/cross_venue/bybit_spot_feed.py)（统一 `usdt_peg.mid` 折算、`max_reads=256`/`max_frames=1024` 无滞后 WS 与 REST 成交客户端）；[coinbase_trade_flow.py](../src/oasis_agent/cross_venue/coinbase_trade_flow.py)、[maker_hedge_shadow.py](../src/oasis_agent/cross_venue/maker_hedge_shadow.py)（按产品 lot 计时、未冲销部分成交保位、`strict_cross` 隔离、`t1` 无重复波动扣减、仅负收益触发冷却、两步严格同向动量抑制及 `cex_dust_unwind`）与 [oos_execution_tracker.py](../src/oasis_agent/cross_venue/oos_execution_tracker.py)；[profit_strategy_daemon.py](../src/oasis_agent/live/profit_strategy_daemon.py)（Wave 31 修复 P2.2：`DualRpcEnvelopeSimulator` 双源锁定区块/字节码/`GasPriceOracle` 预签名模拟与 `ProfitStrategyBridge.run_tick`/`run_loop` 挂起对账/候选提交接线）与 [run_profit_strategy.py](../scripts/run_profit_strategy.py)（`--candidate-proposal-json`）；[OasisIntentVault.sol](../contracts/src/OasisIntentVault.sol)（`IOasisSwapAdapter.swapExactInput` 绑定 `step.pool`、300s 报价上限）与零告警 `crates/oasis-core`；[run_multicall_100tick_campaign.py](../scripts/run_multicall_100tick_campaign.py)（`--cex-venue`、`--include-aero-cl2`、Keep-Alive `250` 请求轮换、`$20` 做市准入下限、9 并发预拉取、WS 尾帧排空与 `MulticallUniverseError` 兜底停机）；[DeployOasisProfitEngine.s.sol](../contracts/script/DeployOasisProfitEngine.s.sol)、[create_software_scope_signoff.py](../scripts/create_software_scope_signoff.py) 与 [check_profit_engine_readiness.py](../scripts/check_profit_engine_readiness.py) | **Wave 30 在 Base 主网 untouched `1,200`-Tick 实测实现 `100%` 胜率（`86 / 86` 笔）、`+$28.534535` 净利、`$0` 回撤与 `8 / 9` 技术门禁全面通关（仅余 Gate 9 人类范围授权），Wave 31 完成 P2.2 双源预签名模拟器与主循环接线闭环**：经 Wave 27–29 解锁 Bybit WS `256` 次全帧吸干、隔离 `strict_cross`、去除 `t1` 重复 `eff_adv_bps`、仅负收益触发冷却及两步累计动量过滤后，Wave 30（`--allowed-products AERO-USD,BTC-USD,DEGEN-USD,VVV-USD --maker-min-edge-bps 6.0`）完整跑完 **`1,200/1,200` tick / `1,199` 唯一 Base 区块（`60433927..60443884`、`2,405` RPC `0` 中断、`56` 池、`28,308` WS 成交 `0` 断流/重连）**；录得 **`oos_trades_count = 86`（`82` Taker IOC + `4` >= `$20` FIFO 做市）、`100.0%` 胜率、均净边缘 `+16.41 bps`、95% LCB `+13.59 bps`（`13 >= 5` 通关）、净总盈利 `+$28.534535`、最大回撤 `$0.000000`**，9 项门禁首次达成 **`8 / 9` 通过**（`report_sha256 = 4d9001aa...`，Gate 5 `positive_cost_adjusted_lcb_edge` 通关，仅 Gate 9 `--rehearsal` 保持 `release_permitted=false`、`execution_eligible=false`）；Wave 31 同步关闭复核项 P2.2，使四项复核项（P1/P2.1/P2.2/P3）全部完成回归核验。见[Wave 23–31 修复与实测核验](verification/WAVE23_REVIEW_FIXES_AND_ANTITOXICITY_20261121.md)、[2026-11-21 审查复核](verification/REVIEW_208E3E8_07AA611_20261121.md)与[Wave 21–22 600-Tick 核验](verification/BASE_AERO_CL2_BYBIT_600TICK_20261119.md)。 |
| 后续策略 | 对冲、做市、跨市场交易、Stage 7 组合分配器及全部工程基线 | 工程交付与只读影子闭环见[CTO 全路线核验](verification/CTO_FULL_ROADMAP_COMPLETION_20261118.md)；资本晋级与真实样本外净收益验收仍受[长期路线](strategies/LONG_TERM_PROFIT_ROADMAP.md)约束。 |

## Base LP 验收记录

- 2026-11-14，owner 要求暂停新 Base LP canary。保留既有 STOP、服务记录和原费用账本。
- 2026-11-07，公开 USD 数据策略解决了原 USDC 喂价更新周期与 1,800 秒上限不兼容的问题。新策略使用 Kraken 与 Bitstamp 的公开成交，
  与签名预言机证明分别记录。
  早期一次持有启动完成后，Docker 对签名服务升级为 KILL，退出码为 137。该记录保留。
  见[价格策略及早期验收](verification/BASE_PUBLIC_USD_PRICING_20261107.md)。
- 随后的 `73c2f50` 一轮及 `81bcaeb` 两轮记录均显示策略、签名服务退出码 0，双 STOP
  恢复，历史收据复核通过。两轮记录是持有，新增成交结算为 0。其本地终验文件为
  `reports/review-20261107/base-81bcaeb-two-ticks-terminal-verification.json`，自哈希为
  `76669eb30b296a42998cd7aec760ec94a404663bce64d4413442d093001c1958`。
- `81bcaeb` 三轮请求首次因策略 RPC 子限额在离线阶段拒绝。`1bb8fe0` 调整了完整流程的
  预算校验，随后在线的无签名模拟仍因 dRPC HTTP 429 停止，未启动生产服务。后续
  `9288432` 将限流原因纳入有界入口和前端诊断。本地终验文件为
  `reports/review-20261107/base-rpc-budget-three-ticks-terminal-verification.json`，自哈希为
  `34117149744497db4a2b6d6c1e652818bdcd71e10354cc3b38438559d7d76152`。
- 较早的[付费只读复核](verification/LIVE_PAID_EVIDENCE_20261107.md)确认五笔原有收据和
  一次无签名全封装退出模拟。历史持有、收据和退出模拟支持各自的验收范围。新的成交后
  双账本结算和更长的自主运行仍需独立的真实验收。

当前操作流程、原账本限额和 STOP 事故处理统一见 [Base LP 运行手册](operations/BASE_BOUNDED_RUN.md)。
价格见证、RPC 账单与风险准入是三类不同证据。保守费用估计需与账单核验区分。

## Base 现货下一阶段

1. 用完整影子会话冻结输入、策略和对照。比较 OKX 信号与 GMGN 交叉验证的增量价值，
   记录不可用数据、净交易成本、样本外表现和尾部损失。FOMO 需先有明确接入权限和数据契约。
2. 构造仅允许的 Base 现货交易，固定路由、代码、账户、授权额度、最小到账和过期时间。
   用独立账户风控和同区块双源模拟验收同一份交易，再由独立签名服务执行。
3. 在原累计 RPC 预算下关联发送结果、双源收据、事件、余额和费用。结果不明时冻结新单，
   验证人工接管与恢复，再为单独授权的 canary 提交证据。

[现货运行与回放](operations/BASE_SPOT_SHADOW.md)记录命令、证据范围与拒绝语义。
影子研究运行可以在第一次提供者调用前把冻结计划绑定到采集会话，离线验收只按该计划的窗口
判断观察覆盖。目前只有合成样本，见[预注册验收记录](verification/SPOT_PREREGISTERED_ACCEPTANCE_20261110.md)；
真实影子采集、独立时间证明和可执行退出仍缺。
没有已验证的持续净盈利。合成测试、冻结文件回放和真实签名或广播属于不同验收层次。

## 证据导航

- [链上多跳套利技术调研与 P0/P1 内核升级规范](strategies/MULTIHOP_ARBITRAGE_RESEARCH.md)
- [Base 常规新头入口](verification/BASE_HEAD_DELIVERY_20261124.md) · [Robinhood 整路径报价](verification/ROBINHOOD_QUOTE_DELIVERY_20261124.md)。Base `--base-newheads` 已用两个直接父子块验证常规 4/58/66 池入口与固定哈希回放，第二块状态就绪耗时 2,427 ms，其中约 1,447 ms 在取数前等待。Robinhood 两个独立父子块范围用双源验证状态和 Quoter，20 跳有限金额毛差为负，24/37 跳均因第 4 腿流动性不足回滚。
- [Ethereum 九池留出样本](verification/ETH_FUNDED_HOLDOUT_20261124.md) · [BSC 192 块四池样本](verification/BSC_FOUR_POOL_TAIL_20261124.md)。Ethereum 九池闭环扣实际 gas 后可见净额 76,553,183,579,326 wei，原 calldata 在成交块末和直接子块均回滚。BSC 33 个逻辑池的借款批次扣费后有四种代币残留与两个实际原生币付款，整体可兑现净额待核。
- [同本金有序批次与 100 跳整笔对照](verification/V2_ORDERED_ROUTE_ANVIL_20261124.md)。回环 EVM 逐笔核对付款、余额和退款后的收据 gas。共享池重置高估最佳批次毛差 44.2%。直接子块同包络全回滚，百跳成本 4,877,917 gas。公链机会频率尚无对应证据。
- [Base 新头与轮询](verification/BASE_HEAD_DELIVERY_20261124.md) · [Robinhood 双流到达顺序](verification/ROBINHOOD_NITRO_ARRIVAL_REPLAY_20261124.md) · [BSC 80 Swap 多资产长尾](verification/BSC_ALTERNATE_LONG_TAIL_20261124.md)。Base 同源免费样本新头覆盖 12/12 个直接子块，轮询为 0/12。Robinhood 离线注入缺口后 411.928 ms 回补，两个已释放区块绑定 45 份收据。另一次主动关闭本地主连接后，真实备用流在 515.102 ms 后补上首个连续高度，累计 102 个。BSC 的 80 条事件拆成 20 条四跳，扣 gas 和直接付款后为负 BNB 余额与正 USDT 余额，实际变现待核。
- [Base 已推送块的 4/58/66 池复核](verification/BASE_HEAD_DELIVERY_20261124.md) · [BSC 48 块定向长尾](verification/BSC_ALTERNATE_LONG_TAIL_20261124.md)。Base 的一次免费前瞻采集从新头接收到固定哈希 66 池状态就绪耗时 1,188.130 ms，完成三组双图对照耗时 1,199.246 ms，零模型正候选。BSC 用三次免费读固定池地址并核对目标块收据和调用树，52 条 Swap 样本的可证最长资金分支为五跳，结算包含 LP NFT。
- [Ethereum 同块四跳费用余量](verification/ETH_ORDERED_FOUR_HOP_MARGIN_20261124.md)。两笔新核实的 WETH 闭环各走四个独立池。复算区块哈希及 210 份收据根，独立调用树与收据一致。扣实际 gas 后分别剩 7,145,491,293,251 和 1,288,115,539,886 wei。较薄样本的原始调用在成交块末和直接子块均回滚。
- [Base 盈利筛选和热路径](verification/MULTIHOP_PROFIT_PIPELINE_20261124.md)。官方头和运行码验证期间先发固定哈希地址池读，完整一致后发布。三个真实样本重叠 220–445 ms，状态就绪 0.94–2.04 秒。V4 跨 bitmap 字的同哈希双源真实报价与新模型逐整数一致，旧模型高报 3 个 USDbC 最小单位。本轮零模型正候选。
- [Ethereum 十池与长路径分析](verification/ETH_LONG_SERIAL_EVIDENCE_20261124.md) · [BSC 有序批次边际成本](verification/BSC_ORDERED_BATCH_MARGINALS_20261124.md) · [Robinhood 两腿结算](verification/ROBINHOOD_FEED_PAIR_20261124.md)。Ethereum 十池样本最长四跳，实际 gas 后仅余 3.22% 毛差。BSC 35 条闭环分别归因路径 gas 和整笔公共费用。4+3 跳样本补核唯一原生币付款，扣 gas 和付款后可见净额 5,964,823,211,929 BNB 等值 wei。Robinhood 两个无 hook V4 池的中间资产在同次 unlock 精确抵消，首尾为不同资产。
- [Base 58+8 池常规影子](verification/BASE_MIXED_FORWARD_CLI_20261124.md) · [Robinhood 双 feed](verification/ROBINHOOD_FEED_PAIR_20261124.md)。Base 双源固定哈希入口完成三个完整混池状态，4/58/66 池扣模型成本后均无正候选。状态就绪 1.93–2.26 秒，采样间跳过 26 个高度。Robinhood 主、备用配对 132 个同哈希块，备用到达中位晚 505 ms，另有两个块的独立区块与收据根复核。
- [BSC 60/80 条 Swap 批次完整对账](verification/BSC_BATCHED_CLOSURES_20261124.md) · [Ethereum 执行器两周跟进](verification/ETH_BATCH_COHORT_FOLLOWUP_20261124.md)。BSC 新核实 35 个四跳闭环，两笔扣 gas 和直接付款后保留约 5% 毛差。Ethereum 本轮新增 29 个两至三跳闭环，固定 11 笔已落块调用均已对账，计三笔失败及直接付款后剩约 0.000842 ETH 等值和单列的 DAI 余额。
- [Base 并行状态 A/B](verification/BASE_STATE_DELIVERY_AB_20261123.md) · [含费截止时间](verification/MULTIHOP_DEADLINE_ENGINEERING_20261123.md) · [多链盈亏线与时点](verification/MULTIHOP_EXECUTABLE_EDGE_20261123.md)。固定哈希下的 66 池状态交付中位从 725.7 ms 降至 445.5 ms。Base 新事件影子的固定路线 4/4 在 750 ms 内完成含费模型，四例均负。Ethereum 两笔已落块批次付完可见成本后保留 9.77% 和 11.67% 毛差；BSC 精确交易前有毛差、同块末已转负。
- [Robinhood 和 BSC 长交易的资金路径](verification/MULTICHAIN_LONG_ROUTE_SAMPLES_20261123.md)。Robinhood 的三个收据根重建一致，一笔 22 条 Swap 分出 5 条用户路径和 8 次 hook 交换。BSC 的 56 条 Swap 分出 14 条资金路径，最长 5 跳。两个完整对账的样本均为跨资产兑换。
- [Base 常驻图前瞻对照](verification/BASE_RESIDENT_FORWARD_20261123.md) · [BSC 568 池长环筛选](verification/BSC_LONG_FORWARD_20261123.md) · [Ethereum 批次费用异常值](verification/ETH_BATCH_GAS_OUTLIER_20261123.md)。常规影子支持可选常驻对照。Base 八块 48 个扣费报告一致。BSC 两块的 5/11/13 跳样本全金额净额上界均为负。Ethereum 最高 gas 样本的 16 个闭环付完可见费用后仍为正。
- [Rust 常驻池图与多锚离线对照](verification/MULTIHOP_RESIDENT_GRAPH_20261123.md)。块绑定会话支持原子增量与显式完整替换，Base 58 池双锚后续本地扫描 p99 从 0.722 ms 降到 0.200 ms。实池候选和未决报告一致，合成正例的逐腿整数一致。前瞻整笔经济召回待测。
- [多跳 Rust/Python 决策与分段测量](verification/MULTIHOP_RUST_BOUNDARY_20261123.md) · [Base 四池/58 池/66 池 V4 混图](verification/BASE_V4_MIXED_GRAPH_20261123.md)。现有 Rust 短图与 Python 批次、长路定量继续各作参考，长 V2 整数上界的分段合成已接入。Base 新增 8 个无 hook V4 池，同块模型正例为 0，保留未决区间和负报价。
- [BSC 同本金批次回放](verification/BSC_V2_BATCH_COMPARISON_20261123.md) · [Ethereum 执行器两周成本](verification/ETH_BATCH_SELECTOR_COHORT_20261123.md) · [Base V4 池双源核验](verification/BASE_V4_FORWARD_DISCOVERY_20261123.md)。BSC 顺序更新排除了重置储备产生的正净额；Ethereum 三笔回滚经独立收据根确认；Base 12 个 V4 PoolKey 重算一致，六个无 hook 池有双源有限金额报价。
- [前瞻机会筛选与 119 条经济腿批次](verification/MULTIHOP_FORWARD_OPPORTUNITY_20261123.md)。支持按改变的池搜索长环，Base 常规 Multicall 影子保留阶段时钟与原报价。Base 六块和 BSC 一块均无已证实的可执行正净额。Ethereum 历史批次中，117 条 DEX Swap 与两次直接兑换来自 40 个短闭环，共用一笔贷款，计直接付款和 gas 后有可见正净额。
- [BSC 和 Base 多跳交易前状态回放](verification/MULTIHOP_PRETRADE_REPLAY_20261123.md)。BSC 已完整复现 5+1 跳所在交易，五跳毛差从精确前置报价的 0.01580 USDT 降至支路实得的 0.01339 USDT，下一块复报价转负。Base 的 35 条 V4 交换与回执一致，后续同一 UserOperation 因 nonce 拒绝，四池有限金额对照亏损。扩扫仍未证实几十跳的纯串行资金路径。
- [多链长交易尾部与资金深度](verification/LONG_SWAP_TAIL_20261123.md)。Base 回执出现 35 条 Swap 和 2 个 V4 池，BSC 的 80 条样本由 20 个四次交换的调用组构成，另有 5 跳闭环及一跳利润兑换的资金证据。新增只读回放分别统计事件、池、调用分组与有凭证的资金路径。
- [中小池闭环增量与整数金额认证](verification/MULTICHAIN_CLOSURE_SIZING_20261123.md)。在 retained Ethereum/BSC 快照中比较新池组成的回路，对最多 400 个不同池执行保守上界与精确整数寻优，明确未决区间。Robinhood 已有可持久化游标的离线影子提示回放，实时缺口回补和真值定价仍待在前瞻窗口完成。
- [中小池扩样与百跳闭环](verification/MULTICHAIN_TAIL_DEPTH_20261123.md)。Ethereum 与 BSC 分别核验 798、568 个双边非零 V2 池，Robinhood 严格身份核验 214 池。BSC 在 100/101 个不同池上完成整数报价，手续费后毛差为负；Robinhood 找到 100/109 池的拓扑闭环，其长环有限金额和整笔价值仍待验证。100–400 跳的研究对照见核验记录。
- [Ethereum、BSC 实时影子扫描与 Robinhood sequencer 接入](verification/MULTICHAIN_FORWARD_20261123.md)。Ethereum 的双 DEX 搜索发现四跳正毛差，十档资金均未覆盖基础 gas；BSC 20 个块的 12 池图均取得无正边际环证书，另有 3/30/102 串行跳的整笔余额复核。Robinhood 已解码 136 个连续有序消息并用官方 RPC 锚定两个块。目前是已结束的有界观测，稳定净利和持续运行可靠性待验证。
- [四链多跳机会的免费数据实证](strategies/MULTICHAIN_MULTIHOP_DISCOVERY.md) · [可复核数据](verification/MULTICHAIN_DISCOVERY_20261122.json)。Ethereum、BSC 和 Robinhood 已复核历史成交或固定块状态；四链仍缺同窗、含完整成本和排序约束的前瞻经济证据。
- [多跳套利实施 TODO](strategies/MULTIHOP_ARBITRAGE_TODO.md) · [2026-11-22 离线复核](verification/MULTIHOP_RESEARCH_20261122.md)：两池 V2 门槛漏报已修；同哈希离线回放、逐候选归因、Anvil 整笔复核及 Aleph RTX 4070 Ti 对照见[第一批实现验证](verification/MULTIHOP_IMPLEMENTATION_20261122.md)。真实 Base 窗口的整笔净额与召回仍待测。
- [Wave 23–31 `208e3e8..07aa611` 复核全量修复（P1/P2.1/P2.2/P3）与波次实测核验](verification/WAVE23_REVIEW_FIXES_AND_ANTITOXICITY_20261121.md)
- [2026-11-21 `208e3e8..07aa611` 审查结论复核](verification/REVIEW_208E3E8_07AA611_20261121.md)
- [2026-11-21 工具链与依赖升级核验](verification/TOOLCHAIN_UPGRADE_20261121.md)
- [2026-11-20 利润引擎安全复核修复记录](verification/SECURITY_REVIEW_FIXES_20261120.md)
- [Wave 21–22 Aero Slipstream 2、Bybit Spot V5 与 600-Tick 样本外实测核验](verification/BASE_AERO_CL2_BYBIT_600TICK_20261119.md)
- [Wave 19–20 做市生命周期、`TickMath` 闭上界、Keep-Alive 轮换与 2,942 块长窗口综合核验](verification/BASE_1000TICK_LONG_WINDOW_READINESS_20261119.md)
- [Wave 17–18 Coinbase Limit IOC 优先、并发亚块采集与 300-Tick 复测核验](verification/LIMIT_IOC_CONCURRENT_FASTPOLL_20261119.md)
- [Wave 15–16 双平面软件 EOA 托管、WSL Docker 隔离与 300-Tick 样本外核验](verification/SOLO_EOA_CUSTODY_AND_WSL_DOCKER_20261118.md)
- [Wave 13–14 自适应做市、样本外持久化执行、策略-权威桥接与容量雷达核验](verification/ADAPTIVE_OOS_STRATEGY_AND_AUTHORITY_BRIDGE_20261118.md)
- [Wave 11–12 可配置本金风控、动态容量与 Coinbase WS FIFO 做市核验](verification/DYNAMIC_CAPACITY_RISK_AND_WS_MAKER_20261118.md)
- [Wave 9–10 扩展生态、Maker-Hedge、借贷清算与隔离签名仓核验](verification/ECOSYSTEM_MAKER_LIQUIDATION_AND_AUTHORITY_20261118.md)
- [Base 100-Tick Multicall3 多资产与双源 RPC 实测核验](verification/BASE_100TICK_MULTICALL_CAMPAIGN_20261118.md)与[早期 3-Tick 核验](verification/PRELIVE_PROFIT_BRIDGE_20261118.md)
- [准实盘盈利操作手册](operations/PROFIT_ENGINE_PRELIVE_RUNBOOK.md)
- [CTO 全路线推进与 P1–P4 终验记录](verification/CTO_FULL_ROADMAP_COMPLETION_20261118.md)
- [自主机会引擎与四路并行升级核验](verification/AUTONOMOUS_OPPORTUNITY_ENGINE_20261118.md)
- [外部审计报告原文](references/EXTERNAL_OASIS_AUDIT_20261115.md)与[逐项复核](verification/EXTERNAL_OASIS_AUDIT_REVIEW_20261115.md)
- [Base Denim 头部时间戳观察](verification/BASE_DENIM_HEADER_20261115.md)
- [Wintermute 对手方观察：归档读取被拒绝](verification/WINTERMUTE_COUNTERPARTY_20261115.md)
- [持续执行及未完成项](strategies/NEXT_STAGE_EXECUTION.md)与[公共只读采集](strategies/PUBLIC_RPC_OBSERVATION.md)
- [Base 发布和恢复边界](operations/LIVE_RELEASE.md)
- [Phase 1 实验锁](strategies/PHASE1_LP_EXPERIMENT_LOCK.md)和[生产准备记录](operations/PREPRODUCTION_COMPLETION.md)
- [RPC 与证据管线](architecture/LP_MARKET_FEATURE_PIPELINE.md)和[控制台读模型](architecture/CONSOLE_READ_MODEL.md)
- [历史证据报告](verification/)及本机 `reports/evidence/`。已提交的原始证据和失败记录应保留。
  历史实现索引可通过 `git show 5cea5f7:AGENTS.md` 查询。
