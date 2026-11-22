# Ethereum、BSC、Robinhood 与 Base 多跳套利发现

2026-11-22。**发现候选池和资金流可以完全绕开链节点；淘汰一部分假价差可以用免费公共 RPC；能否按时兑现，要另测数据新鲜度、整笔成本和交易排序。** 四条链各自寻找同链原子路径，不要求跨桥。以下数字分为市场索引、已确认链状态、历史成交和供应商文档四种口径。[可核对的样本与原始响应摘要](../verification/MULTICHAIN_DISCOVERY_20261122.json) · [Oasis 实施 TODO](MULTIHOP_ARBITRAGE_TODO.md)

后续已完成两条链的免费前瞻搜索和 Robinhood 实际 feed 接入；三条链各自的样本数、费用门槛和跨块复核见[前瞻实验](../verification/MULTICHAIN_FORWARD_20261123.md)。

## 一、从两个锚定币生成三币环，不调用 RPC

北京时间 23:41 和 23:45，分别对四条链的两个指定代币读取一次 DEX Screener 的公共 token-pairs 接口。8 个无密钥 HTTP GET 均成功。每次返回 30 行。在同一链按规范化池标识去重，重复项须具有一致的 DEX 标签和无序代币地址对，池筛选用较晚返回的记录。找三个不同代币，由三条边各选一个池；起点旋转和反方向各算同一组。结果受供应商选池及两次采样的时间差影响。[数据接口](https://docs.dexscreener.com/api/reference)

| 链与两个查询锚 | 唯一池条目 | 不同三币组合 | 可以拼成的三池组合 | 三腿均达到示例筛选后的组合 |
| --- | ---: | ---: | ---: | ---: |
| Ethereum · WETH、USDC | 60 | 2 | 4,104 | 1,400 |
| BSC · WBNB、USDT | 60 | 1 | 3,703 | 126 |
| Robinhood · USDG、PONS | 56 | 1 | 10 | 0 |
| Base · WETH、USDC | 60 | 1 | 2,028 | 0 |

示例筛选要求每个池的供应商流动性估值至少 10 万美元，且过去 24 小时成交额至少 1 万美元，用来观察结果对选池的敏感性。BSC 的 3,703 来源于 USDT/USDC、USDC/WBNB、USDT/WBNB 各有 23、7、23 个可选池。筛选后分别为 7、2、9。Robinhood 返回集的三币组合为 PONS、USDG、ORBIO。某条边的估值足以淘汰整组。每链仅查两个锚，遗漏的池、其他锚、其他 DEX 及更长路线都可能改变排序。表格不衡量全链流动性、机会数或胜率。[逐边数量、代币地址与样例池](../verification/MULTICHAIN_DISCOVERY_20261122.json)

Robinhood 第一轮 USDG 查询单独就返回了 4 个 PONS/USDG 条目，来自 Ramses、Aero 和两个 Uniswap V4 池，供应商流动性显示约 16.9 万至 31.1 万美元。过去仓库核到的特定 PONS/USDG PoolId 在固定块直接报价 0/12，其池 ID 与这四个索引条目不同。研究应保留同币对的多个池，逐池绑定 factory 或 PoolManager/PoolId、hook、方向与实际成交深度。[历史固定块核验](ROBINHOOD_V4_AUTOLP_BLUEPRINT.md) · [Robinhood 官方部署](https://developers.uniswap.org/docs/protocols/v4/deployments#robinhood-chain-4663) · [Ramses 部署](https://docs.ramses.exchange/pages/contract-addresses)

## 二、免费 BSC 节点怎样淘汰屏幕上的正价差

上述第一轮索引中，同一交易对 WBNB/USDT 在 Pancake V2 和 V3 显示 557.7176 和 556.7570 USDT/WBNB。直接相除得到约 **17.25 bp**。两分钟后，从 BNB Chain 官方无密钥端点读一次 finalized 块头，接着按该哈希、带 requireCanonical 发起一次 Multicall3 的 eth_call。两次 RPC 均 HTTP 200，零付费账户、无提交。子调用核对 chain ID 56、两池代币顺序、两家 factory 返回的池地址、V2 储备、V3 slot0/费率/活跃流动性。单来源只读对照使用区块 **135,105,061**，哈希为 `0xb1e8564ba25cf26d38c495d1618c7368b89825a928433cd24e0d6fd4d8b45e60`。[官方公共端点](https://docs.bnbchain.org/bnb-smart-chain/developers/json_rpc/json-rpc-endpoint/) · [V2 费率](https://docs.pancakeswap.finance/earn/pancakeswap-pools) · [V3 工厂](https://developer.pancakeswap.finance/contracts/v3/addresses)

| 同块双池，固定从 token0 USDT 出发 | 扣两腿池费后的边际回报上界 |
| --- | ---: |
| V2 买 WBNB，V3 卖回 USDT | −41.8450 bp |
| V3 买 WBNB，V2 卖回 USDT | −10.1248 bp |

同块最佳方向的费前边际差为 **+15.9141 bp**。以原始单位计算，令 `p2 = reserve1 / reserve0`、`p3 = sqrtPriceX96² / 2¹⁹²`。两方向的毛比率分别为 `p2/p3`、`p3/p2`，都要乘 V2 的 `9975/10000` 和该 V3 池的 `999900/1000000`。裁决用整数分数，表中展示才舍入。对于这两个标准池，同方向持续交换的边际汇率只会变差，整除也无法增加输出。因此，在该状态和普通交换规则下，两个方向在计 gas 前已无法盈利。换池、额外补贴、未观测的状态变动或其他区块需要重新计算；索引与节点样本也不是同一时刻。这次筛掉路线无需 archive、trace、逐个尝试投入或 GPU。[储备、平方根价格、响应摘要](../verification/MULTICHAIN_DISCOVERY_20261122.json) · [固定区块](https://bscscan.com/block/135105061)

## 三、用免费历史数据区分赢家、普通路由和误报

**Ethereum 有完整可识别的拆分合流案例。** 从 Blockscout 无密钥 REST 取得区块 20,217,499、交易索引 31 的成功交易及全部 19 条日志。7 个池的 Swap 构成三个两跳分支加共享出口，资金流最长依赖深度为 3。执行器支出 0.057490540262399745 WETH，收回 0.059797646646384764 WETH；毛差 0.002307106383985019 WETH。回执 gas 为 484,990，实际单价 4,642,876,251 wei，消耗 0.002251748552972490 ETH，约占毛差 **97.60%**。按 WETH/ETH 等值合并、只扣这笔执行费，余额为 **0.000055357831012529 ETH 等值**。执行器与 gas 付款方应分别记账；其他直接付款和资金成本尚未做独立核实。公开索引能提供历史训练标签，未来执行时的可得排序仍需前瞻观察。[交易](https://etherscan.io/tx/0x275f9642556a58802eafdd8289aa19a7275e9d40962056095bb9b5c51ac3d246) · [原始日志接口](https://eth.blockscout.com/api/v2/transactions/0x275f9642556a58802eafdd8289aa19a7275e9d40962056095bb9b5c51ac3d246/logs) · [论文图 2](https://arxiv.org/html/2608.20377v2#S3.SS5)

**负对照同样要保留。** Ethereum 区块 24,720,714 的另一个案例有 70 条日志，覆盖多次兑换，发送者获得 19,211.992442 USDC。其资金流包含收益收割和平仓。只按多个 Swap 加某一地址有正余额来生成套利标签，就会把这笔分配算入盈利样本。分页的后 20 条日志和中间资产来源都会影响判断。[对照交易](https://etherscan.io/tx/0x5d44c672cce36070dae0419d4cd2852f05bac823637e2eb58e2c403847c080d9) · [作者归因](https://arxiv.org/html/2608.20377v2#A6.SS3)

**BSC 的研究对象包括有排序优势的竞争者。** 研究者从两家 builder 的历史交易标注约 963 万个环，常见路由资产为 USDT、USD1、USDC、WBNB。这个样本支持从稳定币和 WBNB 的 2–3 跳开始；它也表明同一价差的竞争者可能本身就参与出块。研究中由 builder 执行的机会不能原样计入 Oasis 的可得收益。[样本构造与路径分布](https://arxiv.org/html/2602.15395v2#S3.SS3)

**在预先选定的一个历史 BSC 区块发现一条四跳线索。** 先固定区块 61,000,000，再通过 SQD 免费公开索引取该块日志。832 条日志，关联到 114 笔有日志的交易。宽筛要求多次 Swap 且某个非池地址 WBNB 有正差额，得到两笔，其中一笔只剩 1 wei。再要求有正投入且可还原的兑换链从 WBNB 出发、最终回到 WBNB，留下交易索引 4。最初的 V2/UniV3 过滤仅命中两腿；补入 Pancake V3 扩展 Swap 签名后，日志索引 28、32、36、39 串成 WBNB → 三种中间代币 → WBNB 的四池路线。WBNB 返回毛差 23,888,063,753,725 wei，随后整额解包。回执记 494,718 gas × 100 wei = 49,471,800 wei。扣此项后还有 23,888,014,281,925 wei BNB 等值，第一次取得的全块 trace 缺交易索引；补取 transactionIndex 与 traceAddress 后，哈希与交易索引均吻合，62 个调用帧无报错。解包所得全额由执行器转出，经两个地址后，向 `0x956e…88f0` 和 `0xffff…fffe` 各转 11,944,031,876,862 wei，中间地址剩 1 wei。执行器留存的该笔原生币增量为零。收款方与交易发起人的控制关系仍未知，所以不能把仅扣 gas 的余额全记为搜索者收入。此区块是单个历史样本，四跳是事后发现；不能由此估算实时赚钱概率。[BSC 交易](https://bscscan.com/tx/0xc443a3242897a18e1293eed35987a52c3a8d5651166a53c8fd8a47d3658cb749) · [BSC 历史索引与字段](https://docs.sqd.dev/en/data/evm/binance-mainnet)

**Robinhood 的免费收据纠正了长交易的池数。** 在官方公共端点做 8 次零费用只读调用，前 7 次取得 chain ID、三笔成功收据及对应高度的区块并核对交易索引。最后一次 callTracer 请求返回 JSON-RPC `-32601`，已停止。另一次无密钥的 Blockscout 站点 REST 探测得到 403，统一 API 的 trace 入口得到 402。不同入口能回答的问题不同。[公共端点与 feed](https://docs.robinhood.com/chain/connecting/) · [Blockscout 服务说明](https://github.com/blockscout/docs/blob/main/robinhood-api.mdx)

| Robinhood 交易 | 块号 / 交易索引 | 匹配 Swap（V3/V4） | 不同日志地址 | 按地址及 V4 PoolId 的不同池标识 |
| --- | --- | --- | ---: | ---: |
| [21 Swap](https://robinhoodchain.blockscout.com/tx/0x57110d3c1a28867544969054504e0abb875060908a6780dd29534faf0895d7e2) | 120,369,170 / 1 | 21（18/3） | 6 | 6 |
| [22 Swap，EntryPoint](https://robinhoodchain.blockscout.com/tx/0x98a5143fcf86defcb660b9548f8eea52011856a24526fa2d2564864cc604362a) | 120,354,773 / 3 | 22（3/19） | 4 | 7 |
| [另一笔 22 Swap](https://robinhoodchain.blockscout.com/tx/0xa96d1eda66c013d77e2d89bcb623be3275fb38bfbba748ccac535ae6cb4b6a88) | 120,369,109 / 1 | 22（13/9） | 3 | 6 |

旧记录的“22/4”和“22/3”第二个数字数的是事件发出地址；V4 多池共享 manager。第二笔的 22 条 Swap 均位于 ERC-4337 EntryPoint v0.7 的 BeforeExecution 和一个成功 UserOperationEvent 之间，事件的 actualGasCost 为零，外层交易实际付出约 0.00008081468238 ETH。第一和第三笔共享外层发件人与目标。上述结果支持更细的交易分类；没有完整调用帧和余额对账，仍不能认定是 22 跳简单环或锁定最终受益人。收据中三笔 gasUsedForL1 均为零；Robinhood 的收据费用遵循 Nitro 口径，不能再把同一 L1 成本扣第二次。[EntryPoint v0.7](https://github.com/eth-infinitism/account-abstraction/releases/tag/v0.7.0) · [Robinhood 费用说明](https://docs.robinhood.com/chain/gas-and-fees/)

## 四、四条链分别去哪里拿数据

| 链 | 不付 RPC 订阅可以做什么 | 需要单独验证的缺口 |
| --- | --- | --- |
| Ethereum · 1 | 历史普查可用 AWS 公共 Parquet 的 logs、transactions 和 traces，或浏览器 REST。实时用公共头/日志提示、HTTP 回补；自建执行客户端配合共识客户端接收公开区块。公开 MEV-Share 事件也可当另一类行情，须标注发送方选择披露的 hints。 | 公共 mempool 视野不完整，私有订单流、builder 排序及直接付款会改变收益。完整历史节点也只能重建已揭示的历史。 [AWS 数据目录](https://registry.opendata.aws/aws-public-blockchain/) · [Geth 订阅](https://geth.ethereum.org/docs/interacting-with-geth/rpc/pubsub) · [Flashbots 事件流](https://github.com/flashbots/mev-share) |
| BSC · 56 | 官方免费节点能做上述固定块读数。官方主网种子禁用 eth_getLogs，可用确实支持该方法的第三方公共节点、按区块取 receipts 再在本地过滤，或自建节点订阅交易回执。Pancake V3 的 Swap 比 UniV3 多两个 protocolFees 字段，topic 不同。 | 每个端点逐项测试批量回执、历史范围和重组回补。AWS 的 BNB 前缀本次只列出 blocks/transactions，直接做 Swap 普查还需日志来源。链上手续费以 BNB 计，真实 builder 付款另核。 [官方节点限制](https://docs.bnbchain.org/bnb-smart-chain/developers/json_rpc/json-rpc-endpoint/) · [公共回执接口](https://drpc.org/docs/bsc-api/blocksinfo/eth_getBlockReceipts) · [协议事件适配](https://github.com/DefiLlama/dimension-adapters/blob/master/dexs/pancakeswap-v3.ts) |
| Robinhood · 4663 | 官方提供限流公共 HTTP RPC；Nitro sequencer feed 可把**已排序**的消息交给本地节点执行。约慢 500 ms 的备份 feed 用于恢复。用 PoolManager/PoolId、Ramses、Pancake 等多协议目录选池。 | feed 不是通用 JSON-RPC WSS，也不显示全部待排序订单；同运营方的备份不能充任独立价格源。官方节点指南需要 L1 执行及 beacon/blob 数据、NVMe 和内存，公开快照为裁剪状态。tokenized stocks 的做市权限、交易时段和公司行动会影响跨市场估值。 [连接与 feed](https://docs.robinhood.com/chain/connecting/) · [自建节点](https://docs.robinhood.com/chain/run-a-full-node/) · [股票代币机制](https://docs.robinhood.com/chain/stock-tokens/) |
| Base · 8453 | 沿用已验证的原始状态采集；公共 RPC 或公开历史 Parquet 可作为基线。前瞻研究记录真实收到状态的时钟。 | 官方公共端点没有 debug、trace、txpool。完整成本包括 L2 执行、适用的 L1 数据与 operator fee；既有三块同模型复跑零正例，经济召回仍无分母。 [Base 官方说明](https://docs.base.org/get-started/connect-to-base) · [既有验证](../verification/MULTIHOP_IMPLEMENTATION_20261122.md) |

**免费也有三种不同的资源约束。** 无密钥市场索引适合筛选池，刷新不同步且通常没有区块哈希。免费账号可能提供历史状态，但 debug/trace、单次日志跨度、调用配额取决于方法和链。自建节点省掉供应商调用账单，仍要支付 SSD、带宽、同步与运维。Ethereum 历史公共数据可按日期、列和行组裁剪，本次列目录发现单日 logs Parquet 样例已逾 1 GB；云查询服务计算和本地存储要单独算成本。AWS 的“Lighter on Robinhood”是特定应用数据，其覆盖面不能当作整条 Robinhood 链。[AWS 数据说明](https://registry.opendata.aws/aws-public-blockchain/) · [Geth archive 模式](https://geth.ethereum.org/docs/fundamentals/archive) · [BSC 节点配置](https://docs.bnbchain.org/bnb-smart-chain/developers/node_operators/node_best_practices/)

**同一个免费索引也能同时有成功样本和覆盖缺口。** SQD 可查询历史 finalized stream。本轮另一组固定区块请求成功，Ethereum 正案例 19 条原始日志的索引、地址、topics、data 与 Blockscout 均一致，并取得上述 BSC 四跳样本。两家索引器的一致只说明字段对齐，其底层基础设施独立性未验证。试图对齐 2026-11-18 12:00–12:01 UTC 的 ETH、BSC 和 Base 窗口时，Ethereum 起点解析成功，终点及另外两链起点返回 HTTP 529 overloaded。该轮没有取得一分钟的日志分母，没有用部分数据填数。SQD 将 Robinhood 明列为 private；Public/Testing 的公布条款限开发评估，其商业使用和未来套餐另有条件。这些差异适合用来决定是否换数据源或采购可靠性。[SQD 方案与适用范围](https://docs.sqd.dev/en/portal/pricing) · [Robinhood 专项覆盖](https://docs.sqd.dev/en/data/evm/robinhood-mainnet) · [历史流接口](https://docs.sqd.dev/en/api/evm/finalized-stream)

## 五、Oasis 可以换一种起步方式

1. **建立成交案例库和市场目录。** Ethereum 保留真实的分流赢家和收割负例。BSC 从稳定币与 WBNB 同时覆盖 Pancake、Uni 和其他来源，Robinhood 并行研究同币对多池及反复使用同池的长交易。按链、factory、池地址或 manager/PoolId 核身份；用 token 地址区分桥接币与原生币。展示符号、索引成交额和 TVL 只用于初筛。
2. **让池更新触发计算。** 从一个可复核的块状态启动，订阅已发布的头/日志或读取整块回执。按 block hash、txIndex、logIndex 顺序应用增量；断线补齐缺块，重组回滚到共同父块。V2 用 Sync 储备；CL 的精确深度还需初始化 tick 及增减流动性；v4 读取 hook、动态费和调用上下文。普通同链交换先用扣费边际上界剪枝，幸存路线才做整数金额求解。只支持部分池型的上界必须标明范围。以本轮 BSC 3,703 组为例，若每组单方向试 16 个金额、每腿一次远程报价，就有 177,744 次逻辑报价。用固定块池状态本地算，远程请求随发生变化的池及幸存候选增长；批量调用还需预算响应字节、gas 上限与数据新鲜度。
3. **把前瞻性和事后标签分开采集。** 保存首次可用状态的本地单调时间、候选耗时、整笔模拟完成时间、预算截断、失败原因。历史交易前状态从父块及前序交易还原；对当前块末候选要明确模拟的是下一个可用位置。各链以独立时间窗记录正例、机会维持时间和当前可成交量；同池互斥的候选不能叠加净额。
4. **只为已经测出的瓶颈付费。** 免费端点先测连续覆盖、重组回补、按方法的失败率及 p50/p95/p99 到达延迟。必要时冻结同一池集、同一时段、同一计算硬件和相同复核预算，对照免费源/付费源或自建节点。按每月新增且能及时整笔复核的独立候选、可得包含概率及完整净额，与月费/硬件运维比较。包含和额外付款不可观测时给区间；区间下界不足以覆盖成本，继续影子研究。4070 Ti 可共用离线批量搜索，已测的大批量计算收益需和各链的网络延迟分开。[Aleph 实测](../verification/MULTIHOP_CUDA_20261122.md)

样本来自两轮指定锚定币索引、一个预选区块的盲扫、少量固定状态与事后挑选的案例。各链的机会发生率、可承载本金和竞争后的持续净利，须由预先冻结的真实前瞻窗口估计。

### 离线复算

本次 BSC 状态用 2 次免费 RPC，Robinhood 用 8 次，其中 1 次方法不可用；未使用付费 RPC。其他现场数据来自公开 HTTP 索引。原始响应与失败记录在本工作区的 `reports/research/multichain-discovery-20261122/`。仓库保留了下列复算需要的逐边计数、BSC 原始整数、块身份和响应摘要。可在仓库根目录断网运行。

```sh
python3 - <<'PY'
import itertools, json, math
from fractions import Fraction
from pathlib import Path
sample = json.loads(Path("docs/verification/MULTICHAIN_DISCOVERY_20261122.json").read_text())
for name, chain in sample["chains"].items():
    edges = {(a, b): (n, kept) for a, b, n, kept in chain["edge_counts"]}
    tokens = sorted({token for edge in edges for token in edge})
    triples = [((a, b), (a, c), (b, c)) for a, b, c in itertools.combinations(tokens, 3)]
    totals = [sum(math.prod(edges[e][k] for e in tri) for tri in triples
                  if all(e in edges for e in tri)) for k in (0, 1)]
    print(name, totals)
s = sample["bsc_state"]
p2 = Fraction(int(s["reserve1"]), int(s["reserve0"]))
p3 = Fraction(int(s["sqrtPriceX96"]) ** 2, 2 ** 192)
fee = Fraction(9975, 10000) * Fraction(1_000_000 - s["fee_pips"], 1_000_000)
assert max(p2 / p3, p3 / p2) * fee < 1
PY
```
