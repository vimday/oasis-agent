# 四链近期 DEX 套利成交复盘

样本为北京时间 2026-11-22 至 11-23 的真实落块交易。公共索引、链上 Swap 和收据用于发现候选，再按池身份、资金方向和费用核对。各链观察窗口与选取方法不同，结果仅作案例研究。ETH 与 WETH 按可包装单位一比一计。[前瞻扫描](MULTICHAIN_FORWARD_20261123.md) · [长路径与中小池对照](MULTICHAIN_TAIL_DEPTH_20261123.md)

| 链和北京时间 | 观察到的资金路线 | 可核对的结果 |
| --- | --- | --- |
| Base · 11-23 00:57:18.800 | 三跳，从 USDC 回到 USDC，涉及两个 V4 PoolId 和一个 V3 风格池 | 毛差 +0.000198 USDC。仅已知 L2 与 L1 费用按同笔汇率就约 0.019407 USDC。 |
| Ethereum · 11-22 21:07:47 | 三跳 WETH → USDC → USDT → WETH，V3 → V4 → V3 | 毛差 143,922,496,516,185 wei。扣执行 gas 和给区块 fee recipient 的直接付款后，交易范围剩 +14,872,651,508,991 wei。 |
| BSC · 11-23 01:00:38 | 四段 WBNB → USDT → USDC → PUFFER → WBNB，包含 Infinity CL | 毛差 10,097,499,118,203 WBNB wei。收据有效 gas 单价为 0；可见 WBNB 派发和解包，解包后原生币去向未知。 |
| Robinhood · 11-22 23:50:24 | 四个不同 V3 池，WETH 经三个中间币回到 WETH | 执行器多收到 7,694,668,320,147 WETH wei，扣另一地址实付 gas，交易范围结余 +787,566,174,147 wei。 |

交易范围的结余不等同于钱包已实现净利。完整的跨交易结算、链外付款、钱包共同控制和排序方式仍需证据。

## Base · 成功闭环与费用门槛

同一个免费 PublicNode 拒绝无地址日志查询和超出 10 地址的查询。原账本保留两次拒绝。缩到支持的 10 个 emitter 后，在累计 28 次的上限内完成四段各 96 块的观察，范围包括官方 Uniswap V4 PoolManager 和九个已有候选池。418 条匹配日志涉及 367 笔交易，其中 23 笔关联到两个或以上不同地址/PoolId，抽查了 10 个完整收据。至少一笔 ERC-4337 交易在筛选范围内命中两个 Swap，完整收据有七个。另有一笔四 Swap 交易呈现付款人减少一种非稳定币、增加 USDC 的开放方向，可用作误报负控。

三跳闭环 `0x1b10…336b` 在块 **60,881,316**、索引 **12**。外层目标是 ERC-4337 v0.6 EntryPoint，收据有一条成功的 UserOperationEvent。执行器先取得 500,000 个 USDC 最小单位。日志 42、45、50 的资金依次走过：

1. 500,000 USDC 最小单位 → 10,368,998,348,135,322,112,569 个中间代币最小单位，地址 `0xa9b631abcc4a…`。
2. 中间代币 → 290,616,258,474,103 ETH wei，随后等量包装为 WETH。
3. 290,616,258,474,103 WETH wei → 500,198 个 USDC 最小单位，偿还 500,000 后留 198。

收据执行费用为 11,263,326,000,000 wei，L1 数据费用为 12,435,586,704 wei，合计 **11,275,761,586,704 wei**。末腿实际汇率约为 1,721.16 USDC/ETH，折合约 0.019407 USDC。盈亏平衡所需汇率约为 17.56 USDC/ETH。Operator fee 仍待固定块 oracle 验证。UserOperationEvent 的费用字段与外层收据属于不同付款层次。账本将其分列，避免重复收费。第三池的事件采用 Pancake V3 形态，独立固定块工厂验证未做。[原交易](https://basescan.org/tx/0x1b10cc853439321b799213e3864fbdc7b9ca12fe2f5e0669c2d63c0fe3a4336b) · [OP 费用规范](https://specs.optimism.io/protocol/isthmus/exec-engine.html) · [EntryPoint v0.6](https://github.com/eth-infinitism/account-abstraction/tree/v0.6.0)

三个池标识都在 Oasis 的四池 WETH/USDC 窄基线之外。保留原基线，再对比 V4 PoolId、V3 跨厂和非 WETH/USDC 中间边。Base Denim 已采用 200 ms 规范块。该交易位置为 12，下一步用父块及块内前 12 个位置重建精确交易前状态，再用采集时钟判断公开状态何时可见。这里已验证成交位置，尚未进行交易前状态重放。[四池基线](../../src/oasis_agent/fastpath/live_shadow_runner.py) · [Base Denim 官方说明](https://www.base.org/blog/denim)

## Ethereum · 直接付款改变净额

块 **26,470,852**、索引 **7**。11 条日志和 48 条内部调用均重解，三腿为：

| 池身份 | 输入 | 输出 |
| --- | ---: | ---: |
| V3 `0xe0554a476a092703abdb3ef35c80e0d76d32939f` | 0.311090615686414055 WETH | 529.273312 USDC |
| V4 PoolId `0x0fb0e40cec3bb23e13abc585958a93c796fbea56955e19a23727a716a0423239` | 529.273312 USDC | 530.066966 USDT |
| V3 `0xc7bbec68d12a0d1830360f8ec58fa599ba1b0e9b` | 530.066966 USDT | 0.311234538182930240 WETH |

376,369 gas × 135,103,083 wei/gas 为 50,848,612,245,627 wei，已含 priority fee。执行器另转 78,201,232,761,567 wei 给区块头所列 fee recipient。二者消耗毛差的 **89.67%**。包裹和解包在取到的原生调用树内抵消。该交易用的资产与此前 V2 三币扫描相同，V3/V4 跨池路径超出当时的协议覆盖。[交易](https://eth.blockscout.com/tx/0xca35f795e198efdfe6219ada8b1eaf5d02ce3b0e3b66316649d14babbb87df5b) · [原始日志](https://eth.blockscout.com/api/v2/transactions/0xca35f795e198efdfe6219ada8b1eaf5d02ce3b0e3b66316649d14babbb87df5b/logs) · [完整内部调用](https://eth.blockscout.com/api/v2/transactions/0xca35f795e198efdfe6219ada8b1eaf5d02ce3b0e3b66316649d14babbb87df5b/internal-transactions)

## BSC · Infinity ABI 与回调顺序

在冻结的五个最终块偏移中取得 **403 笔完整回执**。旧筛法按 V2/V3 Swap 和日志索引找严格闭环，得到零笔。按转账重新连接后，发现块 **135,115,390**、索引 **14** 的四段资金流，再用单笔回执核对：

`12755440000000000 WBNB → 7146408377111875790 USDT → 7137331461254009143 USDC → 332874226045522778053 PUFFER → 12765537499118203 WBNB`

实际资金路径对应日志 **39 → 29 → 34 → 37**。首 V3 池在嵌套回调完成后才发 Swap。第二腿的 Pancake Infinity CL 事件有额外的 `uint16` 字段，同 Uniswap V4 的签名不同。Vault 两端真实转账与事件 delta 相等。三个 V3 池身份已用官方 deployer、init hash、CREATE2 和 100 ppm 费档复算，Infinity PoolId 也与官方 PoolKey 哈希相符。

收据消耗 525,450 gas，价格为 0。毛差中 8,582,874,250,473 wei 以 WBNB 转出，1,514,624,867,730 wei 解包。原节点历史状态返回 missing trie node 后即停止，当前可见的资金流不能证明解包后的原生币去向和链外成本。[四段交易](https://bscscan.com/tx/0xdccd0c9ba83f7c42957f35d492804c0c8a1312b2ac4fddc10bbaea40f892ac33) · [Pancake V3 官方地址](https://developer.pancakeswap.finance/contracts/v3/addresses) · [Infinity CL 官方 ABI](https://github.com/pancakeswap/infinity-core/blob/main/src/pool-cl/interfaces/ICLPoolManager.sol) · [Infinity PoolId](https://github.com/pancakeswap/infinity-core/blob/main/src/types/PoolId.sol) · [V3 回调与事件顺序](https://github.com/Uniswap/v3-core/blob/main/contracts/UniswapV3Pool.sol#L700-L722)

同块 69 笔交易有 8 笔有效 gas 单价为 0。用同块非零最低价 0.05 gwei 作为情景，主例在相同 gas 用量下净额会变为 **−16,175,000,881,797 wei**，盈亏平衡气价约 **0.0192 gwei**。这些是费用敏感性分析，零气价的来源和能否由 Oasis 获得都未验证。

同块索引 64 一笔还有四个不同池事件，前三段是 USDT 闭环，第四段是把剩余利润换成 WBNB。投入 0.132401391873999120 USDT，先取回 0.133550809404210066 USDT，再用全部 0.001149417530210946 USDT 差额换得 2,051,155,759,984 WBNB wei。证据应分别记录闭环深度和变现腿。[同块变现样本](https://bscscan.com/tx/0xe5d8bc4ace6fe988cb978c97999172cb06ff2d3c81820e20c1b6892ec9522956)

## Robinhood · 执行器、付款地址与实际收款人

从留存 **1,955 条**日志筛得 17 个闭环候选。所抽六笔均核对了完整成功回执、事件原字节和块内位置。四跳样本位于 **120,733,321**、索引 **15**，资金顺序对应日志 46 → 38 → 41 → 44。执行器的三个中间币净额都为零。按最小单位逐腿为：

`380159400000000 WETH → 67734744615482952977 (0x0339…9e1b) → 12847290319375109351714 (0x1c25…1e18) → 152065661715217257455 (0x7dbf…e6a) → 387854068320147 WETH`

执行器收到 7,694,668,320,147 WETH wei，外层另一地址支付 6,907,102,146,000 ETH wei gas。另一笔两跳与此前留存 feed 对应，位于 **120,740,599**、索引 **3**，执行器增加 14,506,843,006,266 WETH wei，扣收据 gas 后的交易范围结余为 6,710,764,234,266 wei。还有一笔混合两跳看到 WETH 毛差转给与外层发送者不同的地址。把执行器、付款人、包装中转和最终收款地址分别保留，才有可能核到真实余额。[四跳交易](https://robinhoodchain.blockscout.com/tx/0x7534c1c15b8a18d80850e94dfe93e5ab85ff6aed486ce53fcdf3e6992a43a44a) · [与 feed 对应的两跳](https://robinhoodchain.blockscout.com/tx/0x6e6a0d79e9f2a37acab5bc7ca5db4a095c65ddab7b82aa2ea4d2d44bd3d2ce9e)

## 接入 Oasis 的验收用例

1. **协议覆盖。** Base 保留四池基线并与 V4 PoolId、跨厂 V3 及长尾中间资产对照。Ethereum 检查 V3/V4 混合路径，BSC 增加 Infinity 独立事件和 Vault 结算。协议名从可核的部署身份得到。
2. **资金依赖。** 用调用树、Transfer、包装/解包和 pool delta 还原方向，再统计串行深度、不同池、分支及转账数量。把 BSC 回调倒序和 Base 开放方向作为一正一负的识别回归。
3. **全成本。** 分列 ERC-4337 内外层付款、链上 gas、直接 fee recipient 转账和最终收款。对未解释残差保留原币与付款人，Base 查询 L1/L2/Operator，BSC 零 gas 和 Robinhood 付款归因保留各自边界。
4. **可发现性。** 对同一确认交易比较实际池集与现有预设，取得交易前状态并验证在固定时间预算内能否识别和完成整笔模拟。再报告模拟完成后可见状态的余额差。使用短路线作为长路线的同资本对照，同时保留损失样本与缺失分母。

原始数据保留在本地忽略目录 `reports/research/recent-dex-arbitrage-20261123/`、`reports/research/bsc-recent-arbitrage-20261123/`、`reports/research/robinhood-ordinary-arb-20261123/`。本轮免费公开数据支出为 $0。Base 三跳的原始交易和收据为 `base/17.response.json`、`base/18.response.json`；BSC 的单笔复核收据为 `wire/12.response`。
