# Metric 与事件型套利：下一阶段研究目标

日期：2026-11-14（Asia/Shanghai）。状态：研究设计及公开提供者观察；没有执行授权或净盈利证明。
本记录扩展[长期路线](LONG_TERM_PROFIT_ROADMAP.md)，保持 Robinhood LP 基准、Base 有界验收、
Base 现货 shadow 的既有边界。[本轮验证](../verification/METRIC_RESEARCH_20261114.md)记录实际结果。

## 1. 决策与优先级

下一阶段的目标是证明系统能在机会消失前取得**完整、可执行、扣成本的闭环证据**。
新闻、价差、赢家收据和高 APR 都是研究入口，不能单独作为出资依据。

| 顺序 | 研究对象 | 支付收益的一方与机制 | 下一项可验收交付 |
| --- | --- | --- | --- |
| P0 | 已有 Elysium 同链异常交换 | 不平衡池让渡价格，竞争者争夺恢复价格的利润 | 完成赢家精确 prestate；冻结前瞻窗口，对比事件触发与全扫的覆盖、延迟、成本 |
| P0 | Metric 与同链池的报价差 | 不同参考价、库存偏斜、更新及路由使可成交价格分离 | 本轮提供 Metric 深度采集和同资产双池离线筛选；随后接入独立 DEX 及精确整笔模拟 |
| P1 | Metric 公共池 LP / 自有报价策略 | 真实 taker 支付 spread，LP 承担库存及逆向选择 | 先证明外部存取权限和自然路由；比较独立定价的 NAV、持币、被动 LP 与更新成本 |
| P1 | 事件导致的借贷与现货错价 | 借贷库存提供短期融资，两个实际市场间存在可成交价差 | 先做债务、抵押、cap、HF、退出的场景回放；不实现借贷执行器 |
| P2 | 包装 / LST / 稳定币转换、清算 | 可执行兑换、赎回或协议清算奖励 | 固定转换率、队列、cap、权限和结算语义；同链原子可闭环者优先 |
| P2 | CEX–DEX、perp basis/funding | 资金和库存分割、风险溢价 | 预置库存、独立头寸与非原子对冲状态机先于下单 |
| P3 | 跨链、新链流动性迁移、权益资产 | 转移摩擦、市场时段和不等价资产风险 | 桥和赎回延迟不能计作原子套利；最后推进 |

上述顺序是研究资源建议，不调整资本上限，也不把任何研究提升为 canary。
并行研究多条 EVM 链上的同链机会；每条链分别通过状态、规模、失败到净结算的验收。
这里的 P3 是依赖桥/转账的跨链策略，不是限制 Ethereum、Base、Robinhood、Elysium 的研究。
四链发现与借贷压力核算已加入[后续实验](MULTICHAIN_LENDING_EXPERIMENT.md)。

## 2. Metric 是否存在套利空间

存在值得检验的机制，当前没有足够证据判断可持续净利润。Metric 的 active pool 把外部
Price Provider 的 bid/ask/reference 与流动性 bins 分开；成交还受库存偏离、深度和费用影响。
因此不能照搬常数乘积池公式，也不能把 oracle reference 当成可成交价。
[工作方式](https://docs.metric.xyz/start-here/how-metric-works)
[流动性与 bins](https://docs.metric.xyz/how-pools-work/liquidity-and-bins)

可检验的三类策略：

1. **Taker 同链闭环。** 用 Metric 与独立 DEX 的完整报价比较两个方向、多个规模，
   所有腿必须回到同一资产。扣除 gas、builder、融资、失败与机会成本；同一笔金额不得
   同时在多个替代路径上占用。薄深度、滑点和第三方竞争会使显示价差消失。
2. **库存调整与报价更新后的短窗口。** 监听成交、流动性和报价更新，以区块哈希隔离缓存。
   同一波行情相关的几十个报价不能算几十个独立收益样本。禁止操纵 oracle 或制造异常价格。
3. **Maker / LP。** 研究更可靠的参考价、库存偏斜与路由质量是否带来净超额收益。
   优势必须来自更好的做市，而不是把迟到报价损失漏记；公开存入和可撤出需单独证明。
[价格提供者](https://docs.metric.xyz/how-pools-work/price-providers)
[路由质量](https://docs.metric.xyz/run-a-pool/routing-quality)

**最关键的反证是交易排序。** Ethereum 的参与 builder 可以在 taker 交易前放入最新 maker
更新；这属于 builder 服务承诺，不是全链强制规则。链上旧报价可能过时甚至不能成交。
BSC 的保护通道和 Robinhood 到达顺序也不能互换。文档未在该排序表中定义 Base/Elysium
的全部行为，所以不能自行复制 Ethereum 的保证。研究需分别记录链上报价、最新流式报价、
带更新状态的模拟，以及实际包含后的结果。高 priority fee 不自动战胜报价更新。
[鲜度与排序](https://docs.metric.xyz/how-pools-work/freshness-and-ordering)
[Titan 集成](https://docs.metric.xyz/integrate/integrate-metric-liquidity/via-titan-propamm-streams)
[Titan 原始接口说明](https://docs.titanbuilder.xyz/propamms/takers)

## 3. 适配器必须识别的陷阱

| 输入或行为 | 正确解释与本轮处理 |
| --- | --- |
| `healthy`、HTTP 200 | 不代表两个方向均可成交；bid=0、ask=2^128−1 是禁用哨兵。缺 depth、错误或缺池保留为不可评估。 |
| `serverTs` | 仅提供者快照时间，不是底层 feed 的更新时间、区块身份或独立首次观察证明。 |
| Q64.64 价格 | token1 / 整个 token0；不可直接乘以原始 token0 余额。深度的输入与输出已经是各自资产最小单位。 |
| cumulative depth | 累计输入、累计输出分别差分。重复空 bin 不增加流动性；不完整成交不能接入闭环。 |
| spread / deviation / pool fees | 已反映在深度报价，不能再对 adjusted bid/ask 重复加价。增量成本须单列。 |
| extensions | 能拒绝某个方向、调用者、recipient 或上下文；深度估算不能证明真实 caller 可成交。 |
| quoter | 单跳 exact-input 返回实际用掉的输入，可能小于请求；必须检查两项 delta。非 view ABI 仍仅用 `eth_call`。 |
| pause | 当前 Live quoter 检查 pause，hypothetical 不检查；旧 quoter/SDK 行为不同，不能只看名称。 |
| 同一确定性地址 | 文档列地址不是跨链字节码、proxy/admin 或运行配置一致的证明。 |

[API 深度和哨兵](https://docs.metric.xyz/reference/api/retrieve-current-bid-and-ask)
[深度内成交](https://docs.metric.xyz/integrate/integrate-metric-liquidity/estimate-a-fill-from-depth)
[Extensions](https://docs.metric.xyz/how-pools-work/extensions-and-permissions)
[Quoter](https://docs.metric.xyz/reference/smart-contracts/get-quote)
[SDK 版本限制](https://docs.metric.xyz/reference/sdk)

源码研究固定到：

- pool-contracts `ddf8ae0cdb2ad0041c3ff3aebc200cf488d5b0d3`：
  [swap 事件、scaled 与 native 单位](https://github.com/Metricxyz/pool-contracts/blob/ddf8ae0cdb2ad0041c3ff3aebc200cf488d5b0d3/contracts/interfaces/IMetricOmmPool/IMetricOmmPoolActions.sol)。
- periphery-contracts `5ceaec32a4a8ceb5fe1411ec1bc5d5f9aef32647`：
  [quoter ABI、实际输入及 sender 语义](https://github.com/Metricxyz/periphery-contracts/blob/5ceaec32a4a8ceb5fe1411ec1bc5d5f9aef32647/contracts/interfaces/IMetricOmmSwapQuoter.sol)、
  [Live pause 检查及模拟实现](https://github.com/Metricxyz/periphery-contracts/blob/5ceaec32a4a8ceb5fe1411ec1bc5d5f9aef32647/contracts/lens/MetricOmmSwapQuoter.sol)。

对 quoter 应传入真正调用 pool.swap 的 router/executor 作为 sender，不直接把钱包地址当作
合约 caller。显式 recipient/extensionData 的重载更适合完整模拟。Live 查询省略 eth_call
的 from 以满足受限 provider 的零 origin 模拟语义，随后整笔交易仍需真实 from 验证。
本轮没有接入 quoter、状态覆盖或真实交易构造；以上是下一步的接口约束。

审计报告和公开 tag 的关系由官方 VERIFY 记录说明，不应直接把 tag 名当作原始审计 commit。
本轮未复现审计证明、编译部署或验证链上 runtime。
[审计和范围](https://docs.metric.xyz/reference/security-and-audits)
[部署目录](https://docs.metric.xyz/reference/supported-chains-and-deployments)

## 4. Aave 与事件型价差的闭环条件

**2026-11-14 纠正：** 用户指的是 2026-10-29 Binance 热钱包事件，已由
[币安官方公告](https://www.binance.com/en-AU/support/announcement/detail/d3306d9dca5243548c813b605e1d1751)
及 [Aave 风险响应](https://governance.aave.com/t/binance-hot-wallet-exploit-aaves-response/25954/1)
确认。前一轮查证不充分，关联 Bybit 的说明不适用；原记录保留并链接
[后续证据与纠错](../verification/MULTICHAIN_LENDING_20261114.md)。用户给出的
[地址](https://etherscan.io/address/0x0cdd9cbb825212c6cc5b0b36239e94218ee661d8)
也是 Bitquery 当晚卖方分析中的地址，但其价差归因不是完整偿债后 NAV 的证明。

Aave 不是按 CEX 价格出售 ETH 的现货交易所。特定市场的 `AaveOracle.getSourceOfAsset`
决定资产的价格源；版本、feed/adapter、base currency、精度、更新时间和风险参数必须
在目标区块查证。用户描述的机制有研究依据，但更准确的表述是“借贷 oracle 估值与
DEX 可执行卖价分离”。Chainlink 的 crypto price 数据可来自多个聚合商的 CEX 和/或 DEX
数据，并不保证恒等于某个 CEX 价格。
[Aave V3 Oracle](https://aave.com/docs/aave-v3/smart-contracts/oracles)
[Chainlink 数据源](https://docs.chain.link/data-feeds/data-sources)

假设链上 ETH 卖价高于其他可成交市场：

- 存稳定币、借 ETH、在 DEX 卖 ETH，可先获得较多稳定币，但仍有 ETH 债务。
- 真正闭环需要买回足够 ETH 并还本付息；低价腿在 CEX 时，有提现、结算和库存风险。
- 存 ETH、借稳定币、再买贵 ETH，不会自动实现相同价差。
- 重复抵押借贷扩大的是总债务、抵押和退出需求。不能把新增现金或借款记作利润。
- 抵押物贬值、ETH oracle 上调、stablecoin 脱锚、借款利用率飙升、cap 耗尽、暂停、
  拥堵或平仓失败均可能使原计划失效。
[Aave 借贷约束](https://aave.com/faq)
[Aave 健康因子](https://aave.com/help/borrowing/liquidations)

一个说明问题的简化推导：设抵押稳定币新增额为 C，LTV 为 L，oracle ETH 价为 O，
DEX 出售价为 D，忽略所有成本。最多新增借款 L*C/O ETH，卖出后可新增抵押
`C_next = L*(D/O)*C`。循环比例低于 1 时容量是有限几何级数；比例达到或超过 1
时也不代表无限利润，因为真实 cap、库存、市场冲击、oracle 更新及 HF 会约束过程。
按相同 D 买回债务时，卖出得到的钱基本都用于还债，净额只会被成本进一步减少。
只有独立的便宜买回或已锁定兑换腿才能产生待验证价差。这个推导是压力场景，不是 Aave
精确执行模型；它不覆盖 eMode、isolation、利息、舍入及其他账户余额。

## 5. 已有 Elysium 研究怎样变成机会捕获能力

[原验证记录](../verification/ELYSIUM_CYCLE_RESEARCH_20261111.md)已经区分误操作收据、赢家
收据、父块末单源调用和未知的赢家精确 prestate。保留这个区别。历史赢家能赚到，不证明
我们当时能发现、排序获胜或以同样成本执行。

下一次采集应在事件发生前冻结池、资产、规模网格和采集窗口。订阅只负责唤醒，HTTP 重叠
范围负责补齐；断线、重组、缺日志、provider 分歧导致冻结候选。机会记录至少包括：
事件时间、接收时间、第一次完整状态、报价/模拟耗时、过期原因、失败次数、每源请求用量、
竞争后仍有利润的规模和可退出深度。覆盖率的分母包括空窗、失败、无机会及遗漏事件。

Metric 的外部报价更新也需要唤醒逻辑，只监听 Swap/LP 事件会漏掉价格变化。
新池索引仍先完成 V3 factory 与 Solidly 身份，再把 Metric factory 的事件与 metadata 对照；
不能让网页发现自动扩大允许交易的资产或调用目标。

## 6. 冻结的本轮实验

- ID：`metric-depth-screen-20261114`；所有者：项目 owner；实现与自审：Codex（无独立审阅者）。
- 基线：任务起始 `09fec54`；源码/计划/响应摘要由 replay 输出，实际提交见 Git 历史。
- 机制：同链、同一有序 ERC20 对的两个不同 Metric 池，按完整深度比较有向闭环。
- 资产：示例 Ethereum WETH(18)/USDC(6)，规模 0.001、0.01、0.05 WETH；只是报价试验。
- 资本、已有承诺、额外预留和五类成本均未知，因此真实样本不得给净收益结论。
- 数据：一次定向 metadata GET，加一次 depth=true batch POST；不分页、不重试、不接入 RPC。
  先写冻结计划及 request，再写原始响应 body；HTTP/传输/资源限制失败停止并保留。
- 鲜度：serverTs 到接收最多 15 秒，两个报价最多相差 2 秒，总采集 30 秒。
  这是快照一致性检查，不是底层 feed 鲜度证明。HTTP 单次读超时 10 秒，流式读取额外检查总期限；
  阻塞 read 最多到其 timeout 才返回，超期结果不能通过离线验收。
- 成交：每 bin 取比例输出向下舍入，费用与冲击已在深度；部分成交清除候选。
  多腿是独立快照的条件估算，不是联合执行模拟；extensions 跨池共享状态也可能导致结果不同。
- 比较：所有有向池对 × 所有规模；同一批的候选互斥，不累计为策略收益。
- 回归门槛：错误身份、方向、鲜度、缺深度、成本、资金或篡改不能产生正净候选；整数守恒、
  对称方向、边界、失败停机、重复输出与确定性回放必须通过。
- 切分：公开元数据是 discovery；合成夹具是 engineering validation；本轮单次 API 快照也是
  integration observation。没有 calibration、walk-forward 或 untouched final economic window。
- 盈利晋级：不做；最终经济实验必须另行冻结净超额收益、置信区间、尾损、样本数、容量和 gas
  阈值，再获取 untouched 窗口。不能用当前探索结果反向选择通过门槛。

## 7. 运行与后续验收

```sh
# 完全离线：建立合成 capture 并回放；目标目录/文件必须不存在。
.venv/bin/python -m tests.fixtures.metric_research /tmp/oasis-metric-synthetic
.venv/bin/python -m scripts.research_metric replay \
  --capture /tmp/oasis-metric-synthetic --output /tmp/oasis-metric-synthetic-report.json

# 明确选择公开数据采集；仅两个无需认证的 HTTP 只读请求，不发送 RPC/签名/交易。
.venv/bin/python -m scripts.research_metric capture \
  --plan configs/metric-research.ethereum.example.json --out /tmp/oasis-metric-public
.venv/bin/python -m scripts.research_metric replay \
  --capture /tmp/oasis-metric-public --output /tmp/oasis-metric-public-report.json
```

退出 0 表示操作完成；capture 退出 3 表示保留了失败尝试，停止本次运行；退出 2 表示
输入、完整性或输出拒绝。回放拒绝不删除先前报告，新旧输出用不同文件保存并按 capture
身份使用，不能把旧报告当当前状态。示例地址会变化，需重新发现核验并冻结新计划。

后续按以下交付推进，不先扩充执行权限：

1. **身份与精确模拟。** 双源链 ID/区块、pool/factory/quoter/provider/extensions 代码和配置；
   caller/recipient/extensionData；完整两腿交易模拟。Ethereum 还要保留更新内容、顺序和
   state override 来源，拒绝不受约束的覆盖。完成前只显示 provider estimate。
2. **前瞻事件试验。** 冻结共同窗口，比较轮询/事件/更新流的覆盖、p50/p95/p99 延迟、
   每完整候选请求数和消失率；预算在原账本预留，失败与无结果不丢弃。
3. **经济与部署决策。** 用独立价格衡量净 NAV、持币/无交易/被动 LP 对照、失败成本、
   容量和尾损；完整风险、原子执行、结算及恢复通过后才准备单独 canary 提案。

Metric LP 的 markout 是固定 horizon 的交易质量，不是完整 NAV；小 traded-bin 分母可使
年化很高，缺价格样本也会影响覆盖。需要独立参考价和完整库存路径。积分不计收入。
[收益指标](https://docs.metric.xyz/use-metric/provide-and-manage-liquidity/understanding-your-returns)
