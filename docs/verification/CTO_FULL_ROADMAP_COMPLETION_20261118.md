# CTO 全路线推进与 P1–P4 终验记录（2026-11-18）

2026-11-18（Asia/Shanghai）；接续基线 `943298f`（Wave 1 + Wave 2），完成 Wave 3 四路并行与 Wave 4 集成、实链只读取证与全仓库门禁验收。所有代码、测试与 `public-shadow` 运行严格保持在离线、仿真与只读影子模式（`execution_eligible = false`），未持有或加载私钥、未签名广播、未改动远端 WSL `aleph` 作业，未触动根目录下已有未跟踪 `oasis-agent-*.txt` 文件及锁定依赖（`pyproject.toml`、`uv.lock`、`requirements*.lock`）。

## 1. 既定规划逐项对齐矩阵

| 规划来源与编号 | 目标交付 | 落地实现与位置 | 验证方式与实测摘要 |
| --- | --- | --- | --- |
| [升级核验 §5](AUTONOMOUS_OPPORTUNITY_ENGINE_20261118.md#5-尚未完成及后续准入边界)（热路径加速） | 消除 `PoolGraph::scan_cycles` 内部 `String` 分配与哈希查找，达亚毫秒扫描 | [graph_arb.rs](../../crates/oasis-core/src/graph_arb.rs) 内建整数 ID `IndexedGraph` 与 `ternary_search_indexed` | Release 单测实测 6 池 2-hop：`101 us/scan`（较此前 `11.9 ms` 降幅 >100x），逐位对齐参考实现 |
| [升级核验 §5](AUTONOMOUS_OPPORTUNITY_ENGINE_20261118.md#5-尚未完成及后续准入边界)（日志增量同步与清算） | V2/Aerodrome/V3/Slipstream/V4 日志增量状态同步、重组回滚及闪电贷清算发现 | [state_sync.py](../../src/oasis_agent/fastpath/state_sync.py)、[liquidation.py](../../src/oasis_agent/fastpath/liquidation.py) | 支持连续块校验、`ReorgDetectedError` 与 64 块 CoW 回滚，Morpho/Aave V3 分段还款优化；[状态同步与清算单测](../../tests/unit/test_fastpath_state_sync_and_liquidation.py) 全过 |
| [外部审计复核 §8](EXTERNAL_OASIS_AUDIT_REVIEW_20261115.md#8-下一步优先级) P1 | `LaunchpadSafetyGate` 接入 `SpotAgent` 入场买前路径 | [spot_agent.py](../../src/oasis_agent/launch/spot_agent.py) 新增 `make_launchpad_safety_evaluator` 与 `_try_entry` 防线 | [现货发射台集成测试](../../tests/unit/test_spot_launchpad_integration.py) 与原有 `test_spot_agent.py` 共 `82 passed`；衰减期直接跳过且消耗 0 次报价 |
| [外部审计复核 §8](EXTERNAL_OASIS_AUDIT_REVIEW_20261115.md#8-下一步优先级) P2 | `BaseTime`（`0x4200...0030`）同块预部署实读校验 | [base_time_probe.py](../../src/oasis_agent/evidence/base_time_probe.py) | `public-shadow` 在 Base 块 `58978574` 实测 `header_matches_predeploy = true`（见第 2 节） |
| [外部审计复核 §8](EXTERNAL_OASIS_AUDIT_REVIEW_20261115.md#8-下一步优先级) P3 | `OasisIntentVault`（ERC-1271 + `IReactorCallback` + 双平面托管）设计与测试 | [OasisIntentVault.sol](../../contracts/src/OasisIntentVault.sol)、[unit](../../contracts/test/OasisIntentVault.t.sol) / [adversarial](../../contracts/test/OasisIntentVaultAdversarial.t.sol) / [invariant](../../contracts/test/OasisIntentVaultInvariant.t.sol) | Foundry 13/13 全过（含 512 轮 65,536 次调用 0 回滚不变量测试）、Slither `--fail-low` 0 告警 |
| [外部审计复核 §8](EXTERNAL_OASIS_AUDIT_REVIEW_20261115.md#8-下一步优先级) P4 | `UniswapX` 订单簿只读轮询与非原子对冲/研究边界 | [uniswapx_client.py](../../src/oasis_agent/intents/uniswapx_client.py) | 限频只读客户端与 SSRF 校验；`public-shadow` 实测获取 Base `Dutch_V3` 开放盘口快照 |
| [长期路线 §9](../../docs/strategies/LONG_TERM_PROFIT_ROADMAP.md#9-stage-7--portfolio-allocator)（Stage 7 组合分配） | 独立储备隔离、风险调整边际配置、7 类风险域相关性上限、故障/负收益自动归零 | [src/oasis_agent/portfolio/](../../src/oasis_agent/portfolio/__init__.py)（`models.py`、`allocator.py`） | [组合分配单测](../../tests/unit/test_portfolio_allocator.py) 全过；USD 500 组合中预留 USD 150 不可动用储备、无效赛道即时 `scale_to_zero` |
| 控制台可观测性与全雷达集成 | 只读 `/api/v1/radar/*` API 及 v2 统一机会雷达脚本 | [routers_radar.py](../../src/oasis_agent/server/routers_radar.py)、[run_opportunity_radar.py](../../scripts/run_opportunity_radar.py) | [雷达 API 单测](../../tests/unit/test_server_radar_api.py) `31 passed`，兼容 v1/v2 清单 SHA-256 校验与 ETag 304，OpenAPI 契约一致 |

## 2. 本轮实链只读与 `public-shadow` 取证记录

本机测试网络环境下先于 `reports/research/cto-roadmap-20261118` 记录了一次原始 TUN Fake-IP DNS 阻断事件：由于本机系统路由将公开域名解析为 RFC 2544/5180 基准测试段（`198.18.0.0/15`、`2001:2::/48`），被 `ipaddress.is_private` 视作内网而在发出 0 次网络请求（`requests_used = 0`）前自我拒止；同期 `eqn.news` 返回 HTTP 200（`eqn_raw_sha256` = `9b52aaeec15d1612e3440be2355cba6848fa309f6c4efb07125474bf3c3ab4af`）、`ssh aleph` 探针 `reachable = true`。保留该阻断记录后，我们在 `base_time_probe.py` 与 `uniswapx_client.py` 中对**非字面 IP 域名且仍受 `certifi` TLS SAN + 禁重定向保护**的 DNS 解析仅放行 `198.18.0.0/15` 与 `2001:2::/48`（同时继续硬阻断全部字面 IP 与 `127.0.0.0/8`、`10.0.0.0/8`、`169.254.0.0/16`、`192.168.0.0/16`、`::1`、`fc00::/7`、`fe80::/10` 等私有网段），并在全新目录 `reports/research/cto-roadmap-20261118-live/` 执行单次有界 `public-shadow` 运行：

| 取证项目 | 实测观测结果 | 密码学承诺与边界 |
| --- | --- | --- |
| 统一雷达 v2 报告 | `mode = "public-shadow"`，`observed_at_ms = 1794986088666`，7 大顶层引擎全通过 | `radar_report_sha256` = `880a43cdef64f257a0761499452997122983fc5525e9ffc1c2efa9d7ca1d4927`（仅本机未提交归档） |
| Base `BaseTime` 预部署实读（P2） | 源 `https://base-rpc.publicnode.com`，Base 块 `58978574`（`0x774884d7fbe84f68c2c9a582bd5bfe5c9f664a9b841f8a81c389e6fa98c5e5d2`），`header_timestamp = 1794986090`，`header_timestamp_ms = 1794986090400`，`BaseTime.timestampMs() = 1794986090400`，`timestampMillisPart() = 400`，**`header_matches_predeploy = true`**，消耗 `3/4` 请求 | 请求哈希 `38a161f3376c012276c3d6c9666dcbd4addcc3e56076a48d5d04e13fe2cd2b7d` / `39f6268b0f59c9c4ebadf613cc139f43ec87e687c53803adf5162ec34c1a3b90`；响应哈希 `91e4a5b1038965dd5c6349b405d70af7f981c2c2e5eb3cd98081accbfd10e6cf` / `f80c6ff46f0e5ec3adf2c3e7866f575339afed1f025a89249fcd0c235f5a4826` |
| `UniswapX` Base 订单簿轮询 | `https://api.uniswap.org/v2/orders?chainId=8453&orderType=Dutch_V3&orderStatus=open&limit=20` 返回 `status = "ok"`、`order_count = 0` | `raw_sha256` = `e031acd7d2a0a13081112896146071e885399a95d98315a55efcd0d0e1817b10` |
| `eqn.news` 实况抓包 | HTTP 200 获取 `20` 条快讯，`counts_by_kind` 含 2 条冻结、2 条 Tier1 上新、3 条极速价格冲击；自动冻结 `APT` | `eqn_raw_sha256` = `9b52aaeec15d1612e3440be2355cba6848fa309f6c4efb07125474bf3c3ab4af` |
| `ssh aleph` 湖仓与 `Stage 7` 分配 | 只读探针 `reachable = true`（`campaigns=38`、`replay_tapes=40`、`parquet=28`、`gold=10`）；Stage 7 隔离储备 USD 150、按域上限与步骤上限分配 USD 350、遗留未核验策略 `scale_to_zero` | `portfolio_allocation_sha256` = `3ebbe7ec38a7502d3e86976d88248540def1a00b0b4ac3455b1b223d38e5b435`、`aleph_shm_probe_sha256` = `fdd6303099e64a45527dad006d09c001d90d0967078bec194317ba662773d243` |

## 3. 全仓库质量与安全门禁（最终代码态）

| 门禁目标 | 命令 | 实测结果 |
| --- | --- | --- |
| Rust 工作区门禁 | `make rust-check` + `--release` 性能单测 | 退出 `0`：`cargo fmt --check`、`cargo clippy -- -D warnings` 0 警告、`cargo test --offline` **16 passed, 0 failed**；Release 6 池 2-hop `indexed_hot_loop` = `101 us/scan` |
| 依赖、SAST 与密钥门禁 | `make security-check` | 退出 `0`：`pip_audit` 0 漏洞；`bandit -r src scripts -ll` 0 Medium / 0 High（扫描 `99,476` 行）；`scripts.check_secrets` 通过 |
| Solidity 静态安全分析 | `make contract-security-check` | 退出 `0`：Slither 分析 **97 个合约、102 个检测器**，`--fail-low` **0 项阻断告警** |
| Python、前端与 Foundry 全门 | `OASIS_FASTPATH_LIB="$PWD/crates/target/release/liboasis_core.dylib" make check` | 退出 `0`：`ruff check` & `ruff format --check`（**842** 文件）、`mypy src`（**345** 文件 0 错）、`pytest`（**6,352 passed, 138 subtests passed, 0 failed**）、`scripts.check_dashboard`（9 模块）、`forge fmt --check`、`forge build`（158 文件 `Solc 0.8.26`）、`FOUNDRY_PROFILE=ci forge test`（**16 suites, 112 tests passed, 0 failed, 0 skipped**，三大不变量测试均 512 轮 65,536 次调用 0 回滚）、`forge lint` 全通过 |

## 4. 明确非声明与后续合规前提

- 本次交付完成全部既定工程模块、不变量测试、静态安全审计与有界 `public-shadow` 接口采证，**不构成**对 untouched 实盘窗口持续正净收益的承诺，也**不授予**任何实时钱包签名、广播、资金划转或合约部署权限（全部输出维持 `execution_eligible = false`）。
- 实盘执行链路尚未接线：`scripts/run_profit_strategy.py` 只执行状态同步，`submit_dual_simulated_intent` 目前仅由测试调用；在完成真实信号到签名/广播的接线并通过 9 门禁前，`--mode live` 不得视为可签名路径。
- 任何未来从影子或离线研究向实盘 Canary 的晋级，仍必须遵循 [AGENTS.md](../../AGENTS.md) 与 [LIVE_RELEASE.md](../operations/LIVE_RELEASE.md)：由 owner 显式批准当次资本与 RPC 范围，提供独立完整 NAV 与双源 pinned 模拟证据、双账本结算对账及事故暂停恢复演练。
