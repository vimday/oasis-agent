# Wave 9–10 扩展生态套利、Passive Maker 对冲、借贷清算与隔离签名仓实测核验（2026-11-18）

2026-11-18（Asia/Shanghai）；在 `b135533`（Wave 8 已推送基线）之上通过四路并行合入 Wave 9（合入提交 `c9fae30`）并完成 Wave 10 联调与实测，推进：**IPv4 优先防 TUN `SSLEOFError` 传输加固**、**11 资产 Base `Multicall3` 与 2-hop WETH 路由**、**9 交易对 Coinbase 跨所 L2/L1 与 Passive Post-Only Maker 对冲仿真**、**Aave V3 / Morpho Blue 无密钥实时健康度与闪电贷清算扫描**，以及 **6 服务隔离 Docker Compose 与 Unix socket 签名权威闭环**。

全程严格保持无真实私钥、`execution_eligible = false`、`release_permitted = false`、无真实链上广播与付费 RPC 消耗。本文档衔接并保留早期核验记录（[PRELIVE_PROFIT_BRIDGE_20261118.md](PRELIVE_PROFIT_BRIDGE_20261118.md) 与 [BASE_100TICK_MULTICALL_CAMPAIGN_20261118.md](BASE_100TICK_MULTICALL_CAMPAIGN_20261118.md)）。

## 1. Wave 9–10 核心工程交付

- **IPv4 优先 DNS-SSRF 校验 HTTPS 传输层与 11 资产扩展 Base 宇宙**（[multicall_universe.py](../../src/oasis_agent/fastpath/multicall_universe.py)、[dual_rpc_probe.py](../../src/oasis_agent/live/dual_rpc_probe.py)、[run_multicall_100tick_campaign.py](../../scripts/run_multicall_100tick_campaign.py)、[单测](../../tests/unit/test_multicall_universe.py)）：
  - 在 `_IPv4PreferredHTTPSConnection` 与 `_KeepAliveHttpsPool` 中将 DNS 校验后的 `AF_INET`（含 macOS TUN `198.18.0.0/15` 合法虚拟映射）排在 `AF_INET6`（`2001:2::`）之前，消除经由本机 TUN 时的间歇性 `SSLEOFError`。
  - 通过 `--include-ecosystem-tokens`（默认 `False` 向后兼容 5 资产基准）新增 6 个高流动性 Base 生态资产：`VIRTUAL`、`MORPHO`、`WELL`、`DEGEN`、`VVV`、`cbETH`，与原有 `WETH`、`USDC`、`cbBTC`、`AERO`、`wstETH` 共同组成 **11 资产** 跨 DEX `Multicall3` 宇宙。
- **9 交易对 Coinbase CEX-DEX 与 Passive Post-Only Maker + DEX Taker Hedge 生命周期**（[live_cex_dex_shadow.py](../../src/oasis_agent/cross_venue/live_cex_dex_shadow.py)、[maker_hedge_shadow.py](../../src/oasis_agent/cross_venue/maker_hedge_shadow.py)、[campaign 集成](../../scripts/run_multicall_100tick_campaign.py)、[Maker 单测](../../tests/unit/test_maker_hedge_shadow.py)）：
  - 支持 `ETH-USD`、`BTC-USD`（当 `?level=2` 触发 `1 MB` 响应上限时单次安全回退至 `?level=1` 盘口）、`AERO-USD`、`VIRTUAL-USD`、`VVV-USD`、`MORPHO-USD`、`DEGEN-USD`、`WELL-USD` 与 `CBETH-USD`；对缺乏直接 `/USDC` 深池的生态资产自动路由 `USDC ↔ WETH ↔ TOKEN` 2-hop 报价，并在 `--trade-notional-usdc` 显式记录单笔交易名义额。
  - `MakerHedgeLifecycleTracker` 按 `t → t+1` 严谨样本外顺序跟踪 Post-Only 买/卖挂单与下一区块 Base DEX Taker 对冲：默认仅当下一刻对手盘**严格穿价**（`strict_cross`）时计入保守净盈利与实现收益率；同价 `touch_lift` 单独归类为诊断上界且绝不冒充已实现利润。
- **无密钥 Base Mainnet Aave V3 & Morpho Blue 实时健康度与清算扫盘**（[lending_reader.py](../../src/oasis_agent/fastpath/lending_reader.py)、[单测](../../tests/unit/test_lending_reader.py)）：
  - 基于分段 `eth_getLogs` 发现近期活跃借款人并经单次 `Multicall3.aggregate3` 同块拉取 `getReservesList()` 与 `getUserAccountData(borrower)`；`Morpho Blue` 同步提供 `idToMarketParams` / `market` / `position` 只读核验，并复用 [liquidation.py](../../src/oasis_agent/fastpath/liquidation.py) 闪电贷清算估值引擎（CLI 直接支持 `python -m oasis_agent.fastpath.lending_reader`）。
- **隔离盈利引擎 Unix Socket 签名权威与 6 服务硬化 Compose**（[atomic_authority_socket.py](../../src/oasis_agent/live/atomic_authority_socket.py)、[run_profit_authority.py](../../scripts/run_profit_authority.py)、[Dockerfile](../../deploy/profit-engine/Dockerfile)、[compose.yaml](../../deploy/profit-engine/compose.yaml)、[verify_profit_compose.py](../../scripts/verify_profit_compose.py)、[Compose 单测](../../tests/unit/test_profit_authority_socket_and_compose.py)）：
  - 将策略进程（`profit-strategy`）与独立密钥/签名服务（`profit-authority`）拆分为跨容器只读/只写受限挂载：`profit-authority` 持有唯一只读 `/run/oasis-keys:ro`、通过 `0o700` 目录 + `0o600` Unix 域套接字响应 `status` / `sign` / `record_settlement`（强制校验执行器与金库 EIP-1559 参数边界、持久化单调 nonce、2-failure `HALT` 持久化、`STOP` 急停与未决收据阻断）；`verify_profit_compose.py` 已接入 `make profit-deployment-check`。

## 2. 本机实测与无密钥实证汇总（归档于 `reports/research/wave10-20261118/`）

### 2.1 隔离 6 服务 Compose 离线配置核验（`profit_compose_verification.json`）

- 执行 `.venv/bin/python -m scripts.verify_profit_compose --out reports/research/wave10-20261118/profit_compose_verification.json`：`passed = true`、`execution_eligible = false`、`network_calls = 0`。
- 核验全部 6 项服务角色（`initialize`、`profit-authority`、`profit-authority-recovery`、`profit-authority-status`、`profit-strategy`、`profit-strategy-recovery`）在无 RPC 与合成双 RPC 两种配置下的边界。
- 摘要哈希：`compose_hash = 4cc26758e49e738317d63b069c637796c5269c1e258d4a7bd830620f0db5253b`、`verifier_hash = e2ee7c998dbb915a83bbb0f28ead6f630b642a231eea9aa901147be35acdbbe3`、`evidence_hash = 158aae7594ab8bf6a0384ec4daada6867699786ebb53f11ba88d4e78b3294baf`。

### 2.2 Base 主网 Aave V3 无密钥健康度实读（`aave_v3_base_health.json`）

- **环境问题留档**：本机开启 macOS 系统代理（`127.0.0.1:7890`）时，Python `urllib.request` 默认读取系统代理导致直连 HTTPS 客户端连接本地回环地址而触发 fail-closed `Base lending RPC forbids private or loopback IP endpoints` 拒绝；随后的 600 块初读（区块 `59040169`）仅发现 1 名无活跃债务借款人（`5` 次 RPC，`report_sha256 = a01ec3cd...`）。使用 `no_proxy='*'` 后（底层网络由 TUN 直接路由，DNS-SSRF 仍严格放行且无代理绕过），5,000 块滑窗成功拉取真实活跃仓位。
- **5,000 块滑窗实读结果**：
  - 固定区块：`59040294`（`block_hash = 0x76ce47a9dc2873a310788e740d794d307343a271134838b90b46a6fe92418820`），消耗 **`12`** 次免费 RPC。
  - 链上储备与借款人：识别 Aave V3 全量 **`15`** 种储备资产，发现 `6` 名近期借款人，其中 **`5`** 名持有活跃债务；`liquidatable_accounts = 0`、`at_risk_watchlist = 0`（`< 1.08`）。
  - 最低健康因子仓位：`0x3055d1826c6c5d292e4d4b1e5b0d9759ffa9410a`，抵押品折合 USD **`203,051.70`**、总债务 USD **`130,951.56`**、**`health_factor_wad = 1258910189455112784`（`1.2589`，距清算安全垫 `2,589 bps`）**。
  - 报告自哈希：`01916cb708617b7b3846100def76a95c10425579f2bb266fff34c5610e6fd9bc`（`execution_eligible = false`）。

### 2.3 2-Tick 冒烟（`smoke-2/`）与 100-Tick Base 扩展生态连续影子战役（`shadow-100-ecosystem/`）

- **2-Tick 扩展生态冒烟（USD 1,000 默认档位）**：在区块 `59038885` 起连续完成 2 tick / 7 次 RPC，发现 **`43`** 座深池并覆盖全 9 项 Coinbase 产品；其中 8 项 `?level=2` 返回正常，仅 `BTC-USD` 按设计降级为 `book_level = 1` 并因 L1 顶档深度 `< USD 1,000` 诚实记为 `degraded`；`CBETH-USD` Maker Ask 挂单报价正边际已达 `+93.259058 bps`（`summary_sha256 = a4c6a533a110e07b21cf026072c8d749e48c72b2c2d94c03ba3667715f1727c9`）。
- **100-Tick 规范扩展战役（`--include-ecosystem-tokens --trade-notional-usdc 250000000`，合宪法 USD 250 单笔）**：
  - **区间与系统稳定性**：连续观察 Base 区块 `59040453..59046274`（**`100` 个唯一 Base 区块**、`ticks_completed = 100`），真实 `Multicall3` 深池Universe **`43` 座池**，每 tick 在 Rust 内核评估 **`1,204` 条原子环路**，总计消耗 **`203 / 350`** 次免费 RPC，**0 次 `SSLEOFError`**、**0 次 `QuoterV2` 幻觉正候选**。哈希链尾 `aaa7ea6d8fba5b04301b5a4ed236c223dbee86484bedc9266ae3e6d5a4976669`，汇总自哈希 **`6596bd1654a51a3c69acb1e9399d4d469ba504bdb6d6839a77695a9ff5c23b44`**。
  - **首个成本后正收益 Taker 跨所事件**：在全 100 tick、888 次成功 Coinbase 评估（`12` 次因 `Coinbase book transport failure` 降级）中，首次捕捉到 **1 次扣除全额费用与延迟折价后的正净收益事件** —— **Tick 54（区块 `59043175`）`BTC-USD`（`buy_dex_sell_cex`，`book_level = 1`）：毛价差 `+11.644735 bps`，扣 `3.5 bps` Taker 费率、`1.5 bps` 延迟折价与 USD `0.006316` 链上 gas 后净盈利 `+6.386260 bps`**（0 bps Maker 诊断下为 `+9.890335 bps`）。但整体样本下 **Taker 模式仍为负 LCB**：`mean_net_edge_bps = -2.718435`、`std = 1.040646`、`lcb_net_edge_bps_exact = -2.922402`、**`lcb_net_edge_bps = -3`**（原子多 DEX 最佳毛差 `-1.058904 bps`、最佳扣 gas 净差 `-1.409023 bps`）。

| Coinbase ↔ Base 产品 | 有效 / 降级评估 | 盘口深度级别 | 峰值 Taker 毛差 (bps) | 峰值 Taker 净边际 (bps) | 平均 Taker 净边际 (bps) | Maker-Hedge 有效报价腿（Ask / Bid） | 峰值 Maker 挂单边际 (bps) | 诊断 `touch_lift` 成交（正盈利 / 总数） |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `BTC-USD` | `97 / 3` | `[1, 2]`（`96` 次 `L1`、`1` 次 `L2`） | **`+11.644735`** | **`+6.386260`**（Tick 54） | `-7.2755` | `1 / 0` | `+9.988517` | `0 / 0` |
| `ETH-USD` | `100 / 0` | `[2]` | `+2.433134` | `-2.819757` | `-4.9548` | `0 / 0` | `+1.292241` | `0 / 0` |
| `VVV-USD`（2-hop） | `98 / 2` | `[2]` | `+3.155063` | `-2.098643` | `-25.8658` | `1 / 2` | `+12.566722`（Bid） | `1 / 2` |
| `VIRTUAL-USD`（2-hop） | `98 / 2` | `[2]` | `+1.033921` | `-4.218919` | `-14.0718` | `2 / 36` | `+10.844042`（Bid） | `6 / 6` |
| `AERO-USD` | `97 / 3` | `[2]` | `-4.767792` | `-10.020988` | `-21.5419` | `0 / 0` | `-2.751845` | `0 / 0` |
| `CBETH-USD` | `100 / 0` | `[2]` | `-3.736402` | `-8.987059` | `-17.0906` | **`89 / 87`** | **`+129.333805`**（Ask） | **`65 / 65`** |
| `MORPHO-USD` | `98 / 2` | `[2]` | `-12.999781` | `-18.245692` | `-30.8921` | `0 / 0` | `-3.821573` | `0 / 0` |
| `DEGEN-USD` | `100 / 0` | `[2]` | `-23.277635` | `-28.531840` | `-49.9940` | `12 / 6` | `+22.682763`（Ask） | `2 / 2` |
| `WELL-USD`（2-hop） | `100 / 0` | `[2]` | `-111.700348` | `-116.896822` | `-174.1048` | `0 / 0` | `-57.714718` | `0 / 0` |

- **Passive Post-Only Maker + DEX Taker Hedge 关键经济洞察**：
  - 全 9 个产品共运行 **`888`** 步状态转移，在 `CBETH-USD`、`VIRTUAL-USD`、`DEGEN-USD`、`VVV-USD` 与 `BTC-USD` 上共挂出 **`236`** 条正净边际（≥ `3.0 bps`、已扣除 `1.5 bps` 逆向选择与 Base DEX gas）的被动挂单腿，`best_quoted_edge_bps = +129.333805 bps`、`unhedged_fill_warnings = 0`。
  - 保守 **严格穿价成交（`strict_cross_fills`）为 `0`**（`strict_total_net_profit_usd = 0.0`），即下一秒级采样窗口内尚未出现对手盘完全吞没并穿透我方挂单价的离散位移；但仅同价触碰诊断（`touch_lift_fills`）发生 **`75`** 次（其中 `74` 次净正，平均 **`+17.5409 bps`**、区间 `-3.3484` ~ `+101.6631 bps`、USD 250 档位名义诊断累计 **`+$32.8798 USD`**）。这明确指出了实盘转正的最短技术路径：将 800 ms HTTP 轮询替换为 **Coinbase WebSocket 真实成交流（matches）排队位置模拟器**，以实证 Post-Only 挂单在高点差生态资产（`cbETH`、`DEGEN`、`VIRTUAL`）上的真实样本外成交率。

### 2.4 最新双独立 RPC 仲裁与 9 门禁就绪状态（`dual_rpc_quorum.json` & `readiness_report.json`）

- **最新双源仲裁（区块 `59046420`）**：`publicnode_base` 与 `base_official_mainnet` 对 `block_hash = 0xf333344d0895b5d911aca7cfae404c162412672144ae401fcd9e1ad2c8cc0169`、`state_root = 0x06865acf76b40e683be392eb2691f9cfae40322c0eacf1926e4057c5d182ec42` 与 `base_time_ms = 1794999659600` 完全一致，自哈希 `66a1538ad7384f61b08229ef20ab7dc62d0256fb207478019c901b0badea9bf2`。
- **9 项发布门禁评估**（`report_sha256 = c53cfd8ce754d9e0d8c9711f64dd413da354fee618b0e415d0aecb2fe49f95a3`）：**`6 / 9` 通过**，`release_permitted = false`、`execution_eligible = false`：
  1. `compiled_contracts_pinned`：PASS（`executor=3d1d2aac1dfa`, `vault=c4492642b730`）
  2. `anvil_e2e_settlement_reconciled`：PASS（4/4 drills，early abort `58,204 gas`，0 授权残留）
  3. `base_time_predeploy_verified`：PASS（block `59010596`，`header_match=True`）
  4. `untouched_shadow_sample_size`：PASS（`ticks_completed = 100`）
  5. `positive_cost_adjusted_lcb_edge`：**BLOCKED**（`positive_net_ticks = 1`，Taker `lcb_net_edge_bps = -3 < 5`；Maker 被动对冲尚缺 WebSocket 成交流真实验证）
  6. `revert_budget_healthy`：PASS（`active`，`unresolved_residuals = 0`）
  7. `dual_rpc_quorum_verified`：PASS（`providers=publicnode_base+base_official_mainnet`，block `59046420`）
  8. `isolated_signer_authority_ready`：**BLOCKED**（需运维配置独立签名密钥卷与容器实例并出具运行时 `run_profit_authority --out-proof` 证明）
  9. `owner_scope_release_manifest_present`：**BLOCKED**（需 owner 对具体窗口出具不超 USD 500 资本 / USD 200 日亏上限的书面签名授权）

## 3. 安全与发布结论

- 当前所有能力处于**只读发现、扩展影子计量、无密钥健康扫描与离线部署硬化校验**阶段，`execution_eligible = false`、`release_permitted = false`。
- 下一步唯一高确定性转正方向：基于 [maker_hedge_shadow.py](../../src/oasis_agent/cross_venue/maker_hedge_shadow.py) 接入 Coinbase WebSocket 实时成交流（matches）排队位置校准，在高点差生态资产（`CBETH-USD`、`VIRTUAL-USD`、`DEGEN-USD`、`VVV-USD`）上以 untouched 窗口证明 `strict` + 真实排队位置 `lcb_net_edge_bps >= 5`，同步由运维挂接独立密钥卷与 `run_profit_authority` 实证第 8/9 门禁。
