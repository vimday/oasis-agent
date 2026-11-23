# 同链多跳套利的 Rust/Python 边界

2026-11-23 · 决策为保留现有混合架构。Rust 承担高吞吐候选搜索和批量整数报价，Python 负责研究编排、证据、预算与独立金额参考。先验证常驻池图和批量调用，再根据真实窗口中各阶段耗时扩大 Rust 范围。独立风险、签名和结算沿用现有权限边界。此次同时优化了 Python 的长路线整数上界构造。

## 正常入口和量纲

常规 Base 与 Multicall 已通过 ctypes 调用 Rust。Rust 现有图搜索上限为 4 跳。Python 为每个扫描请求创建整份池和 tick 的 JSON，Rust 再建图、验证和构造索引。`changed_pools` 过滤闭环，索引仍按请求构造。Python 的长环拓扑、V2 精确利润上界和顺序批次另有研究入口，最大不同池 V2 定量为 400 跳。V4 已核的方向协议费由池键、方向和状态共同决定，当前 Rust 池模型只接一个 `fee_pips`。跨方向不同费或有 Hook 状态池需自己的模型和仿真证据。[正常 Base 调用](../../src/oasis_agent/fastpath/live_shadow_runner.py) · [池图请求](../../src/oasis_agent/fastpath/pool_graph.py) · [Rust 扫描](../../crates/oasis-core/src/graph_arb.rs) · [PoolKey 与费用证据](BASE_V4_FORWARD_DISCOVERY_20261123.md) · [长路金额参考](../../src/oasis_agent/fastpath/v2_sizing.py)

## 同输入分段测量

Mac Studio M1 Max arm64，CPython 3.15.2、rustc 1.100.0 release，基于 `36eede2`。Rust 动态库 SHA-256 `22734001223ea11563b6e557f9bfd062f4df93fdcf48bbf544b0225fd976e0bb`。不含 RPC、整笔 EVM 或提交。下表不同工作负载各自比较，各数字为墙钟阶段中位数。内核计时包含现有 Rust 索引和扫描步骤，完整接口还包含输入装载、跨语言调用和输出解析。

| 工作负载 | Python | Rust 内部 | 完整 Rust JSON 接口 |
| --- | ---: | ---: | ---: |
| 4,096 条固定投入 V2 报价，75 批，42–112 位储备 | 0.418 µs/条 | 0.396 µs/条 | 12.216 µs/条，逐条接口调用 |
| 34 池、6 资产、2–4 跳、1–64 整数投入，75 次 | 41.954 ms | 15.756 ms | 23.892 ms |
| Base 留存六块、58 池、单 WETH 锚、最多三跳，每块 14 次 | 未运行对应混池参考 | 0.0147 ms | 0.3159 ms |

第一行的输出和手续费逐位相同。第二行完整接口约快 1.76 倍，Python 和 Rust 对 2,210 条环的 **622 条正候选**逐路径、选中金额、各腿输出和净额一致，未决区间 0。第三行每块 1,774 条，模型正例 0，保留原采集的合成 CL tick 模型。边界占该轻负载本地扫描约 95%，绝对值约 0.30 ms。在该 Base 窗口，状态可用中位 **374.2 ms**，两笔逐腿报价各约 **309–313 ms**，整笔模拟分母为 0。Base Denim 区块为 200 ms。[原 Base 捕获](BASE_FORWARD_POOL_COMPARISON_20261123.md) · [Base Denim](https://www.base.org/blog/denim)

历史 BSC 前瞻在 115 池上以 Python 对 8,536 条路线评分。现从原始字节复跑 41 次，路径拓扑重建中位 29.773 ms，复用拓扑后的评分 34.664 ms。原线上单次头部收到到状态就绪、状态就绪到评分、评分到整笔调用回退分别为 183.360、51.497、238.209 ms。该次评分即使缩短到 0，按留存阶段相加也只少约 10.9% 的头部至回退时间。该比例用于定位本例，真实多链时延分位和盈利召回待测。[BSC 原捕获](BSC_V2_WIDE_FORWARD_20261123.md)

## 百跳计算的独立结论

300 跳合成固定金额的逐腿除法中位约 0.054 ms。原先构建连续上界从头逐腿累乘并约分，合成 300 跳的系数达 22,796 位。现将相邻段按原执行方向分段合成，并在每次合并时约分。对先 `a*x/(b+c*x)` 后 `d*x/(e+f*x)` 的两段，合成系数为 `(d*a, e*b, e*c+f*a)`。归约顺序保留相同有理曲线、峰值下标和整数上界；报价仍逐腿向下取整。现有 U256/U512 足以表达很多链上输入和局部运算，长曲线证书需支持更宽中间结果或保守报告未决。[实现](../../src/oasis_agent/fastpath/v2_sizing.py) · [大整数算子](../../crates/oasis-core/src/u256_math.rs)

41 组交替 AB/BA 测量相同的完整 `V2Route` 构造，弃 4 组热身。12 条 BSC 留存路线和 3 条合成路线的规范系数及 route 对象均完全一致，新的 301 跳回归还以逐腿 `Fraction` 作为独立对照。

| 路线 | 逐腿构造 p50 | 当前分段构造 p50 |
| --- | ---: | ---: |
| BSC 100–123 跳、不同池、可重复资产的 8 条留存路线 | 2.61–4.10 ms | 0.59–0.78 ms |
| 合成 100 跳 | 3.591 ms | 0.723 ms |
| 合成 200 跳 | 19.521 ms | 2.170 ms |
| 合成 300 跳 | 55.897 ms | **3.815 ms** |

另在 40 条负 48 跳环加 1 条正 300 跳环的合成图，用 100,000 次边访问预算做 15 次复跑。全图与已知变池范围均找到 300 跳，分别访问 98,739 和 300 条边，耗时中位 52.566 和 8.656 ms，含两臂各自拓扑构建。变池范围覆盖基于已验证的更新集，断流、费用变化和重组仍需重新考虑全图。合成正例用于测算法，BSC 百跳留存用于测真实整数负载，经济证据见旧快照记录。[长尾及留存路线](MULTICHAIN_TAIL_DEPTH_20261123.md) · [变池搜索实现](../../src/oasis_agent/fastpath/long_cycle.py)

## 迁移顺序及验收

1. 在 Rust 现有 crate 内增设只读影子对照，让池拓扑和索引常驻，接收有链号、块哈希、父哈希和输入版本绑定的增量。添加 ABI 版本、构建 ID 和能力标识，拒绝把旧库缺失的覆盖字段当作完整覆盖。对同一块的多个锚批量调用，仍逐锚传入金额单位、成本与阈值。Python 的日志和回放负责独立校验重组、缺口与恢复后的结果。[现有 FFI 契约](../../src/oasis_agent/fastpath/bridge.py) · [状态同步](../../src/oasis_agent/fastpath/state_sync.py)
2. 对真实保留窗口和前瞻窗口并行 A/B。冻结相同路由空间、状态、费用、候选预算及整笔模拟请求。记录来源到状态、排队、解码、索引/传输、搜索/定量、整笔模拟和后继块到达时钟。逐腿金额、排序、预算截断与未决区间应完全对应。零正例窗口只报告耗时与假阳性，不估正机会召回。沿用 TODO 的端到端 p99 与同期限经济召回门槛。
3. 优先用拓扑约束、变池范围和金额上界控制 5–400 跳工作量。只有在等覆盖、等精度下还是 CPU 造成错过窗口，才把长环遍历和批量定量逐项迁入 Rust。互斥的协议费用、tick 数据不完整、代币行为和整笔模拟继续单列。GPU 在 Aleph RTX 4070 Ti 上用同一套窗口和最佳批量 CPU 基线复测，当前报告未联机运行 Aleph。[既有 GPU 取样](MULTIHOP_CUDA_20261122.md) · [多跳验收与 TODO](../strategies/MULTIHOP_ARBITRAGE_TODO.md)

## 复现

忽略目录 `reports/research/rust-core-decision-20261123/` 包含 `long_plan.json`、`bench_long.py`、`bench_compose.py`、`compare_shipped_composition.py` 和逐次计时原文。长路基准 `long_profile.json` SHA-256 `a33e558193f5a69ffcb4c983ff2a97617b85f883d118984ad40d7a4bb9dcd936`，交替 A/B 的 `shipped_composition.json` SHA-256 `d059383db70e39eb90da5637b49307ae46b9e699f32571d682f39c31f18cbc03`。另一个忽略目录 `reports/research/rust-python-stage-bench-20261123/` 保留同输入 Python/Rust 基准、`README.md` 及 `verified.json`，结果 SHA-256 `d33f8da39012cfd043a7765b24445ac01c8acbd83b9e6ca3099d59990bbc8282`。这些哈希标识当次测量，重跑计时会变化；整数结果必须一致。

```sh
PYTHONPATH=. .venv/bin/python reports/research/rust-core-decision-20261123/compare_shipped_composition.py
```

`make check`、`make security-check`、`make contract-security-check`、`make rust-check` 均退出 0。6,609 个 Python 测试、138 个子测试、125 个 Foundry 测试和 23 个 Rust 测试通过。原 BSC 批次三份确定性摘要逐字节一致，Base 的 13 对原始报文、三个图结果和七腿金额复算通过，172 个文档相对链接与 `git diff --check` 通过。保留 10 条 Python 警告与 6 条既有 Slither 提示。日志在 `/private/tmp/oasis-core-split-{check,security,contract,rust}-20261123.log`。
