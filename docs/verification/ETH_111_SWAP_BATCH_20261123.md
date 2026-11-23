# Ethereum 单笔 111 个 Swap 的资金路径核对

Ethereum 区块 25,818,126 在 UTC 2026-08-23 13:22:11 收录了一笔成功交易，索引 `45`。
按公开审阅采用的五类 Swap 计数为 **111 次交换、72 个逻辑池**。补解 Curve 后共有
**117 次池交换、76 个逻辑池**，另有 **2 个直接代币兑换腿**。47 次执行器自调用中，7 组
回滚，40 组成功。补齐兑换后的 40 组共有 **1 组两腿、39 组三腿**，逐组从 WETH 等值
起始并回到 WETH 等值。同额 ETH/WETH 封装与解包单列。这是有实际净差的批量套利样本，
为此样本分解出的最长经济路径 3 腿，无法推断 Ethereum 全局最大长度。

交易 `0x6da88e9f429ec1e43dbc0368b82048a546b7e21f31c82763846137556a075650`，块哈希
`0x8ba41b5d62a0718fc302b435f729c9b2e4f08f9eec3eeffa9ae1dc48771a5945`。样本线索来自公开
工程审阅的块号和缩略哈希。 [交易浏览器](https://etherscan.io/tx/0x6da88e9f429ec1e43dbc0368b82048a546b7e21f31c82763846137556a075650) · [公开样本出处](https://gitlab.kit.edu/uexco/RFC-Understanding-MEV-via-LLMs/-/blob/main/docs/design/mev-inspect-audit.md?ref_type=heads)

## 原始数据和重建

- 匿名 SQD 历史归档按块取 1,609 条日志和 88 笔带日志交易，恢复完整交易哈希。另取同块
  trace，用交易索引及对应哈希定位 1,379 条目标调用。SQD 是索引数据供应商。
  [SQD 归档接口](https://docs.sqd.dev/en/portal/evm/api)
- 匿名 PublicNode 返回同块哈希、105 笔有序交易及 `receiptsRoot`，指定交易在第 45 位。
  同一批的单笔 receipt 返回 `null`。另用匿名 dRPC 按块读取 105 份完整收据，目标 410 条
  原始日志的索引、地址、topics、data 与 SQD 全部一致。把所有收据依序编码重算 MPT，得到
  同一 `receiptsRoot` `0x572094955e0a7146aa75ebe6a99a559d6579b01bbce5395193961f1d4e73f029`。
  PublicNode 与 dRPC 是独立公开网关，收据根核验绑定这里观察到的区块头。
  [PublicNode Ethereum](https://ethereum.publicnode.com/) · [dRPC 区块收据](https://drpc.org/docs/ethereum-api/blocksinfo/eth_getBlockReceipts) · [Ethereum MPT](https://ethereum.org/en/developers/docs/data-structures-and-encoding/patricia-merkle-trie/)
- Blockscout 的首个无凭据交易 REST 请求为 HTTP 500，按预登记停止该源。两个历史归档请求、
  一个浏览器请求、两个只读 RPC HTTP 请求合计 5 次，$0，无签名或广播。各源先预占并保存
  请求、响应、时钟及 SHA-256。

## 111 次交换与补解的资金腿

| Swap 家族 | 成功收据事件 | 不同逻辑池 |
| --- | ---: | ---: |
| V3 型 | 48 | 27 |
| V2 型 | 25 | 25 |
| Pancake V3 型 | 18 | 1 |
| 官方 Uniswap V4 PoolManager | 15 | 15 |
| Balancer V2 | 5 | 4 |
| 五类小计 | 111 | 72 |
| Curve `TokenExchange`，补解 | 6 | 4 |
| 池交换合计 | 117 | 76 |

V2/V3 类型按事件 ABI 识别。V4 与 Balancer 按 `(合约地址, PoolId)` 计池。对 15 条 V4
事件，从同一执行器发起的 `PoolManager.swap` 原始调用参数读取两币及其余 PoolKey 字段，
重新哈希并与事件 PoolId 匹配，再按两个有符号余额差核对返回值。
[Uniswap 官方部署](https://developers.uniswap.org/docs/protocols/v4/deployments) · [PoolKey 源码](https://github.com/Uniswap/v4-core/blob/main/src/types/PoolKey.sol) · [PoolId 源码](https://github.com/Uniswap/v4-core/blob/main/src/types/PoolId.sol)

SQD trace 保留交易索引、行顺序、调用者、被调用者、输入及输出，没有调用树路径字段。按
执行器自身调用的起始行切分。七个回滚组可见 20 个属于这五类的尝试调用，它们未进入成功
收据。五类计数把 40 个成功组分为 9 组两跳和 31 组三跳，汇总与收据一致。每组首末
`WETH.balanceOf` 可核对成一条连续余额账，跨回滚段余额无缺口。

五类事件逐条匹配到已生效的调用。其中 31 个三腿组和 1 个两腿组可直接按资产及整数金额
接成闭环。剩余 8 组各补一腿，包含 5 个 Curve 旧式交换、1 个 Curve TwoCrypto 交换和
2 个直接兑换。Curve 按原始事件、调用参数及同额双向代币流核对。直接兑换按带两资产地址
和金额的日志、执行器发出的对应调用及两笔 ERC-20 Transfer 核对，未将兑换合约计作池。
补齐后每组输入输出资产及金额逐腿吻合，原生 ETH 首尾均有同组、同额 WETH 封装或解包。
40 组毛差分别等于各组的 WETH 余额增量，合计精确等于归还闪电贷后的余额增量。
[Curve 官方 TwoCrypto 参考](https://resources.curve.finance/developer/twocrypto-ng/pools/twocrypto)

最后一组三个 V4 PoolId 互异，执行器 `0xdadaa485c5a798871802eb89d474a31bca82ab0e`
在三条事件里均为调用者。该组先把 WETH 解包，完成三腿后把 ETH 重新封装。它的 WETH
增量 `25,110,359,002,873 wei` 与末腿减首腿精确相等。
[USDS 官方地址](https://developers.skyeco.com/guides/skylink/usds-ethereum-solana-bridge/#tokens)

| 日志 | PoolId | 输入 → 输出，整数原子单位 |
| ---: | --- | --- |
| 1073 | `0x2287a9620adcbf6250dc71be9ee9b2d3a1ec85a464fc6f5c06669e8d07b61bba` | 40,239,321,657,854,516 ETH wei → 98,994,080 USDT |
| 1075 | `0x3b1b1f2e775a6db1664f8e7d59ad568605ea2406312c11aef03146c0cf89d5b9` | 98,994,080 USDT → 98,990,120,590,728,314,395 USDS |
| 1078 | `0x08dfbbd62ff57f920b44457b5440fbd982d965e166ee482d36027a8d93df4e3e` | 98,990,120,590,728,314,395 USDS → 40,264,432,016,857,389 ETH wei |

## 可见资金流和成本

发送者 `0xf4f1a9c40c62788e60ea90b8fc246e4ab34077bb` 提交交易并付 gas，根调用带 `0 ETH`。
执行器由 Morpho 借入 `260,638,523,769,282,715 WETH wei`，复用资金执行各组，再在同一笔
交易同币同额偿还。开始 WETH 余额 `2,729,978,564,282,749 wei`，还贷后和付款前为
`5,661,382,620,404,833 wei`。WETH 转账和原生币封装、解包日志复核到相同余额差，其余可解
ERC-20 Transfer 对执行器的观察净流为零。 [Morpho 正式合约](https://docs.morpho.org/developers/contracts/addresses/) · [Morpho 闪电贷实现](https://github.com/morpho-org/morpho-blue/blob/main/src/Morpho.sol)

| 项目 | 整数 wei |
| --- | ---: |
| 40 组 WETH 毛差 | 2,931,404,056,122,084 |
| `9,788,061 gas × 132,362,671 wei/gas`，全额为 base fee | −1,295,573,897,870,931 |
| 执行器直付块头 fee recipient `0x4838b106fce9647bdf1e7877bf73ce8b0bad5f97` | −1,349,351,440,788,410 |
| 发送者与执行器合并后的可观察净额，ETH 等值 | **286,478,717,462,743** |

直付由最后的 WETH 解包日志和随后向同块 fee recipient 的最终原生币调用相互核对。两项已观测
成本约占毛差 90.23%。净额按 ETH/WETH 一比一合并观察账，发送者与执行器的实际控制关系、
区块费用接收方角色、链外支付及全局订单流均未验证。

## 离线复跑

忽略目录 `reports/research/serial-long-tail-20261123/ethereum/` 保留原始传输和资金分解。
工作区运行 `.venv/bin/python reports/research/serial-long-tail-20261123/ethereum/verify.py`。
同目录 `verify_group_assets.py` 复核 40 组逐腿资金分解。两个复核均用保留的收据、区块头和
按序 trace 重算，无需网络或账户。

| 保留件 | SHA-256 |
| --- | --- |
| SQD 日志 `archive.ndjson` | `8ed3424804a2caf58d53bbfb942662e79ecf3c6e5cfdde6a8f42ef8c9fc964ae` |
| SQD trace `archive.trace.ndjson` | `f35d206c5717dbcb08cb789b07c413af67494a814a126b1de894a3ad4ba3e717` |
| PublicNode `receipt.rpc.response.json` | `53809d6c6a3830d6db3ec51f41284dba21d7fd7d6669915579ef3be995b1d556` |
| dRPC `block-receipts.response.json` | `6a5329bbdcd5c8abc0679944a989ee7fbdda9ad6a11389bfd15635c77b06f13e` |
| `verified.json` | `b7e10d11d5dc2d6b8463a1b88b1d9ae78313641b06eef0fab1e5ebc814fbacdc` |
| `groups.verified.json` | `162aafbebd758e497944d1ce1ca12133dfe9f230df96a9f84ab405330c9dbe28` |
