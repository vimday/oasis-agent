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

在投入任何资金前，必须在真实 Base 区块、真实 `BaseTime`（`0x4200...0030`）、真实 OP L1 数据费（`GasPriceOracle` `0x4200...000f`）与真实池深下跑出持续为正、扣除全部成本后的样本外下界收益（默认要求 `ticks_completed >= 100` 且 `lcb_net_edge_bps >= 5`）。推荐使用带防幻觉余额上限、chain-on `QuoterV2` 复验及 Coinbase 跨所对比的 100-tick `Multicall3` 战役入口（仅约 203 次 RPC 请求）：

```bash
OASIS_FASTPATH_LIB="$PWD/crates/target/release/liboasis_core.dylib" \
  .venv/bin/python -m scripts.run_multicall_100tick_campaign \
  --mode public-shadow \
  --ticks 100 \
  --interval-ms 800 \
  --max-rpc-calls 350 \
  --out reports/research/<run-id>/shadow-100
```

（若仅单独测算 4 池 WETH/USDC 基准，亦可运行 `scripts.run_base_arb_shadow --mode public-shadow --ticks 100 --interval-ms 1200 --max-requests 1550`。）

说明：2026-11-18 的 100-tick Base 主网 `Multicall3`（25 座深池 + Coinbase）实测测得原子最佳毛差 `-1.40 bps`、Coinbase `ETH-USD` ↔ Base 跨所峰值毛差 `+2.78 bps`（0 bps Maker 诊断下 `14/96` 次为正 `+1.15 bps`，但按 `3.5 bps` Taker 计算 `lcb_net_edge_bps = -3`）。当 `lcb_net_edge_bps < 5` 时**系统必须拒绝发布**，应转向极低费率 CEX Maker 对冲、增量日志同步（`state_sync.py`）、闪电贷清算（`liquidation.py`）或 `UniswapX Dutch_V3` 零池费意图流，直到在 untouched 窗口上证明正样本外净期望。

## 阶段 3：双源 RPC 仲裁、确定性部署与隔离签名就绪

参照 [custody 模型](../security/CUSTODY_AND_AGENT_KEY_MODEL.md) 与 [Base 有界运行手册](BASE_BOUNDED_RUN.md) 完成：

1. 首先通过无密钥双源仲裁脚本实证主/副 Base RPC 对同一区块 `block_hash`、`stateRoot`、`BaseTime` 与核心代码哈希一致：

```bash
.venv/bin/python -m oasis_agent.live.dual_rpc_probe \
  --out reports/research/<run-id>/dual_rpc_quorum.json
```

2. 在获得实盘发布许可后，由多签/外部硬件签名器使用 [DeployOasisProfitEngine.s.sol](../../contracts/script/DeployOasisProfitEngine.s.sol) 通过 `CREATE2` 部署并校验 `OasisAtomicExecutor` 与 `OasisIntentVault` 运行时代码哈希（强制 `deployer` / `owner` / `guardian` / `operator` / `quoteSigner` / `treasury` / `revenueSink` 七角色两两严格分离，脚本绝不读取私钥）。
3. 配置两条物理/运营独立的 Base RPC（主读源 + 独立归档/状态模拟与收据对账副源），各自保留独立持久化请求预算账本（失败/429 立即停机不换账本重试）。
4. 配置隔离策略进程与独立 Unix socket 签名权威（[atomic_authority.py](../../src/oasis_agent/live/atomic_authority.py)），策略进程零私钥接触，`REVENUE_SINK` 利润自动隔离。
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
