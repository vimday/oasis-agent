# Base V4 单例池发现与双源报价

北京时间 2026-11-23 18:13:13.200，以 Base 区块 **61,192,088** 和哈希
`0xdcff7762b2bb44613aaffbbc0ad15d01a47f173e28114d0cf6f1f31b1ea01668` 固定状态，
从官方 Base 公共 RPC 与 PublicNode 各做 5 次只读请求。两端的链 ID 8453、块哈希、
`parentHash`、`stateRoot` 和块内 `BaseTime` 毫秒值一致。两端 PoolManager 运行字节
均为 24,009 字节，Keccak 匹配仓库已有身份
`0x83b2af6e9f3158defc2811cbcb0db71ecf8b2ba2abea39c39e370ac5c6f43eb6`。
PositionManager、StateView 和当前官方 Quoter 均指向同一个 PoolManager。
原账本累计 10/10 次，费用 USD 0，整笔执行模拟和链上写入均为 0。
[Base 公共 RPC](https://docs.base.org/get-started/connect-to-base) ·
[PublicNode Base](https://base.publicnode.com/) ·
[Uniswap V4 部署](https://github.com/Uniswap/docs/blob/main/content/protocols/v4/deployments.mdx#base-8453)

## 池键、回调与费率

样本包括近期三跳交易的两个 PoolId、此前从 calldata 解出的两条 Zora 池、WETH/USDC
和 USDC/USDbC 索引的七条线索，以及仓库已验证的一条 ETH/USDC 池。两端在同哈希查询
PositionManager 的 `poolKeys(bytes25)`，对每个返回的 Key 重新计算完整
`keccak256(abi.encode(currency0, currency1, fee, tickSpacing, hooks))`。
12/12 个完整 PoolId 均匹配，12 个在样本状态的活跃流动性均为正。以 PoolManager 地址
与完整 PoolId 共同标识单例池。活跃流动性是当时价格所在区间的状态字段，未测全价带深度。
解码参照仓库锁定的 `@uniswap/v4-core@1.0.2` 和 `@uniswap/v4-periphery@1.0.3`。
[PoolId 源码](https://github.com/Uniswap/v4-core/blob/main/src/types/PoolId.sol) ·
[PositionManager](https://github.com/Uniswap/v4-periphery/blob/main/src/PositionManager.sol)

| 池分类 | 数量 | 观察 |
| --- | ---: | --- |
| ETH/USDC 与 WETH/USDC 无 hook | 2 | ETH 与 WETH 各自保留地址身份，两种资产不合并。 |
| USDC/USDbC 无 hook | 4 | Key 费用为 6、49、100、3,000 pips，方向协议费也分别读取。 |
| 近期三跳的两个长尾池 | 2 | 无 hook，完整币对、费和间距均核实。 |
| WETH/USDC 有 hook | 2 | 一个动态费 Key，另一个静态费 Key 保留 swap 回调。 |
| Zora 对照 | 2 | 共同 hook 权限为 `afterInitialize` 和 `afterSwap`。 |

两个长尾池共用中间币 `0xa9b631abcc4fd0bc766d7c0c8fcbf866e2bb0445`。
USDC 池 `0xa975393c…a9ede1` 的 LP fee 为 75 pips、tick spacing 为 1、两个方向协议费
各 20 pips。ETH 池 `0x272ebdef…f4534` 相应为 3,000、60、500。按 V4 费用顺序合成的
95 和 3,499 pips，各与历史收据日志 42、45 的 Swap fee 一致。历史收据与新状态分别保留。
中间币单位本轮未读，两池无金额报价。
[近期三跳回执](https://basescan.org/tx/0x1b10cc853439321b799213e3864fbdc7b9ca12fe2f5e0669c2d63c0fe3a4336b) ·
[V4 ProtocolFeeLibrary](https://github.com/Uniswap/v4-core/blob/main/src/libraries/ProtocolFeeLibrary.sol)

两条 WETH/USDC hook 池未纳入普通报价组。池 `0x4cd11e74…b84ce` 使用动态费哨兵，
固定状态读到 2,029 pips；hook 低 14 位含 `beforeSwap`、`afterSwap` 和
`afterSwapReturnsDelta`。池 `0xec746b81…93a836` 为 500 pips 静态 Key，但含
`afterInitialize` 与 `afterSwap`。这些地址位说明回调许可，未验证运行时回调语义、
动态覆盖或任意金额下的最终结算。
[V4 Hooks](https://github.com/Uniswap/v4-core/blob/main/src/libraries/Hooks.sol)

## 固定金额报价

在同块验证 ERC-20 `decimals()` 为 WETH 18、USDC 6、USDbC 6。六个正流动性无 hook
池的双向 Quoter 共 12 个方向，两端返回整数金额和 Quoter gas 诊断均逐项相同。例如，
0.001 WETH 在 `0x1d8c55f3…18f54` 得 1.717185 USDC，0.001 ETH 在
`0x96d4b53a…88c0a` 得 1.720106 USDC。USDC/USDbC 六位单位下，一笔 5 USDC 在
`0x867dbb1b…832c4` 得 5.000249 USDbC。各池双方向独立固定大小，连腿金额、包装、
整笔交易 gas、L1 数据费、operator fee 和净机会均未测。
[仓库 V4 Quoter 编解码](../../src/oasis_agent/uniswap_v4/quotes.py)

对该块八个固定费无 hook 池，从两端一致的 `sqrtPriceX96` 和方向费用构造整数有理数边际价。
按不同池、不同中间资产及旋转去重枚举，14 条定向闭环的费后边际毛差均为负。加入理想化、零成本
的 ETH/WETH 一比一包装边后共 18 条，仍为零正例。两臂最接近盈亏线的均是 USDC/USDbC 两池
组合，为 **−0.619 bps**，尚未计 gas。这一快照的边际检查允许长尾中间币原始单位在闭环中抵消，
有限金额和币种行为仍需独立核实。四池和 58 池已有跨协议图尚未与本轮 V4 池在同一块重跑。
[V4 方向费用实现](../../src/oasis_agent/uniswap_v4/fees.py)

此前四池/58 池实验的扩展运行入口仅发现 V3、Slipstream 与 Volatile 地址池。此轮证实
可以从严格 PoolKey 读取增加六个已报价的无 hook V4 池及两条待金额核验的长尾边。
接入需要让扫描和报价都认完整 PoolId，并为原生 ETH 和 WETH 保留有据可查的包装边。
Base Denim 已将 Flashblocks 替换为 200 ms 规范块，订阅应使用普通 `newHeads` 或
`logs`。本轮由 HTTP 读同一规范块，L1 最终性和订阅延迟未测。
[四池/58 池对照](BASE_FORWARD_POOL_COMPARISON_20261123.md) ·
[Base Denim](https://www.base.org/blog/denim)

## 原始证据

忽略目录 `reports/research/base-v4-forward-discovery-20261123/run/` 保存冻结范围、
逐请求/响应字节、来源时间、原始 SQLite 预算、位掩码、完整池键、报价及 STOP。
以下离线复算校验两端 10 份往返字节、同块 EIP-1898 参数、12 个完整池键、PoolManager 代码
和 12 个方向报价。

```sh
PYTHONPATH=. .venv/bin/python reports/research/base-v4-forward-discovery-20261123/replay.py
```

同目录的 `marginal_replay.py` 生成 `marginal.json`，SHA-256 为
`3cd656ccb2223e71462aaf1a97ee7004ad34be2eecdd0424e83287663a2c03a0`。
