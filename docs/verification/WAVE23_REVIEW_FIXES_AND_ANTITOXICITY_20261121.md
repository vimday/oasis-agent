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

## 5. Wave 24 冷却口径与波动惩罚解耦实测验证（2026-11-21）

针对 Wave 23 定位出的两项过严过滤，在不修改 `execution_eligible = false`、`release_permitted = false` 与预注册 Gate 5 门槛（`min_oos_trades = 5`、`min_lcb_net_edge_bps = 5`）的前提下完成三处精准修正与新鲜 600-Tick 实测：

1. **核心改动**：
   - [maker_hedge_shadow.py](../../src/oasis_agent/cross_venue/maker_hedge_shadow.py)：`post_fill_cooldown_steps` 仅对非 `"touch_lift"` 真实成交触发（`and detected.classification != "touch_lift"`）；`peek_effective_adverse_bps` 与 `_update_volatility_and_get_effective_adverse` 移除 `max(rolling_vol_bps, instantaneous_return)` 对滚动波动的双重惩罚，由 `max_same_dir_momentum_bps` 独立承担单 tick 突破动量防毒流职责。
   - [run_multicall_100tick_campaign.py](../../scripts/run_multicall_100tick_campaign.py)：`_select_dynamic_maker_quotes` 在双腿未激活时恢复基于最佳边缘（允许负值被最优手数求解器排除）的评估分支；`--dynamic-sizing` 恢复默认 `maker_min_edge_bps = 6.5` 与 `max_same_dir_momentum_bps = 9.0`（保留 `post_fill_cooldown_steps = 5`）。
   - [test_maker_hedge_shadow.py](../../tests/unit/test_maker_hedge_shadow.py)：新增 `test_touch_lift_does_not_trigger_post_fill_cooldown` 与 `test_adaptive_vol_uses_rolling_mean_even_with_momentum_gate`，并更新超大无效手数单测使超大单价呈现严格负边缘；4 组聚焦单测共 `58 passed`。
2. **新鲜基线与未触碰 600-Tick 实测（`reports/research/wave24-20261121/`，本地保留未入库）**：
   - 双源 Base RPC 仲裁：Base 块 `60336138`（`secondary_rpc_verified=true`，`report_sha256 = 7a1d2350a70f8c4caf42eb75c226e9eb2e347f4bea655a6c36c3c4ea57403848`）；Opportunity Radar `public-shadow`：`report_sha256 = afcf2248fb981836cf713bce8fe389f6da2b0e3a06cc32b69d7d67f250b44df5`；复用已验 Anvil/Compose/权限演练及 `aleph` WSL Docker 隔离证明；演练 signoff `manifest_sha256 = e017551ad9e538b1688551ed6a4c7ca0c4182f6aa21cc54c4bec132f259e068d`。
   - `shadow-bybit-600`（`summary_sha256 = 3d603ffac961fd8ef8bc52f3d3af376a3685a2f116d724cb95946dc17001e9a9`）：`600 / 600` 唯一 Base 区块（`60336220..60340124`）、`1,209` RPC 请求、`56` 个活跃池、实时 `USDT-USD` 锚定 `0.998285`；Bybit Spot V5 WS `600 / 600` tick `0` 断流 / `0` 重连、`16,223` 笔实时成交。
   - 策略与样本外表现：`active_quote_legs = 378`、`cooldown_quote_skips = 12`（较 Wave 23 的 `222` 显著解除误杀）、`momentum_quote_skips = 7`（有效拦截单 tick 动量追涨杀跌）、`touch_lift_fills = 16`、`trade_flow_fifo_fills = 2`、`open_unhedged_lots = 0`、`unresolved_residuals = 0`；**2 / 2 笔真实 OOS 成交全部正盈利（`oos_win_rate = 1.0000`）**、均净边缘 **`+35.773988 bps`**、标准差 `5.314595 bps`、成本调整后 95% LCB **`+28.408334 bps`**（`28 >= 5` 已跨越正边际门槛）、总净收益 **`+$0.411350`**、最大回撤 **`$0.000000`**。
3. **9 项门禁总验（`readiness_report.json`，`report_sha256 = 552f1f6992b71d804db6239a3fda51b86c57a4684556ec43ddf7cba0a2f1e3cb`，退出码 `2`）**：
   - **通过**：`7 / 9` 项门禁；
   - **如实阻断（`2 / 9`）**：`positive_cost_adjusted_lcb_edge`（`oos_lcb_net_edge_bps = 28 >= 5` 已达标，但 `oos_trades_count = 2 < 5` 未满最小样本下限）与 `owner_scope_release_manifest_present`（`rehearsal_only = true`）。下一步仅需在更高成交窗口运行未触碰长窗样本以累积 `oos_trades_count >= 5`。

## 6. Wave 25 Bybit 汇率对齐、未冲销队列保位与 Keep-Alive 长窗重连验证（2026-11-21）

针对 Wave 24 归因日志发现的两处结构失配与长跑连接断流问题完成三项修复，并运行两轮未触碰 600-Tick 实测（`reports/research/wave25-20261121/`，本地保留未入库）：

1. **核心改动**：
   - [bybit_spot_feed.py](../../src/oasis_agent/cross_venue/bybit_spot_feed.py)（`parse_bybit_orderbook_payload`）：买一/卖一订单簿与 WS/REST 成交统一采用 `usdt_peg.mid` 折算 USD（替代此前 `bid/ask` 与 `mid` 混用导致的顶档成交错位），[test_bybit_spot_feed.py](../../tests/unit/test_bybit_spot_feed.py) 验证盘口顶档与成交流逐位对齐。
   - [maker_hedge_shadow.py](../../src/oasis_agent/cross_venue/maker_hedge_shadow.py)（`_next_queue_ahead_for_side` 与 `step()`）：先运行 `_check_and_flush_triggered_lots` 批量冲销，再仅对本步实际产生非 `touch_lift` 实盘冲销的 `realized_filled_sides` 开启 `post_fill_cooldown_steps`；低于 `$25` 门槛的未冲销 FIFO 部分成交保持 `queue_ahead == 0` 队列首位且不提前冷却；[test_maker_hedge_shadow.py](../../tests/unit/test_maker_hedge_shadow.py) 新增 `test_sub_threshold_partial_fifo_fill_retains_front_queue_without_premature_cooldown` 验证（5 组聚焦单测共 `65 passed`）。
   - [run_multicall_100tick_campaign.py](../../scripts/run_multicall_100tick_campaign.py)：取消循环内每 30 tick 刷新 `active_usdt_peg` 造成的跨 tick 假 `touch_lift` 抖动；首轮（`shadow-bybit-600`）因公共 RPC 约百次 Keep-Alive 复用连接在 `reused=True, attempt=0` 抛出超时而安全停机后，将 `_KeepAliveHttpsPool` 默认 `max_requests_per_conn` 从 `200` 降至 `80` 并为 `HTTPException/OSError/TimeoutError/EOFError` 增加一次旧连接销毁重连（`test_dual_rpc_and_cex_dex_shadow.py` `27 passed`）。
2. **基线、首轮早停对照与次轮完整 600-Tick 实测**：
   - 新鲜基线：双源 Base RPC 仲裁锁定块 `60355703`（`secondary_rpc_verified=true`，`report_sha256 = 416975f5af46157dadab57283f5e893a1ed930bdf6c4c5c22a14eabca95bd0e6`）；`Opportunity Radar` `public-shadow`：`report_sha256 = fcae0fc49ca8396b1a6dadba133f44d271f1bacb3723ef49696c4c2dc76fbd67`。
   - 首轮早停留存（`shadow-bybit-600`，`summary_sha256 = 3ca08527751facbccaee8ee9294dd7b45297a50255bed3b97f3734282510cb7a`）：`ticks_completed = 55 / 600`、`stopped_early = true`（`stop_reason = "Base shadow RPC transport failure"`、`115` RPC）；在统一汇率锚与固定 `active_usdt_peg` 下 55 个 tick 录得 `active_quote_legs = 55`、`touch_lift_fills = 0`、`cooldown_quote_skips = 0`。
   - 续跑完整实测（`shadow-bybit-600-r2`，`--maker-min-edge-bps 4.0`，`summary_sha256 = b7425be7c180186a1cc2b6af22fc1225a1789fee8ef6ac6fcb2003e08ed8e38d`）：Keep-Alive 80 请求主动轮换配合失败重连完整跑通 **`600 / 600` 唯一 Base 区块**（`60357699..60362355`、`1,212` RPC 请求、`57` 个活跃池、`0` RPC 中断、`stopped_early = false`），Bybit WS 在遭遇 1 次断流后通过 14 次 REST 回补与 1 次重连完成全 600 块（`21,621` 笔实时成交、`feed_gap_cancels = 1`、`open_unhedged_lots = 0`、`unresolved_residuals = 0`）。
   - 做市与 Gate 5 对照：`active_quote_legs = 531`（覆盖 `DEGEN-USD`、`AERO-USD`、`VVV-USD`、`VIRTUAL-USD`、`MORPHO-USD`）、`cooldown_quote_skips = 2`、`momentum_quote_skips = 4`；在 `--maker-min-edge-bps 4.0` 下触发 2 笔 `strict_cross` 成交（`VVV-USD` `-$0.023097` / `-1.025879 bps`、`VIRTUAL-USD` `+$0.032810` / `+1.461193 bps`），合计 `oos_trades_count = 2`、`oos_win_rate = 0.5000`、`oos_mean_net_edge_bps = +0.217657 bps`、`oos_lcb_net_edge_bps = -3`（`-2.219674 bps`）、净赚 `+$0.009713`、`oos_max_drawdown_usd = $0.023097`。实证显示 `4.0 bps` 做市边际在 `t -> t+1` 跨块漂移后偏薄，后续长窗应继续保持 Wave 24 的 `--maker-min-edge-bps 6.5` 默认门槛。
3. **9 项门禁总验（`readiness_report.json`，`report_sha256 = 5564882e89d97d967c6118ebe052a8d4b38381e33fdbdb0bd74b51b69e5b2375`，退出码 `2`）**：
   - **通过**：`7 / 9` 项门禁；
   - **如实阻断（`2 / 9`）**：`positive_cost_adjusted_lcb_edge`（`oos_trades_count = 2 < 5` 且 `4.0 bps` 组 `oos_lcb_net_edge_bps = -3 < 5`）与 `owner_scope_release_manifest_present`（演练清单 `manifest_sha256 = d86a74ab...`、`rehearsal_only = true`），`release_permitted = false`、`execution_eligible = false`。
