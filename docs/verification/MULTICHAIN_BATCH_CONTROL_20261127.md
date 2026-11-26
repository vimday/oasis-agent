# Base 与 BSC 事件数、闭环和费用对照

北京时间 2026-11-27。新增验证一笔 BSC 实际交易。25 次交换分属 8 个外层交换分支，
其中可证一条四跳和五条三跳闭合资金路线。相同执行器曾有 60／80 次交换的 WBNB 批次，
这个新样本的资金跨 USDT 与 BNB，单看锚定币扣费已为负。Base 的 50 次 V4 交换落在
两次 unlock 内。另一个 20 事件的 Universal Router 样本可证两条跨 unlock 的等额资金链。
[机器可读证据](MULTICHAIN_BATCH_CONTROL_20261127.json) · [BSC 60/80 次交换参考](BSC_BATCHED_CLOSURES_20261124.md)

## BSC 新样本及原分母

只在既有 BSC 八地址 `135,377,727–135,378,110` 窗口内选样。本链匹配 365 个有事件块、
4,000 条明示池和 manager 事件、2,900 笔交易。命中 `≥20 / ≥50 / ≥100 / ≥200` 的交易
分别为 `2 / 0 / 0 / 0`。其中一笔 36 命中已核验。另一个 20 命中候选
[`0xd4f4…ec91`](https://bscscan.com/tx/0xd4f463f5087b8e57ec89bd65932943a055816466e0f41489e0e8903e5f7fec91)
的冻结取数首读收到 PublicNode HTTP 200 与空 `result:[]`。保留 37 字节回复、一次
预留和 STOP，没发第二源请求。该笔仅可证旧窗口内的 20 次匹配，整笔交易形态仍缺证。
[原分母和两笔首选](BSC_PAGED_LONG_TAIL_20261124.md)

另以已经保留完整收据的两个区块为独立留出范围，排除先前两笔，取匹配形态事件最多的
成功交易。两块共有 230 份收据，其中 222 份成功。去除原两笔后还有 220 份成功，样本是 **135,377,965 / 14** 的
[`0xa578…9792`](https://bscscan.com/tx/0xa578d37f34bf73830b3464884ad72c07a4d22e806f55a0dd9156b2b24c5c9792)。
冻结后只发一个 Triport `debug_traceTransaction(callTracer, withLog)` 免费请求。
在该笔的块内重算 173 份收据的根，完整核对 181 个调用帧与 66 条有序日志。区块哈希
`0x4d5a78ccab70016295404213a7995a57229d6c530d8c207a16991ddb9fd46292`，没有失败帧。
[BSC RPC 方法](https://docs.bnbchain.org/bnb-smart-chain/developers/json_rpc/json-rpc-endpoint/) · [Geth callTracer](https://geth.ethereum.org/docs/developers/evm-tracing/built-in-tracers#calltracer)

新交易与旧 60／80 样本同执行器 `0x9af0fb60af553be5aeb7a81372f30a462b24b04d`。
25 次交换涉及 18 个逻辑池，8 个外层分支各有 `5 / 4 / 1 / 3 / 3 / 3 / 3 / 3` 次交换。
五条 USDT 闭环中一条四跳、四条三跳，累计成交输入 `27,819,951,074,771,486,171`
原单位，合计毛差 `29,589,474,085,627,677` USDT 原单位。剩余一条已证闭环三跳，以
`16,464,309,764,969,069` WBNB wei 支付，原生 BNB 出口 `16,473,397,495,181,892 wei`
全部重新包装。剩余六个单腿事件没有唯一的更长串行归属。累计成交输入无法推出同额期初
库存或外部融资。

| 同链样本 | 交换事件 | 外层交换分支 | 最长已证资金链 | 资金结构 |
| --- | ---: | ---: | ---: | --- |
| 新 `a578…` | 25 | 8 | 4 | 5 条 USDT 闭环，另 1 条 WBNB→BNB→包装回 WBNB |
| `893c…` | 52 | 12 | 5 | 5 条严格 WBNB 闭环，其余连边未决 |
| `14f9…` | 63 | 1 | 2 | WBNB、USDC、USDT 三笔本金在同一子树内借出并逐资产等额归还 |
| 旧同执行器 `b7ed…` / `10f4…` | 80 / 60 | 20 / 15 | 各 4 | 所有分组均为四跳 WBNB 闭环 |
| `3a24…` | 89 | 23 | 4 | 20 条四跳、3 条三跳，其中一次末端重新包装 |

`a578…` 用量 `1,721,363 gas`、有效单价 `50,000,000 BNB wei/gas`，实付
**86,068,150,000,000 BNB wei**，并由执行器向 `0x1266c6…c5f20` 直接支付
**349,849,511,770 BNB wei**。该地址与对应区块头费用地址不同。发送者及执行器合计
观察到 **−47,435,492,252,789 BNB 等价 wei** 与 **+35,871,740,132,323,228 USDT 原单位**。
前者已含 gas 和直接付款。后者至少需变为 `47,435,492,252,789 BNB wei` 才能覆盖前者；
额外清算成本需要更高实际到账。没有同状态的可兑现反向报价，净盈亏保持未知。与之相比，
旧同执行器 `80 / 60` 两笔只留 WBNB 余额，扣 gas 和可见直接付款的净额能分别复算为
`+10,459,079,088,425 / +9,245,623,944,707 BNB 等价 wei`。

## Base 的分支和跨 unlock 资金

两笔均复用 Base 本链块头、目标收据和独立调用树，重放全部有序日志。此前未保留整块
收据全集，本次也未计算 Base 收据根。50 事件样本的 2,822 帧对应 402 条日志，18 帧显式
失败。对两个 Zora hook PoolId 的 50 个 calldata、PoolKey 与 BalanceDelta 均核对成功，
两个成功 unlock `[2]` 和 `[4,2]` 各有 5、45 次交换。观察到两种不同 ERC-20 资产的
结余，无法证明闭合本金。其 L2 执行费加 L1 数据费为 `65,609,582,501,257 wei`。
[Base 50 事件样本](https://basescan.org/tx/0xbc860b989bc68c8f449e3a6d4a59bdc28a0cd898c6d652fd74db80393c961246) · [Base 费用说明](https://docs.base.org/specifications/transactions/network-fees)

20 事件的 Base Universal Router 样本的 236 帧与 64 条日志一致。常规解码已证 V3 资金
路径 `89→100`。另有 10 条 V4 形态日志，三个 unlock 的 V4 数为 `5 / 3 / 2`。其中
`53 / 76 / 83` 带 hook、两侧 core delta 均为零，但返回给调用者的 delta 非零。以下
两组核实了 V3 输出、同资产 hook 收款、路由器持有期间无同资产干预、再次结算，以及
下一个无 hook V4 的实际整数输入。相邻两次 unlock 共享这些中间资产，不能据此拆成
两套财务闭环。带 hook 的零 core-delta 事件与其内部 V3 经济动作分别保留。
[Base 20 事件样本](https://basescan.org/tx/0x4f05bfa344adcf8b3cd6707f2a74fcc632a7b2299d4b2284717cc7db4136d456) · [Base Universal Router 部署](https://developers.uniswap.org/docs/protocols/v4/deployments#base-8453)

| 资金链 | V3 池输出原单位 | hook 转付款原单位 | 付给下一 V4 的原单位 | 资金日志 |
| --- | ---: | ---: | ---: | --- |
| `72→95`，资产 `0x0b3e…7e1b` | 6,209,483,487,124,025,568 | 1,862,845,046,137,208 | 6,207,620,642,077,888,360 | 70、74、85、93 |
| `79→96`，资产 `0x22af…6f3b` | 5,525,654,374,574,511,564,692 | 1,657,696,312,372,353,470 | 5,523,996,678,262,139,211,222 | 77、81、86、94 |

路由器和发送者合计以 `167,990,000` USDC 最小单位支出并收取另一资产。日志及 WETH
规范包装证明有 `47,019,154,042,609,331 wei` Base 原生 ETH 流入并全部重新包装，合计
WETH 余额归零。L2 执行费加 L1 数据费为 `21,625,816,958,059 wei`。两份 Base 收据均缺
operator fee 字段，未以零代填。现有证据没有对两个 Base 样本的净利润作同币核算。

## 离线复核

新 BSC 范围两次原预留中实际发出第一源一次即停止。不同归档交易的独立范围实际另有
一次免费的 Triport 读取，trace SHA-256
`9cc5141166e13ae99e70d2114d338aa81eea3ff4d97d647787cdeb20c98cc5ff`。计划、原始账本、
STOP 和线级字节保留在 `reports/research/multichain-batch-control-20261127/`。重新计算
新 BSC 取证及最终机器证据均逐字节一致。最终 JSON SHA-256
`75638996e307ae1b6c8f3d7aa2d82223c38ac72a170765785a5b9cd0388f3a09`。

```sh
set -o pipefail
scope=reports/research/multichain-batch-control-20261127
PYTHONPATH=. .venv/bin/python "$scope/a578-trace/complete.py" replay | \
  cmp - "$scope/a578-trace/verified.json"
.venv/bin/python docs/verification/fixtures/replay_multichain_batch_control.py \
  reports/research | cmp - docs/verification/MULTICHAIN_BATCH_CONTROL_20261127.json
```
