# Ethereum 独立区间里的九池串行套利与五跳批次

北京时间 2026-11-24。新留出区间中核实到一笔 **9 个互异池的串行套利**，全程逐腿核对
资产、整数金额和实际调用。可见净额 **+76,553,183,579,326 ETH 等值 wei**。另一笔
10 条 Swap 被分解为两个各自闭合的 **5 跳**，它们共享 3 个逻辑池，整笔可见净额
**+10,261,395,873,886 wei**。区间内事件数最多的样本有 30 个池，逐池调用分组和多资产
余额另行核对。此前的十事件样本和同块四跳样本均在这个区间之外。
[此前十事件样本](ETH_LONG_SERIAL_EVIDENCE_20261124.md) · [此前同块四跳样本](ETH_ORDERED_FOUR_HOP_MARGIN_20261124.md)

## 盲采范围、分母与选择

冻结 Ethereum 区块 **26,477,200–26,477,263**，共 64 个连续高度。用 Nodely 读取
预定 7 种事件形态，包含 V2 类、V3 类、Pancake V3 类、V4、Balancer V2 和两种 Curve
事件。共有 **2,678 条匹配、1,753 笔交易**，覆盖有匹配事件的 63 个高度。按逐笔
预定形态计数，≥5、≥10、≥20、≥50、≥100 分别是 **64、13、2、0、0** 笔。

从等额整数连接、互异池、允许回调逆序的候选图选前两笔，再按事件数选一个对照。数值
粗筛的最大路径是 6，≥5 有 4 笔。图缺少代币身份、实际持币方和部分协议，结果用于
候选排序。真实九池样本中的两次 Bancor Conversion 就未命中预定过滤器。三个复核样本
里没有验证到 ≥10 个不同池组成的一条资金闭环。剩余区间没有逐笔完整追踪，长路径发生率
未知。
[Nodely 免费入口与限制](https://nodely.io/docs/ethereum) · [转账结构等价研究及适用边界](https://arxiv.org/html/2608.20377v2#A4.SS1)

匿名请求预留 **10/10**，USD 0，Nodely 4、PublicNode 3、dRPC 3。PublicNode 三个
区块头的哈希和 Nodely 三个区块内的 **938 份有序收据**分别重算，三个收据根全部通过。
三笔收据和独立 dRPC 调用树的 **37、28、113 条日志**逐项相符。签名交易和提交均不在范围内。
[dRPC callTracer](https://drpc.org/docs/ethereum-api/debugandtrace/debug_traceTransaction) · [Ethereum 收据树](https://ethereum.org/en/developers/docs/data-structures-and-encoding/patricia-merkle-trie/)

## 九个互异池如何闭合

交易 `0xce5a52338f6a5544607d880b50d75edc9c894bbe201fc05f74e1625952449998`
在区块 **26,477,242**、索引 **81**，UTC 2026-11-23 10:29:23。190 份收据重建根
`0x0fbc267e3b377a4a6b979d56f009df30691e9dd68b6ddf7d7e776cd12c6af45b`。
105 个调用帧支持从同一执行器持有的 WETH 开始并回收到该执行器。先解包
**1,267,301,312,590,498 WETH wei**，同额 ETH 用于首腿，解包单独记账。

| 腿 | 收据索引、协议和池 | 资产及输出，最小单位 |
| ---: | --- | --- |
| 1 | 557，V4 PoolId `0x2287…1bba` | USDT 2,182,565 |
| 2 | 559，V4 PoolId `0x8f4e…ac1c` | `0xb3a6…bc03` 46,543,485,059,834,023,306,328 |
| 3 | 562，V4 PoolId `0x9fdc…49ba` | `0xbf5e…eab7` 277,920,834,738,060,414,591,690 |
| 4 | 565，V4 PoolId `0xc46a…024d` | `0xb30f…9406` 503,819,305,130,909,061,847 |
| 5 | 568，V4 PoolId `0x0196…9699` | USDC 2,748,685 |
| 6 | 574，V2 型池 `0xa40b…f5c5` | `0x69af…3074` 10,212,160,666,165,313,536 |
| 7 | 578，Bancor 转换池 `0x9a2d…c928` | `0x1f57…7f1c` 12,765,394,420,910,310,408 |
| 8 | 585，Bancor 转换池 `0xf8d9…f670` | `0xa1fa…0975` 8,356,418,047,912,439,167,969 |
| 9 | 592，V2 型池 `0xf55c…4964` | WETH 1,614,009,568,591,872 |

每个 V4 PoolId 都从调用的 PoolKey 重算，返回的 BalanceDelta 与事件一致。两次 Bancor
转换分别核对网络入口 `convertByPath`、实际流动性池 `convert`、`Conversion` 事件、
币种与返回值。记录的八段衔接都是上一腿全量产出付给下一腿，含七段执行器中转和末段
池间直付。除 WETH 外，发送者和执行器的可解 ERC-20 净流为零。原生币出入均为首腿同额
解包和支付，未观察到额外付款。
[交易](https://etherscan.io/tx/0xce5a52338f6a5544607d880b50d75edc9c894bbe201fc05f74e1625952449998) · [Uniswap V4 PoolId](https://github.com/Uniswap/v4-core/blob/main/src/types/PoolId.sol) · [Bancor legacy 入口与池](https://github.com/bancorprotocol/contracts-solidity#overview)

投入与产出之差为 **346,708,256,001,374 WETH wei**。实际支付 **807,136 gas ×
334,708,243 wei/gas = 270,155,072,422,048 wei**，已消耗 77.92% 的毛差。保持原成交金额
与 gas 用量时，再增加 **94,845,457 wei/gas** 后仍有正净额。费用余量以已落块状态计，
下一状态的可成交性需要用独立回放校验。
[EIP-1559](https://eips.ethereum.org/EIPS/eip-1559)

## 两个五跳闭环与三十次换币对照

交易 `0x47340d7a3d3b4c2fd9562c46b7e3ff54596591b4ca67417c70515ccba2b9216b`
发生于 **26,477,205** 的索引 **45**，UTC 10:21:59。94 个调用帧分出以下两条实际
资金链。第一条有回调倒序。第二条的末两腿在同一个 V4 unlock 内等额抵消中间币。

| 路径 | 实际资金顺序，Swap 日志索引 | WETH 入 → 出，wei | 毛差，wei |
| --- | --- | --- | ---: |
| 先执行 | 191 → 192 → 193 → 184 → 194 | 579,644,088,368,286 → 624,926,312,146,927 | +45,282,223,778,641 |
| 后执行 | 209 → 210 → 197 → 202 → 203 | 453,482,069,592,034 → 497,428,722,492,949 | +43,946,652,900,915 |

两条闭环各用 5 个互异池，总计 7 个不同池。毛差合计 **89,228,876,679,556 wei**，
扣除实际 gas **51,376,015,112,685 wei** 和执行器向区块费用接收方支付的
**27,591,465,692,985 wei**，可见净额 **10,261,395,873,886 wei**。固定成交金额和
直付时，有效 gas 单价再提高 **16,373,461 wei/gas** 仍为正。样本记录实际共享池执行
顺序，调换前后顺序的结果未验证。
[五跳批次交易](https://etherscan.io/tx/0x47340d7a3d3b4c2fd9562c46b7e3ff54596591b4ca67417c70515ccba2b9216b)

交易 `0xf4bda3dc3a38d0e135641c595d97d37b44fdeb861f2b5dec8279a34675e77490`
在 **26,477,251** 的索引 **12**，UTC 10:31:11。30 条事件使用 30 个逻辑池，
分别位于 30 个不同的根调用分组，每组一条匹配 Swap。合并发起和执行地址后有 21 种
ERC-20 的可见余额发生变化，包含多种资产净支出。实际 gas
**346,175,159,855,612 wei**，向块费用接收方另付 **8,189,390,004,296,958 wei**。
该笔只保留多资产变化和 gas，整笔利润未知。
[三十池对照交易](https://etherscan.io/tx/0xf4bda3dc3a38d0e135641c595d97d37b44fdeb861f2b5dec8279a34675e77490)

## 原始档与离线复验

证据保存在忽略目录 `reports/research/eth-funded-holdout-20261124/`，包含冻结范围、
预留账本、十份请求和响应、取数时钟及独立资金重建。给定原始日志，离线复验会按预先
登记的规则重算选择，再从头部、收据和调用树重建完整金额。

| 保留件 | SHA-256 |
| --- | --- |
| `scope.json` | `5ab3a1cf58d19745069ca893ae2d9af96fd6602bd9bc3153829e6a486c066516` |
| `holdout-receipts.response.json` | `ed9dd912cd0bda95135b32d6b6c596881dc031451f0688f8ea1ef8f2d5f18b53` |
| `selection.json` | `5c19d5966f8bfb6d1947602eb865c84d0767f09664712683480072b636f92926` |
| `verified.json` | `ddc0c5de6423f77faa7a884f400e684d2c67bcb468733f88bb754fb21bf4d30f` |

```sh
set -o pipefail
dir=reports/research/eth-funded-holdout-20261124
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python "$dir/verify.py" | cmp - "$dir/verified.json"
```

## 九池原样调用在两个后续状态的寿命

补充范围只选择九池成交。四次匿名免费请求分别读取原交易、直接子块头和两份
`debug_traceCall`，4/4 成功。实际已成交的索引 81 提供原状态对照。以原始 EIP-1559
类型化交易重算哈希并恢复发送者，保留原始 calldata、from、to、value、gas limit、
费率上限和 access list。按两个区块的确切哈希调用，省略 nonce、签名、txIndex 和覆盖。
子块 **26,477,243** 的头部哈希重算为
`0xe73cda4913117843dbd9c29c555990fc04c32cf8cb4ba1a0ebd075898179a11e`，
其父哈希与原块匹配，块时钟相差 12 秒。
[Geth TraceCall 状态选择](https://github.com/ethereum/go-ethereum/blob/master/eth/tracers/api.go) · [dRPC 区块参数](https://drpc.org/docs/ethereum-api/debugandtrace/debug_traceCall)

| 状态 | 执行结果 | 模拟 gas 用量 | 若失败落块，仅 base fee / 原费率参数，wei |
| --- | --- | ---: | ---: |
| 26,477,242 块末 | V4 首腿最低输出校验回滚 | 144,391 | 12,679,818,397,350 / 48,328,857,915,013 |
| 26,477,243 块末 | 根调用空数据回滚，尚无子调用 | 120,280 | 10,453,890,107,120 / 40,150,107,137,160 |

在成交块末，原首腿输入仍是 **1,267,301,312,590,498 ETH wei**。V4 PoolId
`0x2287…1bba` 的返回仅 **2,182,461 USDT 最小单位**，比原有最低输出 **2,182,565**
少 104。对应的 `V4TooLittleReceived` 参数、带符号 BalanceDelta 和原 route key 均
逐整数吻合。直接子块只有根调用空回滚，具体检查点未定位。两次模拟均无生效日志、
无提交余额变动或实际 gas 扣款。若把两个失败情景的成本都按原交易的费率参数计入
同一观察账，其费用合计 **88,478,965,052,173 wei**，超过一次原成交净额
**76,553,183,579,326 wei**。这些是两个固定状态的反事实，区块内的失效时刻、自然
失败率和重复可成交性仍未知。
[Uniswap V4 最低输出错误](https://github.com/Uniswap/v4-periphery/blob/main/src/interfaces/IV4Router.sol) · [EIP-1559](https://eips.ethereum.org/EIPS/eip-1559)

`lifetime-81/` 保存新增账本、发送前固定的范围修订、原始报文和复验报告。修订在账本
使用为零时，把最初计划的交易前模拟改成成交块末，用已经验证的实际成交作对照。
两份 trace 原始响应 SHA-256 分别为
`7048a7e919a0260b0763d8e3c2a522aa1424873da4ea858f7aba9eab8d2199b9` 和
`ebdadcd8efff4adba8026d49a71ee02d319c14f9d09255214067f190dc5b6e3b`，
派生报告为 `aca7d53d936ab897d6d5b4ac601e328c0e2f3600e7a2676fb972504edd2c8bef`。

```sh
set -o pipefail
dir=reports/research/eth-funded-holdout-20261124/lifetime-81
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python "$dir/verify.py" | cmp - "$dir/verified.json"
```
