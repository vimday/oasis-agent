# Robinhood 与 BSC 长交易的实际资金路径

2026-11-23 UTC。固定复核此前保存的三笔 Robinhood 21/22 Swap 交易，以及 BSC 一笔 56 Swap 交易。以原收据与块头为锚，用独立匿名来源获取调用轨迹和可用的整块收据。两个逐腿资金完全对账的案例分别为 **3 跳和 5 跳的分流兑换**，首尾资产不同。这四个按事件数选取的案例不构成机会发生率样本。[Robinhood 选样](../strategies/MULTICHAIN_MULTIHOP_DISCOVERY.md) · [BSC 选样](LONG_SWAP_TAIL_20261123.md)

## Robinhood · 三笔均完成收据根和调用日志核验

Triport 的公开 `callTracer` 带日志结果与官方节点原收据逐笔比对，合计 **691 条日志完全匹配**。用 PublicNode 的三个整块收据列表独立重建 **3/3** 个 `receiptsRoot`，共 15 份收据。顶层成功状态、交易索引、交易哈希、全部日志和实际 gas 与官方留存一致。调用树内显式失败帧仍计入研究。收费及退款轨迹与收据 `gasUsed × effectiveGasPrice` 一致，三笔 `gasUsedForL1` 均为零。[Robinhood 官方 RPC](https://docs.robinhood.com/chain/connecting/) · [Triport 匿名归档方法](https://triport.io/docs/robinhood/public) · [PublicNode Robinhood](https://robinhood.publicnode.com/)

| 区块 / 交易索引 | Swap / 逻辑池 | 调用帧 / 显式失败帧 | 实付 gas（ETH wei） | 资金核对 |
| --- | ---: | ---: | ---: | --- |
| [120,369,109 / 1](https://robinhoodchain.blockscout.com/tx/0xa96d1eda66c013d77e2d89bcb623be3275fb38bfbba748ccac535ae6cb4b6a88) | 22 / 6 | 1,219 / 13 | 141,499,145,502,000 | 有 Swap 的五个同级子树各 1、3、10、5、3 条 |
| [120,369,170 / 1](https://robinhoodchain.blockscout.com/tx/0x57110d3c1a28867544969054504e0abb875060908a6780dd29534faf0895d7e2) | 21 / 6 | 1,223 / 11 | 144,492,700,000,000 | 同一顶层发送者和目标，五个同级子树各 7、4、4、4、2 条 |
| [120,354,773 / 3](https://robinhoodchain.blockscout.com/tx/0x98a5143fcf86defcb660b9548f8eea52011856a24526fa2d2564864cc604362a) | 22 / 7 | 982 / 2 | 80,814,682,380,000 | 14 条用户资金 Swap，8 条 hook 内部 Swap |

前两笔分别在日志 `162→165` 和 `218→221` 核到上一步的 USDG 输出由同一个路由地址等量支付下一池。22 条和 21 条样本的所有等额整腿候选边均只组成最多两跳的链。第一笔的十 Swap 子树存在多个分支和不同结算资产，未拆全所有不等额和分流资金。收据显示两笔目标合约分别增加 `227283447576000` 和 `227865394041600` WETH wei，外层发送者支付上表 gas，其他地址亦有余额变化。两者控制关系及全笔资金成本未知，因此保留分地址结果。

EntryPoint 样本的全部 **22** 条可分解为 **5 个 TORCH→URANUS 分流路径**与 **8 条 hook 反向交换**。分流为 4 个三交换路径和 1 个两交换路径。三交换路径经过原生 ETH 到 SPCX 再到 URANUS，其中两条先把 ETH 包装成 WETH，计包装的最长路径为 **4 条经济腿**。同时跨越多个同级调用子树。第三条主交换之后的两条反向交换由 hook 作为独立调用者，投入仅是 hook 获得的部分 URANUS 并回到同一 PoolId，用户余量另行交割。五条路径的用户输出合计在日志 `170→171→174` 等额经两个中介转给智能账户。[EntryPoint v0.7](https://github.com/eth-infinitism/account-abstraction/releases/tag/v0.7.0) · [Uniswap v4 PoolManager](https://github.com/Uniswap/v4-core/blob/main/src/PoolManager.sol)

| EntryPoint 样本核对项 | 原整数 |
| --- | ---: |
| 用户支付 TORCH | 345218722571395572704086 |
| 五条路径实际进入池的 TORCH | 342284363429538710336102 |
| 路由另转两个地址的 TORCH | 2934359141856862367984 |
| 五条路径等额归集并交付的 URANUS | 6413042009723591446189409 |
| UserOperationEvent 的 gas 费用（ETH wei） | 0 |
| 顶层发送者实际支付（ETH wei） | 80814682380000 |

三个 Robinhood 样本的 **31 条 v4 Swap** 中有 **8 条**，事件金额与 PoolManager 实际返回给调用方的 `BalanceDelta` 不同。例见 EntryPoint 日志 `33`，池记录输出 `330789132543627451436176` URANUS 最小单位，用户路径收到 `326158084688016667116070`，差额由匹配的 hook 转账核实。逐腿金额应使用结算口径。仅用事件数和池内金额会同时把 hook 交换算进用户深度，并高估最终可用金额。[Uniswap v4 自定义结算](https://developers.uniswap.org/docs/protocols/v4/guides/custom-accounting)

## BSC · 56 Swaps 对应 14 条分流路径

交易位于 **135,113,546 / 81**，时间 `2026-11-22T16:46:48Z`。官方留存收据的 **168 条日志**与 SQD 同区块、同交易索引的 **720 个调用帧**交叉核对。5 条 V2、39 条 Pancake V3、12 条 Pancake Infinity CL 的 56 次交换与对应调用逐一匹配。Infinity 的 `PoolKey` 编码重算 PoolId，合计 **22 个逻辑池**。两个失败调用帧保持在交易分母中。[BSC 交易](https://bscscan.com/tx/0x37de3d32d138e9b16858dce922ac2a08853041e2c6af63132ff5a2bd34801343) · [SQD BSC 数据与字段](https://docs.sqd.dev/en/data/evm/binance-mainnet) · [Pancake Infinity CLPoolManager](https://github.com/pancakeswap/infinity-core/blob/main/src/pool-cl/CLPoolManager.sol)

**14 条分流，共 5 条 3 跳、4 条 4 跳、5 条 5 跳。** 42 个相邻依赖都有等额同资产转账，或 Infinity Vault 原生币收付支持。例如五跳分支用日志 `469→471→474→478→481`，从 `76631776331989` WBNB wei 开始，最后得到 `46158377523740991` 个目标币最小单位。各份付款的起始资产合计 **0.02 WBNB**，其中一条在进池前先解包给 Vault。14 条结算输出汇合后，日志 `589` 一次把 `12035736649757421666` 个 `0x1d2f0da169ceb9fc7b3144628db156f3f6c60dbe` 最小单位送回付款人。全部用户路径均为跨资产兑换，没有回到 WBNB 的收益闭环。

实际 gas 为 `3786985 × 50009011 = 189383374521835` BNB wei。路径内原生币给 Vault 的收付与解包均按对应份额核对，未见额外直接支付给块头收款地址。若把该订单单独估值，目标币的全部成交价值至少要覆盖 **0.020189383374521835 BNB** 的本金和已见 gas。未取得同刻可执行退出价格，不把这个资产差额计作套利收益。

## 留存与复跑

2026-11-23 `14:25:00Z–14:29:43Z` 期间，三个匿名来源各成功一次，Robinhood 两个 HTTP 批次各含四个逻辑读，BSC 一个 HTTP 读固定区块。每次在原来源预算中预留后发出，共 $0。BSC 保留原收据及块头，独立核到交易和调用但未重算 BSC 收据根。本地忽略目录 `reports/research/multichain-long-route-samples-20261123/` 保存范围、预算、原响应、逐路径与余额向量。整笔历史重放的行级结果不提供当时块前报价或新的机会时效。

```sh
set -o pipefail
evidence=reports/research/multichain-long-route-samples-20261123
.venv/bin/python "$evidence/rh_verify.py" | cmp - "$evidence/rh-verified.json"
.venv/bin/python "$evidence/bsc_verify.py" | cmp - "$evidence/bsc-verified.json"
```

| 响应或结果 | SHA-256 |
| --- | --- |
| Robinhood 的 PublicNode 整块收据批次 | `78b8ebd6a76109e554f248076b6065b06365d783b16f79922976a01f33b1bd53` |
| Robinhood 的 Triport 调用批次 | `b25a2ee4bfeb04d73561393f2b6019b8544cb86e4c284c90f7210d78876ba680` |
| BSC 的 SQD 原文 | `5a6d1ce2fb84341fad670b22a8db116b8c6d15adbccfe784c359aea77e7c90a0` |
| `rh-verified.json` | `5c55d1d074a84e1582c81e92d48ae788beeaa21948616568a200cfa2ecd7b35d` |
| `bsc-verified.json` | `c593fea0c61c2b846cd0f226f063f6780781f2a999436025c936618501a02d86` |
