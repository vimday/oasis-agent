# Ethereum 272 次转账指纹与 109 条计数

北京时间 2026-11-27。单日公共 Ethereum 日志中，四笔交易正好有 **272 条 ERC-20
Transfer**，只有其中一笔匹配上一轮的八种交易事件。该笔在 **26,405,358** 区块、交易
索引 **44** 落块，匹配 97 条。排除零核心差额，并补入六类日志后，一套明确的计数规则得到
**109 条**。同一交易可核实四组 WETH 等额归还和资金分支，合计向同一地址转出
**9,163,708,652,972,954 WETH wei**，其链上执行费为 **1,454,316,959,357,430 wei**。
这是一笔高度吻合的链上候选。作者尚未公布哈希或计数规则。
[该笔交易](https://etherscan.io/tx/0x1a52ee375bcdbe79ec67824cbb2a4d2cea35fd095fe5a93c79626fe02f5874f8) · [原帖](https://x.com/aliez_ren/status/2121185236390400329) · [更正帖](https://x.com/aliez_ren/status/2121232963820270022)

## 时间和覆盖

AWS Parquet 导出交易的全部 **473 行**，块内日志索引连续覆盖 **474–946**，涉及
**27 个 ERC-20 转账事件发出地址**。全部行的交易、区块和时间一致，块哈希为
`0xf278ef51e04af84e397e8cc5bd6f2de0fe0d8487ab0ac175a8fe9094b261fb21`，
区块时间为 **2026-11-13 10:10:59 UTC**。根据原帖 Snowflake ID 解码，
发帖时间为 **10:15:44.728 UTC**，相差 **285.728 秒**。此前公开研究采用的费率示例
区块 26,405,382 的时间为 **10:15:47 UTC**，两块相差 24 个高度和 288 秒。
AWS 日志表提供块标识和时间。这份导出没有完整区块头或收据根。
[AWS 公共链数据目录](https://registry.opendata.aws/aws-public-blockchain/) · [X 的时间戳 ID](https://docs.x.com/fundamentals/x-ids) · [原费率示例](../strategies/MULTIHOP_ARBITRAGE_RESEARCH.md)

## 109 的复算规则

| 事件范围 | 行数 | 计入口径 |
| --- | ---: | --- |
| 原八种签名 | 97 | 36 V3 形态、33 V4、17 V2 形态、5 Curve、3 Curve 扩展形态、2 Balancer V2、1 Pancake V3 形态 |
| V4 核心两个差额同为零 | −9 | 索引 520、558、565、603、643、701、782、794、883 |
| 额外具名日志 | +16 | 9 条适配器 `Swapped`、2 条 Mooniswap 形态、2 条 Uniswap V1 `EthPurchase`、2 条旧 Curve Crypto、1 条 Fluid |
| Ekubo 形态的无主题日志 | +5 | 索引 522、542、629、678、692。均为 116 字节，锁定者字段与执行地址一致，两侧有符号差额异号且非零 |
| **所选规则合计** | **109** | 所选日志行数 |

这个规则可复算作者更正的数字。它没有为所有协议建立唯一的经济腿定义。例如索引
519、642、781、793 的 hook 结算日志与紧随其后的零核心差额 V4 日志共用 PoolId，
对应的稳定币转换仍有实际转账。索引 555 的 Pancake V3 之后还有零核心差额的 V4 外层
事件 558。索引 496 的适配器与前面的自定义池日志 495 同属一组收付。额外未计入规则的
具名日志也有双边资金，例如索引 888 附近的 USDT/DAI 收付。独立池总数和准确最长串行
资金深度仍未确定。研究使用合约事件布局辨认日志形态，对未识别池未逐池核验运行码。
[V4 PoolManager](https://github.com/Uniswap/v4-core/blob/main/src/PoolManager.sol) · [Ekubo Core 的 log0 布局](https://github.com/EkuboProtocol/evm-contracts/blob/main/src/Core.sol) · [Mooniswap](https://github.com/1inch/mooniswap/blob/master/contracts/Mooniswap.sol) · [Uniswap V1](https://github.com/Uniswap/v1-contracts/blob/master/contracts/uniswap_exchange.vy) · [Curve Crypto](https://github.com/curvefi/curve-crypto-contract) · [Fluid 合约](https://github.com/Instadapp/fluid-contracts-public)

## 四段本金与可见毛额

执行地址 `0x96cb0b6dd66cbd3201681b9f9588b7107084f9ea` 从 V4 PoolManager 收入
本金。每组随后有等额 WETH 转回同一 manager，再向
`0x610aa9ce9da1360852c3419f93a5f7d424379aa0` 支付 WETH。

| 组别的日志范围 | 本金转入／转回日志 | 各段本金，WETH wei | 转至同一收款地址，WETH wei |
| --- | --- | ---: | ---: |
| 474–568 | 474／567 | 5,408,843,843,337,289,531 | 3,729,679,746,767,616 |
| 569–709 | 569／708 | 3,225,536,614,437,827,752 | 2,465,647,606,643,522 |
| 710–848 | 710／847 | 1,945,333,917,862,349,312 | 1,952,279,017,095,705 |
| 849–946 | 849／945 | 1,262,678,576,234,012,672 | 1,016,102,282,466,111 |
| **累计可见付款** | | | **9,163,708,652,972,954** |

首组起始本金立刻分成两个 WETH 输入，分别出 USDT 和 USDC。USDT 一笔收入
**9,345,791,866** 最小单位，随后拆成 **2,170,921,544、1,725,585,629、5,449,284,693**
三笔投入，其中第三笔换得另一份 USDC。两份 USDC 收入合计 **6,669,859,933**，
随后分成七笔支出。五笔 WBTC 流入合计 **10,902,087**，随后分成四笔。这样的实收实付
证明交易内有批次、分流和合流。同币在执行地址汇合时，每一份输出归于哪一份输入仍有
歧义。第四组可复算五层分支资金结构，依次经过两条 WETH/USDC 兑换（853、858）、一次
USDC/USDT 兑换（880）、一次 USDT/WBTC 兑换（893）和两条 V4 兑换（914、917）。
USDC 分组包含前段结余 2 个单位，USDT 与 WBTC 的后续各笔支出分别与本组流入守恒。
914 产出的第三币 **84,334,519,893,118** 个单位由转账 915 和 916 等额交给 917。
分组口径没有为合流后的余额指定唯一来源，整笔最长严格串行深度仍留空。

执行地址涉及 26 个可见 ERC-20 转账发出地址。计入执行地址的两笔规范 WETH Deposit
后，四组的 WETH 账都归零。其余资产只有第三组 `+2`、第四组 `−2` 个 USDC 最小单位，
整笔结清。两笔 Deposit 对应本地址等额原生 ETH 流出，累计
**494,760,050,433,013,986 wei**。表中付款是可核实的 WETH 收款。

同日 AWS 交易表按交易哈希、块高、块哈希、交易索引与日志表四字段吻合。
type 2 成功交易由 `0x1d042c727d3f0b228a7438378e531773d1d8e353` 发往上述执行地址，
gas 上限 **14,650,521**，实用 **10,998,057 gas**，有效单价 **132,233,990 wei/gas**，
执行费 **1,454,316,959,357,430 wei**。把两地址的可见 WETH 收款与执行费作情景差额得到
**7,709,391,693,615,524 wei**。收款人与 gas 支付者是不同地址，控制关系、全部原生币
流量、直接构块付款和最终净盈亏未知。独立 Blockscout 取证在首个请求发生无 HTTP
状态或正文的传输失败后停止，未取得调用树。

原八签名在当天识别出 **794 笔**至少 20 条形态事件的交易，均与交易表匹配。此笔用量
降序第 **16／794**，执行费用第 **594／794**。全天 7,183 块、2,099,945 笔中，用量降序
第 **110**。高 gas 用量与本笔的低有效单价共同解释了这两个排名的差别。
[AWS 交易表和收据字段](https://github.com/aws-solutions-library-samples/guidance-for-digital-assets-on-aws/blob/main/analytics/consumer/schema/eth.md#table-transactions)

## 复算

只读导出对象
`v1.0/eth/logs/date=2026-11-13/part-00000-cdf4019e-abaf-4aee-a44f-fadb06776bcf-c000.snappy.parquet`。
源对象为 1,220,242,534 字节，SHA-256 为
`d52f744a7482c7a5e8e268fe90240d63f8ec920c14164ece5c3ba0aec4100588`。
原筛查查询、四条指纹结果和下载记录保存在忽略目录
`reports/research/eth-open-archive-20261127/`。本次使用既有 Parquet，新增链上读取为 0。

| 保留件 | SHA-256 |
| --- | --- |
| `logs.json` | `6728d75514fae32bc14e7359f978ad7c60a5c635ecfd6a3300a4597456aa788b` |
| `extract.sql` | `a5a75c5de4ca13732db1b0e0a0562a8ac8930c4eda60e653926ec317900dc74d` |
| `verify.py` | `d3f2160f7d7798e5b630ff9ce6253a92f366cad57015c271ea80f31bcd440b93` |
| `verified.json` | `0e3a6d7f8f60af80005cf2a80cddb3226ec24e537ce709ad6b79fe64c10561a6` |

交易表独立取证使用 1 次匿名目录列举和 1 次对象读取，源 Parquet 为 856,849,407 字节，
SHA-256 `43d1ce6259fd9bed3f78f8769cf74df7647d26592e6c09a310a2214a9f58b6b6`。
该取证的确定性输出 SHA-256 为
`262e732151e75d619edde00087c31a4fd24e909e90c8c5a0d3a0d64233a4c782`。
独立 Blockscout 停止记录的 SHA-256 为
`a85ce01e888580f1a90b13fbb4b9d693292b7b0d0a50b2d9a35d9c62c1d37ce1`。

```sh
set -o pipefail
dir=reports/research/eth-transfer-fingerprint-20261127
.venv/bin/python "$dir/verify.py" | cmp - "$dir/verified.json"
fees=reports/research/eth-transfer-fingerprint-fees-20261127
.venv/bin/python "$fees/verify.py" | cmp - "$fees/verified.json"
```
