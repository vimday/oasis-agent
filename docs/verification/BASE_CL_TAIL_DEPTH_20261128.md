# Base 原 936 个缺报价点的协议全价域复核

原 68 个 Base 规范头、六池目录和逐头本金保持原值。**936 个缺报价点的两腿全部用完各自输入，0 条毛利为正，0 条模型净利为正。** 最接近盈亏平衡的点位于块 63,345,289，经 Slipstream TS100 `0xb2cc224c…` 池买入、`0x4e392fbf…` TS1 池卖出，原本金 67,280,448,940,895,129 WETH wei。第一腿输出 110,997,183 USDC 最小单位，第二腿输出 60,375,509,021,110,058 WETH wei。毛额为 **−6,904,939,919,785,071 wei**，扣原窗口的 L2 gas 模型、L1 费用上界和 operator fee 后为 **−6,907,114,650,980,732 wei**。[逐头原件摘要、完整金额和路径](BASE_CL_TAIL_DEPTH_20261128.json) · [原窗口](BASE_BATCHED_NATURAL_20261128.md)

## 价格边界和 gas

在此前 ±5% sqrt price 对照中，636 点返回部分成交，另 300 点未得到回调金额。现在把价格推到协议允许的全价域，提高单腿 gas，在块 63,345,200 和 63,345,203 分别用完 0.067280448940895129 WETH 和 0.25 WETH。两例的回调探针分别消耗 2,241,008 和 2,475,126 gas。两种方向共四组独立回调金额，由 PublicNode 和 dRPC 逐整数确认。此前 ±10% 取数只覆盖前段流动性。离开该窄区间后，池仍可沿 tick bitmap 找到远端流动性；补齐的路径在扣 gas 前均因价格与池内费用亏损。[上一轮有界结果](BASE_CL_COVERAGE_20261128.md) · [Slipstream swap 和回调源码](https://github.com/aerodrome-finance/slipstream/blob/main/contracts/core/CLPool.sol)

| 固定本金，WETH | 缺报价点 | 两腿全部成交 | 毛利为正 |
| --- | ---: | ---: | ---: |
| 0.067280448940895129 | 240 | 240 | 0 |
| 0.25 | 336 | 336 | 0 |
| 1 | 360 | 360 | 0 |
| 合计 | 936 | 936 | 0 |

三种本金各取一个原规范头，dRPC 先补齐 42 点。PublicNode 在其余 65 个头补齐 894 点。每个头重新报价一条原已完成路线作为对照，**68/68** 条两腿整数金额与原 Quoter 相同。依 EIP-1898 请求原规范哈希，复核块时钟、池工厂和代码摘要、费用及原本金。第一腿实际消耗必须等于本金，第二腿必须用完第一腿全部输出。原已完成的 1,104 点沿用上一轮逐整数校验。恢复后的合并矩阵包含 2,040 个报价点。

薄池的只读单腿 gas 上限为 800 万，其余池保留 44 万。每个头里同一首腿只查一次。补价过程中单腿探针消耗最高 4,636,331 gas，整批 RPC 调用 gas 上限为 3,200 万。探针回调逐次验证实际池并主动回滚，按回调金额串接两腿。手续费模型沿用原窗口的 310,551 L2 gas 常数。这批报价的毛额已经为负，因而未进入整笔影子模拟和直接子块存续率统计。

## 工程影响和复算

实时窗口仍用有界探针，遇到 `partial` 或空回调时输出缺报类型，保留本金与状态哈希供回放。把协议全价域修复放入有独立限额的历史校验队列。持续记录报价时的 H、本金、方向、已消耗输入与 RPC gas。原生执行层可按池代码、tick bitmap 和费用状态维护只读快照。完整且费后为正的报价进入同头整笔优先队列。

单腿双源和全范围重放分别保留原始请求、响应、独立预算、冻结范围与运行码。单腿对照用了 10 次免费请求。分批重放用了 72 次免费请求（dRPC 5，PublicNode 67）。支出 USD 0，全部请求一次完成。三个输出已通过锁定环境离线逐字节复算。

```sh
set -o pipefail
for scope in exact-pilot exact-bulk; do
  d="reports/research/base-cl-tail-$scope-20261128"
  PYTHONPATH=. .venv/bin/python docs/verification/fixtures/replay_base_cl_tail_bulk.py replay "$d" |
    cmp - "$d/stdout.json"
done
d="reports/research/base-cl-tail-protocol-20261128"
PYTHONPATH=. .venv/bin/python docs/verification/fixtures/replay_base_cl_tail_capacity.py replay "$d" |
  cmp - "$d/stdout.json"
```
