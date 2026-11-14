# Metric / Uniswap V3 固定区块报价实验

设计锁定：2026-11-14；起始提交 `862e9e8`。实现与自审：Codex，无独立 reviewer。
范围：跨 venue 的同链 ERC20 双向报价研究；不增加执行、签名或资金权限。

## 假设、范围与反证

同一资产对在 Metric 与 Uniswap V3 的库存和参考价格不同，可能出现规模相关的价差。
本轮将已有 Metric API 深度研究推进到固定区块 quoter 回放：两个起始资产、两种 venue
顺序、每种至多 16 个预先声明的规模。没有参数搜索，也不将跨链同名资产连接成路线。

每条路径都以同一资产结束。两跳均为独立调用同一块末状态，前跳输出作为后跳输入；
**这不是整笔原子交易模拟**。第一腿可能改变第二腿所依赖的 oracle/extension 状态，
maker 更新或 builder 排序可能消灭价差；单跳 quote 不证明 callback 付款、余额或权限。
Metric partial fill、V3 触及开放价格边界、任何 ABI/代码/链/块/来源冲突均拒绝该候选。

固定接口：Metric `quoteLiveExactInSingle` 的显式 sender/recipient/extensionData 重载，
拒绝只回传 amountOut 而丢弃实际 amountIn；V3 使用 QuoterV2 的 tuple 参数与四项返回。
Metric 的 token0/token1 不假定地址排序，Uniswap V3 必须按地址排序。费用已经体现在
报价里，不再对 output 重复减 pool fee；增量 gas、builder、融资、失败与机会成本单列。

## 数据与控制

- 明确 chain ID、块号/哈希、检索截止时间、来源、资产精度及 pool/factory/quoter/caller
  runtime pin；Metric getImmutables 全量 ABI 哈希、token 顺序与 scale multiplier 同时校验。
- 复用已有严格 RPC export 读取器，所有合约读取必须 EIP-1898 `requireCanonical=true`。
  不把相同 URL 换两个 alias 当成独立来源，也不把 parsed export 标为原始 HTTP。
- 每个候选的资金、成本未知时保持净额未知。中间报价失败保留已完成腿及失败原因。
  资源上限按完整计划计算，缺失数据、错误、预算耗尽不能变成“没有机会”。
- 入口离线可复现，不包含网络客户端。可选当前公开观察只访问官方声明免费的匿名
  PublicNode Ethereum RPC，单独新目录先记录范围，再至多 40 次逻辑读取、120 秒、每回复
  2 MiB；首次失败/429 停止，不接触既有付费 RPC 或历史账本。不重试。

## 验收与基准

先跑合成失败场景，再跑完整仓库检查。接受条件是 ABI/方向/金额守恒、逐跳全量输入、
跨链隔离、身份/来源/区块冲突拒绝、有限请求计划与确定性报告。CLI 输出不可覆盖。
使用独立 `cast calldata` 核对编码。回放数据及代码摘要随报告保留。

基准是同规模起始资产原地持有；资金使用按互斥候选，不把全部路线同时累加。
本轮没有 untouched evaluation window、成交收据、实际持有期或 net NAV，故不计算
胜率、置信区间、回撤、年化或可投额度。合成正价差只验证算术，不提升研究等级。
后续需精确 caller 的整笔模拟、前瞻冻结窗口和净结算，才可检验真正收益。

结果记录在[本轮验证](../verification/METRIC_V3_QUOTES_20261114.md)。

## 离线复现

```sh
# 目录、输出文件必须尚不存在；合成数据不访问网络。
.venv/bin/python -m tests.fixtures.metric_v3 /tmp/metric-v3-fixture-new
.venv/bin/python -m scripts.research_metric_v3 \
  --plan /tmp/metric-v3-fixture-new/plan.json \
  --export alpha=/tmp/metric-v3-fixture-new/alpha.json \
  --export beta=/tmp/metric-v3-fixture-new/beta.json \
  --output /tmp/metric-v3-report-new.json
```

`schema=oasis-metric-v3-plan-v1` 固定两个 ERC20、一个 Metric pool、一个 V3 pool。
每个起始资产显式填写资本、承诺、额外准备金，以及五类增量成本；它们均按该资产的
整数 base units 表示，可填 `null` 表示未知。成本为每条候选的保守总额，不从 ETH gas
价格自动折算。每条候选独立使用资本，不可累加为并发持仓。

`--export SOURCE=PATH` 接受已有严格 RPC export 格式；至多两个来源，端点摘要必须不同。
同一结果的一致性不证明提供者独立。计划最多 64 条候选、256 种离线查询；预算不足会
保留拒绝结果。所有输入必须早于 cutoff，已使用的块状态读取若早于块时间则清空派生报价。

合法但不完整的研究报告退出 0，`decision=incomplete_evidence`；格式错误或输出已存在
退出 2。因此自动化必须读取 decision 与完整性字段，不能把进程成功解释为发现套利。
即使 `positive_conditional_quote`，`execution_eligible` 仍固定 false。报告绑定计划文件、
规范化计划、解析输入、实现代码摘要及每条查询，未自动验证原始 HTTP 或外部时钟。
