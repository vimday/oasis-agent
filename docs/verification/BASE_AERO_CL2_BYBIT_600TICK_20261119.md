# Base Aero Slipstream 2、Bybit Spot V5 接入与 600-Tick 样本外实测核验（Wave 21–22，2026-11-19）

## 摘要

本轮（Wave 21–22）针对 Wave 20 暴露的两大结构性瓶颈——Base 主网流动性向 **Aero Slipstream 2 (`aero.xyz`)** 迁移导致旧池点差偏宽、以及 Coinbase 生态币成交频率偏低导致的粉尘超时 gas 拖累——交付了三项核心工程升级并完成了未经事后调参的 **600 区块 Base 主网 + Aero Slipstream 2 + Bybit Spot V5 WebSocket 实时影子战役**：

1. **Aero Slipstream 2 (`CLFactory` `0x9cb220581b91b0c83223012fe69688b92ad07a76`) 发现接入**：在 [multicall_universe.py](../../src/oasis_agent/fastpath/multicall_universe.py) 中新增可选 `include_aero_cl2: bool = False`（CLI `--include-aero-cl2`）及 `AERO_CL2_TICK_SPACINGS = (1, 10, 50, 100, 200)`，在同一 3-RPC-call 发现阶段将 Base 活跃池池深从 `44` 扩展至 **`57` 池**（涵盖 `cbBTC/USDC` ~1.7 bps 动态费率、`VIRTUAL/WETH` 4.1 bps、`VVV/USDC` 19.5 bps、`MORPHO/WETH`、`DEGEN/WETH`、`cbETH/WETH` 0.6 bps），且严格不跨代混用 Slipstream 1 `QuoterV2`。
2. **无密钥硬化 Bybit Spot V5 订单簿与 WebSocket 成交客户端**：新增 [bybit_spot_feed.py](../../src/oasis_agent/cross_venue/bybit_spot_feed.py)（附单元测试 [test_bybit_spot_feed.py](../../tests/unit/test_bybit_spot_feed.py)），覆盖 `ETH/BTC/AERO/VIRTUAL/VVV/MORPHO/DEGEN` 7 个 Base 生态交易对；强制 IPv4 优先 TLS、DNS/SSRF/TUN 校验、`ProxyHandler({})`、禁止重定向、`1 MiB` 字节上限、实时 Coinbase `USDT-USD` 锚定归一化（`0.95 <= bid < ask <= 1.05` 失败关闭，本次实测 `usdt_usd_peg_mid = 0.998885`，消除 `-11.15 bps` 假稳定币基差）及 Bybit 吃单方向到被动挂单方向（`Buy -> maker_side='sell'`、`Sell -> maker_side='buy'`）的严格映射。
3. **成本感知 CEX 粉尘平仓（Cost-Aware CEX Dust Unwind）**：在 [maker_hedge_shadow.py](../../src/oasis_agent/cross_venue/maker_hedge_shadow.py) 中新增可选 `cex_dust_unwind_enabled` / `cex_dust_unwind_taker_fee_bps` 与 `hedge_mode = "cex_dust_unwind"`。仅当未对冲批次低于动态对冲门槛并触发超时/止损/强制清仓时，比较链上 DEX 付全额 gas 与 CEX BBO 吃单平仓（零链上 gas、实付点差与吃单费）的净收益并择优结算，彻底杜绝 `$0.99` 粉尘烧 `$0.006` Base gas 的失血且保持 `0` 隐藏残差。

所有验证均在 `execution_eligible = false`、`release_permitted = false` 的只读影子/演练边界下完成，未放松任何预注册门禁阈值（Gate 5 保持 `min_oos_trades = 5`、`min_lcb_net_edge_bps = 5`）。

## 1. 基线安全与部署证据（`reports/research/wave22-20261119/`，本地保留未入库）

| 证据项 | 命令 / 路径 | 实测结果 | SHA-256 摘要 |
| --- | --- | --- | --- |
| Compose 离线策略核验 | `scripts/verify_profit_compose.py` | `passed=true`，6 服务 2 用例，`network_calls=0` | `625b6077e2e4d9f1...` |
| 无密钥权限演练 | `scripts/verify_profit_authority_rehearsal.py` | `passed=true`（`$500/$200` 与 `$5,000/$750` 双配置） | `141b5fe7778ecff1...` |
| 回环 Anvil 合约结算 | `scripts/verify_atomic_vault_anvil.py` | `all_drills_passed=true`（`early_abort=58,204 gas`） | `cef58bf5a5eeef54...` |
| 双源 Base RPC 仲裁 | `oasis_agent.live.dual_rpc_probe` | Base 块 `59319889`，`secondary_rpc_verified=true` | `f51474dba2234c85...` |
| Opportunity Radar | `scripts/run_opportunity_radar.py --mode public-shadow` | `execution_eligible=false` | `8ca90647ce5f586e...` |
| `aleph` WSL Linux Docker 隔离复验 | `docker_confinement_report_v2.json` / `wsl_authority_status_proof_v2.json` | `passed=true`，`uid/gid=10001`，`cap_eff_zero=true`，`account=0x00abc394...` | `2552521d...` / `0f4f281b...` |

## 2. Base 主网 + Aero Slipstream 2 + Bybit Spot V5 600-Tick 实测

- 产物目录：`reports/research/wave22-20261119/shadow-bybit-600/`（`summary_sha256 = 52c0eddad0abe632c55ad806c55b8c1e6f8499efb2ef2dec790096e020590b0b`）
- 运行参数：`--mode public-shadow --ticks 600 --interval-ms 1200 --max-rpc-calls 1500 --timeout-s 15 --include-ecosystem-tokens --include-aero-cl2 --cex-venue bybit --dynamic-sizing --enable-coinbase-ws --stop-gracefully-on-rpc-error`
- 运行结果（退出码 `0`）：
  - **网络与数据完整性**：`600 / 600` 唯一 Base 区块、`1,212` RPC 请求、`57` 个活跃 Base 池（含 13 个 Aero Slipstream 2 池）、实时 `USDT-USD` 锚定 `0.998885`、Bybit Spot WebSocket 连续 `600` tick **`0` 断流 / `0` 重连**、接收 **`17,132` 笔真实成交**（`BTC-USD: 12,279`、`ETH-USD: 2,673`、`VVV-USD: 2,050`、`MORPHO-USD: 58`、`AERO-USD: 57`、`VIRTUAL-USD: 15`）。
  - **跨市场与原子套利机会**：`positive_net_ticks = 80`（含 **`1` 次链上原子 DEX 正净值 tick** 与 **`79` 次 CEX-DEX 正净值 tick**），最佳原始点差 `+22.48 bps`，最佳被动挂单毛边缘 `+22.68 bps`。
  - **样本外成交归因（`oos_execution_summary`）**：
    - 总成交 **`13` 笔**（较 Wave 20 `shadow-500-final` 的 `1` 笔提升 **13×**，且 13 笔全部为 `$225` 满额动态手数、`0` 未对冲残差、`0` 粉尘超时拖累）。
    - **`9 / 13` 笔正盈利（`69.23%` 胜率）**，累计样本外净盈利 **`+$0.044395`**，均值 **`+0.157071 bps`**，标准差 `6.504407 bps`，95% 高斯 LCB **`-3.378765 bps`（取整 `-4 bps`）**，最大回撤 `$0.493403`。
    - **逐笔毒流归因**：
      - `AERO-USD`：**`2 / 2` 正盈利**（`+5.25 bps`、`+2.99 bps`，drift `+1.98` / `+5.91 bps`）。
      - `MORPHO-USD`：**`1 / 1` 正盈利**（`+7.83 bps`，drift `+1.17 bps`）。
      - `VVV-USD`：`6 / 10` 正盈利，全部 `4` 笔亏损（`-5.03`、`-4.28`、`-5.12`、`-16.82 bps`）均集中在单边上冲行情（`drift = +13.90 / +14.78 / +13.74 / +36.73 bps`）中的连续 `ask_maker` 挂单被吃单扫穿（如 tick `23 -> 24`、tick `595 -> 596`），明确指向下一阶段优化方向：**单边成交后毒性冷却（Post-Fill Toxicity Cooldown）与按波动率分层的更高挂单门槛**。

## 3. 9 项门禁总验结论

`scripts/check_profit_engine_readiness.py --require-ready`（输出 `reports/research/wave22-20261119/readiness_report.json`，`report_sha256 = 7a3be39330775ba10a541b5354f20c7d02830b15b59a89752c16151bc3be1027`）退出码 `2`：

- **通过**：`7 / 9` 项门禁。
- **如实阻断（`2 / 9`）**：
  1. `positive_cost_adjusted_lcb_edge`（Gate 5）：`oos_trades_count = 13 >= 5` 且 `oos_total_net_profit_usd = +$0.044395 > 0`，但因 `VVV-USD` 连续上冲毒流拉低 `oos_lcb_net_edge_bps = -4 < 5`。
  2. `owner_scope_release_manifest_present`（Gate 9）：当前使用 `--rehearsal` 演练清单（`owner_scope_approved = false`），需人工在全自动门禁通过后独立签发 `--approve-software-scope`。
- `release_permitted = false`，`execution_eligible = false`。

## 4. 全量 CI 与安全门禁

- `make rust-check`：`18 passed`（`cargo fmt --check`、`clippy -D warnings`、`cargo test --locked`）。
- `make profit-deployment-check`：`passed=true`。
- `make security-check`：`pip_audit` 0 漏洞、`bandit`（`125,386 LOC`）0 中高危、`scripts.check_secrets` 通过。
- `make contract-security-check`：`slither`（97 contracts / 102 detectors）通过。
- `OASIS_FASTPATH_LIB="$PWD/crates/target/release/liboasis_core.dylib" make check`：`ruff check/format`（879 files）、`mypy src`（359 files）、`pytest`（**`6,513 passed, 138 subtests passed`**）、`check_dashboard`、`forge test`（`123 passed / 17 suites`）、`forge lint` 全数通过。
