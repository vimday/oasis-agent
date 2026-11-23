# Ethereum 11 月 10 日批次的资金闭环和净额

2026-11-23 UTC 复核。目标是 [两周执行器样本](ETH_BATCH_SELECTOR_COHORT_20261123.md)中实际 gas 费用最高的一笔。Ethereum 主网块 **26,385,456**、交易索引 **8**，时间 `2026-11-10T15:37:59Z`。块哈希为 `0x7e6f6110efc8123579247b46dc934cbb346e66997972bc1d2819daae56b74007`。该案例按成本选取。它能复核已上链批次怎样回收资金和支付费用，不能代表同一执行器的其余交易。[交易记录](https://eth.blockscout.com/tx/0x17e3a88f3c8bea04ea966b728cbba119fbd4f60375a1892fd918aec338f71503) · [Routescan 交易记录](https://ethereum.routescan.io/tx/0x17e3a88f3c8bea04ea966b728cbba119fbd4f60375a1892fd918aec338f71503?chainid=1)

## 资金流和实际深度

Blockscout 的有序调用树有 **596** 帧。交易日志四页到尽，共 **186** 条、块内日志索引连续 `448–633`。按协议事件字段、调用帧的入参或返回值、原始整数和资产逐条绑定，共还原 **53 次 DEX 交换、34 个逻辑池**。池身份按地址计，Uniswap v4 同一 PoolManager 下用完整 PoolId 区分。53 条分别为 31 条 V3 ABI、13 条 V2 ABI、7 条 v4 和 2 条 Curve。调用树中最深的 Morpho 回调执行 20 个同级套利分组，16 个成功，4 个回滚且未提交交换日志。三笔嵌套借款和偿还各自整数相等，分别是 `580673521` USDT 最小单位、`524563846645955968` WETH wei 与 `681432003051834179584` FRAX 最小单位。[Blockscout 原始调用树](https://eth.blockscout.com/api/v2/transactions/0x17e3a88f3c8bea04ea966b728cbba119fbd4f60375a1892fd918aec338f71503/raw-trace) · [Morpho 闪电贷实现](https://github.com/morpho-org/morpho-blue/blob/main/src/Morpho.sol) · [Uniswap v4 PoolId](https://github.com/Uniswap/v4-core/blob/main/src/types/PoolId.sol)

16 个组首尾资产与中途整数金额均对齐，按资金回到起始资产时的交换数计，**1 组 2 跳、15 组 3 跳**。其中 6 组另用一次交换将闭环盈余转换成 WETH，4 组在闭环开头将 WETH 解包为 ETH 后向 v4 足额结算。把这些资金转换和盈余处理一并计入，**7 组 3 条经济腿、9 组 4 条经济腿**。四个回滚组保留在交易成本分母中。本次交易级支付所用的 WETH 解包另行计账。

第 2 组的原始日志 `482、485、488、491` 提供一个可逐腿核对的例子。`580673521` USDT 最小单位先换成 `284780863250925104` WETH wei，经 HEZ 返回 `582271908` USDT 最小单位。该组留足起始本金，剩余 `1598387` USDT 最小单位另换成 `779238535898170` WETH wei。Morpho 在外层回调结束后收回本金。闭环与盈余转换的边界由原始整数金额区分。[日志原始首屏](https://eth.blockscout.com/api/v2/transactions/0x17e3a88f3c8bea04ea966b728cbba119fbd4f60375a1892fd918aec338f71503/logs)

## 费用与可见净额

Flashbots 单独读回同一块哈希的区块头，确认交易位于索引 8、基础费为 `1256048561 wei/gas`，块费用收款地址为 `0x4838b106fce9647bdf1e7877bf73ce8b0bad5f97`。最后一个成功顶层子调用将解包所得的 `810886485995164` wei 全额转给该地址。两个浏览器索引显示实际 gas `4529713`，其有效 gas 价等于块基础费，链上优先费为零。该笔占固定两周样本 gas 成本的 **77.71%**。其余 10 笔的 gas 用量中位数是 `907954`，有效 gas 价中位数是 `101080557 wei/gas`，两项都低于本笔。[Flashbots 公共 RPC](https://docs.flashbots.net/flashbots-protect/ratelimiting) · [Ethereum 费用规范](https://eips.ethereum.org/EIPS/eip-1559)

| 交易粒度对账 | 原整数，wei 或 WETH 等值 wei |
| --- | ---: |
| 16 组 WETH 毛差合计 | +7,359,407,665,189,944 |
| 直接支付区块费用收款人 | −810,886,485,995,164 |
| 实际 gas | −5,689,539,495,392,993 |
| 合并付款人与执行器的可见净额 | **+858,981,683,801,787** |

WETH 毛差汇总与调用树中 Morpho 外层借款流程前后的 WETH `balanceOf` 差精确一致。扣除直接支付后，WETH 净增 `6548521179194780 wei`，同笔已提交的 ERC-20 转账与 WETH 解包日志也得到相同差额。付款人与执行器合并后的原生 ETH 流在 gas 前归零，其他已见 ERC-20 差额为零。数字只覆盖这笔交易可见资产变动和费用，链下付款、外部库存、数据与基础设施成本未计。

## 完整性与留存

PublicNode、Nodely 和 dRPC 的首次本机尝试都在 DNS 层终止，HTTP `000`、`curl` 退出 `6`，已各自入原账。随后独立匿名 Blockscout 范围成功 5 次。Flashbots 只读范围成功获得区块头，第二次整块收据请求返回 `null` 后停止。区块头公布的 `receiptsRoot` 为 `0x3c32dfeb476499d8b7c7e876ee52777f572eef7848acd4091de4614b73c0ec2f`，本次没有足够的整块收据重算该根。五个来源合计 10 次本地预留、7 个实际 HTTP 响应、费用 $0。采集时间 `2026-11-23T11:43:31Z–11:47:19Z`，没有签名或链上提交。[Blockscout API 规范](https://github.com/blockscout/blockscout-api-v2-swagger/blob/main/swagger.yaml)

本地忽略目录 `reports/research/eth-cohort-cost-outlier-20261123/` 保留固定范围、账本、原始请求及响应、离线逐腿证据和摘要。`verified.json` 保留每一腿的日志索引、逻辑池、输入输出资产与金额，以及全部响应 SHA-256。下列复跑不访问网络。

```sh
set -o pipefail
evidence=reports/research/eth-cohort-cost-outlier-20261123
.venv/bin/python "$evidence/verify.py" | cmp - "$evidence/verified.json"
```
