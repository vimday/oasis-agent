# Base 两个 Slipstream 池的回滚与缺报价复核

对上轮 68 个规范哈希和原本金重放了全部 **936** 个缺报价点，其余 **1,104** 个原已报价点的两腿输出均逐整数一致。旧 Quoter 的泛化错误可以复现为池调用在协议极限价耗尽转发 gas。把价格范围限制在现有 sqrt price 的 ±5% 后，936 点中 **636 点出现可计量的部分成交**，其余 **300 点仍在一腿耗尽 gas**。本次有界复核没有把部分成交登记为原本金的完整报价。[逐点金额、失败类别和原件摘要](BASE_CL_COVERAGE_20261128.json) · [原窗口](BASE_BATCHED_NATURAL_20261128.md)

## 回滚原件与本金容量

冻结原来的 V3／Slipstream 池集合、68 个头、实际本金和原报价哈希。用相同工厂注册和运行码摘要定位池，原价与费用在原哈希解析。只读夹具记录真实池回调里的方向、要求支付的输入和返回输出。它的回调无付款逻辑，每次先核回调池再回滚。只有两腿都消耗各自全部输入才计为完整。该口径能区分输入超出有界深度和调用本身无法返回结果。两个代表头另有 dRPC 同哈希结果，和 PublicNode 的整组观测逐项一致。[Slipstream QuoterV2 错误解析](https://github.com/aerodrome-finance/slipstream/blob/main/contracts/periphery/lens/QuoterV2.sol) · [池换币及回调源码](https://github.com/aerodrome-finance/slipstream/blob/main/contracts/core/CLPool.sol)

| 旧缺报价分类 | 路线点 |
| --- | ---: |
| 第一腿部分成交，第二腿未尝试 | 340 |
| 第一腿完整，第二腿部分成交 | 296 |
| 第一腿未返回报价数据 | 220 |
| 第一腿完整，第二腿未返回报价数据 | 80 |

在 63,345,200 的 `0x4e392fbf…` 与 63,345,203 的 `0xdbc69982…`，按默认协议极限，两组把 pool call 上限从 400,000 提到 2,000,000，分别消耗 394,163 与 1,969,163 gas 后返回空错误数据，与克隆代理转发上限吻合。公开 Quoter 遇到过短的错误数据只报 `Unexpected error`。在同哈希用 200 万 gas 和两个有界价格做额外回调观测，两个池都能返回金额，但仍用不完原本金。

| 池 | 计划本金，WETH wei | ±5% sqrt 范围内实际输入 | ±10% sqrt 范围内实际输入 |
| --- | ---: | ---: | ---: |
| `0x4e392fbf…` | 67,280,448,940,895,129 | 2,977,720,322,720,920 | 3,466,503,778,353,345 |
| `0xdbc69982…` | 250,000,000,000,000,000 | 157,783,200,307,236,444 | 169,415,476,973,331,594 |

部分成交量出指定价格范围内的容量。价格继续向协议极限移动时，仍可能遇到另一段流动性，因此需要协议全范围对照。

随后在每个旧规范状态只对缺报价路径升高逐腿 gas、恢复协议全价域。**936/936** 个原本金的两腿报价全部补齐，原 68 个头的旧基准逐项一致。恢复点毛差均为负，单腿探针最高测到 4,636,331 gas。高 gas 的读法用于历史补价，实时发现继续使用冻结的有界范围。[完整恢复与复算](BASE_CL_TAIL_DEPTH_20261128.md)

## 预算与原件复算

68 头有界复核合计 **79** 次免费预留，PublicNode 76 次、dRPC 3 次。代表点的高 gas、有界价格对照另用 **5** 次。支出 USD 0。原始响应和独立预算分别留存在 `reports/research/base-cl-coverage-bootstrap-20261128/` 和 `reports/research/base-cl-tail-gas-20261128/`。第一个范围可逐字节复算。夹具未签名、未部署、未出块。Solidity 0.8.26 的 Cancun 运行码采用 IR 优化，源码和二进制摘要已冻结。夹具的专项 Slither 高危检查退出 0，详细检测日志保留在 `reports/research/base-cl-coverage-integration-20261128/`。

```sh
set -o pipefail
PYTHONPATH=. .venv/bin/python docs/verification/fixtures/run_base_cl_coverage.py replay \
  reports/research/base-cl-coverage-bootstrap-20261128 |
  cmp - docs/verification/BASE_CL_COVERAGE_20261128.json
```
