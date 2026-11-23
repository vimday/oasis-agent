# BSC 四池长尾采样与 33 池借还批次

北京时间 2026-11-24。用四座已在逐腿结算里核过的池，对固定 **192** 个区块做地址过滤。
返回 **476 条 Swap、459 笔交易、170 个有匹配的块**。命中最多的交易分别经过四座目标池
各一次，原始收据共有 **33 条 Swap、33 个逻辑池**。完整对账说明这是跨资产借还和兑换的
批次。单池和四池命中数均用于发现样本，串行深度仍需逐腿资金依赖。
[前次单池及 LP 样本](BSC_ALTERNATE_LONG_TAIL_20261124.md) ·
[此前 WBNB 批次](BSC_BATCHED_CLOSURES_20261124.md)

## 冻结范围与分母

固定高度 **135,308,800–135,308,991**，与已查单池窗口不重叠。三项匿名读取于北京时间 **01:29:57–01:29:58** 发出。
PublicNode 按四个明确的地址与两种 V3 Swap 事件查日志，以命中最多、独立地址最多、最早交易
为顺序选一笔。按指定块哈希取整块收据，再向 Triport 请求完整调用树。同一范围原账本
**3/3**，PublicNode 2 次、Triport 1 次，费用 USD 0。[PublicNode BSC](https://bsc.publicnode.com/) ·
[Triport BSC](https://triport.io/bsc-rpc)

| 预先锁定的池 | 匹配日志 |
| --- | ---: |
| WBNB/USDC，`0xf2688f…0612c4` | 52 |
| USDC/BUSD，`0x225360…8e5e6` | 2 |
| WBNB/USDT，`0x172fcd…46f849` | 417 |
| USDT/BUSD，`0xc98f01…10718f` | 5 |

12 笔交易在目标集合内命中不止一次。按目标集合的事件数量，**≥10、≥20、≥50、
≥100、≥200** 五个桶均为零。这个分母仅覆盖四池，排名第一的整笔有 33 条 Swap
本身就表明筛查命中数可能远低于实际交易腿数。只解析规则预选的一笔整笔调用树，
其余 458 笔完整资金拓扑未知。

## 逐笔可见资金、费用和深度

交易 [`0x5979842f…3efa8b`](https://bscscan.com/tx/0x5979842fab01ead3c7e0e8a15de14ad9f9fe3bb38d19653fb30de031ba3efa8b)
位于 **135,308,916 / 8**。116 份整块收据核对同块哈希和交易位置，目标 130 条有序
日志与 Triport 的 550 个调用帧交叉匹配，2 个显式失败帧保留。收据 gas 用量由累积差核对。
本轮三读没有取区块头来重算收据根，也没有取交易前状态。

33 条 Swap 分为 4 条 V2、8 条 V3、7 条 Pancake V3、8 条 Infinity CL、6 条 Uniswap V4。
两个 manager 的 `PoolId` 从调用 `PoolKey` 重算，返回的结算 delta 与所发事件一致。
本笔 V4 事件费用范围为 **3–625 pips**，Infinity CL 为 **3–800 pips**。
这些是已成交事件值，后续状态下的费率和报价缺失。
[Uniswap V4 PoolManager](https://github.com/Uniswap/v4-core/blob/main/src/PoolManager.sol) ·
[Pancake Infinity CLPoolManager](https://github.com/pancakeswap/infinity-core/blob/main/src/pool-cl/CLPoolManager.sol)

所有 Swap 位于同一个对外资金调用的子树。资金方 `0x8f73b6…318e5d8c` 向执行器
`0x632edb…06d7c46` 送出四种资产，随后分别收到同币、同金额偿还。逐腿输出与输入
只在同资产、同整数金额且有唯一资金证据时相连，检查了直接走池、路由中转和可确认的
原生币包装路径。当前 **33** 条中没有满足这一整腿连接条件的配对。部分产出进入同一个
执行器余额，投入也可来自四种预先借入的资产，无法逐腿唯一归属。33 个不同池
同时说明池地址去重也不足以证明串行深度。

| 可见项 | 原整数 |
| --- | ---: |
| 借入并归还 `0x8d0d…8b0d` | 5,752,048,018,733,769,717 |
| 借入并归还 USDT | 409,793,814,843,564,814,141 |
| 借入并归还 WBNB，wei | 33,006,041,096,329,447 |
| 借入并归还 USDC | 729,369,418,190,182,636,676 |
| 交易 gas / 收据有效单价（wei） | 4,575,410 / 0 |
| 执行器的 WBNB 解包并全部另付，wei | 41,080,884,987,228 |
| 其中付给 `0x1266c6…dd9c5f20`，BNB wei | 12,324,265,496,168 |
| 付给 `0x3e4934…0f411398`，BNB wei | 28,756,619,491,060 |

按外层发送者和执行器合并，结算后的原生 BNB 与 WBNB 变化均为 **0**。其余 ERC-20
分别增加 **569,634,354,815 USDT**、**22,010,259,312,214 USDC**、
**4,569 BUSD** 和 **8,377,256,950,282 个 `0x8d0d…8b0d`** 最小单位。
两个原生币付款地址单列，其控制关系未核。零 gas 单价只属于该收据，不能作为回放或未来
执行费假设。没有同刻可兑现估值、预交易状态或下一块重报价，净收益统一折算与时效均未知。

## 离线复跑

忽略目录 `reports/research/bsc-four-pool-tail-20261124/` 保留范围、账本、三项
原始请求与响应、排名和逐腿资金数据。下方命令复跑冻结样本。

```sh
set -o pipefail
evidence=reports/research/bsc-four-pool-tail-20261124
.venv/bin/python "$evidence/replay.py" | cmp - "$evidence/verified.json"
```

| 文件 | SHA-256 |
| --- | --- |
| `scope.json` | `554c72b030e06409a777c4523163b5cb3e3727d75fcebb0ee51b73b48795df19` |
| `logs.response.json` | `222e283a65fe8423ea6dc08c9cb46bd6eca62716c8378bc6648efc19e515b140` |
| `receipts.response.json` | `2b1c2383a2c8d7706031880003f6e37735fe8972347a665394986025635e1996` |
| `trace.response.json` | `943c07e828c1145ed14bf1d3b23842247b6e34d181a8ecfa69796129f3535532` |
| `verified.json` | `a6a63e5ecaeb54e6fa72aa03fac616a22c421647811d014d7e9c29ea7e8a4a4e` |
