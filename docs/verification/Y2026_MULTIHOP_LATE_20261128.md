# 2026 年 Robinhood 与 Gnosis 多跳成交补样

十个独立窗口覆盖 **1,000 块、510 笔新多事件候选**，12 笔按独立源重建整块收据根。Robinhood 核出一个先取 USDG、三池闭环还本、第四池变现的成交。Gnosis 最大的 15 事件成交用同一笔借款换入中间资产，再拆成 12 次销毁和 13 笔 Balancer V2 兑换，最后兑回 wxDAI 并原额还款。[逐笔原始证据摘要](Y2026_MULTIHOP_LATE_20261128.json)

## 窗口与覆盖

在 11 月 28 日 UTC 冻结两链各五个固定偏移窗口和 16 个市场事件签名。Robinhood 排除既有范围并去重旧目录交易。Gnosis 是本目录首次覆盖。筛查保留 ≥200 开放尾档，按事件数、不同地址和区块序号依次排序，每个区块最多取一笔。两链均通过官方端点筛查，独立 PublicNode 按哈希返回整块收据，逐笔校验入块顺序、全部日志、累计 gas、Bloom 和收据根。12 个核验块合计 **643 份收据、342 笔回滚**。其余区块只筛固定事件，全量交易与回滚数量仍未知。[Robinhood RPC](https://docs.robinhood.com/chain/connecting/) · [Gnosis RPC](https://docs.gnosischain.com/about/networks/mainnet/)

| 链 | 完成区块 | ≥2 事件 | ≥3 | ≥5 | ≥10 | ≥20／50／100／200 | 最大事件数 | 重建收据根 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Robinhood | 640 | 36 | 18 | 5 | 0 | 0／0／0／0 | 9 | 6 |
| Gnosis | 360 | 474 | 373 | 42 | 3 | 0／0／0／0 | 15 | 6 |

## Robinhood 的三池还本与第四池变现

`0x77713285` 从 V4 Manager 取 **2,125,077,892 个 USDG 最小单位**，按已证的实转金额依次执行日志 55、59、60。第二池完整回款 **2,128,062,505,828,334,263,120** 个中间币最小单位实际进 V4，同一 `PoolId` 的 Swap 和转账确认 V4 返 **2,127,633,640** 个 USDG 最小单位。日志 63 将本金原额送回 Manager，剩 **2,555,748** 个 USDG 最小单位进第四池，收回并销毁 **1,548,838,235,878,175 WETH wei**。收据网络费是 **42,748,273,584,000 ETH wei**，已经包含 L1。若解包后的原生权益和付费人归同一主体，这两项差为 **1,506,089,962,294,175 wei**。最终原生受益人仍缺调用树验证。另一个 9 事件的样本由原持有人付出一种代币、收到另一种，只证实其中一腿。[三池加变现成交](https://robinhoodchain.blockscout.com/tx/0x777132856550f9f2c8d46aed268ac608efbdad79482bcaab8ea83283cbad4fb0) · [USDG 与 WETH 官方地址](https://docs.robinhood.com/chain/contracts/)

## Gnosis 的 15 事件分支和余额

`0x764425ad` 与 Balancer V3 Vault 间借还 **508,693,406,859,696,001 wxDAI wei**，日志 108、370 原额相等。起手 sDAI 包装铸得的份额比第一池实际付入少 **3,081,265,885 sDAI 最小单位**，说明首池还使用了额外份额。V3 与 V2 间出现两个相同金额、相同池端点、不同代币发出方的转账日志。这两个池的 `token0` 和 `token1` 在成交块哈希上读取，交集为 `0xcb444e90…`，因此资金接续只采用日志 **116**。V2 回款分为 12 次销毁及极小余额，13 条 Balancer V2 `Swap` 的代币、数量和调拨逐项吻合；铸得的中间代币等于投入各分支的金额加余额。结尾从 sDAI 收回 **509,645,207,902,167,115 wxDAI wei**，原额还给 Vault，执行器留下 **951,801,042,471,114 wxDAI wei**。全笔收据费为 **340,620,264,108 xDAI wei**，同一主体且包装币按面值时，单看这一币种扣费余 **951,460,422,207,006 wei**。另三种余额及原生付款的全成本仍待核。[15 事件成交](https://gnosisscan.io/tx/0x764425ad3f683a80a3f130764316b5e98d13c6f097e769bc0a913468d2393645) · [Gnosis 官方地址](https://docs.gnosischain.com/developers/Usefulcontracts) · [Balancer V2 Swap ABI](https://github.com/balancer/balancer-v2-monorepo/blob/master/pkg/interfaces/contracts/vault/IVault.sol) · [Balancer V3 Vault 资金机制](https://docs.balancer.fi/concepts/router/technical.html)

## 预算与复算

两链合计 60 次匿名 RPC 预留，两个 Gnosis 池的代币身份另用 4 次，支出 **USD 0**。初筛来自日志索引，所有逐笔结论来自完整且可重算的已入块收据。原始请求、响应、时钟摘要与预算账本均归档。

```sh
set -o pipefail
PYTHONPATH=. .venv/bin/python docs/verification/fixtures/verify_y2026_multihop_late_settlement.py replay \
  reports/research/y2026-multihop-late-settlement-20261128 |
  cmp - docs/verification/Y2026_MULTIHOP_LATE_20261128.json
```
