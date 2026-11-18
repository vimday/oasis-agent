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

在投入任何资金前，必须在真实 Base 区块、真实 `BaseTime`（`0x4200...0030`）、真实 OP L1 数据费（`GasPriceOracle` `0x4200...000f`）与真实池深下跑出持续为正、扣除全部成本后的样本外下界收益（默认要求 `ticks_completed >= 100` 且 `lcb_net_edge_bps >= 5`）。推荐使用 11 资产扩展生态 + 2-hop WETH 路由 + 9 交易对 Coinbase Taker/Maker-Hedge 的 100-tick `Multicall3` 战役入口（约 203 次 RPC 请求，按 USD 250 单笔对齐宪法上限，避免顶档深度不足导致大额 `BTC-USD` 退化）：

```bash
OASIS_FASTPATH_LIB="$PWD/crates/target/release/liboasis_core.dylib" \
  .venv/bin/python -m scripts.run_multicall_100tick_campaign \
  --mode public-shadow \
  --include-ecosystem-tokens \
  --trade-notional-usdc 250000000 \
  --ticks 100 \
  --interval-ms 800 \
  --max-rpc-calls 350 \
  --out reports/research/<run-id>/shadow-100-ecosystem
```

（同步运行无密钥 Base Aave V3 健康度与可清算仓位扫描时，若本机启用了回环系统代理，请显式置空 `no_proxy='*'` 以保持直连 DNS-SSRF 校验有效：

```bash
no_proxy='*' OASIS_FASTPATH_LIB="$PWD/crates/target/release/liboasis_core.dylib" \
  .venv/bin/python -m oasis_agent.fastpath.lending_reader \
  --lookback-blocks 5000 \
  --chunk-blocks 500 \
  --out reports/research/<run-id>/aave_v3_base_health.json
```

若仅需单独测算 4 池 WETH/USDC 基准，亦可运行 `scripts.run_base_arb_shadow --mode public-shadow --ticks 100 --interval-ms 1200 --max-requests 1550`。）

说明：2026-11-18 的 100-tick 11 资产 Base 扩展 `public-shadow`（43 池 `1,204` 环路 + 9 交易对 @ USD 250）实测在 Tick 54（`BTC-USD`）首次获得正净边际 `+6.386260 bps`，但 Taker 总体仍为 `lcb_net_edge_bps = -3`；Passive Maker 在 `CBETH-USD`（最高 `+129.33 bps`）、`DEGEN-USD`、`VIRTUAL-USD`、`VVV-USD` 等生态币上挂出 236 条正边际报价腿（`strict_cross_fills = 0`，诊断 `touch_lift_fills = 75`、均值 `+17.54 bps`）。当 `lcb_net_edge_bps < 5` 时**系统必须拒绝发布**，需通过 WebSocket 成交流排队位置真实验证样本外 Maker/Lending 清算或 `UniswapX Dutch_V3` 净期望为正。

## 阶段 3：双源 RPC 仲裁、确定性部署与隔离签名就绪

参照 [custody 模型](../security/CUSTODY_AND_AGENT_KEY_MODEL.md) 与 [Base 有界运行手册](BASE_BOUNDED_RUN.md) 完成：

1. 首先通过无密钥双源仲裁脚本实证主/副 Base RPC 对同一区块 `block_hash`、`stateRoot`、`BaseTime` 与核心代码哈希一致，并运行离线 6 服务 Compose 安全边界检查：

```bash
no_proxy='*' .venv/bin/python -m oasis_agent.live.dual_rpc_probe \
  --out reports/research/<run-id>/dual_rpc_quorum.json && \
.venv/bin/python -m scripts.verify_profit_compose \
  --out reports/research/<run-id>/profit_compose_verification.json
```

2. 在获得实盘发布许可后，由多签/外部硬件签名器使用 [DeployOasisProfitEngine.s.sol](../../contracts/script/DeployOasisProfitEngine.s.sol) 通过 `CREATE2` 部署并校验 `OasisAtomicExecutor` 与 `OasisIntentVault` 运行时代码哈希（强制 `deployer` / `owner` / `guardian` / `operator` / `quoteSigner` / `treasury` / `revenueSink` 七角色两两严格分离，脚本绝不读取私钥）。
3. 配置两条物理/运营独立的 Base RPC（主读源 + 独立归档/状态模拟与收据对账副源），各自保留独立持久化请求预算账本（失败/429 立即停机不换账本重试）。
4. 使用 [compose.yaml](../../deploy/profit-engine/compose.yaml) 与独立 Unix socket 签名权威（[atomic_authority_socket.py](../../src/oasis_agent/live/atomic_authority_socket.py)、[run_profit_authority.py](../../scripts/run_profit_authority.py)）分离策略进程与 `/run/oasis-keys:ro` 只读密钥卷，并在只读状态模式下生成第 8 门禁证明（`scripts.run_profit_authority --status-only --out-proof ...`），`REVENUE_SINK` 利润自动隔离。
5. 明确设置本次有界资本授权：默认不超出 USD 500 总资本、USD 200 单日止损、USD 500 累计止损及 STOP 急停文件。

## 阶段 4：9 项硬门禁总验与有界发布准入

汇总合约产物、Anvil 报告、`BaseTime` 核验、>= 100 tick 公开影子摘要、双源仲裁证明、隔离签名器证明及 owner 本次书面范围授权：

```bash
.venv/bin/python -m scripts.check_profit_engine_readiness \
  --anvil-report reports/research/<run-id>/anvil_e2e_report.json \
  --radar-report reports/research/<radar-id>/radar_report.json \
  --shadow-summary reports/research/<run-id>/shadow-100/shadow_summary.json \
  --infra-proof reports/research/<run-id>/dual_rpc_quorum.json \
  --operator-signoff <private-owner-signoff.json> \
  --require-ready \
  --out reports/research/<run-id>/readiness_report.json
```

仅当退出码为 `0` 且 `release_permitted == true` 时，才允许进入单笔有界 canary 演练；任何残差、未对账收据、双源分歧或熔断触发必须立即落盘停止信号并人工复核。
