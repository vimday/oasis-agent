# Robinhood 固定窗口长尾和十池样本

北京时间 2026-11-24 10:03，一次官方匿名 `eth_getLogs` 查询预先固定的
**121,590,369–121,590,880**。过滤器纳入官方 V4 PoolManager 和此前固定块验证了工厂身份的
16 个 Uniswap V3 池地址。两类 Swap 事件按 emitter、topic 和 ABI 复核，范围与此前留存的
日志窗口分离。[此前池集与资金复核](MULTICHAIN_LONG_ROUTE_SAMPLES_20261123.md)

| 口径 | 结果 |
| --- | ---: |
| 高度 / 有匹配事件的高度 | 512 / 322 |
| 官方 V4 / 名单内 V3 事件 | 622 / 325 |
| 可见交易 | 805 |
| 交易所含 1、2、3、4、5、6、10 条匹配事件的频数 | 722、49、23、5、2、3、1 |
| 达到预注册 20 条门槛的交易 | 0 / 805 |

该范围用 **1/4** 个原始预留，费用 USD 0。原范围在门槛判断后结束。最高者位于块
**121,590,404**、索引 **1**，筛中 10 个不同 PoolId 的 10 条 V4 事件。其交易哈希为
`0xd702baf3702ca2ab278bb09d78c9e519db96ff2d782ae505f057b1b8d9b585fe`。

## 独立三读复核

另一预注册范围只复核上面的确定性最高者。官方区块头重算哈希，PublicNode 通过固定哈希
返回 **15** 份区块收据，完整重建
`0x1639c4fdcd46a54d7abfe1ff598159b703e40925b627d9a285dc9df6bc3640ac`。
目标收据成功，含 **132** 条日志。除窗口筛中的 10 条官方 V4 事件，还有 **9** 条 ABI
符合 V3 Swap 形状的日志，来自 9 个名单外 emitter。两层统计分别是 10 条筛中事件和
19 条收据内同形事件。新增 emitter 缺少工厂身份核验。

收据的 gas 为 **5,713,676 × 21,900,000 = 125,129,504,400,000 ETH wei**，
`gasUsedForL1` 报告为零。按 62 条 ERC-20 Transfer 形状日志分别累计，顶层发送者和目标均无
可见代币差额。第三次 Triport 独立 `callTracer` 在收到 HTTP 头之前超时，原始请求和
`STOP` 已保留。本范围耗用 **3/3** 个匿名免费预留。成功资金腿、最长串行深度、分流、
直接原生币支付和整笔净额均未知。

## 证据与回放

忽略目录 `reports/research/robinhood-tail-512-20261124/` 包含两份冻结计划、两个原始账本
和实际响应。范围日志原始体 SHA-256 为
`374b66aa821020bd635ced6375afb6d62395a7a571fd94957a036b825b059159`。第二范围的
`STOP` SHA-256 为 `20c94087709374b40afc0631dc15418ce5e6efd07207c191a970e586c6640fb4`，
部分真值摘要 SHA-256 为 `f018aa08683bb178d4d2929cf95f2313da48c76c2137ff59242902c8614e5f7c`。

```sh
set -eo pipefail
evidence=reports/research/robinhood-tail-512-20261124
PYTHONPATH=. .venv/bin/python "$evidence/replay.py"
PYTHONPATH=. .venv/bin/python "$evidence/forensic/replay_partial.py" |
  cmp - "$evidence/forensic/partial-verified.json"
```
