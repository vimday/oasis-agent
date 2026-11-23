# Base 四池、58 池与 V4 混合图同块比较

北京时间 2026-11-23 18:44:35.800，在 Base **61,201,501** 块
`0xf7fbaa1e8f5fd5625a861448f7908ff4124389d8a1b565e760fb9a19d6eb4b53`
对比三个同状态、同费用输入的池集。保留此前四池和 58 个地址池，并增加此前两源核过的
8 个无 hook V4 单例池。这次状态、bitmap 和 Quoter 均来自一个匿名 PublicNode 端点，
`eth_call` 与运行码读取绑定同一 `blockHash`，并要求 `requireCanonical=true`。
此前 PoolKey 的双源证明只关联池身份，当前块的状态和报价为单源。
[四池和 58 池基线](BASE_FORWARD_POOL_COMPARISON_20261123.md) ·
[V4 双源身份](BASE_V4_FORWARD_DISCOVERY_20261123.md) · [EIP-1898](https://eips.ethereum.org/EIPS/eip-1898)

## V4 定价范围

8 个完整 PoolKey 均在当前块由 PositionManager 重新读出并重算 PoolId，PoolManager
运行码与先前验证的 Keccak 一致。StateView 带回 `slot0`、正活跃流动性、LP fee 和双向
协议费，合成后这些池的两个方向费率均相同。此前长尾中间币
`0xa9b631abcc4fd0bc766d7c0c8fcbf866e2bb0445` 的 `decimals()` 本轮读为 18。
原生 ETH、WETH、USDC 和 USDbC 仍按各自地址区分。三个图中没有包装边，也未纳入有 hook 池。

每个 V4 池读取当前压缩 tick 所在字及其左右两个字。6 池在该范围内找到上下两侧最近的
初始化 tick，2 池的五个字未命中，模型在已读页边界截断。每个 V4 池仅使用当前活跃
流动性和这个已证区间。边界之外的流动性与报价未覆盖；58 个地址池继续沿用此前
的余额限深模型。
[V4 StateView](https://github.com/Uniswap/v4-periphery/blob/main/src/lens/StateView.sol) ·
[V4 TickBitmap](https://github.com/Uniswap/v4-core/blob/main/src/libraries/TickBitmap.sol)

## 同状态模型结果

保留 0.001–1 WETH 与 5–2,000 USDC 两个规模范围、至多三跳的扫描输入。
环路计数按锚与方向。金额区间仍有搜索预算未闭合的部分，因此零正候选只描述已经评估的模型结果。

| 同块池集 | WETH 环路 | USDC 环路 | 两锚模型正候选 | 未闭合金额区间 |
| --- | ---: | ---: | ---: | ---: |
| 4 池 | 12 | 12 | 0 | 4 |
| 58 地址池 | 1,774 | 1,596 | 0 | 34 |
| 66 混合池 | 1,940 | 1,776 | 0 | 34 |

新增 V4 边带来 346 条方向环路，比 58 池组多约 10.27%。整笔交易模拟为 0，
没有实测净正机会。

## 固定金额逐腿报价

两条跨版本混合路线使用 Uniswap V3 QuoterV2 与官方 V4 Quoter。
三角路线的三个输入依次由 V4 Quoter 返回的前一腿金额给出。三条路线的七腿均为同块只读，
终点整数与本地模型一致。原生 ETH 三角沿用历史闭环的两个 V4 池，
并用 ETH/USDC V4 池取代历史交易的包装加 V3 风格回程。该对照用 0.5 USDC，
规模低于图扫描的 5 USDC 下限。

| 路线 | 输入 | 最终输出 | 毛差 | 扣 L2 模型与 L1 探针 |
| --- | ---: | ---: | ---: | ---: |
| V4 → V3，WETH 最小单位 | 1,000,000,000,000,000 | 998,502,120,468,189 | −1,497,879,531,811 | −4,998,833,867,678 |
| V3 → V4，WETH 最小单位 | 1,000,000,000,000,000 | 993,504,091,848,175 | −6,495,908,151,825 | −9,996,862,487,692 |
| USDC → 中间币 → ETH → USDC，USDC 最小单位 | 500,000 | 497,234 | −2,766 | −8,787 |

L2 为 500,000 gas 乘已读 base fee 5,000,000 wei 与模型 tip 2,000,000 wei 之和。
L1 为固定数据探针 954,335,867 wei。两项按同窗 WETH/USDC 价格折算时分别向上取整。
此块 L1Block 读取到 operator scalar 和 constant 均为零。整笔执行所需 gas、最终
calldata 的 L1 费用、资金成本、排序付款及失败成本皆未测。逐腿 Quote gas 保留为各
Quoter 自身诊断。该样本支持扩大观察覆盖，交易经济性和实时延迟尚无充分实测。
[仓库 V4 报价 ABI](../../src/oasis_agent/uniswap_v4/quotes.py) ·
[OP Stack 费用规范](https://specs.optimism.io/protocol/isthmus/exec-engine.html)

## 留存和复算

原账本共用 **13/16 次**匿名免费请求，其中 10 次 `eth_call`、1 次 `eth_chainId`、
1 次 `eth_getBlockByNumber` 和 1 次 `eth_getCode`。模型正候选为零，留给可选报价的
3 次未使用。忽略目录 `reports/research/base-v4-mixed-graph-20261123/run/` 留有原始请求/
响应、接收时钟、哈希、账本、状态、bitmap 和 STOP。以下离线复算通过 13 对原始字节、
三组图结果和七腿整数报价校验。

```sh
PYTHONPATH=. .venv/bin/python reports/research/base-v4-mixed-graph-20261123/replay.py
```
