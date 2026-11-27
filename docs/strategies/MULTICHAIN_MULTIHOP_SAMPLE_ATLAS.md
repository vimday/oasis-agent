# 八链多跳套利样本总览

后续已将此批次与第二分片、BSC 新区块合并，累计 118,083 个可定位交易、705 笔入册。后续又从原始收据确认了 Optimism 四笔 100 至 136 次市场兑换的跨轮复投，包含轮间解包与包装。下表保留首批冻结分母，以便复算。[最新黄金样本册](MULTIHOP_GOLDEN_CASEBOOK.md) · [完整逐笔登记](../verification/MULTIHOP_GOLD_CASE_INDEX_20261127.md)

截至 2026-11-27，按链 ID 和交易哈希去重，能够从本项目留存或本次核对的公开数据中逐笔定位 **100,572 笔已入块交易**。其中 **655 笔**进入案例表，涵盖历史个案、按规则入选的待核交易、失败交易和对照。**最强的长串行证据来自 Ethereum，一笔交易连续完成 54 个有资金依赖的动作，其中连续池交易最长 31 跳。** 另两笔公开极值分别使用固定本金做了 71 个两跳环和 24 个三跳环。三类结构给搜索器提出了不同要求。[655 笔案例表](../verification/MULTICHAIN_MULTIHOP_CASES_20261127.tsv) · [全部可定位交易，gzip TSV](../verification/MULTICHAIN_MULTIHOP_ALL_HASHES_20261127.tsv.gz) · [范围、费用、原件与复算](../verification/MULTICHAIN_SAMPLE_CATALOG_20261127.md)

## 各链到底找到了多少

| 链 | 项目原有逐笔登记 | 本轮另核收据 | 事件初筛去重 | 作者分片中的标签 | 合并去重 | 有已知网络费分项 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Ethereum | 211 | 2 | 31,274 | 5,000 | 36,342 | 99 |
| Base | 231 | 0 | 4,259 | — | 4,269 | 17 |
| BSC | 97 | 10 | 46,656 | — | 46,722 | 42 |
| Robinhood | 84 | 0 | 715 | — | 725 | 12 |
| Elysium | 14 | 0 | — | — | 14 | 1 |
| Arbitrum One | 0 | 2 | — | 5,000 | 5,000 | 2 |
| Optimism | 0 | 2 | — | 2,500 | 2,500 | 2 |
| zkSync Era | 0 | 2 | — | 5,000 | 5,000 | 2 |
| **合计** | **637** | **18** | **82,904** | **17,500** | **100,572** | **177** |

各列可能包含同一笔交易。事件筛选门槛随原窗口而异。Ethereum 的 31,274 笔来自四个完整 UTC 日的至少五事件交易；BSC、Base、Robinhood 从留存窗口提取至少两个不同兑换事件索引。五条链的原始逐笔清单还记录每个窗口的块范围、选择规则、对照和证据来源。只有合计最后一列的部分网络费可直接归一，余下成本须回到原案例核查。免费来源缺响应、未保留逐笔哈希的早期调查和仅有影子报价的路线均保留在覆盖说明中。[Ethereum](../verification/ETH_MULTIHOP_SAMPLE_INVENTORY_20261127.json) · [Base](../verification/BASE_MULTIHOP_SAMPLE_INVENTORY_20261127.json) · [BSC](../verification/BSC_MULTIHOP_SAMPLE_INVENTORY_20261127.json) · [Robinhood](../verification/ROBINHOOD_MULTIHOP_SAMPLE_INVENTORY_20261127.json) · [Elysium](../verification/ELYSIUM_MULTIHOP_SAMPLE_INVENTORY_20261127.json) · [宽筛来源](../verification/MULTIHOP_SCREENED_SOURCES_20261127.json)

## 哪些交易改变了对策略的判断

| 成交 | 从本金追到哪里 | 净额与可迁移条件 |
| --- | --- | --- |
| Ethereum，54 动作 | 从 2 个 USDC 最小单位开始，经 52 个池交易或转换、一笔限价单和一次 LP 赎回，回到 469,261 个 USDC 最小单位，之后另行换成 ETH。最长连续池交易 31 跳，整路允许重访资产。 | 只认标准 Swap 和同额邻接的通用方法曾只还原 4 跳。Bancor 中转、直付、限价单和赎回能把先前截断的资金链连接起来。单圈 USDC 差额仍要与整笔费用、其他资产和受益人对账。[逐动作资金证明](../verification/ETH_TWENTY_FLOW_20261127.md) |
| Ethereum，109 事件及其同类 | 109 样本有四组独立 WETH 借还。扣发送方 gas 和直接区块付款、假设收款合约与发送方同属一方，可见差额为 **+0.002116416817936133 ETH**。另有 153 事件样本含 72 组两池桥接，138 事件样本含 29 个可证短环。 | 106 和 100 事件交易中，分别移除一条六池路线，整批条件净额降低 0.000528354751889949 和 0.000121490982829648 ETH。其中一条单独执行会回滚。应在同一交易前状态测每组对整批的增量。[借还与付款](../verification/ETH_109_MOTIVE_20261127.md) · [删组实验](../verification/ETH_BATCH_INCREMENTAL_NET_20261127.md) · [百事件留出样本](../verification/BATCH_HUNDRED_HOLDOUT_20261127.md) |
| BSC，5+1 与 17 腿 | 前者有五跳 USDT 闭环和一跳 WBNB 变现；精确交易前五跳毛差 +0.015805 USDT，实际支路 +0.013386，下一块末同额报价 −0.005972。后者拆成 6、4、3、4 跳四个环，有一腿实收相差 3%。 | 5+1 所在整笔执行用 2,618,918 gas，历史有效费率为零，按普通费率单独执行的成本不同。17 腿的毛差全部转给两个外部地址，需分别归属。池名单和税后到账必须进入正常报价。[精确索引前后](../verification/MULTIHOP_PRETRADE_REPLAY_20261123.md) · [17 腿结算](../verification/BSC_HISTORICAL_POOL_EXPANSION_20261127.md) |
| Base，三池闭环、20 与 50 事件 | 三池从 0.500000 USDC 回到 0.500198 USDC。20 事件样本有两条跨 unlock 资金边，留下跨币种敞口。50 事件样本在两个池中分两次 unlock 做了 5 和 45 次兑换。 | 三池增加 0.000198 USDC，当时估算的已知网络费约 0.019407 USDC。单个 PoolManager 要按完整池 ID、unlock 和币种核算，保留执行费、L1 数据费及未知 operator 费。[Base 逐笔及证据指针](../verification/BASE_MULTIHOP_SAMPLE_INVENTORY_20261127.json) |
| Robinhood，四跳与 22 事件 | 一笔四池 WETH 的中间余额和整数收付对上，毛差 7,694,668,320,147 wei。另一笔 22 事件实际为用户资助的五支开放分流和八次 hook 兑换，最长经济支路 3 跳。 | 四跳毛差减收据 gas 余 **787,566,174,147 wei**，执行人与付费人不同。后者可检验把用户路由误判为套利的风险。前瞻 24 跳候选另列，第四池实际容量不足。[逐笔结算](../verification/ROBINHOOD_MULTIHOP_SAMPLE_INVENTORY_20261127.json) · [第四池容量](../verification/ROBINHOOD_FOURTH_LEG_CAPACITY_20261127.json) |
| Elysium，三池及销毁 | V4、V3、Solidly 依次成交后销毁包装币。原交易、相邻回滚及后续观察均已登记。 | 假定销毁按 1:1 支付，扣本金和收据 gas 后为 **1,093.747630693024750378 HYPE**。原生付款、期初库存和受益归属尚缺，完整净额留空。这是对大额纸面收益尤其重要的反查样本。[逐腿与缺项](../verification/ELYSIUM_MULTIHOP_SAMPLE_INVENTORY_20261127.json) |
| Optimism，142 次兑换 | 71 个两跳环，每轮由发送方提供 32 USDC.e 并收回本金，71 条中间资金边对上，最后另收 **0.698641 USDC.e**。首轮和末轮差额从 0.014987 降到 0.005050 USDC.e。 | 网络费 **0.000064884518946208 ETH**，含 L1。缺同一时点汇率和其他原生付款。每轮已体现在到账的协议费用应只计一次。[逐轮与两源收据](../verification/PUBLISHED_MULTIHOP_SETTLEMENT_20261127.md) |
| Arbitrum，72 次兑换 | 24 个三跳环，每轮复用 0.29 WETH。48 条组内资金边均有转账，23 次组间衔接均重置本金。周转量 6.96 WETH。 | 执行合约增加 0.002041225902218061 WETH；扣发送方网络费后条件差额 **+0.001224405602218061 ETH**。最后一轮毛差仅约首轮的 5%，尚缺它的增量 gas。[逐轮与费用](../verification/PUBLISHED_MULTIHOP_SETTLEMENT_20261127.md) |
| zkSync Era，12 次与五次兑换 | 12 次兑换为三组四跳，每轮从 145 USDC 开始。沿共享 vault、执行合约和返回 vault 的实际转账，可接上九条中间资金边。另一笔五跳结束时执行合约多出约 0.000799373 WETH。 | 前者执行合约增加 71.326107 USDC，发送方支付 0.002211157 ETH。后者扣收据费后条件差额 **−0.000298190990772485 ETH**；系统代币预缴和退款可核对同一笔费。[共享托管与负收益对照](../verification/PUBLISHED_MULTIHOP_SETTLEMENT_20261127.md) |

## 公开研究能回答什么

从 CCS 2024 论文 *Rolling in the Shadows* 作者固定版本的数据中，每链取第一个编号分片。17,500 个公开标签里，有 166 笔达到 20 个不同兑换日志索引，35 笔达到 50 个，6 笔达到 100 个，200 个为零。在作者已经划分的单组内，要求起末同币、相邻输入输出币和整数金额全都相同，四链各自的最大值为 Ethereum 6、Arbitrum 7、Optimism 7、zkSync 5。这个筛法会漏掉跨 vault、税后到账、赎回和协议内净结算。它衡量公开标签的结构，单凭该筛法无法推断市场最长路线或成交胜率。[论文](https://arxiv.org/abs/2405.00138) · [作者数据固定版本](https://github.com/christoftorres/Rolling-in-the-Shadows/tree/79d31e0a07b702b5eabb849ac5d513863cad8bb3/data/mev/arbitrage) · [选样和去重实现](../verification/fixtures/replay_published_multihop_shards.py)

每链各取事件最多及单组精确闭合最深的交易，人工与程序共核对八笔、263 个不同兑换事件。两笔 Optimism 有两源收据，另六笔只有一源有效收据；Arbitrum、Optimism、zkSync 另有含对应交易位置的块头数据。八笔的交易级原生付款、完整持仓和主体控制仍需独立证明。跨币种的差额保留原币。成功交易样本亦不能给出失败尝试的成本分布。[八笔机器证据](../verification/PUBLISHED_MULTIHOP_RECEIPTS_20261127.json) · [资金去向与费用](../verification/PUBLISHED_MULTIHOP_SETTLEMENT_20261127.md)

## Oasis 应按什么收益目标搜索

1. **同一笔里重复做几轮才划算。** 对上述 71 轮、24 轮和三轮案例，冻结原索引前状态、固定每轮本金，按真实顺序回放前 1 至 N 轮，逐轮记录结算币差额、增量 gas、融资及付款。历史收据能给出每轮收入和全笔 gas，边际 gas 仍待实测。线上用共享池更新后的整批绝对净额排名，检验最优截断点随状态变化的程度。[已有整批和删组实现](../verification/MULTIHOP_PRELIVE_ENGINEERING_20261127.md)
2. **同时搜索长串行和短环组合。** 给交易分别记录交换事件数、已证连续池交易数、跨协议闭合动作数、独立还款组数。按大额实际到账、变池、贷款还款和结算币召回，抽查低事件窗口、失败交易和普通用户路由。将 54 动作、重置本金、共享 vault、税后到账留作不同的召回测试；独立日期、执行器和池族检验误判与漏判。[历史资金证明](../verification/ETH_TWENTY_FLOW_20261127.md) · [公开收据对照](../verification/PUBLISHED_MULTIHOP_SETTLEMENT_20261127.md)
3. **按真钱能留下多少衡量速度。** 发现 H 时冻结本金、顺序、账户和整笔参数；记录独立订阅见证的直接子块 C 和 H 整笔返回后取到的最新头 K，三态按同一结算币报净额。逐链保留费用缺项、回滚与超时。Base 持续保留四池对照，BSC 优先补未定价的集中流动性区间，Robinhood 单独记录 sequencer 到达与状态可报价的时间差。[窗口与时序修正](../verification/MULTICHAIN_SAMPLE_CATALOG_20261127.md) · [四链对照](MULTICHAIN_MULTIHOP_ARBITRAGE_SUMMARY.md)
4. **计算和数据采购用同一验收单位。** 先用免费数据、同状态可复算的 CPU 基线，计一定时限内交付的、可验证的增量净额。4070 Ti 可对相同变池图并行扩展路径或评估本金网格，计入编码、传输、同步和 CPU 整数复核时间。Rust 迁移只选端到端剖析显示的瓶颈。付费 RPC 的经济门槛是未触碰窗口中新增可兑现净额覆盖累计成本；当前免费观测已足够完成历史识别和负控复算，付费带来的增量仍待测。[工程和晋级 TODO](MULTIHOP_ARBITRAGE_TODO.md)
