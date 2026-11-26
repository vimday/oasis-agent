# Ethereum 从 20 个筛选事件到 54 段闭合资金流

原开放档案里的 20 个同额事件是整笔资金流的中段。对同一历史交易，核实了 **54 段串行
变换**从 `2` USDC 最小单位出发并回到 `469,261` USDC 最小单位，末后另用 Uniswap v1
将该输出换为 ETH。闭合流包含 52 个池交易或转换、1 笔 1inch 限价单和 1 次 Curve
单币 LP 赎回。最长连续池交易段为 **31**，在整条实际顺序中不重复中间资产的最长闭环
为 **8 跳**。这个样本给出了长路径真实资金证据，也记录了重复资产、限价单和赎回等
通用池扫描难以表达的动作。[55 腿逐项证明和账户转移](ETH_TWENTY_FLOW_20261127.json)

## 固定交易与证据强度

Ethereum 区块 `26,489,188`，北京时间 `2026-11-25 10:24:11`，交易索引 `266`，交易
`0xe8fe407861f22fcfc39e2914de3ead6e0a8c28e87c7ce26236ce3f7b5e369b6e`。
区块哈希为 `0xfa491c993de11545d6b611df12658159534db904b4003620ec692a2c2e682118`。
复核重新计算区块头哈希和全部 327 笔 typed receipts 的 trie root
`0xa73d065036455637032e230de8494d2269e96f8b485ba4bd812112d3eb09512b`，绑定区块的
交易序号，并将该收据 190 条日志全部与 556 帧 `callTracer` 的已提交日志逐字节及按序
匹配。原来源依次是 PublicNode 区块头、Nodely 全块收据和 dRPC 调用树。此轮只读离线
复核。交易索引与地址由留存证据交叉绑定，内部 ETH 付款和未提交调用来自同一条调用树。
[区块与交易浏览](https://etherscan.io/tx/0xe8fe407861f22fcfc39e2914de3ead6e0a8c28e87c7ce26236ce3f7b5e369b6e) · [Geth callTracer](https://geth.ethereum.org/docs/developers/evm-tracing/built-in-tracers#calltracer)

原开放筛选计 48 个兼容形状事件、48 个逻辑池，按相等金额识别其中 20 个。通用资金
重放只支持其中部分 ABI，留存输出为最长 4 跳。本次逐条核对资金日志、成功 CALL 或
执行器代理的 DELEGATECALL、返回值、池身份字段和具体结算语义，恢复 55 条有序动作。
54 段闭合流有 54 个不同的执行标识和 35 种不同资产，标识包括普通池地址、v4 完整
PoolId、Balancer vault poolId、限价单路由和 Curve 赎回合约。20 个原筛选事件是
日志 `966–1030` 的 DAI→wstETH 中段，入口 `230,310,965,611,019,320` DAI 最小单位，
出口 `176,461,793,151,999` wstETH 最小单位。

## 资金连接和失败分支

每个相邻资金连接都证明双方的资产与完整整数金额相同，并绑定实际经手地址或同一个
v4 解锁。55 个有序动作的 54 个连接中，23 个使用同一条池间转移，26 个通过执行器的
下一次同资产转移，3 个通过执行器和 v4 适配器，2 个通过单一 v4 unlock 内部的相反
delta。经手账户没有插入同资产的其他转移，连接间没有重复分配同一条转移。由此证明
观察到的连续供资关系。资产可互换，证据仍无法识别每个经手账户的独立库存来源、
历史成本、控制者身份或签名安排。

| 日志或路径 | 经手与计数依据 |
| --- | --- |
| `876` 开始 | `0xdf1f…2bfc` 的 AMM 调用收到 `2` USDC 最小单位，返回 SUSHI 并转回执行器。 `894` 再用实付、实收和正负返回 delta 绑定另一池。 |
| `904`、`927–929`、`1059` | 三个独立的解锁回调使用同一适配器 `0x66a989…ba8af`。重算五个 PoolId，核对 packed delta、hook 地址为零、入口 `settle()` 的实付和出口 `take()` 的回收。`927→928→929` 中间是两个无 ERC-20 转出的账内连接。 |
| `934` | 1inch `fillOrder` 中两笔 `transferFrom` 分别把上游输出交给订单 maker、再向执行器交付 `136,491` USDT 最小单位，输入、输出与返回整数一致。计为限价单。 |
| `961` | 代理样式池内实付 SHIB、实收 DAI。调用树三条 reverted 子帧均位于该池的辅助调用和检查分支，无提交资金日志。另有日志 `958` 向 `0xc412…2f1e` 的 SHIB 转移，付款人为池，未分配为执行器的重复费用。 |
| `967` | Balancer `GIVEN_IN`，`Swap` 事件先于实际 UNI 收款 `968` 及 USDC 出款 `969`。核对外部余额结算标志与 vault poolId。 |
| `1014→1020` | Bancor 单个五地址 `convertByPath` 里连续两个子 converter。日志 `1013` 把 `1,623,333,481,097,686,867` BNT 最小单位直接转给下一 converter，`1019` 再把后者输出直付下一 V2 池。 |
| `1034–1047` | 四笔 Curve 型交换由代币转移、selector、事件和返回量相互绑定。`1047` 是单币 LP 赎回，LP 通过 `1045` 销毁，DAI 由 `1046` 交给执行器。 |
| `1059→1062` | `1060` 向执行器交付 `469,261` USDC 最小单位，`1061` 按同额支付 Uniswap v1；`1062` 的 `EthPurchase`、`tokenToEthSwapInput` 返回值及原生 ETH 转账核对一致。 |

未统一识别品牌的交易保留池地址、ABI 形状与资金证明。27 个 V2 调用均只剥除留存
calldata 末尾的一整个零字后按空 `bytes` 的标准 ABI 解码。该规则仅用于这个历史
fixture。第一大段为 17 次连续池交易，穿过 1inch 限价单后是 31 次连续池交易
`938–1044`，穿过 LP 赎回后是 4 次至 USDC 闭合。8 跳简单闭环 `909–928` 的 USDT
由 `54,679` 变为同一 unlock 中的 `114,062` 最小单位，随后继续支付 `929`。局部毛差
没有单独可分摊的交易成本，不能与整笔净额相加。
[Uniswap v4 PoolManager](https://github.com/Uniswap/v4-core/blob/main/src/PoolManager.sol) · [Balancer Vault ABI](https://github.com/balancer/balancer-v2-monorepo/blob/master/pkg/interfaces/contracts/vault/IVault.sol) · [1inch OrderMixin](https://github.com/1inch/limit-order-protocol/blob/main/contracts/OrderMixin.sol) · [Curve 3pool](https://github.com/curvefi/curve-contract/blob/master/contracts/pools/3pool/StableSwap3Pool.vy) · [Uniswap v1](https://github.com/Uniswap/v1-contracts/blob/master/contracts/uniswap_exchange.vy)

## 费用和可观察余额

收据根中的前后累计量之差确认使用 `4,349,452` gas。收据 `effectiveGasPrice` 为
`56,698,735 wei/gas`，等于头部 base fee。以下付款以原生 ETH wei 计，另保留 USDC
起始投入，无跨币换算。

| 字段 | 整数金额 |
| --- | ---: |
| 闭合后的 Uniswap v1 ETH 输出，帧 `[165,0,1]` | 283,009,651,159,010 |
| 执行器向区块 fee recipient 的显式付款，帧 `[167]` | 179,497,026,312 |
| 执行器向交易发送者结算，帧 `[168]` | 282,830,154,132,698 |
| 交易 gas 费 | 246,608,426,343,220 |
| EIP-1559 priority fee | 0 |
| 发送者与执行器合计所见 ETH 差额 | **+36,221,727,789,478** |
| 两地址合计所见 USDC 差额，USDC 最小单位 | **−2** |

执行器 `0x50d3865a63d52c0a54e4679949647ea752107390`，交易发送者
`0x2ee36e41387f87b7e6f678a86d1e575b23b996f5`，区块 fee recipient
`0x6adb3bab5730852eb53987ea89d8e8f16393c200`。费收款者独立保留在数据中，不能从路径
推断任何账户共同控制。所见差额取两地址的已提交 ERC-20 变化、可见原生付款和 gas。
未见独立前后余额、链下费用及本金成本的证明。

## 复核输入与命令

原始目录为 `reports/research/eth-open-archive-20261127/`。验证器只读本地文件。可自可用的
公有归档接口获取同交易和同区块，将 JSON-RPC 请求与响应按下表命名为
`<前缀>.request.json`、`<前缀>.response.json`。请求使用 `id=1`、`jsonrpc="2.0"`。可携带
原有 `<前缀>.meta.json` 核验原始字节哈希。重新取得等价响应时，仍核验同一区块哈希、
收据根、调用树日志及逐腿结算。

| 文件前缀 | 方法和参数 | 留存响应 SHA-256 |
| --- | --- | --- |
| `26489188-header` | `eth_getBlockByNumber`，`["0x1943164", false]` | `bc96a84c11fc3e6cfef9cc7d57ae242e558fc06fad4d2f63d17aa90990cb36d2` |
| `26489188-receipts` | `eth_getBlockReceipts`，`[区块哈希]` | `6a743bd3c8bde6a5a03d8aa954b7bc187c423b52662b3ee33ba99bc78864b8b4` |
| `e8fe407861f2-trace` | `debug_traceTransaction`，`[交易哈希, {"timeout":"10s","tracer":"callTracer","tracerConfig":{"withLog":true}}]` | `49ed48d68aadfab0ceb372d742124ecf7f8cec0038aec2c50e2532433c5a3d03` |

从原文复算通过，逐腿证明 JSON 的 SHA-256 为
`9b2c29c3b308381efe28d3053b8e40ccf6b291147dbb8f8d1c05863c0fce554c`。聚焦 Ruff 和
`git diff --check` 通过。[离线证据脚本](fixtures/replay_eth_twenty_flow.py)

```sh
set -o pipefail
.venv/bin/python docs/verification/fixtures/replay_eth_twenty_flow.py \
  reports/research/eth-open-archive-20261127 | cmp - docs/verification/ETH_TWENTY_FLOW_20261127.json
```
