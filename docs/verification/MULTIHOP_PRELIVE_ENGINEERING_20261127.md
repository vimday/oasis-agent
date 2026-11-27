# 多跳套利整批搜索及准实盘工程核验

2026-11-27 北京时间，代码基线 `3f844a1308d5a553e12193a8fcf15dcaafc2d24d`。本次使用保留的历史数据、合成回归、离线工具和一轮 Base 匿名只读影子。当前四链影子窗口在已覆盖范围内仍为零个自然费后正候选，下一块存续率不可估。 [四链研究总结](../strategies/MULTICHAIN_MULTIHOP_ARBITRAGE_SUMMARY.md)

## 按整批净额搜索

同一池和本金可以被不同路线顺序使用。每次扩展用上一段模型整数输出后的储备与剩余资金，在同一快照上复算，再按扣固定开销后的整批净额和持有基线排名。固定候选菜单提供路线、方向、金额、最低净额及成本。搜索保留能执行但尚未覆盖批次固定开销的前缀，也允许独跑未达到门槛的路线在前序更新后进入组合。每个入选组合中的子段都须在当时的本金及池状态下满足其门槛。结果给出净正短名单、最佳组合的删组边际、失败状态和预算覆盖。若删去一段导致余批不可行，报告阻断段及原因，边际金额留空。 [整数与资金模型](../../src/oasis_agent/fastpath/v2_batch.py)

`replay_v2_batch_comparison` 保留旧 `oasis-v2-batch-plan-v1` 回放。从同一份计划改用 `oasis-v2-batch-search-plan-v1` 并添加 `search` 字段，指定 `max_batch_orders`、`beam_width`、`max_leg_evaluations`。新结果保持 `execution_eligible: false`。搜索至多 64 个候选，每个组合不超过 512 腿，束宽不超过 128，搜索预算不超过 100,000 腿。删组复算另计入输出。束宽剪枝或预算截断时，已见最佳值只代表已评估组合。此入口使用标准 V2 全额到账、非负逐段最低净额及冻结费用情景。带税代币、混合协议、链上还款、动态纳入付款和完整 EVM 结算需另行核验。 [离线入口](../../scripts/replay_v2_batch_comparison.py)

```sh
.venv/bin/python -m scripts.replay_v2_batch_comparison SNAPSHOT SEARCH_PLAN --chain 56
```

合成回归中的逆向两跳独跑失败。先跑正向两跳后，逆向输出从同一池的新储备确定，整批净额从 111 升至 112 个锚资产最小单位。删掉正向段后余批不成立，因而其删组边际无可比较净额。另一对照验证两个各自无法覆盖整笔开销的前缀能共同产生正净额。

BSC 既有快照使用原计划的候选、金额和费用，限定三段、束宽 128、搜索 10,000 腿。四个旧计划解释结果均与留存输出相等。新结果只比较已使用过的窗口。

| 费用情景 | 组合相对持有净额，WBNB wei | 检查扩展／腿数 | 完整性 |
| --- | ---: | ---: | --- |
| 全池，乐观 | +1,345,888,911,808 | 2,564／9,999 | 束宽剪枝、预算截断 |
| 全池，费用敏感度 | 0，持有 | 40／156 | 声明范围完成 |
| 排除 FLOKI，乐观 | 0，持有 | 333／1,299 | 声明范围完成 |
| 排除 FLOKI，费用敏感度 | 0，持有 | 9／35 | 声明范围完成 |

全池乐观的最佳组合与原研究中的三段一致，相关深度和税行为没有一起进入 V2 模型。旧报告的保守首次访问成本下界下，这三个固定子段的组合仍为负。本次也逐字节复算了 Ethereum 已留存的 18 次整笔报告。其两笔分别增加 528,354,751,889,949 和 121,490,982,829,648 wei 条件净额，继续作为多协议整笔归因的历史对照。 [BSC 费用下界](BSC_V2_BATCH_COMPARISON_20261123.md) · [Ethereum 整批边际](ETH_BATCH_INCREMENTAL_NET_20261127.md)

## 状态交付与实际到账

- **Base。** 正常影子入口增加 `--base-newheads-overlap-v4` 对照臂。在核对双 HTTP 源与 WebSocket 头后，让同哈希的 PoolManager runtime 与 V4 状态请求按原间隔进入同一官方源，两项均验证方可发布。保留每源原账本、250 ms 最小间隔、固定读取上限及首拒绝停止，记录实际在途重叠。既有默认串行与旧归档复算继续可用。历史三个配对头的官方认头请求自身已比对应直接子头间隔长 51.644–68.929 ms，同源三个预留至少跨 500 ms。该对照臂适合量化边际改善，达到直接子头时限仍取决于减少同源往返或更快的独立见证。 [正常入口](../../scripts/run_multicall_100tick_campaign.py) · [此前配对时钟](BASE_INCREMENTAL_STATE_AB_20261127.md)
- **BSC。** 正常历史回放 `replay_swap_flow --transfer-splits` 能在链 56 上用 Pancake V3 的已支付 exact-input 回调和前后输入余额、下游 V2 的实际转账与逐币储备复核，恢复 17 次兑换样本的资金连边 `783 → 788`。前池输出 57,300,053,414,015,550,613，代币合约留存 1,719,001,602,420,466,518，下游到账 55,581,051,811,595,084,095 个代币最小单位。该交易的资金连边从 12 条增至 13 条，另一笔对照没有新增连边。后续 tick 深度和各方向的前瞻到账仍需独立证据。 [锁定的 Pancake V3 源码](https://github.com/pancakeswap/pancake-v3-contracts/blob/ffa4fb2cef38cf4769ff88e1cc5551c4af4f6c57/projects/v3-core/contracts/PancakeV3Pool.sol) · [回放入口](../../scripts/replay_swap_flow.py) · [原交易与资金证据](BSC_HISTORICAL_POOL_EXPANSION_20261127.md)
- **Robinhood。** 正常离线回放的 `--capacity-diagnostics` 从同哈希状态显示逐腿方向、距首个可初始化 tick 网格的含费输入，并将 Quoter 的明确流动性错误归到路内池。两组留存头上的 24 和 37 跳均定位到第四腿。另一个固定快照的第四腿到首个网格需要 689,656,662 wei，留存前缀的实际输入为 98,962,916,720,681 wei。首个网格只用于定位需要补读的深度。缺前缀时中间腿实际输入留空，完整方向容量需后续 tick 或同哈希逐腿报价。 [回放入口](../../scripts/measure_robinhood_quote_delivery.py) · [第四腿专项数据](ROBINHOOD_FOURTH_LEG_CAPACITY_20261127.json)

### Base 新窗口首轮验证

两次正常影子 tick 使用同一预注册、固定公开来源与原账本，消耗 **13/13** 个预留（官方 HTTP 6、PublicNode HTTP 4、PublicNode WebSocket 3），费额 **USD 0**。共收到 14 个连续原始头，两个派发、五个被更新覆盖、七个在范围结束前未派发。停止原因为达到预定两轮，退出 0。四池、58 池和 66 池组全部零模型正候选。 [Base 免费公开 RPC](https://docs.base.org/get-started/connect-to-base) · [PublicNode Base](https://base.publicnode.com/)

| 新头 | V4 运行码与状态在途重叠 ms | 收头到完整状态 ms | 收头到发现结束 ms | 直接子头间隔 ms |
| ---: | ---: | ---: | ---: | ---: |
| 62,961,983 | 252.562 | 1,333.850 | 1,367.574 | 208.686 |
| 62,961,989 | 341.504 | 1,200.108 | 1,232.121 | 200.433 |

两次状态均晚于直接子头，及时率 0/2。新窗口只运行重叠臂，端到端增益需用同头全量配对才可估计。原始请求、响应、完整时钟和 STOP 位于本机留存目录 `reports/research/base-v4-overlap-normal-20261127/`。正式预注册、离线复算和 STOP 的 SHA-256 分别为 `50780bc86941f6d703ab5e2f3ff60e4b52c9c3641360e0d9e7b09cc4455b03dd`、`d01bc9e86e4a375836945842af365dcad85c81b68eea59ea234d84ce8c3bc71e`、`dd2a85f99ff1d95a964721f2e3ed2074a8ca0152cff6972caab3e3c85493541f`。

```sh
.venv/bin/python -m scripts.replay_base_shadow_survival \
  reports/research/base-v4-overlap-normal-20261127/run --arrival-deadlines
```

## 实盘晋级依据

单独的 `check_multihop_readiness` 命令读取冻结计划及其独立提供的摘要、声明的单链影子样本和按内容摘要留存的本地文件。检查冻结范围、来源声明、完整成本和剩余量、发现 H、首回报头 K、直接子块 C 的绑定与时限，并分别统计自然候选、负控与留存重放。摘要校验覆盖本地文件完整性与结构化申报的一致性。独立评审还需审核原始响应和余额复算、来源独立性、冻结时间及采集覆盖。既有四链留存报告早于该输入契约，直接运行下列命令退出 2，原因为 `forward_shadow_evidence_missing`，存续项为空，发布和执行标志均为 `false`。 [评估入口](../../scripts/check_multihop_readiness.py) · [阶段政策](../governance/PRODUCTION_CONTROL_FRAMEWORK.md#5-promotion-and-capital-scaling)

```sh
.venv/bin/python -m scripts.check_multihop_readiness --require-review-thresholds
```

进入隔离 canary 评审，首先需要同一未触碰窗口中有自然正例，独立复核同包络 H/K/C、资产整数、整笔模拟及包含未纳入、失败和机会成本的保守净收益。看到正例后才计算直接子块存续、成交容量与成本敏感性。随后按单链、单范围冻结独立实时 NAV 和风险准入、允许的运行码与包络、nonce、原预算、STOP 和异常恢复，执行前还需当次明确授权。实际结算用独立收据、余额、gas 和付款对账。 [同链晋级条件](../strategies/LONG_TERM_PROFIT_ROADMAP.md#4-stage-2--same-chain-atomic-arbitrage) · [预注册实验模板](../templates/STRATEGY_EXPERIMENT.md)

## 回归与留存

| 校验 | 实际结果 |
| --- | --- |
| 定向 Python 回归 | 退出 0，118 项通过 |
| `make check` | 退出 0，6,783 项 Python 测试和 138 个子测试通过，10 条警告。前端检查和 127 项合约测试通过 |
| `make rust-check` | 退出 0，格式、Clippy 和 30 项 Rust 测试通过 |
| `make security-check` | 默认沙箱的 pip 初始化失败，退出 2。允许依赖审计访问包索引后，完整重跑退出 0，依赖审计无已知漏洞，Bandit 无中高危，仓库密钥检查通过 |
| `make contract-security-check` | 退出 0。Slither 检查 98 个合约，6 条发现低于失败阈值 |
| 留存复算 | Base 新旧状态、BSC 两份机器报告、Robinhood 两组原回放及第四腿专项、Ethereum 整批报告均逐字节一致 |
| 离线穷举对照 | 固定种子 `20261127` 的 64 组共享储备菜单共核对 4,096 个有序组合，完整搜索的净正短名单与穷举一致 |
| 文档与范围 | 本地链接和锚点、`git diff --check`、文档秘密扫描通过 |

本机留存日志、模型计划和回放结果位于 `reports/research/multihop-prelive-engineering-20261127/`。`batch-search-summary.json` 的 SHA-256 为 `f47aae243c97fc79cc847e7b00ba03d9a713092aed34726c6a04b06775573f7a`。缺少前瞻输入的准实盘验收保存在 `baseline-readiness.json`。本次变更在基线 `3f844a1` 的工作区，状态索引与 TODO 已附本验证记录。 [当前实现](../CURRENT_IMPLEMENTATION.md) · [多跳 TODO](../strategies/MULTIHOP_ARBITRAGE_TODO.md)
