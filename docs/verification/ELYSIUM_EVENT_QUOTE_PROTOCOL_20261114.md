# Elysium 三腿实时对照预注册

2026-11-14，基线 `efa0b93`。在首个事件覆盖窗口及独立部署探测后，另开未观察的
90 秒窗口。沿用[原预注册](EVENT_QUOTE_PROTOCOL_20261114.md)的池、Swap topic、
首四个不同块、4 个预定轮询时刻、750 ms 和补数规则。固定路线为 0.01 HYPE → KNTQ
（Uniswap V4，3000 pips、spacing 60、零 hook）→ USDC（Aero Slipstream 指定池，
MetaQuoter）→ WHYPE（原 volatile 池）。禁止签名或广播。

两组每个样本各使用 1 个锚点和 3 条精确输入报价，最多 16 次只读请求。
事件组从日志的块号直接发报价并并行取哈希块头。Elysium 请求采用数值块号，
由事件/轮询的块哈希及第二来源同高收据复核。数值参数保留瞬时分叉或
负载均衡读漂移风险，即使首尾对账通过也仅作为研究报价。
Aero Quoter 在指定池上模拟交换，检查价格边界以拒绝可能的部分输入。
它可能漏掉执行时优先级相关的 MEV 税，费用与完整净差继续保留未知。
毛差非正的样本，在任何非负增量成本下都无正净差。若 4 笔可核验、窗内连续、
且四个毛差均非正，关闭本固定窗口的后继路线；正毛差在费用未知时仅留作探索，
样本不足、源拒绝、重组、断线均为不完整。
[Aero 固定 ABI](https://github.com/dromos-labs/metadex-slipstream-public/blob/f99f2095918cab69eafeffa2c3d0a8762de737b7/contracts/periphery/interfaces/IMetaquoter.sol)
