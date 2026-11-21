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
  --timeout-s 25.0 \
  --stop-gracefully-on-rpc-error \
  --ticks 500 \
  --interval-ms 1500 \
  --max-rpc-calls 1500 \
  --out reports/research/<run-id>/shadow-500-final
```

可选精细过滤与韧性参数：`--include-aero-cl2`（将 Base 活跃发现池扩展至 Aero Slipstream 2 `CLFactory`）、`--cex-venue coinbase|bybit`（切换 Coinbase 或 Bybit Spot V5 实时订单簿与 WebSocket 成交，Bybit 路径自动拉取实时 Coinbase `USDT-USD` 锚定归一化）、`--allowed-products`（逗号分隔交易对白名单）、`--min-taker-edge-bps`（`dynamic-sizing` 默认 `6` 且开启 CEX Limit IOC 优先模式）、`--maker-min-edge-bps`（`dynamic-sizing` 默认 `6.5` 配合 `1.5x` 波动倍增、按产品 lot 计时、队列顺位保留、成本感知 CEX 粉尘平仓、`post_fill_cooldown_steps=5`、`max_same_dir_momentum_bps=8.0` 及 `$25` / `60`-step 批量对冲）、`--max-book-workers`（默认 `9` 并发 L2 预拉取）、`--timeout-s`（单请求 HTTPS/WS 超时，默认 `15.0s`）与 `--stop-gracefully-on-rpc-error`（首 tick 后遇公共 RPC 错误不重试、即停机并落盘已完成区块哈希链与汇总）。

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

说明：2026-11-18/19 Wave 14–20 实测证明（`--dynamic-sizing`、`configs/profit-engine-risk.example.json`）：
- Wave 14（`100` 唯一区块 `59099892..59104859`、`18,283` WS 成交）：`12` 笔真实 OOS 成交 **`10/12` 正（83.33%）**、净赚 **`+$0.232804`**、均值 **`+9.541982 bps`**，因正尾方差致高斯 95% LCB 为 `-4 bps`；
- Wave 16（`300` 唯一区块 `59133003..59145017`、`59,811` WS 成交）：核心深对 **`AERO-USD` 双腿稳定正盈利（`21` 笔净赚 `+$0.210795`）**，尾币拉低全局 `oos_lcb = -4 bps`；
- Wave 18（`300` 唯一区块 `59157950..59160987`、`21,358` WS 成交、`9` 并发 L2 预拉取 0 限流）：单 tick 速度提升至 **`2.040 秒/tick`（`612.08s` 跑完 `300` 块）**，`10 bps` 做市过滤 + Limit IOC 优先模式实现 **`0` 亏损 / `0` 毒穿价（`strict_cross=0`、`drawdown=$0`）**，唯一成交 `DEGEN-USD` 净赚 **`+$0.023090`（`+11.586679 bps`、`100%` 胜率）**；
- Wave 20（四轮合计 **`2,942` 唯一区块**，定位并修复 Rust/Python `TickMath` `slot0` 闭上界及 HTTP/1.1 Keep-Alive 200 请求轮换）：前三轮达 **`9/10` 正盈利（`+$0.2471`、`max_drawdown <= $0.0022`）**，其中第三轮连续无断流跑至 **`1,045` tick（`56,090` WS 成交 0 断流）**；终验收口 `shadow-500-final`（`500 / 500` 块 `59213388..59217254`、`1,003` RPC、`25,184` WS 成交 `0` 断流）因低波凌晨窗口仅 1 笔 `$0.99` 超时批冲平单（`-$0.003682`），预注册 Gate 5 如实阻断（`oos_trades_count = 1 < 5`），**系统严格锁定 `release_permitted = false` 拒绝发布**。

## 阶段 3：双源 RPC 仲裁、双平面软件 EOA 部署与隔离签名就绪

参照 [custody 模型](../security/CUSTODY_AND_AGENT_KEY_MODEL.md)（支持极简 **2 个标准软件 EOA**，无需硬件钱包或多签）与 [Base 有界运行手册](BASE_BOUNDED_RUN.md) 完成：

1. 首先通过无密钥双源仲裁脚本实证主/副 Base RPC 对同一区块 `block_hash`、`stateRoot`、`BaseTime` 与核心代码哈希一致，并运行离线 6 服务 Compose 策略校验、双配置无密钥签名演练及真实 Docker 硬化隔离演练（已在 `aleph` WSL Linux Docker 实测产出真实 Gate 8 证明 `passed=true`）：

```bash
.venv/bin/python -m oasis_agent.live.dual_rpc_probe \
  --out reports/research/<run-id>/dual_rpc_quorum.json && \
.venv/bin/python -m scripts.verify_profit_compose \
  --compose-file deploy/profit-engine/compose.yaml \
  --out reports/research/<run-id>/profit_compose_verification.json && \
.venv/bin/python -m scripts.verify_profit_authority_rehearsal \
  --out reports/research/<run-id>/authority_rehearsal.json && \
.venv/bin/python -m scripts.verify_profit_docker_confinement \
  --out reports/research/<run-id>/docker_confinement_report.json \
  --out-signer-proof reports/research/<run-id>/authority_status_proof.json
```

2. 在获得实盘发布许可后，个人运营者仅需准备 **2 个加密软件钱包**：**Cold Admin EOA**（通过 `cast wallet import --interactive` 存放于本地加密 keystore，兼任 `deployer`/`owner`/`guardian`/`treasury`/`revenueSink`）与 **Hot Canary EOA**（私钥仅隔离存放在独立 Docker `oasis-canary-keys` 卷中，兼任 `operator`/`quoteSigner`），使用 [DeployOasisProfitEngine.s.sol](../../contracts/script/DeployOasisProfitEngine.s.sol) 通过核心环境变量（`OASIS_CHAIN_ID`、`OASIS_PROFIT_OWNER`、`OASIS_PROFIT_OPERATOR`、`OASIS_EXECUTOR_SALT`、`OASIS_VAULT_SALT`）配合 **`OASIS_ALLOW_SOLO_ROLE_COLLAPSE=true`**（严格默认要求全部冷角色互异且 `operator != quoteSigner`，个人双 EOA 模式必须显式开启该开关，否则触发 `RolesNotSeparated()`）及 `OASIS_WIRE_OPERATORS_ON_DEPLOY=true`（`forge script ... --account <admin-keystore> --broadcast`）原子完成 `OasisAtomicExecutor` 与 `OasisIntentVault` 的 `CREATE2` 部署与操作员授权接线（脚本强制校验冷/热双平面隔离，且绝不读取裸私钥）。
3. 配置两条物理/运营独立的 Base RPC（主读源 + 独立归档/状态模拟与收据对账副源），各自保留独立持久化请求预算账本（失败/429 立即停机不换账本重试）。
4. 使用 [compose.yaml](../../deploy/profit-engine/compose.yaml) 与独立 Unix socket 签名权威（[atomic_authority_socket.py](../../src/oasis_agent/live/atomic_authority_socket.py)、[run_profit_authority.py](../../scripts/run_profit_authority.py)）以及无密钥策略桥接守护进程（[profit_strategy_daemon.py](../../src/oasis_agent/live/profit_strategy_daemon.py)、[run_profit_strategy.py](../../scripts/run_profit_strategy.py)）彻底分离策略进程与 `/secrets:ro` 只读密钥卷（`profit-strategy` 预设 `--mode shadow`；若切换为 `--mode live`，必须同时显式传入 `--policy-config <path>` 与已通过 9/9 门禁并经 `report_sha256` 核验的 `--readiness-report reports/research/<run-id>/readiness_report.json`，支持通过只读控制台 `GET /api/v1/radar/capacity` 观测风控额度与样本外容量，并在只读状态模式下生成第 8 门禁证明 `scripts.run_profit_authority --status-only --out-proof ...`），`REVENUE_SINK` 利润自动隔离。
5. 通过 [profit-engine-risk.example.json](../../configs/profit-engine-risk.example.json)（经 owner 当次授权范围一致签署的配置文件）显式设定本金总额、单日亏损上限、累积亏损上限、最小手数与尾部压力系数（默认基线仍为 USD 500 / 200 / 500），并保留 STOP 急停文件；最后由无密钥 [create_software_scope_signoff.py](../../scripts/create_software_scope_signoff.py) 生成并校验范围声明清单（演练使用 `--rehearsal`；实盘使用 `--approve-software-scope --attestation ...` 并严格绑定 `admin_eoa != operator_eoa`）。

## 阶段 4：9 项硬门禁总验与有界发布准入

汇总合约产物、Anvil 报告、`BaseTime` 核验、>= 100 tick 公开影子摘要（含 `--min-oos-trades 5` 样本外双模验收）、双源仲裁证明（`--infra-proof`）、Docker 隔离签名权威证明（`--signer-proof`）、风险策略配置与无密钥范围授权清单（`--operator-signoff`）：

```bash
.venv/bin/python -m scripts.check_profit_engine_readiness \
  --risk-config configs/profit-engine-risk.example.json \
  --min-oos-trades 5 \
  --anvil-report reports/research/<run-id>/anvil_e2e_report.json \
  --radar-report reports/research/<radar-id>/radar_report.json \
  --shadow-summary reports/research/<run-id>/shadow-300-adaptive-oos-v2/shadow_summary.json \
  --infra-proof reports/research/<run-id>/dual_rpc_quorum.json \
  --signer-proof reports/research/<run-id>/authority_status_proof.json \
  --operator-signoff <software-scope-signoff.json> \
  --require-ready \
  --out reports/research/<run-id>/readiness_report.json
```

仅当退出码为 `0` 且 `release_permitted == true` 时，才允许进入单笔有界 canary 演练；任何残差、未对账收据、双源分歧或熔断触发必须立即落盘停止信号并人工复核。
