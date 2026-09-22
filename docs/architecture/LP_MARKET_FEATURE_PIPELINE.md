# LP 原始市场证据到特征窗口

2026-09-14；已实现有界在线采集、离线重放与特征组合，未完成持续交易闭环。

## 不变量与数据流

`build_market_window` 读取两源原始 Swap 日志与两源 slot0 证据，分别执行现有 wire
replay 验证，再要求来源集合、完整连续区块头、链、PoolManager 和 PoolId 一致。
日志过滤必须精确指定 Swap topic 和 PoolId。不能混用 latest 快照或用派生摘要代替
原始请求响应验证。数据来自何条 EVM 链由保留的区块证据决定，无 Robinhood 特例。

canonical v4 Swap 事件使用调用方余额符号：输入为负、输出为正。市场适配器只反转
一次，将它转换为现有 `SwapMarkout` 所需的池侧资金流。价格使用 Q96 整数，markout
价格使用 Q192 整数；不采用二进制浮点，也不声称池侧资金流等于 LP 所有者现金。

每笔成交使用不早于 `event timestamp + horizon_seconds` 的首个区块价格，同时要求
该价格没有超过显式 `max_lateness_seconds`，且不晚于决策窗口末块。不满足条件的
成交记为 pending，不填零，不静默丢弃。派生价格保留两源引用和原始父证据。

`MarketWindow.feature_window` 将上述价格和成熟 markout 接入现有 `LpFeatureWindow`。
持仓和独立参考价必须另外提供；调用方负责其原始证明验证，字段名称检查不能替代
验证器。只要存在 pending，组合就失败。默认完整窗口保留这一保守行为。
显式启用 `mature_prefix` 时，成交截止点固定为末块时间减 horizon 与 max_lateness；
截止后的成交计入 deferred_count，仍包含在原始证据及 swap_count 中，不按收益筛选。
窗口必须覆盖这一尾窗，截止点以前没有合格成熟价格仍产生 pending 并阻断。
经过完整日志验证的零成交窗口与有成交但尚未成熟的窗口具有不同语义。

## 离线入口

```sh
.venv/bin/python -m scripts.build_lp_market_window \
  --request /absolute/path/request.json \
  --evidence /absolute/path/evidence.db \
  --report /absolute/path/new-market-window.json
```

请求包含 `swap_references`、`price_references`（各两条 EvidenceRef）、`pool`（PoolKey）、
`pool_manager`、`horizon_seconds` 和 `max_lateness_seconds`。后两项为 1..86400 整数，
必须显式给定。入口不调用 RPC，会将派生证据追加至现有 evidence DB，拒绝覆盖报告。
可选 `mature_prefix` 默认 false。输出 schema 为 `oasis.lp-market-window.v2`，`execution_eligible` 始终为 false。

## 旧诊断输出的修正

`producer.py` 输出升级为 `oasis.lp-live-shadow-observation.v3`：

- 根据调用方负输入确定方向和 exact-input/output 数量。
- 某笔成交的诊断窗口不能包含同区块之后的成交；特征仍明确是 post-swap diagnostic，
  不能当作原始 beforeSwap 输入或无前视的策略回测。
- Swap 事件 fee 是总 swap fee；未获取协议费拆分时 `exact_fee_match` 保持 null，
  不能以它和 LP fee 相等宣称 hook 策略已验证。

符号与费用依据为本地锁定 npm `@uniswap/v4-core` 1.0.2 的
`src/libraries/Pool.sol` swap delta/fee 计算及 `src/PoolManager.sol` Swap emit。
旧 v2 报告不会被重写，其方向、金额、窗口或手续费比较结论不能自动沿用至 v3。

## 未解决的发布条件

当前测试证明解析、证据绑定、符号、时间边界和失败阻断行为；没有新增交易收益假设，
也没有实盘或样本外收益结论。持续生产者、独立价格、持仓现金核账、路由深度、全部
成本及冻结后的样本外实验仍需完成。MarketWindow 不能直接授予 signer 或广播权限。


## 有界在线序列

`run_pool_state_observer` 可显式配置 market（PoolKey、2..10 个连续区块、horizon 与
max_lateness），把完整两源 slot0 + Swap 采集直接交给本适配器，并启用固定成熟尾窗。
同进程的连续样本必须有重叠区块，重叠状态一致，覆盖上一窗口的 deferred 前缀，
决策 head 与成交截止点前进；缺口、
重组或规则变化 HALT。该检查不证明两个窗口之间每笔 Swap 的完整性，也不防两个 RPC
共同漏报；最终性仍 unresolved。配置、配额和采集时间可能使窗口无法维持重叠，正确
行为是停止，而不是放宽约束。持续增量索引和缺口回填尚未实现。跨重启游标已通过 MarketCheckpoint 实现：
恢复时重放全部留存原始窗口，并重验窗口间连续性；丢失原始证据或配置变化即停止。
