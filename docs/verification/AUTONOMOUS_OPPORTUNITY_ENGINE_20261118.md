# 自主机会发现与多引擎架构升级核验（2026-11-18）

2026-11-18（Asia/Shanghai）；起止基线 `5b66a13`，四路并行交付 + Wave 2 集成核验。所有新增模块与 CLI 严格运行在离线、仿真或只读影子模式（`execution_eligible=false`），未加载私钥、未签名、未广播、未触及真实资金或远端写入权限。

## 1. 升级背景与「慢脑 × 快手」混合架构

针对 [外部审计与核验](EXTERNAL_OASIS_AUDIT_REVIEW_20261115.md) 及 [双链实时报价结果](EVENT_QUOTE_RESULTS_20261114.md) 暴露出的三大结构性约束——（a）无路由白名单的自定义 v4 LP 承接逆向选择损耗；（b）远程 RPC 串行 `Quoter` 询价耗时 `489–2,256 ms`，显著滞后于 Base Denim `200 ms` 原生出块；（c）缺乏 `LaunchpadSafetyGate` 衰减税/锁仓解析与多引擎机会雷达——本轮引入 **Rust 确定性内核与状态图 + Solidity 原子执行边界 + Python 认知情报/账本/跨所 Saga** 的分层架构，并在不改动锁定依赖（`pyproject.toml`、`uv.lock`、`requirements*.lock` 未修改）的前提下完成全量落地。

## 2. 四大引擎落地清单

### 2.1 Rust 本地多 DEX 状态图内核与 AlephTX 共享内存桥接
- **Rust 工作区**：[crates/Cargo.toml](../../crates/Cargo.toml)、[crates/oasis-core/Cargo.toml](../../crates/oasis-core/Cargo.toml) 及 `crates/Cargo.lock`（`edition = "2024"`，`rustc 1.95.0`，`rlib` + `cdylib` + `oasis-fastpath` 二进制）。
- **核心模块**：
  - [u256_math.rs](../../crates/oasis-core/src/u256_math.rs)：零 `unsafe` 小端 `U256` / `U512` 精确整数运算（`mul_div_down`、`mul_div_up`、`isqrt`）。
  - [pool_math.rs](../../crates/oasis-core/src/pool_math.rs)：逐位对齐 [liquidity_math.py](../../src/oasis_agent/uniswap_v4/liquidity_math.py) 的 `sqrt_price_at_tick` / `amount0_delta` / `amount1_delta` / `amounts_for_liquidity`，实现 `V3` / `Slipstream` / `V4` 跨 Tick 精确兑换、`V2` 恒定乘积及 `AerodromeStable`（`x^3 y + y^3 x >= k`）255 步牛顿迭代求解（不收敛即 fail-closed）。
  - [graph_arb.rs](../../crates/oasis-core/src/graph_arb.rs)：本地内存池图 `PoolGraph` 支持 2/3-hop 闭环扫描及 48 步精确整数三分法求最优投入规模 `optimal_amount_in`，扣除 L1+L2 gas 后负净利直接拒绝。
  - [aleph_shm.rs](../../crates/oasis-core/src/aleph_shm.rs) 与 [audit_ring.rs](../../crates/oasis-core/src/audit_ring.rs)：兼容 `AlephTX` `ALEPHTX_SHM_V12\0`（`2048 × 12 × 64B` Seqlock BBO）与 `OASIS_AUDIT_V01\0`（`1024 × 128B` 异步审计环，含原子 `halt_flag` fail-closed 停机位）。
  - **Python 桥接**：[src/oasis_agent/fastpath/](../../src/oasis_agent/fastpath/__init__.py) 通过 `ctypes` 绑定 `liboasis_core`，并提供纯 Python `mmap` 审计消费者。

### 2.2 Solidity 有界原子执行器与分级回滚预算
- **合约**：[OasisAtomicExecutor.sol](../../contracts/src/OasisAtomicExecutor.sol)（`BUSL-1.1`，`solc 0.8.26`，`Ownable2Step`）。
  - **双平面托管隔离**：不可变 `TREASURY` 与 `REVENUE_SINK`；`guardian` 仅可 `emergencyPause()`；`operator` 仅可调用 `executeCycle` 与 `sweepProfit`（只向 `REVENUE_SINK` 转账），无任意地址提币接口。
  - **早期中止预检（Early-Abort Probe）**：可选 `PrecheckGuard` 在锁定状态或转移代币前读取 V3/Slipstream `slot0()`；若 `sqrtPriceX96` 越界则触发 `EarlyAborted` 并干净返回 `(false, 0)`，实测内层耗时 `58,168–58,376` gas（外层总计 `62,422–64,648` gas，严格低于 `65,000` gas），与 [OasisAtomicExecutor.t.sol](../../contracts/test/OasisAtomicExecutor.t.sol) 一致。
  - **严格盈利兜底**：回调校验 `_lockState == 1 && msg.sender == _expectedCallbackPool`，遵循 CEI 清零回调期望状态后转账，末尾强制校验 `balAfter >= balBefore + minNetProfit`。
- **Python 编码与分级风控**：[atomic_bundle.py](../../src/oasis_agent/execution/atomic_bundle.py) 与 [revert_budget.py](../../src/oasis_agent/risk/revert_budget.py)（区分 `EARLY_ABORT_PROBE`、`DEEP_EXECUTION_REVERT`、`AMBIGUOUS_HALT`，按 3,600 秒滑动窗口净燃气燃烧额熔断）。

### 2.3 Equation News (`eqn.news`) 催化情报与 Base Denim 发射台安全门
- **情报引擎**：[src/oasis_agent/intelligence/](../../src/oasis_agent/intelligence/__init__.py) 完整覆盖 `eqn.news` 14 个公开频道（`eqn_catalog.py`、`eqn_client.py` 限额只读 HTTPS/WS/MCP 客户端与无浮点 `Decimal`→整数转换、`catalyst_scorer.py` 优先识别黑客/下架/暂停充提为 `PROTOCOL_INCIDENT_FREEZE`、`discovery_bridge.py` 通过 `VerifiedSymbolRegistry` 仅向 [DiscoveryProvider](../../src/oasis_agent/launch/discovery_slot.py) 提名已核验合约地址，拒绝按 ticker 猜合约），配套 CLI [capture_eqn_catalysts.py](../../scripts/capture_eqn_catalysts.py)。
- **发射台安全门（P1）**：[launchpad_safety.py](../../src/oasis_agent/launch/launchpad_safety.py) 对 Clanker v4、Doppler、Zora、Flaunch、Virtuals 建立纯整数衰减曲线与时间锁模型，强绑定 [base_denim.py](../../src/oasis_agent/evidence/base_denim.py) `timestampMs`，对软跑路（同块 Dev 抢跑/关联钱包簇/历史跑路）及任意缺失字段 fail-closed 拒绝，对纯衰减或锁仓未到期候选返回 `DEFER_UNTIL_DECAY` 与精确毫秒重检槽位 `earliest_safe_entry_ms`。

### 2.4 跨市场 `HedgeGroup` Saga、AlephTX 跨所引擎与 UniswapX 意图填充器
- **分布式 Saga 与跨所评价**：[src/oasis_agent/cross_venue/](../../src/oasis_agent/cross_venue/__init__.py) 实现 [hedge_group.py](../../src/oasis_agent/cross_venue/hedge_group.py)（`(venue, order_id, fill_id)` 幂等成交去重、Ack 不产生对冲需求、未对冲名义金额×时长硬上限，针对陈旧仓位幻影 delta 强制转入 `HALTED` 并仅允许 `reduce_only=True` 平仓）、[aleph_bridge.py](../../src/oasis_agent/cross_venue/aleph_bridge.py)（Python Seqlock `ALEPHTX_SHM_V12` 读取器及只读 `ssh aleph` 湖仓探针）、[lead_lag.py](../../src/oasis_agent/cross_venue/lead_lag.py) 与 [funding_basis.py](../../src/oasis_agent/cross_venue/funding_basis.py)（强约束 RWA `SessionStatus.REGULAR` 与强平距离）。
- **零池费意图影子填充**：[src/oasis_agent/intents/](../../src/oasis_agent/intents/__init__.py)（`dutch_v3.py` 与 `filler_shadow.py`）支持 Base `V3DutchOrderReactor` 分段非线性衰减解析与 `exclusivityOverrideBps` 校验，显式拒绝已废弃的 `PriorityOrderReactor`，并执行 `<= 500 ms` 报价 SLA 预算评估。

## 3. 统一机会雷达实测观察（只读公开影子模式）

统一入口 [run_opportunity_radar.py](../../scripts/run_opportunity_radar.py) 于本地只读目录 `reports/research/opportunity-engine-20261118/` 完成一次带密码学哈希的 `public-shadow` 运行（`observed_at_ms=1794983070538`，`execution_eligible=false`）：

| 检验维度 | 实测观测结果 | 证据边界与说明 |
| --- | --- | --- |
| 统一雷达清单 | `radar_report_sha256` = `358999cdb84b66f7fd1dd4655efe515d8961dae19e98d7cf8240e8b73f7903fd` | 本机未提交归档；不可替代实盘账本 |
| Rust vs Python 数学差分 | 7 个边界 Tick × 100 轮 + [test_fastpath_differential.py](../../tests/unit/test_fastpath_differential.py) 200 组随机 Tick/流动性：**`0` 差异（逐位一致）** | 纯 Python 700 次单运算 `1,798,250 ns` vs 逐次 JSON-FFI `8,067,958 ns`（验证单次小算子不宜逐次跨 JSON FFI）；Rust 进程内 4 池 3-hop `scan_cycles` 实测 `11.92–11.98 ms`（零远程 RPC，对比此前 [实时报价延迟](EVENT_QUOTE_RESULTS_20261114.md) `489–2,256 ms`） |
| `OasisAtomicExecutor` 打包与预算 | 932 字节 calldata（SHA-256 `4731c291...`），`EarlyAborted` 归类 `EARLY_ABORT_PROBE`，`revert_budget_status="active"` | Foundry 测得早期中止 `62,422–64,648` gas，不变量测试 512 轮 65,536 次调用 0 次违规 |
| `eqn.news` 公开快讯抓取 | HTTP 200 获取 20 条真实快讯（`eqn_raw_sha256` = `1f8216d23e808c15c624ebde8885f7de3b49c44838f5ab3d92d1fd5e456871e1`） | 分类出 `alpha_ecosystem_alert=8`、`fast_price_shock=3`、`tier1_listing=3`、`oi_funding_crowding=2`、`protocol_incident_freeze=4`（自动冻结 `APT`/`INJ` 充提暂停与 `SPOT` 交易对下架；7 个未注册 ticker 留存 `unmapped_symbols` 未强行映射） |
| `AlephTX` 跨语言 SHM 与远端湖仓 | 合成 `ALEPHTX_SHM_V12` 镜像（SHA-256 `dd1c3888...`）Rust 与 Python 读取完全一致；只读 `ssh aleph` 探针 `reachable=true` | 观测到 `~/aleph-tx-lakehouse` 下 `campaigns=38`、`gold=10`、`parquet=28`、`replay_tapes=37`；未读取或改动任何远端任务、私钥或运行进程 |
| `LaunchpadSafetyGate` 与 `UniswapX` | Doppler 衰减中正确返回 `DEFER_UNTIL_DECAY`，衰减后 `APPROVED`，Clanker 软跑路 `REJECTED`；Dutch V3 影子评估通过（`86 bps` 净差，省 `5 bps` AMM 池费） | 合成场景校验；真实链上发射台与池子流动性需各自固定块采证 |

## 4. 全仓库质量与安全门禁记录（2026-11-18 终验）

| 检查门禁 | 命令 | 实测结果 |
| --- | --- | --- |
| Rust 质量与测试 | `make rust-check` | 退出 `0`：`cargo fmt --check`、`cargo clippy -- -D warnings` 0 警告、`cargo test --offline` **15 passed, 0 failed** |
| 依赖、SAST 与密钥门禁 | `make security-check` | 退出 `0`：`pip_audit --require-hashes` 无已知漏洞；`bandit -r src scripts -ll` 0 Medium / 0 High（扫描 96,649 行）；`scripts.check_secrets` 通过 |
| Solidity 静态安全分析 | `make contract-security-check` | 退出 `0`：Slither 分析 **92 个合约、102 个检测器，0 项告警**（`--fail-low`） |
| 全量测试与编译检查 | `make check` | 退出 `0`：`ruff check`、`ruff format --check`（829 文件）、`mypy src`（337 文件）、`pytest`（**6,288 passed, 138 subtests passed**）、前端 9 模块语法检查、`forge fmt --check`、`forge build`、`FOUNDRY_PROFILE=ci forge test`（**99 passed, 0 failed, 0 skipped**，13 suites）、`forge lint` 全通过 |

## 5. 尚未完成及后续准入边界

- **本地内存图实时链上同步器**：当前 Rust `PoolGraph` 已具备多 DEX 精确数学与闭环求解能力，下一步需将 Base `eth_subscribe("logs")` 与 `V3`/`Slipstream`/`V4` 初始化同步（`Sync`/`Swap`/`Mint`/`Burn` 增量更新）接入内存图进行连续 24 小时影子收益归因。
- **独立双源验证与发布审批**：[OasisAtomicExecutor.sol](../../contracts/src/OasisAtomicExecutor.sol) 与 `UniswapX V3DutchOrderReactor` 尚未做 Base 主网分叉集成回放或实盘部署；所有策略 `execution_eligible` 仍为 `false`，实盘启动仍受 [BASE_BOUNDED_RUN.md](../operations/BASE_BOUNDED_RUN.md) 与 [LIVE_RELEASE.md](../operations/LIVE_RELEASE.md) 独立审查约束。
