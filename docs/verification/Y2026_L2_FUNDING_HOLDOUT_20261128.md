# 2026 年四条 L2 的新样本与本金续用核对

2026-11-28 上海时间，Robinhood、Optimism、Arbitrum One 和 zkSync Era 在各链上次窗口后新增 **5,696 个块**的事件覆盖。全表含 1,522 笔至少两条市场事件的交易与 23 笔逐笔核对的目标收据。Era 四个市场分两轮等额复用本金。Robinhood 四池闭环的毛差低于链费。Arbitrum 从同一余额拆成一腿和三腿两支交叉结算。[机器报告和逐笔资金](Y2026_L2_FUNDING_HOLDOUT_20261128.json)

## 覆盖与选样

先冻结 16 个市场签名、4 个融资签名与分链预算。按市场事件数取不同块中的宽样本。Robinhood 追加的八笔来自新的预注册，按唯一整数金额的首末相接数选取。逐笔用币种、对手与实转日志核实。200 以上保持开放。

| 链 | 日志区块 | ≥1 | ≥2 | ≥3 | ≥5 | ≥10 | ≥20／50／100／200 | 最大事件数 | 目标收据 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- | ---: | ---: |
| Robinhood | 3,840 | 7,314 | 829 | 297 | 46 | 7 | 0／0／0／0 | 19 | 12 |
| Optimism | 256 | 1,152 | 553 | 272 | 9 | 0 | 0／0／0／0 | 9 | 3 |
| Arbitrum One | 1,120 | 1,063 | 98 | 26 | 4 | 0 | 0／0／0／0 | 8 | 4 |
| zkSync Era | 480 | 193 | 42 | 12 | 0 | 0 | 0／0／0／0 | 4 | 4 |

目标块共取得 399 份有序收据。Robinhood、Arbitrum 和 Optimism 重算 19 个块头及 388 份收据的 MPT 根，并核对目标交易的市场与融资日志。Era 四个滚动交易哈希重算成功，四笔目标收据与独立来源逐字段一致。Optimism 两通道同属 dRPC。其余 1,499 笔多事件候选的收益待核。[Robinhood 公共 RPC](https://docs.robinhood.com/chain/connecting/) · [Era L2 区块哈希](https://docs.zksync.io/zksync-protocol/era-vm/transactions/blocks)

## Era 全额续用两轮

交易按资金依赖的日志顺序为 **14→5→8→12**。第一个池先将中间代币直付第二池，再收 WETH。第二轮的中间代币也从第 3 池直付第 4 池。同一执行器把首轮的全部 WETH 回款作为下一轮的投入，四个池各不相同。[0x7aef1059…](https://explorer.zksync.io/tx/0x7aef1059efbce419afbd35ac9aa3115ced233a15b779b1e365ca478737c2271d)

| 轮次 | 支付 WETH wei | 收回 WETH wei | 整数差额 |
| --- | ---: | ---: | ---: |
| 1 | 10,786,400,685,882,139 | 10,807,267,142,984,932 | +20,866,457,102,793 |
| 2 | 10,807,267,142,984,932 | 10,837,667,352,157,382 | +30,400,209,172,450 |
| 合计 | 10,786,400,685,882,139 | 10,837,667,352,157,382 | **+51,266,666,275,243** |

发送人付 bootloader 的 66,754,443,247,998 wei 减两次退款后，实耗 **45,223,981,250,000 ETH wei**，与收据的气费乘积对平。若执行人与发送人同属一方且 WETH 按面值，扣网络费后余 **6,042,685,025,243 wei**。实际变现与两地址控制关系未知。第一轮和第二轮都贡献了毛差，提前一轮停止的费后结果仍取决于原状态下的真实前缀 gas。[Era 系统合约](https://docs.zksync.io/zksync-protocol/era-vm/contracts/system-contracts)

## Robinhood 的四池微毛差和十九事件开放分流

- 四池样本的资金日志顺序为 **58→50→53→56**。执行器付出 10,043,246,698,658,730 WETH wei，收回 10,045,943,474,375,673。三个中间币转账余额均为零，WETH 增 **2,696,775,716,943 wei**，实耗网络费 **9,973,376,616,000 ETH wei**，含 5,453 L1 gas。按同主体和面值折算，已识别净额为 **−7,276,600,899,057 wei**。按事件数选的前四笔均未用本解码证到闭环，按金额相接追加八笔后找回此例。[0x01cac6f5…](https://robinhoodchain.blockscout.com/tx/0x01cac6f5612b22d2379d1bebf7e316825d7e2809c13e761e88ee8cdec9aecc81)
- 19 事件样本由原始账户给路由器 975,180,958,834,446,621,860,723 个 0xe934…bf50 代币最小单位，结束时同一地址收到 **2,933,917,687 个 USDG 最小单位**。该地址收据中只有这两种非零代币净变动。另一个地址实付 55,627,674,232,000 wei 网络费。[0xe7f0ad68…](https://robinhoodchain.blockscout.com/tx/0xe7f0ad686473fcd982ea1f038fbba99f093b2985ae20be7139ccf4413ea2edb0) · [官方 USDG 地址](https://docs.robinhood.com/chain/contracts/)
- 一笔 V4 与 V3 混合结算的六条 V4 日志由同一 Manager 发出，为同一地址记账。一个 V3 池与 Manager 的两笔 ERC-20 实转在金额上与前后两条 V4 delta 一致。已重算 type-2 交易哈希并恢复发送人。PoolKey 和解锁调用仍缺证据，尚不能给出完整串行深度及最终原生余额。[0xb4930b7f…](https://robinhoodchain.blockscout.com/tx/0xb4930b7f64a84b72e6bf1b0544f50cd672dacdb6dea4ca8bb258a16d8a144b7a) · [V4 内部净额机制](https://developers.uniswap.org/docs/protocols/v4/concepts/flash-accounting)

## Arbitrum 的分流与 Optimism 费项

Arbitrum 有一笔实际给执行地址 **48,446,653,988,067,859 WETH wei**，随后同地址向两个市场分别给出 8,202,283,376,443,392 和 40,244,370,602,983,424 WETH wei，余 **8,641,043**。一支回到起始代币，另一支经两池实转再接 V4，回到原生 USDC。执行地址总共留下 0x498b…c1e5 代币 20,147,831,156,516,639 单位、8,641,043 WETH wei 和 25,543 个 USDC 最小单位。收据网络费 **20,627,319,701,820 ETH wei**，含 L1。多币种实际变现缺，合计净额留空。[0xc71634a2…](https://arbiscan.io/tx/0xc71634a223381b144614fd5cadeb17aea8447d33156266839ea2de284bdf929a) · [V4 Manager](https://developers.uniswap.org/docs/protocols/v4/deployments)

Optimism 在原计划区间之间完成四个新的 64 块窗口，核对三个目标的收据根。9 事件样本有一组资金相接的两腿，L2 与 L1 金额合计 189,094,103,474 wei，operator 费与整笔同币利润缺证。[0x59c92c53…](https://optimistic.etherscan.io/tx/0x59c92c530d8a94246a0dd3ba238bda4027f7b7c4740ffd3b7730b5ba900090bc) · [Optimism 费用](https://docs.optimism.io/op-stack/transactions/fees)

## 复算和费用

三个范围共 **114 次免费匿名预留，USD 0**。原 Optimism 104 块查询收到 HTTP 400，响应给出免费上限 100 块。Era 第四个计划区间触及上一轮高度边界，第三个完成后停止。Robinhood PublicNode 拒绝调用树，第三次请求后停止，未发差分请求。账本、冻结范围及原始字节保存在采集机的 reports/research/y2026-l2-heldout-20261128/，报告保留逐次指纹。[离线复算](fixtures/replay_y2026_l2_funding_holdout.py)

```sh
set -o pipefail
PYTHONPATH=. .venv/bin/python docs/verification/fixtures/replay_y2026_l2_funding_holdout.py |
  cmp - docs/verification/Y2026_L2_FUNDING_HOLDOUT_20261128.json
```
