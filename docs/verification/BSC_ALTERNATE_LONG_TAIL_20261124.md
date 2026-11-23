# BSC 长尾复核的 19 条 WBNB 与 1 条 USDT 闭环

北京时间 2026-11-24。用原官方节点收据和 SQD 调用树拆解此前只按调用分组的
**80 Swap、43 池**样本。80 条全部归入 **20 条四跳资金闭环**，其中 19 条以 WBNB
偿付、1 条以 USDT 偿付。另对 7 笔旧高事件收据做同一转账逐腿筛查，覆盖内未核到
10 跳的串行资金链。计划采集的 96 个新块被 PublicNode 的按地址过滤要求阻止，实际
覆盖为 0。[原 43 池样本](LONG_SWAP_TAIL_20261123.md#bsc--80-条-swap-来自-20-个四次交换的调用分组) ·
[另两笔 42/32 池样本](BSC_BATCHED_CLOSURES_20261124.md)

## 完整资金归因

交易 [`0x004c…0bfe`](https://bscscan.com/tx/0x004c868e409e24143e699f6186b4ef8e6b339602f5e82201a3cf806013420bfe)
在 BSC **135,113,015**、区块索引 **3**，北京时间 2026-11-23 00:42:49。
原始 SQD 片段按交易索引抽出 **435 个调用帧**，与官方块哈希、交易哈希、发送方、目标、
状态、gas 和 gas 单价吻合。该旧样本仅取得了官方目标收据与独立索引，本轮没有整块收据根
重算。**100 条 ERC-20 Transfer** 均唯一匹配代币、付款人、收款人和整数金额的 CALL。
80 条 Swap 均通过池、方向、输入输出、实际收款人与对应调用绑定。20 个组内各有三条
同一转账行的资金依赖，60 条全部分解。20 组的末池均先把本金资产转给执行器，
随后执行器在回调内支付首池。[SQD 字段](https://docs.sqd.dev/en/data/evm/binance-mainnet) ·
[Pancake V3 回调](https://github.com/pancakeswap/pancake-v3-contracts/blob/main/projects/v3-core/contracts/PancakeV3Pool.sol)

- 首条 WBNB 路线的 Swap 日志是 `10 → 11 → 14 → 15`，中间资金转账日志是
  `8、7、12`。资金顺序由转账证明。
- 唯一的 USDT 路线是 `106 → 109 → 112 → 113`，沿 USDT → BUSD →
  `0x947950…12c4` → `0x2170ed…f933f8` → USDT。以
  `3,132,684,154,353,674,889` 最小单位偿付首池，末池给执行器
  `3,139,736,356,288,823,938`，分组毛差与整笔 USDT 净入账相同。

| 整笔可见量 | 原整数 |
| --- | ---: |
| 执行器 WBNB 增加，wei | +211,363,271,059,021 |
| 执行器 USDT 增加，最小单位 | +7,052,201,935,149,049 |
| 外层发送者 gas，BNB wei | −180,022,000,000,000 |
| 执行器原生币付款，BNB wei | −33,289,163,470,889 |
| 合并两地址后的 BNB 等值余额，USDT 另列 | **−1,947,892,411,868 wei** |

直接付款在闭环结束后，通过外层子调用 `[20]` 给
`0x4848489f0b2bedd788c696e2d79b6b69d7484848`，地址与区块头费用接收方不同。
按等小数位，剩余的 USDT 需要以至少约 **0.000276211 BNB/USDT** 计价才覆盖 BNB 缺口。
同笔交易中共享池 `0x172fcd…46f849` 的 19 个 WBNB→USDT 已成交比率合并得到
**560.192999 USDT/BNB**。按该比率反向估值、向下取整，费用后组合账面为
**+10,640,986,677,238 BNB wei**。这是同笔成交价折算。剩余 USDT 的实际变现、
链下成本和付款人与执行器的共同控制均未确认。

## 7 笔旧收据对照

冻结的回执筛查只接受可从一条 ERC-20 转账同时证明前腿产出和后腿投入的 V2/V3 路径。
manager 内部结算及经过另一账户的转账不在这个解码范围。记录的最长值是已证明深度。

| 区块 | 识别的 Swap | 直连可证最长 Swap 数 |
| --- | ---: | ---: |
| 135,110,280 | 80 | 4 |
| 135,110,349 | 66 | 2 |
| 135,107,388 | 64 | 1 |
| 135,110,314 | 60 | 4 |
| 135,110,445 | 59 | 2 |
| 135,109,788 | 52 | 1 |
| 135,110,310 | 50 | 2 |

50 条样本有两条明确的两跳 WBNB 闭环，Swap 日志分别为 `84 → 82` 和 `90 → 88`，
毛差共 **66,656,518,412,157 WBNB wei**。整笔的普通 WBNB 转账差
`−278,283,079,698,925,900` wei 被两笔包装与一笔解包完全抵消，收据 gas 单价为零。
其余路径、原生币用途及直接付款仍未完成归因，整笔净收益未知。
[50 条交易](https://bscscan.com/tx/0x1d2b773dd0dd2bd49ba7b7c7e27a8832c03a5ff17ed71de878ea5ae2c3cf6392)

## 新窗口的限制与留存

在同一原始账本预先限定最多 4 次匿名 HTTP、12 个逻辑读，USD 0。PublicNode 于
北京时间 00:57:43 返回 finalized 高度 **135,306,869**。预定窗口为 **135,306,726–
135,306,821**，与既有 BSC 样本不重叠。第二个请求携带六种事件签名，无发出事件的
地址过滤。PublicNode 返回 HTTP **403**、RPC **−32701**，要求指定地址，执行立即停止。
账本 PublicNode 2、Triport 0，保留原始拒绝。新窗口无可报告的交易或深度分母。
[PublicNode BSC](https://bsc.publicnode.com/) · [Triport BSC 方法](https://triport.io/bsc-rpc)

忽略目录 `reports/research/bsc-alternate-long-tail-20261124/` 保存浏览范围、
匿名预算、HTTP 请求与响应、拒绝、离线输入哈希及逐腿归因。原收据与 SQD 切片仍放在
`long-dex-20261123/expanded/binance-mainnet/` 与 `pretrade-tail-20261123/`。

```sh
set -o pipefail
evidence=reports/research/bsc-alternate-long-tail-20261124
.venv/bin/python "$evidence/verify_retained.py" | cmp - "$evidence/retained-verified.json"
```

| 留存 | SHA-256 |
| --- | --- |
| `scope.json` | `a61d1bc24bfc189441165b894d8833286b8dbe21ca09e802c0239d063e244e93` |
| `discover.response.json` | `4d0a8efd9f40ee01ed409376ccde7ab9f9b473e61e11605eafad7621e3e73960` |
| `STOP.json` | `54e9dc68a16657bc7bf31ab6c08142fdc71523eaf97954609059397cb5f4aba1` |
| `retained-verified.json` | `4ccf71024a1c227bbcbb533460898a439785bf6ffdad5470896dcb052e10a7fd` |

## 单池过滤补采的 52 次交换与 LP 仓位

北京时间 01:05:59 启动另一个固定 USD 0 范围。按原记录提前锁定 WBNB–USDC 池
`0xf2688fb5b81049dfb7703ada5e770543770612c4` 与前面区间相邻的
**135,306,822–135,306,869**。含明确地址的日志查询成功，48 个高度返回 **27 条匹配、
18 笔交易、14 个有匹配的块**。3 笔重复触及此池，最多 6 次。随后用命中区块哈希取
整块收据，并由 Triport 取排名第一笔的调用树。总预算 3/3，PublicNode 2、Triport 1。
覆盖只适用于这一座池。附带的 74 份收据核到同块哈希、交易位置以及 gas 累差，
本次预算未另取块头重算收据根。
[原单池在 20/15 组中的复用](BSC_BATCHED_CLOSURES_20261124.md) ·
[本次交易](https://bscscan.com/tx/0x0eff3f06fd5d5d1d783d286518ce81fee0acaf4d57e58c86a9e39da362ad8918)

最高者位于 **135,306,836 / 51**，共 **52 条 Swap、19 个逻辑池**。797 个调用帧含
37 个显式失败帧。成功调用恢复的 178 条有序日志与原收据完整匹配。34 条是 V3 类交换，
10 条为 Pancake Infinity CL，8 条为 Uniswap V4。manager 池均由 `PoolKey` 重算
`PoolId`，返回 delta 与 Swap 金额吻合。按唯一等额转账及原生币收付确认 33 条依赖，
其中最长可证五跳，有 3 条，均从 WBNB 经过其他币到 USDT。例见 Swap 日志
`240 → 242 → 243 → 247 → 250`，首腿输入 2,249,079,839,117 WBNB wei，
末腿输出 1,269,912,597,080,756 USDT 最小单位，中间两次 Infinity 交换之间有
2,249,216,077,301 原生币 wei 的等额收付。其余未证明的资金连接保留未知。

交易最终给原始付款人铸造 USDT/WBNB LP NFT **7,829,134**。Mint 返回与资金日志
核到 **140,491,537,708,867,534 USDT 最小单位**和 **250,056,215,799,228 WBNB wei**
入池。外层实际付 500,000,000,000,000 原生币 wei，退回 948,138,171,517 WBNB wei，
另付 241,097,400,000,000 wei gas。资金最终交付了 LP 仓位，仓位未估值，
整笔没有可计为闭环的套利净收益。[Pancake V3 仓位管理合约](https://github.com/pancakeswap/pancake-v3-contracts/blob/main/projects/v3-periphery/contracts/NonfungiblePositionManager.sol)

`single-pool-followup/` 保留独立账本、三个原始往返和逐腿结果。下方命令离线复跑并比较留存结果。

```sh
set -o pipefail
evidence=reports/research/bsc-alternate-long-tail-20261124/single-pool-followup
.venv/bin/python "$evidence/replay.py" | cmp - "$evidence/verified.json"
```

`trace.response.json` SHA-256 为 `7833336d0a35679dce805e7f76dcc713a82e0cf982ab68d5df02627aa33ea56f`，
`verified.json` 为 `f84053201ed032bfd65d68aab0512ad653a342076e550ae40b8d509d3978dbca`。
