# Base 100-Tick Multicall3 多资产扫描、双源 RPC 仲裁与 CEX-DEX 实测核验（2026-11-18）

2026-11-18（Asia/Shanghai）；在 `febbfb7`（Wave 5–6 已推送基线）之上完成 Wave 7 三路并行升级与 Wave 8 合入（合入提交 `94f6cb8` + Keep-Alive 传输修复），并在无私钥、只读、`execution_eligible = false` 约束下完成 **Base 主网双独立 RPC 仲裁证明** 与 **100-tick 无干预 `Multicall3` + Coinbase `public-shadow` 连续实测战役**。本记录用于衔接并更新此前 3-tick 基线记录（[PRELIVE_PROFIT_BRIDGE_20261118.md](PRELIVE_PROFIT_BRIDGE_20261118.md)）。

## 1. Wave 7 / Wave 8 核心工程交付

- **Rust 无精度盲区 Spot 上界预剪枝**（[graph_arb.rs](../../crates/oasis-core/src/graph_arb.rs)）：
  在 `IndexedGraph::from_chain_pools` 预计算每条有向边的 `max_rate_upper`（对 V2 与 CL 池基于当前 spot / √P 与费率乘上 `1.000_001` 安全松弛；`AerodromeStable` 保留 `∞` 不剪枝），在 `enumerate_indexed_cycles` 闭合环路前先校验全环路上界 `<= 1.0`，仅当确证全区间无正套利可能时跳过 48 步三分搜索，而保留 `cycles_evaluated` 计数与 `min_amount_in = 1 wei` 截断边界的比特级等价性。`--release` 下 **34 池 6 代币 4-hop（1,868 条环路）实测 `2.37 ms/scan`**（`2,373 us/scan`），**6 池 2-hop 实测 `53 us/scan`**。
- **Base `Multicall3` 五资产多 DEX 发现与防幻觉 Quoter 校验器**（[multicall_universe.py](../../src/oasis_agent/fastpath/multicall_universe.py)、[单测](../../tests/unit/test_multicall_universe.py)）：
  - 在 `WETH`、`USDC`、`cbBTC`、`AERO`、`wstETH` 五种资产跨 `Uniswap V3`（`100/500/3000/10000`）、`Aerodrome Slipstream`（`ts=1/50/100/200`）与 `Aerodrome Volatile` 间通过 3 次只读 RPC 完成发现并按实时 ERC-20 `balanceOf(pool)` 最低深度过滤尘埃池；
  - 每轮行情与费用（`BaseTime` + `GasPriceOracle.getL1Fee` + 全部池状态与余额）合并为 **2 次 RPC**（`eth_getBlockByNumber` + `Multicall3.aggregate3`），并以 `compute_anti_hallucination_cl_bracket` 将合成单区间虚拟可交割量限制在实时池内余额的 `80%` 以内；正利润候选再逐跳提交链上 `QuoterV2` / `SlipstreamQuoter` 复验。
- **无密钥双独立 Base RPC 仲裁验证器**（[dual_rpc_probe.py](../../src/oasis_agent/live/dual_rpc_probe.py)）：
  通过 `publicnode_base` 与 `base_official_mainnet` 两家独立运营方同块交叉核验 `eth_chainId == 8453`、块头 `hash`、`parentHash`、`stateRoot`、`BaseTime` 毫秒时间戳以及核心合约运行时字节码 SHA-256。
- **Coinbase ↔ Base 跨所 L2 VWAP 评估与 100-tick Keep-Alive 战役引擎**（[live_cex_dex_shadow.py](../../src/oasis_agent/cross_venue/live_cex_dex_shadow.py)、[run_multicall_100tick_campaign.py](../../scripts/run_multicall_100tick_campaign.py)、[单测](../../tests/unit/test_dual_rpc_and_cex_dex_shadow.py)）：
  - 基于 HTTP/1.1 Keep-Alive（`http.client.HTTPSConnection` + `certifi` TLS、禁止重定向、HTTP 429 一次失败即停、仅在已复用空闲连接断开时单次重连）修复初轮 `smoke-2` 短连接新建 TLS 握手通过 TUN 超时问题；
  - 对 `WETH`（`0.001..1 WETH`）与 `USDC`（`5..2,000 USDC`）双锚最多 3-hop 原子环路与 Coinbase ↔ Base 双向 L2 VWAP 价差做同块并行计量，支持 1 块副本滞后自动回读对齐并严格阻断真重组。
- **外部/硬件签名器确定性 `CREATE2` 部署脚本与 9 门禁总闸**（[DeployOasisProfitEngine.s.sol](../../contracts/script/DeployOasisProfitEngine.s.sol)、[Foundry 测试](../../contracts/test/DeployOasisProfitEngine.t.sol)、[canary_readiness.py](../../src/oasis_agent/live/canary_readiness.py)、[门禁单测](../../tests/unit/test_profit_engine_readiness_and_anvil.py)）：
  - 脚本禁止读取私钥，强制 7 个角色两两严格隔离（全部 21 组碰撞由 Foundry 验证），部署 `OasisAtomicExecutor` 与 `OasisIntentVault` 并导出码哈希；
  - 将就绪闸门由 8 项拆细为 **9 项**：把可无密钥实证的 `dual_rpc_quorum_verified`（第 7 项）与需运维挂载硬件/隔离签名卷的 `isolated_signer_authority_ready`（第 8 项）彻底解耦。

## 2. 本机实测证据摘要（归档于 gitignore 目录 `reports/research/campaign-20261118/`）

### 2.1 双独立 Base RPC 仲裁证明（`dual_rpc_quorum.json`）

- **主/副节点**：`publicnode_base`（`https://base-rpc.publicnode.com`）与 `base_official_mainnet`（`https://mainnet.base.org`）
- **固定区块**：`59010448`（`block_hash = 0x94d8ade49d95abeca1e9c12bb1e102590a84a9d60a877c87bddc468d9bd1ea15`、`state_root = 0xc768b4224a1c8e52e68b16c5b760ecbdea6b3ef777e33434ec0d84a704195fdd`）
- **`BaseTime` 毫秒时间戳**：`1794992465200`；`WETH`（`667c900c...`）、`BaseTime`（`5dcd6f46...`）与 Uniswap V3 `WETH/USDC`（`0d14cff6...`）字节码哈希双源完全一致。
- **报告自哈希**：`42290931124e6af4e9b0c710a0bb78623e27b83246a246256c818a0d3c0ca604`（`secondary_rpc_verified = true`、`execution_eligible = false`）。

### 2.2 回环 Anvil 结算与实时 `BaseTime` 雷达

- `anvil_e2e_report.json`（自哈希 `beab40af73ae85c2eb711545ba7a319e7249b88f0ce1161c9e96b87ed6931b30`）：4/4 全通过；`executeCycle` `191,538 gas`、`sweepProfit` `66,622 gas`、`EarlyAborted` `58,204 gas`（`< 65,000`）、`rotateQuoteSigner` `36,536 gas`、`executeReactorFill` `271,892 gas`，4 项授权残差均为 `0`。
- `radar/radar_report.json`（`mode = public-shadow`）：Base 区块 `59010596` 核验 `BaseTime` 毫秒时间戳 `1794992494800` 与块头完全匹配（`header_matches_predeploy = true`）。

### 2.3 Base 主网 100-Tick 连续影子采样（`shadow-100/`）

- **初轮失败尝试留档（`smoke-2`）**：初版通过 `urllib.request` 每请求新建 TCP/TLS 连接经本机 TUN 时触发 `Base shadow RPC transport failure`（退出码 `2`）。升级为持久 HTTP/1.1 Keep-Alive 后问题消除，重跑 2-tick 冒烟即稳定完成（`7/40` RPC）。
- **完整 100-tick 连续样本（`shadow_summary.json`）**：
  - 区间与预算：区块 `59014619..59017276`（**`100` 个唯一 Base 区块**、`ticks_completed = 100`），真实深池Universe **`25` 座池**，每 tick 于 Rust 内核扫盘 **`812` 条环路**，总计消耗 **`203/350`** 次免费 RPC（3 次发现 + 100 × 2 次 `Multicall3`），哈希链尾 `df9db4a77751fd366b4950c22ac7f2564964fc70f6ddc42ae1e85d6f190d935a`，汇总自哈希 `b086a42c928f3c2646ec4e6a0c6de8ed974fa309b69a2887f3330c5b5bd2511d`。

| 评估维度 | 样本覆盖 | 实测毛价差 / 净表现 | 工程与经济结论 |
| --- | --- | --- | --- |
| 原子多 DEX（25 池 ≤ 3-hop） | 100 tick（每 tick 812 条环路） | 最优毛差 **`-1.401395 bps`**；最优扣 gas 净差 **`-1.751497 bps`**；平均净差 **`-2.911994 bps`**；幻觉候选 `0` | 公共块上的多 DEX 静态 2/3-hop 被双边 1 bps+ 手续费与 MEV 搜索者压紧，防幻觉上限成功避免虚假正利润 |
| Coinbase ↔ Base `ETH-USD`（USD 500 档位） | `96` 正常 / `4` 传输降级 | 平均毛差 **`+0.256315 bps`**；峰值毛差 **`+2.778516 bps`**；**0 bps Maker 诊断下 `14/96` 次为正，峰值 `+1.151664 bps`**（已扣 `1.5 bps` 延迟折价与均摊 USD `0.00634` L1+L2 gas）；扣 `3.5 bps` Taker 后最优 `-2.348336 bps` | 真实正毛价差集中出现在 Coinbase ↔ Base Slipstream 1 bps 池之间；当前零售 `3.5 bps` Taker 费率吞噬了跨所边际，需极低费率/Maker 对冲（`AlephTX` Saga 模式）或零池费意图流才能转正 |
| Coinbase ↔ Base `AERO-USD` | `96` 正常 / `4` 传输降级 | 平均毛差 `-14.963579 bps`；峰值毛差 `-0.830834 bps` | `AERO/USDC` 链上 LP 费率较高（20–30 bps），跨所套利门槛高 |
| Coinbase ↔ Base `BTC-USD` | `100` 降级（96 超 1MB 上限、4 传输故障） | 安全记为 `degraded`，零崩溃 | 全深度 L2 响应超过 `1_000_000` 字节安全上限，触发 Fail-Soft |
| 综合 out-of-sample LCB | `100` tick | `mean = -2.896731 bps`、`std = 0.489662 bps`、`lcb_exact = -2.992705 bps`、`lcb_net_edge_bps = -3` | 严格未达 `>= +5 bps` LCB 门槛，收益闸门按设计拒绝发布 |

### 2.4 9 项发布闸门总验结果（`readiness_report.json`）

总览：`passed_count = 6/9`、`release_permitted = false`、`execution_eligible = false`、报告自哈希 `1b70eb2d7621c312936af847d550939559e1efdb1d7bfdb4a22213345e061bdf`。

| # | 闸门名称 | 状态 | 实测观测值 | 说明 / 剩余前置工作 |
| --- | --- | --- | --- | --- |
| 1 | `compiled_contracts_pinned` | **PASS** | `executor=3d1d2aac1dfa, vault=c4492642b730` | 合约运行字节码哈希锁定 |
| 2 | `anvil_e2e_settlement_reconciled` | **PASS** | `4/4` drills、`early_abort_gas=58204`、0 残差 | 回环 Anvil 真实 EIP-1559 结算通过 |
| 3 | `base_time_predeploy_verified` | **PASS** | `block=59010596, header_match=True` | `BaseTime`（`0x4200...0030`）匹配 |
| 4 | `untouched_shadow_sample_size` | **PASS** | `mode='public-shadow', ticks_completed=100` | 已达到 `>= 100` 无干预样本 |
| 5 | `positive_cost_adjusted_lcb_edge` | BLOCKED | `positive_net_ticks=0, lcb_net_edge_bps=-3` | 需在免 Taker/低费率路径上证得 `lcb >= +5 bps` |
| 6 | `revert_budget_healthy` | **PASS** | `revert_budget_status='active', residuals=0` | 回滚分级预算健康且无未平残差 |
| 7 | `dual_rpc_quorum_verified` | **PASS** | `publicnode_base+base_official_mainnet @ #59010448` | 双独立 RPC 同块哈希与状态根一致 |
| 8 | `isolated_signer_authority_ready` | BLOCKED | `unprovisioned` | 待运维配置独立 Docker 密钥卷与 Unix 权威 socket |
| 9 | `owner_scope_release_manifest_present` | BLOCKED | `absent` | 待全部技术与收益条件就绪后由 owner 签署（<= USD 500） |

## 3. 下一步安全推进方向

1. **策略与费率解锁**：重点推进 (a) 以 CEX VIP 0 bps Maker / 挂单返佣先成交、DEX 侧作为低滑点 taker 对冲的 `AlephTX` 跨所做市（实测 0 bps Maker 已有 14/96 次正向信号、`+1.15 bps` 峰值），(b) `UniswapX Dutch_V3` 零 LP 费意图流竞价与 `OasisIntentVault` 原子充沛，以及 (c) `state_sync.py` WebSocket 新块增量同步 + `liquidation.py` Morpho/Aave 清算脉冲抓取，在新的 untouched `>= 100` tick 窗口证得 `lcb_net_edge_bps >= 5`。
2. **运维与托管就绪**：按 [PROFIT_ENGINE_PRELIVE_RUNBOOK.md](../operations/PROFIT_ENGINE_PRELIVE_RUNBOOK.md) 通过多签执行 `DeployOasisProfitEngine.s.sol` 并挂载独立隔离签名器卷，最后签署 <= USD 500 范围清单。
