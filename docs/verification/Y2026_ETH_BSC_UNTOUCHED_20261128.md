# Ethereum 与 BSC 的 2026 年多跳成交补样

Ethereum 新样本核实两种微利结算。一笔三池交易由同一 EIP-7702 委托地址收币和付费，WETH 与 ETH 按面值合计净增 **14,947,051,008,769 wei**。另一笔借入并原额归还超过 13,370 WETH，发送人实收扣费后净增 **99,047,440,146,282 ETH wei**。BSC 找到一笔实际由 **5＋4＋3＋4** 个市场腿组成的四环交易，四个环共得 6,732,456,308,007 WBNB wei。其中大部分按 ERC-20 实转付给另一个地址，余款解包。 [逐笔整数金额、日志和原件摘要](Y2026_ETH_BSC_UNTOUCHED_20261128.json)

## 取样

冻结每链五组相对最新头的偏移，每组三个相邻完整块。读取时先核匿名服务的链 ID，以 PublicNode 取头，Ethereum 由 Nodely、BSC 由 BNB Chain 官方端点按头哈希取整块收据。两链 30 个头哈希、收据根按顺序重算并核累计 gas、索引和转账位置。Ethereum 样本跨度为 7 月 21 日至 11 月 27 日 UTC，BSC 为 10 月 15 日至 11 月 27 日 UTC。市场事件表沿用上一批冻结的 16 种签名，对≥200 保留开放档。资金闭环按可唯一绑定的资产、数量、收付人与日志路径记录；合约代码身份及难解协议照实留空。[前一批签名来源](Y2026_BSC_RH_LANDED_20261127.md) · [Ethereum JSON-RPC](https://ethereum.org/developers/docs/apis/json-rpc/) · [BNB Chain 匿名端点](https://docs.bnbchain.org/bnb-smart-chain/developers/json_rpc/json-rpc-endpoint/)

| 链 | 整块 | 收据／回滚 | ≥2 事件 | ≥3 | ≥5 | ≥10 | ≥20／50／100／200 | 资金闭环已见证的成交 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Ethereum | 15 | 4,244／33 | 110 | 37 | 14 | 4 | 0／0／0／0 | 4 |
| BSC | 15 | 1,425／48 | 58 | 24 | 6 | 2 | 0／0／0／0 | 4 |

168 个多事件候选哈希均在此前冻结八链目录之外。先冻结深挖规则，然后对整块窗中最高事件数（含并列）、融资事件与已见证闭环共同最强、以及 WETH 或 WBNB 毛差扣收据费用最大的候选读独立交易信封。六个信封均重算哈希并恢复发送人。Ethereum 三笔均有调用树与状态差分。BSC 的 trace 服务首次链 ID 请求报 HTTP 429，按原预算立即停止，原生资金与场外费用留空。采集含首个拒绝共 **82 次匿名请求，USD 0**。[历史目录范围](MULTIHOP_GOLD_CASE_INDEX_20261127.md)

## Ethereum

| 交易 | 本金和市场 | 最终余额及已识别费用 |
| --- | --- | --- |
| [0x81a8f20b](https://etherscan.io/tx/0x81a8f20b1e3a246b7eaa760280a1d10ab9675c5597a46809bf06fc036497a689) | 同一发送和接收地址在前状态已有 EIP-7702 委托标记。6,054,765,452,768,863 WETH wei 经三个市场变为 6,282,121,824,102,031。对账覆盖回调和两次 `balanceOf`，并匹配 WETH 存储的前后整数。 | 差额 227,356,371,333,168 WETH wei 留在同一地址。直接向块头费用地址付 143,995,871,777,173 ETH wei，收据 gas 68,413,448,547,226 wei。实际 ETH 余额减少两者之和。按 ETH/WETH 面值合计净增 **14,947,051,008,769 wei**。只扣网络费会把这笔净额估高约 10.6 倍。 |
| [0xea937e77](https://etherscan.io/tx/0xea937e777491187bb60ce6bfbcf6fb0362c3192a2411ebf7d801bc14c69de560) | Morpho 借款和还款各 **13,370,835,646,604,000,000,000 WETH wei**，日志 553／554／581 绑定同一借款人。回调里至少有两池和三池两个同币闭环，还观察到一段原生 ETH 付款。 | 执行器把 481,164,858,387,557 WETH wei 的余额解包，直接区块付款后返给发送人 363,664,924,672,819 ETH wei。发送人交易随附 999 wei、付 gas 264,617,484,525,538 wei，前后 ETH 余额实际净增 **99,047,440,146,282 wei**。 |
| [0xd7bd9d0c](https://etherscan.io/tx/0xd7bd9d0cfd96cde1e4a1ad1f584054884f29b3d4109351335413d9f3fff6c488) | 14 个市场签名命中，顶层为账户抽象入口。追到一笔 2,752,350,156,817,844 ETH wei 从执行账户支付给第三方。 | 发送人实际余额减少 1,461,540,410,429,472 ETH wei，等于收据费用。多市场之间没有核到统一本金闭环，第三方的经济所有权未知。 |

第一笔的 WETH 尚未卖出，合计使用 ETH/WETH 面值，跨交易及链下成本未入账。第二笔的 Morpho 外层 calldata 在规范 ABI 对齐后另有一个零字；实际回调通过规范 ABI 携带同一 1,856 字节参数。在正常的只读回放入口，只对该借款入口接受这个布局，仍验证回调载荷、借贷角色与归还整数；任意非零尾字或两个尾字会触发拒绝。[EIP-7702](https://eips.ethereum.org/EIPS/eip-7702) · [Morpho 借款入口与回调](https://github.com/morpho-org/morpho-blue/blob/f8631a27a2ee76ccd63abb945014d31d9efb0aca/src/Morpho.sol) · [只读回放修复](../../src/oasis_agent/evidence/morpho_callback.py)

## BSC

| 交易 | 实际闭环与结算 |
| --- | --- |
| [0x5161b44e](https://bscscan.com/tx/0x5161b44e0fd946ccfc11e62fdd30ef02a337e4c1749f9f683333d7789f36dfc1) | 16 个市场事件全部分到四个 WBNB 环，实际本金分别为 557,401,532,500,000／128,531,355,731,223／11,815,242,363,928,369／1,006,209,541,659,968 WBNB wei。四个环的毛差依次为 5,505,954,695,692／275,513,786,691／483,868,216,656／467,119,608,968 wei。四环之间的首金均与前环末额不同。最后 5,011,301,308,007 wei 通过日志 165 实转至另一地址，1,721,155,000,000 wei 通过日志 164 解包，两项恰耗尽毛差。 |
| [0xb0c28795](https://bscscan.com/tx/0xb0c287958a93e3d7b11893ccfd8d71cc66e60ab5f1b58e704bbdc28c120e47dc) | 同一发送人和执行器在前两个块的交易，也是 16 个事件。可复核 4 腿串行链、三池与两池两个 WBNB 闭环、两次解包共 26,522,201,935,713 wei，以及转给同一外部地址的 10,702,081,863,356 WBNB wei。余下路径包含 v4 的原生差额和按实收拆分的代币流。 |
| [0x1eb0f8a6](https://bscscan.com/tx/0x1eb0f8a6e12ae701660d50d24c08272a43e84f9a1042a6e36afe3068130690e4) | 三池闭环本金 4,497,079,643,539,063 WBNB wei，回款 4,509,545,048,195,012，全部毛差 12,465,404,655,949 wei 解包，未查到原生最终去向。 |

三笔的收据 `effectiveGasPrice` 都是零，重建的两笔 type-2 信封费帽、优先费和一笔 legacy 信封 gas price 同为零。BSC 官方的构块接口允许服务商自定定价。上述三笔的实际原生支付、其他交易承担的成本及链下付款都无充分证据，零收据费用仅按原字段登记。四环样本适合检验打包归因，单纯提高串行路线长度会漏掉这一结构。[BSC 构块接口](https://docs.bnbchain.org/bnb-smart-chain/validator/mev/builder-integration/)

## 复算

原预注册、两个只读采集入口、六本预算账本和原始请求响应保存在采集机 `reports/research/y2026-eth-bsc-untouched-20261128/`。本次新增报告可逐字节复算。同样复算 7 月原例及 11 月同执行器样本，确保新增借款 ABI 布局兼容已有结果。[复算脚本](fixtures/replay_y2026_eth_bsc_untouched.py)

```sh
set -o pipefail
PYTHONPATH=. .venv/bin/python docs/verification/fixtures/replay_y2026_eth_bsc_untouched.py |
  cmp - docs/verification/Y2026_ETH_BSC_UNTOUCHED_20261128.json
```
