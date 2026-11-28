# BSC 十二环原生付款与 zkSync Era 收据补证

BSC 的 36 次兑换成交向第三方支付 **935,608,210,241,515 BNB wei**。签名信封、规范收据、完整调用树和交易前后状态差分交叉核对后，同主体及 WBNB/BNB 面值口径下的可见余额降至 **52,205,937,381,133 wei**。Era 补齐此前全部 **27/27** 份缺失的候选收据，覆盖 **18 块**，两家匿名 RPC 逐笔复核一致。[交易、逐笔资金证据与 RPC 原件摘要](Y2026_BSC_ERA_FOLLOWUP_20261128.json)

## BSC · 十二个三跳环的付款与库存

目标为 2026 年 9 月的 `0xf79cff44…7abc`，块高 **120,509,259**，交易索引 **1**。沿用已有全块收据重建的根与独立取得的块头，本次复算再恢复签名人、重建交易哈希和十二个闭环。新增 Triport 的交易索引处调用树与状态差分。调用树共 231 帧，成功路径上的 116 条日志与规范收据逐条绑定，根调用的发送人、calldata、金额和 gas 与签名信封及收据一致。[原始十二环](Y2026_ETH_BSC_FRESH_20261128.md) · [BSC 成交](https://bscscan.com/tx/0xf79cff4482a38e6157510d25483a20f5e89d2c7c6be1ac8e754a9afac31f7abc) · [BSC trace RPC](https://github.com/bnb-chain/bsc/blob/master/rpc/json-rpc-api.md)

| 最终变化 | 账户 | 金额 |
| --- | --- | ---: |
| WBNB 留存 | 执行器 `0xcfc344af…2213` | +1,089,715,447,622,648 WBNB wei |
| 原生 BNB 付款 | 执行器 `0xcfc344af…2213` | −935,608,210,241,515 BNB wei |
| 原生 BNB 收款 | `0x4848489f…4848` | +935,608,210,241,515 BNB wei |
| 网络费 | 发送人 `0xc83b8850…485b` | −101,901,300,000,000 BNB wei |

前 12 个顶层子调用分别包含一个完整的三跳闭环。唯一成功的非零原生价值转账是最后的第 13 个子调用，两端原生余额的差分逐 wei 对应。收款地址与区块头 coinbase 不同，收款人实体与账外结算待核。交易无 WBNB 解包，执行器的 WBNB 转账净增还与标准余额映射位置的原始存储差分一致，余额从 **118,944,905,230,223,389** 增至 **120,034,620,677,846,037 wei**。原生付款使用执行器原有 BNB 库存，金额相当于 WBNB 毛差的 **85.86%**。

原先仅扣收据费可见 **987,814,147,622,648 wei**。补入原生付款后，全笔在执行器与付费人共同归属、WBNB 与 BNB 按面值的条件下剩 **52,205,937,381,133 wei**。交易结束时 WBNB 仍留在执行器，完整经济主体归属与后续变现价格尚无证据。

## Era · 27 份完整交易回执

本轮从此前三个冻结窗口的原始日志复算排序和交易键，恢复全部 27 个至少两事件的候选。dRPC 和 1RPC 分别按哈希取块及取交易收据，27 份交易的状态、类型、费用字段、Bloom 和全部 **343 条**日志一致。首笔还与 OnFinality 的回包一致。18 个块均按 Era 的顺序交易哈希链重算块哈希，并核对各笔交易索引及旧扫描中的市场日志位置。公开 Era RPC 的三个以太坊式根字段为占位，验证依赖 Era 块哈希和双源完整收据。[以前的 Era 缺口](Y2026_L2_FRESH_20261128.md) · [Era JSON-RPC 语义](https://docs.zksync.io/zksync-protocol/api/ethereum-rpc)

27 笔费用都由 ETH 系统合约向 bootloader 的扣款与退款日志逐笔对齐收据 `gasUsed × effectiveGasPrice`。20 笔资金能闭合在同一持有地址，其中 8 笔为 WETH 闭环。8 笔按 ETH/WETH 面值和付费人共同归属口径扣可见整笔网络费，7 笔正、1 笔负。

- **三跳闭环** `0xf0e3189d…a493` 的实际资金顺序为市场日志 25→20→23，体现先收款再偿还首池。执行器投入 **4,380,765**、收回 **4,408,840** 个 `0x3355…aaf4` 最小单位，同笔执行器净增 **28,075**。发送人另外实付 **15,285,404,750,000 ETH wei**，缺少同币变现证据，整体净额留空。[Era 三跳成交](https://explorer.zksync.io/tx/0xf0e3189dc6dd18dfb6269ae85b5aecf2b1287241d968e0922d98ccdb5159a493)
- **WETH 双跳微利** `0xe328bd68…26b4` 的执行器净收 **26,703,383,401,209 WETH wei**，发送人实付 **15,235,810,750,000 ETH wei**。按上述口径差额为 **+11,467,572,651,209 wei**。收据未给两者经济归属，净额保留为条件值。[Era 双跳成交](https://explorer.zksync.io/tx/0xe328bd685611f969ba96a07c61293e38e3de5475c5e18f8d6c094108076f26b4)

## 预算与复算

共用 **99 次**匿名预留，支出 **USD 0**。Triport 3 次，dRPC 与 1RPC 各 46 次，OnFinality 在第四次请求收到 429 后停用。初次采集后的摘要阶段因主题索引类型错误停止，原始范围与原账本完整保留。修复仅在离线解析处理，未重跑同一范围。1RPC 使用冻结的新来源复核原候选。报告由两份范围的原始账本、回包和来源摘要离线复算。[1RPC 匿名 Era 入口](https://docs.1rpc.io/using-the-web3-api/networks) · [OnFinality 匿名 Era 入口](https://www.onfinality.io/en/networks/zksync)

```sh
set -o pipefail
PYTHONPATH=. .venv/bin/python docs/verification/fixtures/replay_y2026_bsc_era_followup.py replay \
  reports/research/y2026-bsc-era-followup-20261128 \
  --also reports/research/y2026-bsc-era-corroborate-20261128 |
  cmp - docs/verification/Y2026_BSC_ERA_FOLLOWUP_20261128.json
```
