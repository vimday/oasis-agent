# Ethereum 同块两个独立四跳闭环的费用余量

北京时间 2026-11-24。从八个已留存整块的回执中，按池地址、资产和精确整数转账串接
Swap，选出两个可复核的四跳 WETH 闭环。两笔交易均在区块 **26,471,935** 成交，索引
**38、79**。它们有同一个执行器和 WETH 收益地址，使用不同付款发起地址和八个互异
逻辑池。扣除各自真实执行费用后可见净额分别为 **+7,145,491,293,251** 与
**+1,288,115,539,886 ETH 等值 wei**。两笔均无可见额外原生币支付。
[此前八块事件样本](LONG_SWAP_TAIL_20261123.md) · [此前不同区块的十事件分组样本](ETH_LONG_SERIAL_EVIDENCE_20261124.md)

## 区块与实际资金

该块 UTC 时间是 2026-11-22 16:44:47，块哈希为
`0x1c4db301256bb43de7a90e89fe185ae1bd4a4d8c39e8d1d851add0e4c690fd46`。
前轮 PublicNode 的区块收据原文提供 **210 份有序收据**。新增范围在首次请求前固定两个
候选哈希，仅用 PublicNode 读取一个区块头、用 dRPC 各取一份调用树，合计 **3/3 次**，
USD 0。区块头和整块收据来源均由 PublicNode 运营。复算 Prague 区块头哈希、累计 gas
和全区块收据 trie，收据根
`0x2a302bc8805c9e2a41a1c5e5223760ff221a050565955988b663e3d61bfcf304`
与区块头一致。另一个公开网关返回的 26、33 个调用帧逐项匹配两笔收据各 12、10 条日志。
[dRPC 调用树接口](https://drpc.org/docs/ethereum-api/debugandtrace/debug_traceTransaction) · [区块](https://etherscan.io/block/26471935)

| 位置 | 实际资金路径，Swap 日志顺序 | 三个中间产出的整数金额 | 起始 → 末端 WETH wei |
| ---: | --- | --- | --- |
| 38 | 70、71、75、78。WETH → `0x5659…355e` → USDT → `0xecbf…5f7a` → WETH | 6,899,056,183,136 → 3,558,047 → 112,540,275,193 | 1,973,951,062,254,804 → 2,081,986,604,322,347 |
| 79 | 144、145、149、151。WETH → `0x57e1…6061` → USDC → `0xaee5…7d37` → WETH | 128,430,352,576,112,244,193 → 13,162,652 → 10,390,483,106,364,179,754,814 | 7,620,147,326,121,412 → 7,676,229,171,835,338 |

每条路径的三个中间金额分别由一条唯一、等额的直接池间 ERC-20 Transfer 支持，日志索引
为 68、72、73 和 142、146、147。两个 V4 Swap 从原始 calldata 重算 PoolId，并比对
返回 BalanceDelta 与事件数值。位置 79 的末腿是 Curve TwoCrypto 型
`exchange_received`，收据证明上一个池的输出全额转入该池，调用返回的 WETH 输出与
末端给执行器的 Transfer 一致。两笔均从执行器送出 WETH，回到同一执行器。两个发起
地址各自付 gas，实际控制关系未知。两笔跨池没有同一逻辑池，均经过同一个 V4
PoolManager 的不同 PoolId。该块先后顺序是索引 38、79。未重演重排后合约状态。
[位置 38](https://etherscan.io/tx/0xc4d3616ab6ff2a2484ce468aa90a6d2a3c0084545d00ae02ccdb35e499343291) · [位置 79](https://etherscan.io/tx/0x094e02ea6be3addf73e48f667ddc955d85ab8f7dc20dce29dab707732b91f42a) · [Uniswap V4 PoolId](https://github.com/Uniswap/v4-core/blob/main/src/types/PoolId.sol) · [Curve TwoCrypto ABI](https://resources.curve.finance/developer/twocrypto-ng/pools/twocrypto)

## 固定成交金额下的费用边界

| 项目 | 位置 38 | 位置 79 |
| --- | ---: | ---: |
| WETH 毛差，wei | 108,035,542,067,543 | 56,081,845,713,926 |
| 实际 gas × 单价，wei | 308,646 × 326,879,502 | 353,320 × 155,082,447 |
| 执行费，wei | 100,890,050,774,292 | 54,793,730,174,040 |
| 仍为正净额的最低 WETH 输出，wei | 2,074,841,113,029,097 | 7,674,941,056,295,453 |
| 相对实际可再支付的 gas 单价，wei/gas | 23,151,089 | 3,645,747 |
| 单价加 0.01 gwei 后的净额，wei | +4,059,031,293,251 | −2,245,084,460,114 |
| 单价加 0.025 gwei 后的净额，wei | −570,658,706,749 | −7,544,884,460,114 |

两笔历史成交的合并可见净额为 **8,433,606,833,137 wei**。观察账将执行器 WETH 毛差和
各付款地址 ETH gas 合计。位置 79 的余量低于同块 base fee 下 21,000 gas 的
**2,582,408,346,000 wei**。只要同一费用账另承担一次达到该成本下限的失败尝试，这笔
较薄成交的净额就会被抵消。该块收据未观察到失败，费用扰动保持实际成交金额和 gas
用量固定。链外费用、未落块订单、交易前状态下的竞争与可重放性仍待核验。
[EIP-1559 费用规则](https://eips.ethereum.org/EIPS/eip-1559)

## 留档和复验

原始区块收据在忽略目录 `reports/research/long-dex-20261123/ethereum-rpc/05.res`，其余在
`reports/research/eth-two-four-hop-20261124/`。新范围和只读原账本保留，未重试或重新请求
既有整块收据。

| 保留件 | SHA-256 |
| --- | --- |
| `05.res` | `80a5e48a4a90bf7d0743445010d83200c44731d433d2efe13b4484644e76c8cf` |
| `block-header.response.json` | `3c4795bb3bf48d48549e1528e31ea378d2245f94ae37ccd900f687204b2c1dac` |
| `38-trace.response.json` | `550eead9d27afd0ef48c1263b661734a3acf64ecebb2d2a253305c3ccead97f1` |
| `79-trace.response.json` | `274155ca21a417791d22e74d7d7319752a1706c60cc0ec419d47df8f7f63d428` |
| `verified.json` | `9a4f6b80d34582022a02476abc8031210e82bf266bd15fd871fb80dc6b0e7bc4` |

```sh
set -o pipefail
dir=reports/research/eth-two-four-hop-20261124
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python "$dir/verify.py" | cmp - "$dir/verified.json"
```

## 位置 79 原始交易在两个后续状态均回滚

后续只选择费用余量较薄的位置 79。冻结四次匿名只读请求，PublicNode 返回原交易和
直接子块头，dRPC 按两个区块哈希各执行一次 `debug_traceCall`。4/4 次返回有效结果，
USD 0。重算原交易的 EIP-1559 类型化哈希，恢复发送者，并与已留存原调用树逐字节匹配
calldata、from、to、value。模拟使用同一 gas limit、两个费率上限和 access list，
省略签名、nonce 和状态覆盖。请求未设置 `txIndex`，使用对应区块的块末状态。
复算子块 **26,471,936** 的区块头哈希
`0xe8e8367a474fa4b2bd26da940d71602821c005fac27884a5da4b16058471e8ae`，
其 parentHash 与原块一致，区块时钟相差 12 秒。
[TraceCall 的默认状态选择](https://github.com/ethereum/go-ethereum/blob/master/eth/tracers/api.go) · [dRPC 区块参数](https://drpc.org/docs/ethereum-api/debugandtrace/debug_traceCall)

| 状态 | V4 尝试送入第三池的 USDC 最小单位 | gas 用量 | 处置 |
| --- | ---: | ---: | --- |
| 已落块索引 79 | 13,162,652 | 353,320，实付 54,793,730,174,040 wei | 成交 |
| 成交块 26,471,935 块末 | 13,051,743 | 280,345，模拟 | 第三池回滚 |
| 直接子块 26,471,936 块末 | 13,051,743 | 284,221，模拟 | 第三池回滚 |

两次块末模拟都要求第三池输出相同的 **10,390,483,106,364,179,754,814** 个代币最小单位。
第二腿可送至第三池的 USDC 较落块时少 **110,909** 个最小单位。第三池
`0xd86902b7fb91b89973616c30fb5fe6092b82bbdc` 最内层返回 `UniswapV2: K`，
外层回滚原文为 `0x47`。此前的转账尝试随整笔回滚，两次均无生效日志、无实际扣款。
按各固定块 base fee 与模拟用量计算，若原样失败落块，仅 base fee 就分别为
**34,474,536,559,970** 与 **33,727,831,613,567 wei**。这两个条件成本都超过原成交可见
净额。样本只约束两个检查点，成交位置到本块块末之间的失效时刻仍未知。
[Uniswap V2 恒积检查](https://github.com/Uniswap/v2-core/blob/master/contracts/UniswapV2Pair.sol) · [EIP-1559 base fee](https://eips.ethereum.org/EIPS/eip-1559)

原始请求、响应、多层回滚字节、原账本与复验结果在前文忽略目录的 `lifetime-79/`。
两份块末 trace 的 SHA-256 分别为
`bf6e559059d1da1cde7c9e24ebccaf2aa654cc1ff6a4723bf12ae8e830fe940c` 和
`1159569857226ce8010744674ff9a07229f280678fd7458b80d03e3f4b0482a5`。
复验报告哈希为 `a65a80eaa4f557a7311e3506bb62387c12fa24df261b42d3f160e4a4d6a207d9`。

```sh
set -o pipefail
dir=reports/research/eth-two-four-hop-20261124/lifetime-79
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python "$dir/verify.py" | cmp - "$dir/verified.json"
```
