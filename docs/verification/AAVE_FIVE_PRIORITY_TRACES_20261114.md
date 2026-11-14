# Aave 五笔原生余额残差核验

2026-11-14，基线 `b93ef38`。Codex 实现和自审。仅观察 Ethereum 上既有历史钱包的
原始记录、收据和余额。未修改先前账本或策略 PnL。
[原生余额与 trace 优先级](AAVE_FREEZE_ORACLE_20261114.md)

## 证据与结果

先对残差最大的交易固定块哈希。PublicNode 在这笔的首个 `eth_getTransactionReceipt`
返回 HTTP 200、`result:null`，原计划按规则在 5/40 停止。未发送该计划的
`debug_traceTransaction`，不能从该失败推断其 trace 能力。Alchemy 的单独免费收据
范围验证了全部五笔的块、交易索引、成功状态与交易对象角色，实际 33/34。相对期初
已冻结的钱包对账报告，五笔均为新增收据，缺口由 131 降至 **126**。
[Alchemy 免费公共入口](https://www.alchemy.com/rpc/ethereum)

Routescan 五次按交易哈希查询与同一提供者的地址索引一致，六组形状均保留两笔，
相同地址、方向和 wei 数额都保留计数，未人工删除。该接口只作为索引复核。另用 Alchemy
对五个块的父块末和目标块末进行 40 次固定哈希读，核对 10 个块锚和钱包 ETH 余额。
表中索引净额包含该块比对窗口内的全部钱包外部与内部原生索引流，以及钱包自己支付的
索引 gas。数值为 ETH 的精确十进制表示。
[Routescan 按哈希索引](https://docs.routescan.io/api/etherscan/accounts)

| 目标块 | 成功交易位置 | 观察余额 Δ | 原索引净额 | 观察减索引 | 每组只计一次后的块差额 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 26301276 | 7 | 64.019138169992894080 | 83.022346469992894080 | -19.003208300000000000 | 0 |
| 26301278 | 122 | 34.000000000000000000 | 68.000000000000000000 | -34.000000000000000000 | 0 |
| 26301283 | 54 | 145.931392292935488271 | 162.343675880651900554 | -16.412283587716412283 | 0 |
| 26301492 | 17 | 530.954124128000000000 | 561.908248256000000000 | -30.954124128000000000 | 0 |
| 26301494 | 125 | 419.585755213036901787 | 741.219956847238535990 | -321.634201634201634203 | 0 |

五个独立块约束均支持每组只计一次的解释，累计差额 **422.003817649918046486 ETH**。
如果按这一解释计算，全窗尚余 **87.199607215700887302 ETH** 差额。正式原索引残差仍为
`-509203424865618933788` wei。由于余额属整块，其他遗漏或抵消的交易可能给出相同差值，
收据只能证明顶层成功，仍需原生内部执行轨迹区分具体入账和回滚。费用、还款及全窗净收益
尚未计算。没有扩大到其他钱包或把敏感性计算记入余额。

## 回放与下一项

三个免费范围各自保留预算和原文件。另有五次免费交易索引，Aave 本轮合计 83 次。
原始记录在 `reports/research/aave-five-traces-20261114/`。初版离线报告误用 null 查找
原生资产，原输出使用 `native`；修正后两轮正式回放均退出 3，逐字节一致。

| 留存文件 | SHA-256 |
| --- | --- |
| 原 trace 路线首笔失败 capture | `2bc2f9471d42512bdfab62306b9c801d47f90eeb649b85513c7f5e42349602f5` |
| 五笔核验最终报告 | `6e9d06750174bbc1b9f71466e35cd47dc052f7416d487be875f8b41d102ddf5f` |

```sh
r=reports/research/aave-five-traces-20261114
prior=reports/research/aave-regimes-20261114
.venv/bin/python -m scripts.research_aave_priority \
  --priority "$prior/trace-priority.json" --baseline "$prior/wallet-plus-pair.json" \
  --index reports/research/aave-ledger-20261114/wallet-index \
  --receipts "$r/alternate-identity/campaign" --pages "$r/tx-index" \
  --balances "$r/block-balances/campaign" --output "$r/another-review.json"
```

下一步需要不同来源的五笔调用树，优先块 `26301494`。Alchemy 的 Debug API 是可报价
的备选，Blockscout 官方文档另列免费账户的 raw trace 接口，须实际验证覆盖和字段完整性。
有结果后逐层检查祖先回滚、实际 value 语义与整块余额差，并继续剩余差额和 126 笔收据。
[Blockscout 免费层](https://www.blog.blockscout.com/blockscout-pro-api-postman/)
[Alchemy 套餐](https://www.alchemy.com/docs/reference/pricing-plans)
