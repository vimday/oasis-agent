# Wave 23：`208e3e8..07aa611` 复核修复（P1/P3/P2.1）与防毒流做市 600-Tick 实测核验（2026-11-21）

## 摘要

本轮按 [复核记录](REVIEW_208E3E8_07AA611_20261121.md) 建议的优先级顺序，在保持 `execution_eligible = false`、`release_permitted = false` 的研究与只读边界下完成了三项审查问题修复及 Wave 23 防毒流做市升级，并运行了全新未触碰窗口的 **600 区块 Base 主网 + Aero Slipstream 2 + Bybit Spot V5 WebSocket** 实测与全套 CI 验证：

1. **修复 P1（`IOasisSwapAdapter` 绑定 `step.pool`）**：在 [OasisIntentVault.sol](../../contracts/src/OasisIntentVault.sol) 中将 `IOasisSwapAdapter.swapExactInput` 接口扩展为 `(address pool, address tokenIn, address tokenOut, uint256 amountIn, uint256 minOut, address recipient)`，并在 `_executeSingleSwapStep` 中显式传入经 `allowedPools[step.pool]` 校验的 `step.pool`；同步更新 [OasisIntentVault.t.sol](../../contracts/test/OasisIntentVault.t.sol)（记录并断言 `router.lastPool() == address(v3Pool)`）与 [OasisIntentVaultAdversarial.t.sol](../../contracts/test/OasisIntentVaultAdversarial.t.sol)。
2. **修复 P3（消除 Rust `sha3` 未使用依赖告警）**：从 [Cargo.toml](../../crates/oasis-core/Cargo.toml) 移除未使用的 `sha3 = "0.12"` 依赖，更新 [Cargo.lock](../../crates/Cargo.lock) 并重编 `crates/target/release/liboasis_core.dylib`，`make rust-check` 达到 **0 编译与 clippy 告警**。
3. **修复 P2.1（同步部署与运行文档）**：更新 [CUSTODY_AND_AGENT_KEY_MODEL.md](../security/CUSTODY_AND_AGENT_KEY_MODEL.md) 与 [PROFIT_ENGINE_PRELIVE_RUNBOOK.md](../operations/PROFIT_ENGINE_PRELIVE_RUNBOOK.md)，明确严格默认分离规则下个人双 EOA 部署 `DeployOasisProfitEngine.s.sol` 需显式传 `OASIS_ALLOW_SOLO_ROLE_COLLAPSE=true`，以及 `run_profit_strategy --mode live` 需同时提供 `--policy-config` 与 `--readiness-report`。
4. **Wave 23 防毒流做市与有效激活手数选择**：在 [maker_hedge_shadow.py](../../src/oasis_agent/cross_venue/maker_hedge_shadow.py) 中新增 `post_fill_cooldown_steps`、`max_same_dir_momentum_bps`、即时+滚动波动叠加及 `peek_effective_adverse_bps`；在 [run_multicall_100tick_campaign.py](../../scripts/run_multicall_100tick_campaign.py) 的 `_select_dynamic_maker_quotes` 中剔除不满足最小边缘阈值的超大无效手数，并在 `--dynamic-sizing` 下启用 `post_fill_cooldown_steps=5`、`max_same_dir_momentum_bps=8.0`；[test_maker_hedge_shadow.py](../../tests/unit/test_maker_hedge_shadow.py) 新增 3 项单元测试验证。

## 1. 基线安全、合约与 Anvil 核验（`reports/research/wave23-20261121/`，本地保留未入库）

| 证据项 | 命令 / 路径 | 实测结果 | 摘要哈希 |
| --- | --- | --- | --- |
| 合约 17 套件 & 静态检查 | `make contract-check && make contract-security-check` | `125 passed, 0 failed`；Slither 98 合约 / 94 检测器通过 | — |
| 回环 Anvil 真实字节码 E2E | `scripts/verify_atomic_vault_anvil.py` | `all_drills_passed=true`（`early_abort=58,204 gas`，`vault_reactor_fill=287,201 gas`） | `30031fbab7f2c8f1...` |
| Compose 离线策略核验 | `scripts/verify_profit_compose.py` | `passed=true`，6 服务 2 用例，`network_calls=0` | `835544a87de35c3e...` |
| 无密钥权限演练 | `scripts/verify_profit_authority_rehearsal.py` | `passed=true`（`$500/$200` 与 `$5,000/$750` 双配置） | `141b5fe7778ecff1...` |
| 双源 Base RPC 仲裁 | `oasis_agent.live.dual_rpc_probe` | Base 块 `60327326`，`secondary_rpc_verified=true` | `2fe15885abc7552f...` |
| Opportunity Radar | `scripts/run_opportunity_radar.py --mode public-shadow` | Base 块 `60327343`，`execution_eligible=false` | `e47b8ab49b0f1674...` |
| `aleph` WSL Docker 隔离证明 | `docker_confinement_report_v2.json` / `wsl_authority_status_proof_v2.json` | 复用已验非 root 只读隔离仓 `account=0x00abc394...` | `2552521d...` / `0f4f281b...` |

## 2. Wave 23 未触碰 600-Tick 实测与原因定位

- 运行目录：`reports/research/wave23-20261121/shadow-bybit-600/`（`summary_sha256 = 68bf18f5acb9ff62c681a07ec7bc567633bcf94b42719dc5e7ff7c69b37954f8`）
- 运行参数：`--mode public-shadow --ticks 600 --interval-ms 1200 --max-rpc-calls 1500 --timeout-s 15 --include-ecosystem-tokens --include-aero-cl2 --cex-venue bybit --dynamic-sizing --maker-min-edge-bps 8.5 --enable-coinbase-ws --stop-gracefully-on-rpc-error`
- 实测观测：
  - **网络与数据完整性**：`600 / 600` 唯一 Base 区块（`60327366..60331098`）、`1,203` RPC 请求、`57` 个活跃池、实时 `USDT-USD` 锚定 `0.998105`、Bybit WebSocket 连续 `600` tick **`0` 断流 / `0` 重连**、接收 **`17,965` 笔实时成交**。
  - **市场与策略表现**：`positive_net_ticks = 18`（含 `best_raw_spread_bps = 8.901782`、`best_quoted_edge_bps = 47.506951`、`DEGEN-USD` 获 `378` 个有效挂单 leg 与 `37` 次 `touch_lift` 触价上移事件，其中 `36/37` 为正，`touch_lift` 假定净赚 `$16.28` 但按保守标准不计入 OOS 成交），`strict_cross_fills = 0`、`trade_flow_fifo_fills = 0`、`oos_trades_count = 0`、`unresolved_residuals = 0`。
  - **精确定位出本次 0 OOS 成交的工程/参数双因（作为 Wave 24 唯一输入，未事后篡改本轮结果）**：
    1. `maker_hedge_shadow.py` 对 `detected_fills` 全集应用了 `post_fill_cooldown_steps = 5`，导致**不计入 OOS 成交的诊断性 `touch_lift_fills`（37 次）也触发了 `cooldown_quote_skips = 222` 次同侧撤单冷却**，大幅减少了有效挂单在线时长；
    2. 本轮同时将 `maker_min_edge_bps` 从 `6.5` 提高到 `8.5`，在低波动日间窗口将 `AERO-USD`（最高 `8.502 bps`）、`VIRTUAL-USD`（最高 `5.709 bps`）和 `VVV-USD`（最高 `3.643 bps`）绝大多数可成交挂单过滤至零。

## 3. 9 项门禁总验结论

`scripts/check_profit_engine_readiness.py --require-ready`（输出 `reports/research/wave23-20261121/readiness_report.json`，`report_sha256 = bec53faf256b0630388b252751d9938a21e9ccc71fc57ea3eba06fefc04ca6f1`）退出码 `2`，预注册门槛完全未放松：

- **通过**：`7 / 9` 项门禁。
- **如实阻断（`2 / 9`）**：
  1. `positive_cost_adjusted_lcb_edge`（Gate 5）：`oos_trades_count = 0 < 5`。
  2. `owner_scope_release_manifest_present`（Gate 9）：演练清单 `rehearsal_only = true`。
- `release_permitted = false`，`execution_eligible = false`。

## 4. 全量 CI 与安全检查

- `make rust-check`：`18 passed`（`cargo fmt --check`、`clippy -D warnings` 0 告警、`cargo test --locked`）。
- `make profit-deployment-check`：`passed=true`。
- `make security-check`：`pip_audit` 0 漏洞、`bandit`（`125,562 LOC`）0 中高危、`scripts.check_secrets` 通过。
- `make contract-check`：`125 passed / 17 suites`；`forge fmt --check` 与 `forge lint` 通过。
- `make contract-security-check`：`slither`（98 contracts / 94 detectors）通过。
- `OASIS_FASTPATH_LIB="$PWD/crates/target/release/liboasis_core.dylib" make check` 与 `git diff --check` 在本次文档更新后终验通过。
