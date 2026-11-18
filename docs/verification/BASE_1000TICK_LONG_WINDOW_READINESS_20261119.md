# Wave 19–20 多资产做市队列生命周期、`TickMath` 闭上界对齐、Keep-Alive 轮换与长窗口主网综合核验（2026-11-19）

2026-11-19（Asia/Shanghai）；在 `c0a703c`（Wave 18 已推送基线）之上完成 **Wave 19** 多资产被动做市生命周期与风控筛选校准，并在四轮连续 Base 主网 + live Coinbase WebSocket 实测与重编后 `aleph` WSL 真实 Linux Docker 隔离复验中定位并修复两项仅在长窗口高区块遍历中暴露的边界缺陷：**Rust/Python `slot0` 闭区间上界对齐** 与 **HTTP/1.1 Keep-Alive TLS 200 请求主动轮换及非重试平滑停机收尾**。

全程严格保持无真实私钥落盘或入库、预注册 Gate 5 阈值保持不变（`min_oos_trades = 5`、`min_lcb_net_edge_bps = 5`）、`execution_eligible = false`、`release_permitted = false`。本文档衔接并保留先前核验记录（[LIMIT_IOC_CONCURRENT_FASTPOLL_20261119.md](LIMIT_IOC_CONCURRENT_FASTPOLL_20261119.md)、[SOLO_EOA_CUSTODY_AND_WSL_DOCKER_20261118.md](SOLO_EOA_CUSTODY_AND_WSL_DOCKER_20261118.md) 与 [准实盘操作手册](../operations/PROFIT_ENGINE_PRELIVE_RUNBOOK.md)）。

## 1. Wave 19–20 关键修复与工程交付

1. **多资产做市生命周期修复（[maker_hedge_shadow.py](../../src/oasis_agent/cross_venue/maker_hedge_shadow.py)、[test_maker_hedge_shadow.py](../../tests/unit/test_maker_hedge_shadow.py)）**：
   - **按产品隔离步数计时**：将共享计数器 `_step_index` 替换为按产品跟踪的 `_step_index_by_product: dict[str, int]`，消除 8–9 个产品交错调用导致单笔微额未对冲仓位仅 1 个外部 tick 便被误触发 `batch_age_flush` 的跨产品计数污染。
   - **Opt-in 队列顺位保留与同侧去重**：`MakerHedgeConfig` 新增 `preserve_queue_priority: bool = False`（借助 `_drain_fifo_queue_ahead` 在相同挂单价未成交且价位未变时跨 tick 结转 `min(drained, new_bbo_size)`）与 `deduplicate_fifo_and_strict: bool = False`（避免同一 `side` 在同一 tick 同时因 WS `trade_flow_fifo` 与 BBO `strict_cross` 重复成单），并在 `--dynamic-sizing` 下自动启用（`--enable-coinbase-ws` 同步开启同侧去重）。
   - **动态微额批量阈值调整**：`--dynamic-sizing` 下配置 `volatility_multiplier = 1.5`、`min_hedge_notional_usdc = max(25_000_000, min_clip_usd_micros)`（`25` USD）、`max_unhedged_steps = 60`（保留 `max_unhedged_drift_bps = 15.0` 即时逆向止损）。

2. **跨语言 `TickMath` `slot0` 闭上界对齐（[pool_math.rs](../../crates/oasis-core/src/pool_math.rs)、[graph_arb.rs](../../crates/oasis-core/src/graph_arb.rs)、[live_shadow_runner.py](../../src/oasis_agent/fastpath/live_shadow_runner.py)、[state_sync.py](../../src/oasis_agent/fastpath/state_sync.py)、[test_base_arb_shadow.py](../../tests/unit/test_base_arb_shadow.py)）**：
   - 首轮 1,000-tick 主网战役在连续跑通 721 块（`59171613..59178220`）后因池 `slot0` 状态 `sqrtPriceX96 >= sqrt_price_at_tick(tick + 1)` 终止。核对链上 `TickMath` 与 Uniswap V3 / Aerodrome Slipstream `zeroForOne` 精确落位规则：当卖出方向精确停在已初始化边界 `sqrt_price_at_tick(k + 1)` 且剩余输入为 `0` 时，协议合法写入 `tick = k` 与 `sqrtPriceX96 = sqrt_price_at_tick(k + 1)`（即 [valuation.py](../../src/oasis_agent/live/valuation.py) 已采用的左闭右闭区间 `sqrt_price_at_tick(tick) <= sqrt <= sqrt_price_at_tick(tick + 1)`）。
   - 将 Rust 与 Python 四处校验统一由 `>=` 修正为 `>` 并新增对应 Rust/Python 单测，重编发布态动态库后在后续合计 `2,221` 块实时读取中保持 `0` 次 `slot0` 误差。

3. **Keep-Alive TLS 200 请求轮换与非重试平滑停机（[run_multicall_100tick_campaign.py](../../scripts/run_multicall_100tick_campaign.py)、[test_dual_rpc_and_cex_dex_shadow.py](../../tests/unit/test_dual_rpc_and_cex_dex_shadow.py)）**：
   - `_KeepAliveHttpsPool` 新增 `max_requests_per_conn = 200` 主动轮换、`resp.will_close` 立即丢弃及仅对已复用旧连接（`reused and attempt == 0`）开放 `ssl.SSLError` / `BadStatusLine` / `IncompleteRead` / `EOFError` 的一次重建恢复（新连接失败、`TimeoutError` 与 `429` 仍首次即停机，绝不额外重发请求）。
   - 新增 `--timeout-s`（实测采用 `25.0`）、校准默认选择性（`min_taker_edge_bps = 6`、`maker_min_edge_bps = 6.5`）及 `--stop-gracefully-on-rpc-error`（在 `tick >= 2` 遭遇公共 RPC 传输断链时严格停机不重试，同时保证已完成哈希链与 `shadow_summary.json` 完整落盘校验）。

## 2. Wave 20 前置基线与重编后 `aleph` WSL Linux Docker 隔离复验

归档于本机研究目录 `reports/research/wave20-20261119/`（按仓库规则不入 Git）：

- `profit_compose_verification.json`：`passed = true`、`compose_hash = b3821a8f...`、`execution_eligible = false`。
- `authority_rehearsal.json`：双配置无密钥签名权威演练 `passed = true`（`report_sha256 = 141b5fe7...`）。
- `anvil_e2e_report.json`：回环 Anvil 4/4 全结算通过（`early_abort = 58,204 gas`，0 授权残差，`report_sha256 = 33d902d2...`）。
- `dual_rpc_quorum.json`：`publicnode_base` 与 `base_official_mainnet` 于固定 Base 块 **`59171438`** 一致（`secondary_rpc_verified = true`、`report_sha256 = 4015c2b6...`）。
- `radar/radar_report.json/radar_report.json`：块 **`59171453`** `BaseTime` 预部署交叉校验通过（`report_sha256 = 7833cc09...`、`execution_eligible = false`）。
- **`aleph` WSL 真实 Linux Docker v2 复验**（基于已合入 `TickMath` 修复的 [crates](../../crates/Cargo.toml) 与 [deploy/profit-engine/compose.yaml](../../deploy/profit-engine/compose.yaml) 规范 `--locked` / `--require-hashes` 重建容器）：
  - `docker_confinement_report_v2.json`：`passed = true`、`rehearsal_cleaned = true`、`uid/gid = 10001`、`cap_eff_zero = true`、`no_new_privs = true`、`rootfs_readonly = true`、`secrets_absent = true`、`authority_state_absent = true`、`can_deploy = true`（`report_sha256 = 2552521d...`）；
  - `wsl_authority_status_proof_v2.json`：通过隔离 UDS `/run/oasis/profit_authority.sock` 产出有效 Gate 8 证明（`account = 0x00abc39454b72e0f3a65b5996ea0efe600b77fcb`、`isolated_key_volume = oasis-rehearsal-72b1e82e7e38-canary-keys`、`proof_sha256 = 0f4f281b...`）。

## 3. Wave 20 四轮主网 + Coinbase WS 实测全景记录（累计 `2,942` 唯一区块）

依循 `AGENTS.md` 如实保留全部过程与最终收口实测目录（均采用 `--allowed-products ETH-USD,BTC-USD,AERO-USD,VIRTUAL-USD,VVV-USD,MORPHO-USD,DEGEN-USD,WELL-USD --dynamic-sizing --enable-coinbase-ws --risk-config configs/profit-engine-risk.example.json --timeout-s 25.0`）：

| 轮次与目录 | Base 区块区间 | 完成 Tick | RPC 消耗 | WS 成交 / 断流 | 样本外 OOS 成交与净盈亏 | 终止状态与工程发现 |
| --- | --- | --- | --- | --- | --- | --- |
| 第 1 轮 `shadow-1000-ioc-fast` | `59171613..59178220` | `721` | `1,449` | `35,069` / `0` | **`6` 笔（`5/6` 正盈利，`83.33%`）**，`+$0.109469`，均值 `+4.04 bps`，`lcb_exact = +0.46 bps` | tick 722 触发 `slot0 sqrtPriceX96 >= sqrt_price_at_tick(tick + 1)`；唯一微小亏损 `-$0.002238` 为 `$6.51` 粉尘 lot 因 `8` tick 过期触发 gas 冲洗所致 |
| 第 2 轮 `shadow-1200-canonical-tickmath` | `59189003..59194290` | `676` | `1,362` | `49,744` / `0` | **`1` 笔（`1/1` 正，`100%`）**，`WELL-USD` `+$0.076515`（`+15.22 bps`），`0` 亏损 | 验证 `TickMath` 闭上界 **0 报错**；第 677 tick 暴露长期单连接被上游 TLS 静默回收问题 |
| 第 3 轮 `shadow-1200-keepalive-rotated` | `59203168..59211281` | **`1,045`** | `2,093` | **`56,090` / `0`** | **`3` 笔（`3/3` 正，`100%`）**，`+$0.061113`（均值 `+3.58 bps`、回撤 `$0.00`） | 验证 Keep-Alive 200 请求轮换稳定运行逾千 tick（`1,622.6s`），`0` 次 `429`；第 1,046 tick 公共节点网络瞬断触发停机但未写汇总 |
| 第 4 轮终验 `shadow-500-final`（`--stop-gracefully-on-rpc-error`） | `59213388..59217254` | **`500 / 500`** | **`1,003`** | **`25,184` / `0`** | **`1` 笔**，`WELL-USD` `$0.99`（`ratio=0.026`）在毛利正 `+25 bps` 下因 `60` tick 过期单笔承担 `$0.006151` Base gas 净亏 `-$0.003682`（`-37.31 bps`） | **exit 0 完整收口**，`chain_head_sha256 = f17bc95c...`、`summary_sha256 = d470d44a...`、`open_unhedged_lots = 0`、`strict_cross_fills = 0` |

**微观结构客观结论**：
1. 跨四轮合计 **`2,942` 唯一区块**与 **`166,087` 笔实时 WS 成交**中，`strict_cross_fills = 0`、WS **`0` 断流**、`0` 次 HTTP 429、`oos_max_drawdown_usd` 均不超过 `$0.0037`；第 1–3 轮实盘影子合集达到 **`9 / 10` 正盈利、累计净赚 `+$0.2471`、`oos_max_drawdown <= $0.0022`**。
2. 然而，在凌晨低波动至平静美股收盘窗口内，Coinbase 与 Base 池之间扣完 `3.5 bps` Taker 手续费与 `1.5 bps` 延迟折价后的高把握价差（`>= 6 bps`）极其稀疏（Taker 触发 `0` 笔，Maker 均单净利均值围绕 `+3.5..+4.0 bps` 波动），加之低于 `$5` 的极小局部成交在到达 `max_unhedged_steps` 强平时被 `$0.006` 固定 Base gas 穿透，**真实客观市场数据尚未在单个独立窗口达到预注册 Gate 5 门禁（`oos_trades_count >= 5` 且 `oos_lcb_net_edge_bps >= 5`）**。

## 4. `shadow-500-final` 9 门禁联合总验（`readiness_report.json`：`7 / 9` 通过）

搭配 `rehearsal_software_signoff.json`（`--rehearsal`、`manifest_sha256 = 9db791c1...`）、Wave 20 `dual_rpc_quorum.json` 与 `wsl_authority_status_proof_v2.json`，[check_profit_engine_readiness.py](../../scripts/check_profit_engine_readiness.py) 以 `--require-ready` 返回退出码 **`2`**（`report_sha256 = 14dcc4caef3a5b75907cb4ce89c5c817fb446fb734b189ca14beb6368bf6ccbf`，`release_permitted = false`、`execution_eligible = false`）：

- **通过 7 项**：
  1. `compiled_contracts_pinned`（`executor=3d1d2aac1dfa,vault=c4492642b730`）
  2. `anvil_e2e_settlement_reconciled`（`4/4` 通过、`early_abort = 58,204 gas`、0 残差）
  3. `base_time_predeploy_verified`（Base 块 `59171453` 一致）
  4. `untouched_shadow_sample_size`（`mode='public-shadow', ticks_completed=500 >= 100`）
  5. `revert_budget_healthy`（`revert_budget_status='active', unresolved_residuals=0`）
  6. `dual_rpc_quorum_verified`（Base 块 `59171438` `publicnode_base + base_official_mainnet` 一致）
  7. `isolated_signer_authority_ready`（`oasis-rehearsal-72b1e82e7e38-canary-keys` / `/run/oasis/profit_authority.sock`）
- **如实阻断 2 项（不降门槛、不绕过）**：
  1. `positive_cost_adjusted_lcb_edge`：`shadow-500-final` 样本内 `oos_trades_count = 1 < 5`、`oos_lcb_net_edge_bps = -38 < 5`。
  2. `owner_scope_release_manifest_present`：演练清单有意锁定 `rehearsal_only = True`，需先在高波窗口自然跨过 Gate 5 后再由运营者显式以 `--approve-software-scope` 签署。

## 5. 全仓代码与安全复核状态

- **Rust 工具链（`make rust-check` + release 编译）**：`cargo fmt --check`、`cargo clippy --workspace --all-targets --locked -- -D warnings`、`cargo test --workspace --locked` **18 passed**，`liboasis_core.dylib` / WSL `liboasis_core.so` 同步重编通过。
- **全仓质量与安全 CI**：`make check`、`make profit-deployment-check`、`make security-check`、`make contract-security-check`、`git diff --check` 与 `scripts.check_secrets` 全部在本轮终改后跑全确认（具体命令输出见提交与汇总记录）。
