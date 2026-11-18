# Oasis 盈利引擎准实盘操作手册（Pre-Live Runway）

本手册定义从已完成工程基线（`OasisAtomicExecutor.sol` + `OasisIntentVault.sol` + Rust `oasis-core` + `atomic_authority.py` + `live_shadow_runner.py`）进入授权有界 Base Canary 实盘前必须依次通过的 **4 个零跳跃阶段**。每一阶段默认 `execution_eligible = false`；未通过前序全部门禁前，[check_profit_engine_readiness.py](../../scripts/check_profit_engine_readiness.py) 自动锁定 `release_permitted = false`。

## 阶段 1：Anvil 真实字节码端到端结算复核（零成本本地隔离）

在本地回环节点验证合约部署、EIP-1559 签名编码、`<65k` gas 早期中止、利润全额清扫至 `REVENUE_SINK`、`ERC-1271` 域隔离 Epoch 作废及 `IReactorCallback` 零残留授权：

```bash
OASIS_FASTPATH_LIB="$PWD/crates/target/release/liboasis_core.dylib" \
  .venv/bin/python -m scripts.verify_atomic_vault_anvil \
  --out reports/research/<run-id>/anvil_e2e_report.json
```

仅当地址为 `http://127.0.0.1:...` 且 `web3_clientVersion` 确认为 `anvil` 时脚本才会在内存中生成临时测试私钥；严禁将真实私钥传入此脚本。

## 阶段 2：Base 主网无密钥长期实时影子归因（>= 100 样本 & 正样本外净收益）

在投入任何资金前，必须在真实 Base 区块、真实 `BaseTime`（`0x4200...0030`）、真实 OP L1 数据费（`GasPriceOracle` `0x4200...000f`）与真实池深下跑出持续为正、扣除全部成本后的样本外下界收益（默认要求 `ticks_completed >= 100` 且 `lcb_net_edge_bps >= 5`）。推荐使用 11 资产扩展生态 + 2-hop WETH 路由 + 可配置本金/日亏策略 + 动态容量寻优 + 实时 Coinbase WebSocket `matches` FIFO 做市的 100-tick `Multicall3` 战役入口（约 203 次 RPC 请求；可通过 `--risk-config` 或 `--total-capital-usd` / `--max-daily-loss-usd` 动态调节本金和止损额度，无需修改代码）：

```bash
OASIS_FASTPATH_LIB="$PWD/crates/target/release/liboasis_core.dylib" \
  .venv/bin/python -m scripts.run_multicall_100tick_campaign \
  --mode public-shadow \
  --include-ecosystem-tokens \
  --risk-config configs/profit-engine-risk.example.json \
  --dynamic-sizing \
  --enable-coinbase-ws \
  --ticks 100 \
  --interval-ms 800 \
  --max-rpc-calls 350 \
  --out reports/research/<run-id>/shadow-100-dynamic-ws
```

同步运行无密钥 Base Aave V3 健康度与按剩余额度截断清算扫描时（底层直连传输已显式注入 `ProxyHandler({})` 绕过本地系统代理误路由）：

```bash
OASIS_FASTPATH_LIB="$PWD/crates/target/release/liboasis_core.dylib" \
  .venv/bin/python -m oasis_agent.fastpath.lending_reader \
  --lookback-blocks 5000 \
  --chunk-blocks 500 \
  --timeout-s 25 \
  --out reports/research/<run-id>/aave_v3_base_health.json
```

（若仅需固定 4 池 WETH/USDC 基准，亦可运行 `scripts.run_base_arb_shadow --mode public-shadow --ticks 100 --interval-ms 1200 --max-requests 1550`。）

说明：2026-11-18 Wave 14 实测（11 资产 44 池 + 9 对实时 WebSocket `18,283` 笔成交、`--dynamic-sizing` 开启波动率自适应 `effective_adverse_bps` + `$10` 批量对冲 + `t -> t+1` 2-tick 样本外执行追踪，`configs/profit-engine-risk.example.json`）在 `100` 个唯一 Base 区块（`59099892..59104859`）实现 **`12` 笔真实样本外成交、`10/12` 正（胜率 `83.33%`）、总净赚 `+$0.232804`、均值 `+9.541982 bps`、最大回撤 `$0.027576`、零残差**（自适应 FIFO `2/2` 全正 `+$0.145091`）；但单笔 `+80.84 bps` 正尾离群拉高样本 `std=22.94 bps` 使高斯 95% LCB 为 `-4 bps`（低于预注册 `+5` 门槛），故**系统仍严格锁定 `release_permitted = false` 拒绝发布**。

## 阶段 3：双源 RPC 仲裁、确定性部署与隔离签名就绪

参照 [custody 模型](../security/CUSTODY_AND_AGENT_KEY_MODEL.md) 与 [Base 有界运行手册](BASE_BOUNDED_RUN.md) 完成：

1. 首先通过无密钥双源仲裁脚本实证主/副 Base RPC 对同一区块 `block_hash`、`stateRoot`、`BaseTime` 与核心代码哈希一致，并运行离线 6 服务 Compose 检查（含 `profit-strategy` 与 `profit_authority.sock` 只读风险配置隔离检查）及双配置（`$500/$200` 与 `$5,000/$750`）无密钥隔离签名演练：

```bash
.venv/bin/python -m oasis_agent.live.dual_rpc_probe \
  --out reports/research/<run-id>/dual_rpc_quorum.json && \
.venv/bin/python -m scripts.verify_profit_compose \
  --compose-file deploy/profit-engine/compose.yaml \
  --out reports/research/<run-id>/profit_compose_verification.json && \
.venv/bin/python -m scripts.verify_profit_authority_rehearsal \
  --out reports/research/<run-id>/authority_rehearsal.json
```

2. 在获得实盘发布许可后，由多签/外部硬件签名器使用 [DeployOasisProfitEngine.s.sol](../../contracts/script/DeployOasisProfitEngine.s.sol) 通过 `CREATE2` 部署并校验 `OasisAtomicExecutor` 与 `OasisIntentVault` 运行时代码哈希（强制 `deployer` / `owner` / `guardian` / `operator` / `quoteSigner` / `treasury` / `revenueSink` 七角色两两严格分离，脚本绝不读取私钥）。
3. 配置两条物理/运营独立的 Base RPC（主读源 + 独立归档/状态模拟与收据对账副源），各自保留独立持久化请求预算账本（失败/429 立即停机不换账本重试）。
4. 使用 [compose.yaml](../../deploy/profit-engine/compose.yaml) 与独立 Unix socket 签名权威（[atomic_authority_socket.py](../../src/oasis_agent/live/atomic_authority_socket.py)、[run_profit_authority.py](../../scripts/run_profit_authority.py)）以及无密钥策略桥接守护进程（[profit_strategy_daemon.py](../../src/oasis_agent/live/profit_strategy_daemon.py)、[run_profit_strategy.py](../../scripts/run_profit_strategy.py)）彻底分离策略进程与 `/run/oasis-keys:ro` 只读密钥卷（`profit-strategy` 预设 `--mode shadow`，支持通过只读控制台 `GET /api/v1/radar/capacity` 观测风控额度与样本外容量，并在只读状态模式下生成第 8 门禁证明 `scripts.run_profit_authority --status-only --out-proof ...`），`REVENUE_SINK` 利润自动隔离。
5. 通过 [profit-engine-risk.example.json](../../configs/profit-engine-risk.example.json)（经 owner 当次授权范围一致签署的配置文件）显式设定本金总额、单日亏损上限、累积亏损上限、最小手数与尾部压力系数（默认基线仍为 USD 500 / 200 / 500），并保留 STOP 急停文件。

## 阶段 4：9 项硬门禁总验与有界发布准入

汇总合约产物、Anvil 报告、`BaseTime` 核验、>= 100 tick 公开影子摘要（含 `--min-oos-trades 5` 样本外双模验收）、双源仲裁证明、隔离签名器证明、风险策略配置与 owner 本次书面范围授权：

```bash
.venv/bin/python -m scripts.check_profit_engine_readiness \
  --risk-config configs/profit-engine-risk.example.json \
  --min-oos-trades 5 \
  --anvil-report reports/research/<run-id>/anvil_e2e_report.json \
  --radar-report reports/research/<radar-id>/radar_report.json \
  --shadow-summary reports/research/<run-id>/shadow-100-adaptive-oos/shadow_summary.json \
  --infra-proof reports/research/<run-id>/dual_rpc_quorum.json \
  --operator-signoff <private-owner-signoff.json> \
  --require-ready \
  --out reports/research/<run-id>/readiness_report.json
```

仅当退出码为 `0` 且 `release_permitted == true` 时，才允许进入单笔有界 canary 演练；任何残差、未对账收据、双源分歧或熔断触发必须立即落盘停止信号并人工复核。
