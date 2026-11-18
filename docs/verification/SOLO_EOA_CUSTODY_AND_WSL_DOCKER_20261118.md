# Wave 15–16 双平面软件 EOA 托管、`aleph` WSL 真实 Linux Docker 隔离演练与 300-Tick 样本外实测核验（2026-11-18）

2026-11-18（Asia/Shanghai）；在 `9f555d3`（Wave 14 已推送基线）之上通过并行合入 Wave 15（分支 `15a209c`、`05b1144` -> 合入提交）并完成 Wave 16 跨机与长窗口实测：**取消“硬件钱包 + 多签 + 7 个两两独立钱包”的过度安全前提，正式落地仅需 2 个标准软件 EOA 的双平面托管模型（Cold Admin EOA vs Hot Canary EOA）**、**在同网 `aleph` WSL 真实 Linux Docker 29.1.3 引擎下首次拉起生产只读无特权容器产出真实 Gate 8 签名权威证明**，并在本地完成 **300-Tick 连续 Base 主网 + Live Coinbase WebSocket（59,811 笔成交零断流）样本外长窗实测**（9 门禁总验达成 **`7 / 9`**，仅余 `positive_cost_adjusted_lcb_edge` 与人类显式 `--approve-software-scope` 范围签字）。

全程严格保持无真实私钥落盘或入库、`execution_eligible = false`、`release_permitted = false`、无真实链上资金广播或付费 RPC 消耗。本文档衔接并保留先前记录（[ADAPTIVE_OOS_STRATEGY_AND_AUTHORITY_BRIDGE_20261118.md](ADAPTIVE_OOS_STRATEGY_AND_AUTHORITY_BRIDGE_20261118.md)、[CUSTODY 模型](../security/CUSTODY_AND_AGENT_KEY_MODEL.md) 与 [准实盘操作手册](../operations/PROFIT_ENGINE_PRELIVE_RUNBOOK.md)）。

## 1. Wave 15–16 核心架构与工程交付

- **双平面软件 EOA 合约部署重构（`15a209c`，[DeployOasisProfitEngine.s.sol](../../contracts/script/DeployOasisProfitEngine.s.sol)、[DeployOasisContracts.s.sol](../../contracts/script/DeployOasisContracts.s.sol)、[DeployOasisProfitEngine.t.sol](../../contracts/test/DeployOasisProfitEngine.t.sol)）**：
  - 移除 `DeployOasisProfitEngine.s.sol` 与 `DeployOasisContracts.s.sol` 中要求硬件钱包/多签与 7 角色两两全异的硬性限制，改为 Foundry 加密软件 keystore（`cast wallet new` / `--account`）兼容的 **Cold Admin 平面（`deployer == owner == guardian == treasury == revenueSink` 允许复用同 1 个外部软件 EOA）** 与 **Hot Canary 平面（`operator == quoteSigner` 允许复用同 1 个 Docker 隔离 EOA）**。
  - `_validateParams` 仅当热平面 `{operator, quoteSigner}` 与冷平面 `{deployer, owner, guardian, treasury, revenueSink}` 发生交叉碰撞（`2 × 5 = 10` 种越权组合）时抛出 `RolesNotSeparated()`；`_loadEnvParams` 仅强制要求 `OASIS_CHAIN_ID`、`OASIS_PROFIT_OWNER`、`OASIS_PROFIT_OPERATOR`、`OASIS_EXECUTOR_SALT`、`OASIS_VAULT_SALT` 5 个环境变量，其余角色通过 `vm.envOr` 自动默认归口；新增 `deployAndWireSoloOperator(...)` 与 `OASIS_WIRE_OPERATORS_ON_DEPLOY=true` 实现部署+`setOperator`/`setFillerOperator`/`rotateQuoteSigner` 单脚本原子完成（非 `deployer == owner` 触发 `DeployerMustBeOwnerToWire()`）。
  - Foundry [DeployOasisProfitEngine.t.sol](../../contracts/test/DeployOasisProfitEngine.t.sol) `11 / 11` 通过（覆盖全部 `10` 种跨平面冲突拒绝、2-EOA 共用测试与 `OASIS_WIRE_OPERATORS_ON_DEPLOY=true` 5 环境变量部署接线），`forge lint` 与 `slither` 零阻断告警。
- **多源联合门禁与无密钥软件签章工具（`15a209c`，[canary_readiness.py](../../src/oasis_agent/live/canary_readiness.py)、[check_profit_engine_readiness.py](../../scripts/check_profit_engine_readiness.py)、[create_software_scope_signoff.py](../../scripts/create_software_scope_signoff.py)、[单测](../../tests/unit/test_profit_engine_readiness_and_anvil.py)）**：
  - `check_profit_engine_readiness.py` 新增 `--signer-proof` 参数支持同时核验 Gate 7（`--infra-proof dual_rpc_quorum.json`）与 Gate 8（`--signer-proof authority_status_proof.json`），并对 `oasis-profit-authority-infra.v1` / `oasis.profit-authority-infra-proof.v1` 强制校验规范 `proof_sha256` 完整性。
  - 新增无密钥 [create_software_scope_signoff.py](../../scripts/create_software_scope_signoff.py) 生成/验证 `oasis.software-scope-signoff.v1`（绑定 `CapitalRiskConfig` `config_sha256`、强制校验 `admin_eoa != operator_eoa`、关联全部证据文件 SHA-256）：支持 `--rehearsal`（`rehearsal_only: true`, `owner_scope_approved: false`）与 `--approve-software-scope --attestation ...`，Gate 9 显式拒绝任何 `rehearsal_only=true` 或篡改样本开放实盘权限。
- **真实 Linux Docker 硬化演练工具链与 `/trades` 回退修复（`05b1144` + Wave 16，[Dockerfile](../../deploy/profit-engine/Dockerfile)、[Dockerfile.dockerignore](../../deploy/profit-engine/Dockerfile.dockerignore)、[verify_profit_docker_confinement.py](../../scripts/verify_profit_docker_confinement.py)、[run_multicall_100tick_campaign.py](../../scripts/run_multicall_100tick_campaign.py)、[Socket/Compose 单测](../../tests/unit/test_profit_authority_socket_and_compose.py)、[联调单测](../../tests/unit/test_dual_rpc_and_cex_dex_shadow.py)）**：
  - 更新 [Dockerfile](../../deploy/profit-engine/Dockerfile) 为 `FROM rust:1-slim-bookworm AS rust-builder` 并内置 `/app/configs/profit-engine-risk.example.json`，配齐白名单式 [Dockerfile.dockerignore](../../deploy/profit-engine/Dockerfile.dockerignore) 隔离密钥与研发工件。
  - 新增 [verify_profit_docker_confinement.py](../../scripts/verify_profit_docker_confinement.py)：全自动使用临时随机后缀命名卷在离线 `--network none`、`--read-only`、`--user 10001:10001`、`--cap-drop ALL`、`--security-opt no-new-privileges:true` 容器中拉起 `profit-authority`、`profit-authority-status`（无密钥卷提取 Gate 8 证明）与 `profit-strategy --mode shadow --once`（校验 `/secrets` 不存在且可读取可用风控额度），并在 `finally` 块中无条件销毁所有演练容器与临时卷。
  - 修复在真实 WSL 容器实跑与超长 300-tick 战役中断流演练发现的 `_KeepAliveHttpsPool._request` 缺陷：当 Coinbase WebSocket 断流触发 `/products/<id>/trades` REST 回退时允许通过 `_validate_coinbase_trades_url` 校验并捕获 `LiveCexDexError`，同时保持严格禁止 `/candles` 等未授权路径。

## 2. Wave 16 实测结果（归档于 `reports/research/wave16-20261118/`）

### 2.1 `aleph` WSL 真实 Linux Docker 29.1.3 隔离演练与容器内主网实跑

因本机 macOS Docker Desktop 未开启且 WSL 宿主机无法直连海外 `registry-1.docker.io` 拉取 Docker Hub 镜像，在同网络 WSL 主机 `aleph` 上使用 `cargo --locked` 编译原生 Linux `x86_64` `liboasis_core.so`、`uv` 安装可移植 CPython 3.12 与 `--require-hashes -r requirements.lock`，直接基于 WSL 本地已缓存的 Debian Bookworm 镜像组装生产等价容器并完成实跑检验：

- **硬化容器边界与 Gate 8 真实证明（`docker_confinement_report.json`、`wsl_authority_status_proof.json`）**：
  - `passed = true`、`execution_eligible = false`、`rehearsal_cleaned = true`，自哈希 `1ff523f44cacf37e73df8391a26b639eb495d9793dcbc3f4e0c7666d77b919ea`。
  - 容器实测负面检查全通：`uid=10001`、`gid=10001`、`cap_eff_zero=true`、`no_new_privs=true`、`rootfs_readonly=true`、`secrets_absent=true`、`authority_state_absent=true`、`risk_config_bind_readonly=true`、`image_risk_config_present=true`。
  - 无密钥 `profit-strategy` 经共享 `/run/oasis/profit_authority.sock` 从 `profit-authority` 读取策略状态（`authority_reachable=true`, `can_deploy=true`, `deployable_notional_usd_micros=225000000`, `signatures_used=0`），并生成有效 Gate 8 证明（`proof_sha256 = 5e461e7be403fd2b90ab490dad2ec95247965814e635f455103ce8eb3dfe21bf`）。
  - 容器与临时 `/tmp`、命名卷清零验证：`docker ps -a --filter name=oasis-rehearsal` 与 `docker volume ls --filter name=oasis-rehearsal` 均为空。
- **硬化 Linux 容器内 5-Tick 主网实跑（`wsl_container_smoke5_summary.json`）**：
  - 容器内加载 `liboasis_core.so` 完成 `ticks_completed = 5`、`unique_blocks_observed = 5`、`universe_pool_count = 44`、`rpc_calls_used = 13`、`unresolved_residuals = 0`（Base RPC 全通；WSL 容器默认桥接下 Coinbase 触发 REST `/trades` 回退 `45` 次后由修复路径优雅降级无异常崩溃）。

### 2.2 本地前置证明与 300-Tick Base 主网 + Live Coinbase WS 长窗实测

- **前置证明**：
  - `profit_compose_verification.json`：`passed = true`（`compose_hash = b3821a8f...`）
  - `authority_rehearsal.json`：`passed = true`（`report_sha256 = 141b5fe7...`）
  - `anvil_e2e_report.json`：回环 Anvil `4/4` 通过（`early_abort = 58,204 gas`，`vault_rotate_signer = 36,524 gas`，`0` 授权残差，`report_sha256 = b1b95a21...`）
  - `dual_rpc_quorum.json`：`publicnode_base` 与 `base_official_mainnet` 于块 **`59126536`** 一致（`report_sha256 = 870125f2...`）
  - `radar/radar_report.json`：块 **`59126557`** BaseTime 交叉验证通过（`report_sha256 = fc87f5af...`）
- **历史失败保留（`shadow-300-adaptive-oos/`）**：修复前进程跑至 `142` tick 时因 WS 瞬连触发 `/trades` REST 回退遭遇旧 `_validate_coinbase_url` 路径拦截退出，原因已定位并在修复后独立全量重跑。
- **修复后完整 300-Tick 连续主网实测（`shadow-300-adaptive-oos-v2/`）**：
  - 连续覆盖 Base 主网区块 **`59133003..59145017`**（**`300` 个唯一区块**、`44` 座池、`603 / 900` RPC 预算），Coinbase WebSocket `matches` 全 9 对 **`300 / 300` tick 零断流、零重启、零 REST 回退**，实时处理 **`59,811`** 笔真实成交（`BTC-USD: 53,288`、`ETH-USD: 4,832`、`AERO-USD: 911`、`VVV-USD: 683`、`MORPHO-USD: 63`、`VIRTUAL-USD: 17`、`CBETH-USD: 11`、`WELL-USD: 5`、`DEGEN-USD: 1`）；链尾 `64f66636...`，汇总自哈希 **`f7e58dbff330db5c86c44ac22f4c101fdfcc35de22ddb0bd601ef455b728eda8`**。
  - **2-Tick 样本外与跨周期分化实证（`oos_execution_summary`，`0` 悬空残差）**：
    - 全 9 资产不加筛选汇总：`33` 笔真 `t -> t+1` 样本外成交（Taker `22` 笔 + Maker `11` 笔，2-tick 过滤拒绝 `35` 次单帧脉冲），`13 / 33` 正（胜率 `39.39%`）、总净额 `-$0.761626`、均值 `-1.307302 bps`、`std = 7.665633 bps`、`oos_lcb_net_edge_bps = -4`。
    - **逐品种样本外结构性发现**：**`AERO-USD` 在约 40 分钟长窗内双腿均实现样本外正净盈利**——Taker `15` 笔 `8` 正（均值 `+1.36 bps`，净赚 **`+$0.057705`**）、Maker `6` 笔 `3` 正（均值 `+1.14 bps`，净赚 **`+$0.153090`**），合并 `AERO-USD` 共 **`21` 笔净赚 `+$0.210795`**；总组合微亏全部集中于高波/薄盘尾币在 `t -> t+1` 等待确认期间的价格回撤（`VIRTUAL-USD -$0.405631`、`DEGEN-USD -$0.328234`、`VVV-USD -$0.160718`、`WELL-USD -$0.077838`）。
    - **策略演进含义**：该结果在完全未动预注册门槛下，实证出下一阶段样本外转正的关键并非盲目调低风控阈值，而是 **限制可交易高流动核心币对白名单（优先聚焦已证明双腿正期待的 `AERO-USD` 等深盘主力对，屏蔽长尾滑点/均值回归拖累）+ 缩短块间确认延迟**。

### 2.3 9 门禁联合总验（`readiness_report.json`：`7 / 9` 通过）

结合 `--risk-config configs/profit-engine-risk.example.json`、`dual_rpc_quorum.json`（Gate 7）、`aleph` WSL 真实 Docker `wsl_authority_status_proof.json`（Gate 8）与 `--rehearsal` 模式 `rehearsal_software_signoff.json` 产出 **`7 / 9` 通过**（`report_sha256 = 939e6f291d98ce7b1545239f8c9527670af4b1e1c38d1cfbdece00c52299c789`，`release_permitted = false`，`execution_eligible = false`）：

- **已通过 7 项**：`compiled_contracts_pinned`、`anvil_e2e_settlement_reconciled`（`58,204 gas`）、`base_time_predeploy_verified`（块 `59126557`）、`untouched_shadow_sample_size`（`300`）、`revert_budget_healthy`（`unresolved_residuals=0`）、`dual_rpc_quorum_verified`（块 `59126536`）、`isolated_signer_authority_ready`（`aleph` WSL 真实 Docker `/run/oasis/profit_authority.sock` 证明）。
- **剩余阻断 2 项**：`positive_cost_adjusted_lcb_edge`（全 9 对 300-tick 未筛选池 LCB `-4 < 5`；下一步聚焦核心白名单交易对优化后重测）、`owner_scope_release_manifest_present`（演练样本明确标记 `rehearsal_only=true`，待 Gate 5 通过后由运营者以真实 Cold Admin vs Hot Canary EOA 执行 `create_software_scope_signoff.py --approve-software-scope` 签署）。
