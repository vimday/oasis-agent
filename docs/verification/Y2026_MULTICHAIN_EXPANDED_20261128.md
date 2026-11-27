# 2026 年六链新样本的闭环、组合和实际扣费

上海时间 11 月 28 日新增 **4,310 块**，其中 Ethereum、BSC 各有 15 个整块根核验，另外四链有 4,280 个事件索引块。共有 **1,946 笔**至少两市场事件的候选。Ethereum 和 Optimism 各有一笔整笔微利的条件样本，其单个已证闭环毛差低于全笔费用。实际分组和整笔钱包增量会改变候选排序。[原件摘要和逐笔机器报告](Y2026_MULTICHAIN_EXPANDED_20261128.json)

| 链 | 覆盖块数 | ≥2 事件 | ≥5 | ≥10 | ≥20 | ≥50／100／200 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Ethereum | 15 整块 | 143 | 25 | 11 | 3 | 0／0／0 |
| BSC | 15 整块 | 104 | 7 | 1 | 1 | 0／0／0 |
| Robinhood | 2,304 日志块 | 484 | 19 | 2 | 0 | 0／0／0 |
| Optimism | 256 日志块 | 956 | 22 | 2 | 0 | 0／0／0 |
| Arbitrum | 1,080 日志块 | 74 | 3 | 0 | 0 | 0／0／0 |
| zkSync Era | 640 日志块 | 185 | 1 | 0 | 0 | 0／0／0 |

Ethereum 与 BSC 共 5,353 份整块收据。其余链按冻结的市场与融资签名索引，并选取 13 笔复核。八笔 Optimism/Arbitrum 和三笔 Robinhood 用全收据重建 MPT 根。两笔 Era 用单笔收据核对索引日志与系统扣费，整块根仍缺。固定回看高度偏移、年份、查询额度和样本选择写在查询前的新范围中，复算检验与上轮同类窗口无交叠。≥200 为开放尾档，未设置上限。合约、币种、数额和收付人全部匹配的资金连接才能用来计算已证深度。[此前 L1 窗口](Y2026_ETH_BSC_UNTOUCHED_20261128.md) · [此前 L2 窗口](Y2026_L2_FUNDING_HOLDOUT_20261128.md)

## 同一批次的最薄盈余

- **Ethereum 七月四池与组合。** 同一执行器先付 3,301,380,867,149,383 WETH wei，经过四个市场的资金路径（日志 671→663→666→669），回收 3,323,335,517,280,833 wei。该段毛差 21,954,650,131,450 wei，低于整笔 58,888,375,482,680 ETH wei 的 gas。包括额外的 V4 路径后，执行器的整笔 WETH 增量为 59,141,157,288,931 wei，原生付款经调用树与状态差分复核，发送人的余额扣减与收据 gas 一致。若两个地址归同一主体且 WETH 按 ETH 面值计，整笔余 **252,781,806,251 wei**。11 个市场事件中，V4 片段的完整资金归组仍未知，短环自身可节省的增量 gas 未测。[0x67c9d205…](https://etherscan.io/tx/0x67c9d205900f930e50a6cfe9999f442d0ca1426e220cf9578f2182473148863f)
- **Ethereum 低额三池对照。** 一笔循环给执行地址增加 **18,823,163,806,558 WETH wei**。发送人支付 **17,833,738,056,053 ETH wei** gas，另向执行地址转 221 wei，调用树中无其他原生资金转移。按同样归属及面值条件，二者合计余 **989,425,750,505 wei**。另一笔两腿候选实见执行地址向块头费用地址转出 243,972,259,408,634 ETH wei，合并 WETH、原生支出和网络费后只余条件净额 338,538,623,764,731 wei。[三池成交](https://etherscan.io/tx/0xc430d1de847ea59cd95e3760b497648c187b5de3f9e5ffcadf009c387dc1b334) · [两腿及块付款](https://etherscan.io/tx/0x4f8e42882732f2226b81253899ed68af3ae7917baa17f8f640cc9dabd476909e)
- **Optimism 四池与额外路径。** 日志 96→99→102→105 的活动本金为 203,846,666,666 WETH wei，回款 204,009,105,942,925。该段毛差 **162,439,276,259 wei**，低于整笔 **221,237,241,962 ETH wei**。全笔执行器的 WETH 转账净增为 439,440,798,190 wei，发送人实际 ETH 减少恰等于收据的 L2 与 L1 总额。若二者归同一主体且按面值，整个组合仍余 **218,203,556,228 wei**，其他未核成本留空。[0xde3ca2df…](https://optimistic.etherscan.io/tx/0xde3ca2df6ecfbd756d120a64f3b9e3d0716c71d4127f832e03255dd10a489132)

## 收款边界与费用

- **BSC 同地址四池。** 交易信封的类型 4 签名重建成功，发送与接收均为执行地址，收据有效气价与费帽均为零。经四腿先出币后偿付，实付 3,804,847,908,877,404 WBNB wei，回收 3,836,466,942,247,671 wei。整笔八事件的执行地址 WBNB 转账净增 56,643,973,682,406 wei，日志 166 声明将同额解包；BSC PublicNode 的调用树查询返回方法不支持，最终原生资产余额未知。[0x8a929350…](https://bscscan.com/tx/0x8a9293504d9571c49357549b02a2382dfd4c1e885f9e897b92c6af749ee8f397) · [BNB Chain 公共端点](https://docs.bnbchain.org/bnb-smart-chain/developers/json_rpc/json-rpc-endpoint/)
- **Robinhood 三池与其余市场事件。** 11 事件样本有一条完整三腿，路径依资产交付顺序为 29→24→27。执行地址以 125,065,934,123,438,272 WETH wei 完成偿付，收到 126,159,477,181,115,825 wei。整笔执行地址在 ERC-20 转账中的净残差为零，故三腿毛差不能作为该地址的最终收益。收据网络费是 24,122,553,120,000 ETH wei，其 gas 已包含 29,235 单位 L1 份额。[0xa2c10a33…](https://robinhoodchain.blockscout.com/tx/0xa2c10a334cf03c79ee557329db1c37eb9c849c47f433977b3d821dcc18ee4622) · [Robinhood 接入说明](https://docs.robinhood.com/chain/connecting/)
- **大事件样本保留。** Ethereum 28 事件样本的执行地址期末仍有多种不同资产，另付给块头费用地址 28,732,190,258,615,547 ETH wei；BSC 21 事件样本只从转账证实一条两池 WBNB 闭环。Era 九事件样本的 bootloader 预缴减两笔退款等于 **85,554,446,500,000 ETH wei** 的收据费。三笔的完整策略净额保持未知。[Ethereum 28 事件](https://etherscan.io/tx/0x9f8f2711e2286c19796fb8ef9395f9fd803ee1d8261febf97cc9de754954afa4) · [Era 批次](https://docs.zksync.io/zksync-protocol/era-vm/transactions/blocks)

## 复算和费用

三个范围共 **173 次匿名免费预留，USD 0**。Robinhood 官方索引第 12 次返回 429，明细阶段的首次区域块头同样回应 429，两处停止均保留。新冻结的 PublicNode 范围采用全收据复核先前已见的三个不同块。Era 的 dRPC 历史整块收据返回 400 后停止，补样范围改为官方来源的两笔指定单笔收据。BSC 的跟踪分道在首次不支持后停止。所有固定范围、原始 RPC 和未覆写额度账保留于采集机 `reports/research/y2026-multipanel-expanded-20261128/`。[复算程序](fixtures/replay_y2026_multichain_expanded.py)

```sh
set -o pipefail
PYTHONPATH=. .venv/bin/python docs/verification/fixtures/replay_y2026_multichain_expanded.py |
  cmp - docs/verification/Y2026_MULTICHAIN_EXPANDED_20261128.json
```
