# 链上多跳套利技术调研

Updated 2026-11-22. 研究记录，不构成交易授权或收益承诺。本文中的 Oasis 数字均为只读、影子或
合成证据；`execution_eligible = false`、`release_permitted = false`。外部数字以论文原文为准，
推导数字在文中标注为“本文计算”。

## 0. 结论先行

1. **几十跳在静态图上存在，但已执行、扣费后为正的原子套利仍以短路径为主。** MeVisor 统计
   30,646 笔历史套利，82.21% 涉及少于 3 个 DEX；它允许最长 16 笔交易的搜索，实际发现 2–14 笔，
   其中 14 笔只有 3 个 bundle、平均利润约 USD 350。RICH 默认 `k = 5`，并引用既有工作指出加密
   市场超过 5 跳的环少于 2%。MMBF 在 Uniswap V2 静态图上找到的主模态是 7–11 跳，但该文明确
   不考虑 gas，且大量机会低于 USD 10。
2. **“几十跳”至少混用了三种对象。** 聚合器的支付路由（A→N，可拆单，不是无风险套利）、多交易
   bundle（MeVisor 的 14 是交易数，不是简单环跳数）、静态图上的潜在长环（多数是粉尘）。不先
   区分口径，就会把“搜得到”误当成“执行得了”。
3. **长路径的瓶颈不是搜不到，而是扣完费用、gas、状态漂移和竞争后还剩多少。** 在 Base 上，
   30 bps × 20 跳的费用拖累约 5.83%，盈亏平衡需要约 6.19% 的毛差；32 跳的 L2 gas 约 6.5M，
   按最低 base fee 约 USD 0.054，在 USD 1,000 名义额上约 0.54 bps。gas 不是主要约束，费用拖累
   与状态新鲜度才是。
4. **Oasis 当前 Rust 内核上限 4 跳，34 池、1,868 条四跳环路 2.37 ms。** 真正的瓶颈是状态读取与
   远程报价：历史事件实验两腿报价 489–1,082 ms，加完整费用模型后 0/4 在 750 ms 内完成。先做
   CPU 按变池增量扫，再做 GPU 批量筛选，是合理的顺序；GPU 不应先于状态与复核管线。
5. **建议把长路径限定为研究模式。** 生产仍以 2–4 跳为主；k≤8 的按需枚举只用于研究、可视化和
   对照实验。A/B/C 的判定指标应是“从收到状态到完整复核的 p95/p99、时限内真实正净候选召回、
   漏掉候选的净额”，不是模拟吞吐。

## 1. 先把“跳”的口径定清楚

| 口径 | 定义 | 典型数量 | 经济含义 | 主要证据 |
| --- | --- | ---: | --- | --- |
| 池跳 | 一次池内 swap | 2–4；长尾可到 10 以上 | 一个池的状态、费用、价格冲击 | RICH、MeVisor |
| DEX 跳 | 跨一个 DEX 或协议 | 82.21% 少于 3 | 执行路径的协议数 | MeVisor 图 2 |
| 交易数 | 一个 bundle 内的 tx 数 | 2–14 | 排序、原子性、bundle 级 gas | MeVisor 表 IX |
| 聚合器路由 | A→N 的支付路径，可拆单 | 可达数十 | 最优执行，不是无风险套利 | CFMM routing |
| 跨链跳 | 桥或链切换 | 2–N | 非原子，带桥和链风险 | N-hops |

- MeVisor 的 14 是 bundle 内交易数，不是 14 个池的简单环；它的 path length 统计的是交易数。
- RICH 的 `k` 是环的边数，默认 5；它找的是限跳简单负环。
- MMBF 的 path length 是 token 图上的边数，包含 loop 和 non-loop；不计 gas。
- 聚合器的“几十跳”通常是 A→N 的支付路由，允许拆单和合流；把它叫作“套利”会混淆目标函数。

本文后续若不做特别说明，“跳”指池跳；“简单环”指不重复 token、且每个池最多用一次的有向环。

## 2. 外部证据

### 2.1 MeVisor（NDSS 2026）

MeVisor 把 MEV bot 编译到 GPU，用并行遗传算法搜索套利和 sandwich，再在 CPU 私有分叉上验证。
实验环境为 RTX 3090（24 GB VRAM）；CPU 对照 Lanturn 使用 44 个线程。关键数字如下。

| 指标 | 数值 | 口径与含义 |
| --- | ---: | --- |
| 套利模拟吞吐 | 3,378.49k tx/s | GPU；sandwich 为 5,179.57k tx/s |
| 套利搜索耗时 | 11.49 s | CPU 对照 Lanturn 115.90 s，约 10.1× |
| bundle 吞吐 | 1,425.93k/s | Lanturn 5.83/s |
| 历史套利回放 | 2,380 bundles | 每个 bundle 含 2–5 笔交易 |
| 逐笔完全一致 | 1,149 / 2,380 | 48.28% |
| 仅 V3 案例 | 154 / 649 | 23.7% |
| 汇总值比例 | 99.71% | 按 ETH 计的 output 比例，不是逐笔精度 |
| Q1 2025 套利利润 | USD 884,116.65 | 单个 4 笔交易案例 USD 753,407.19，占 85.2% |
| 长 swap bundle | 6,618 个正收入 | 其中 6,389 个不足以覆盖 gas（96.5%） |
| 历史路径分布 | 82.21% 少于 3 个 DEX | 30,646 笔 2025 年 1–6 月套利 |
| 搜索上限 | 16 笔交易 | 实际发现 2–14 笔；14 笔仅 3 个 bundle，平均 USD 350 |

要点：

- “100,000×”是模拟吞吐口径，不是搜索耗时；搜索耗时约 10×。把两者混用会夸大 GPU 的端到端收益。
- 99.71% 是汇总值比例，不能当作逐笔执行精度；GPU 误差是低估，只产生 false negative，不产生
  false positive。这适合做筛选器，不适合直接作为签名依据。
- 长路径确实存在，但经济上高度集中：一个 4 笔交易案例贡献了 85% 的利润；96.5% 的长 swap
  bundle 连 gas 都覆盖不了。这更像“偶发的大偏离”，不是“稳定的长路径 alpha”。
- 原文表 IX 的 path length 是 bundle 内交易数；把它直接叫“四跳”会把交易数与池跳混为一谈。
  本文按“4 笔交易”表述，不把它等同于 4 个池的简单环。

### 2.2 RICH（PVLDB 2025）

RICH 把套利建模为 `k`-跳负环检测，用 color-coding 和动态规划避免穷举。

| 数据集 | 节点 | 边 | RICH 检测时间 | 相对次优 |
| --- | ---: | ---: | ---: | ---: |
| UNI1 | 11.6K | 27.2K | 0.94 s | 1.6× |
| UNI2 | 44.9K | 99.7K | 2.80 s | 6.3× |
| UNI3 | 70.4K | 151.3K | 3.64 s | 12.57× |
| UNI4 | 148.1K | 307.9K | 18.61 s | 15.73× |
| UNI5 | 262.1K | 536.0K | 33.67 s | 30.05× |
| UNI6 | 359.6K | 734.1K | 47.52 s | 32.69× |

- 默认 `k = 5`、着色次数 `θ = 30`；论文指出超过 5 跳的环在加密市场少于 2%。
- 即使 k=5，最大的图仍需 47.52 s；这不是 200 ms 块内可用的延迟。RICH 的价值是离线/近线搜索
  和召回对照，不是直接替代实时快路径。
- color-coding 是概率算法；论文用多次着色把漏检概率压低，但仍有概率保证，不是确定性完备。

### 2.3 静态长路径枚举（MMBF，arXiv 2406.16573）

该文用 line graph + 改进的 Moore–Bellman–Ford，在 Uniswap V2 上找 loop 和 non-loop。

- 100 个 token、约 400 个池，运行约 8–10 s。
- 路径长度主模态 7–11，最长到 30；MBF 对照的主模态是 3–4。
- 23,868 条路径潜在利润超过 USD 1,000；但大量路径低于 USD 10。
- 论文明确不考虑 gas；也承认找到的路径不一定是最赚钱的。

这说明 7–11 跳是静态图的常见形态，但不是执行分布。静态枚举的“长路径很多”与 MeVisor 的
“已执行路径很短”并不矛盾：前者是图上的对象，后者是扣费后仍有人愿意提交的对象。

### 2.4 已执行循环套利（arXiv 2105.02784）

- Uniswap V2 十一个月：292,606 笔循环套利，收入超过 USD 138M，gas 8,458 ETH。
- 只有 0.03% 的交易者用多笔链上交易顺序执行；原子实现占主导。
- 论文的理论框架分析了 CPMM 循环套利的盈利条件和最优投入。

对本文的意义：原子执行是行业标准；gas 是真实成本；多笔顺序执行会暴露在中间价格变化下。
长路径若要执行，必须同样满足原子性和净额约束。

### 2.5 CFMM 路由理论（arXiv 2204.05238）

- 忽略固定成本时，多 CFMM 的最优路由是凸优化，可解。
- 加入固定成本后，是混合整数凸问题，需要全局优化或启发式。
- 套利识别是路由问题的特例。

因此，谈“全局最优”前必须先说明：允许哪些池、几跳、能否拆单/合流、包含哪些成本。Oasis 当前
的搜索是“简单环、池只用一次、不拆单、固定金额区间”的受限最优，不是所有策略的全局最优。

### 2.6 Base 的块与费用

- Denim 升级后，Base 原生 200 ms 块，每秒 5 个完整块，Flashblocks 退役；`block.number` 增长
  速度是原来的 10 倍，`BaseTime` 提供毫秒时间。
- 费用由 L2 执行费和 L1 数据费组成；L2 最低 base fee 为 0.005 gwei；主网 gas limit 40M、
  target 8M；L1 fee 可通过 `GasPriceOracle.getL1Fee` 读取。
- 含义：状态寿命更短；gas 很便宜；费用拖累、状态漂移和竞争才是长路径的主要敌人。测量
  “状态到达 → 完整复核”的端到端时间，比只看块时间更有意义。

## 3. 计算与定价

### 3.1 图模型

把 token 当节点，把每个池的两个方向当有向边；同一币对的不同池保留为平行边。边权取
`w_e = -log r_e`，其中 `r_e` 是已扣池费的边际兑换率。一圈边权和小于零，表示极小金额的连续
模型有正毛差。

对 CL 池（V3/Slipstream/V4），`r_e` 是当前 tick 的边际率；金额变大要跨 tick，平均率会下降。
对 V4，hook 可能改变费率、结算量和调用上下文，报价必须绑定具体池和调用参数。

### 3.2 复杂度

下表是 `d^k` 的上界示意，未计简单性剪枝、池唯一性和上界剪枝；真实枚举数通常更低，但量级不变。

| 分支度 d | k=4 | k=8 | k=12 | k=16 | k=20 |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 3 | 8.1e1 | 6.6e3 | 5.3e5 | 4.3e7 | 3.5e9 |
| 4 | 2.6e2 | 6.6e4 | 1.7e7 | 4.3e9 | 1.1e12 |
| 5 | 6.3e2 | 3.9e5 | 2.4e8 | 1.5e11 | 9.5e13 |
| 8 | 4.1e3 | 1.7e7 | 6.9e10 | 2.8e14 | 1.2e18 |

- Bellman–Ford 是 O(VE)，能找到负环，但不保证简单环，也不能指定起点；对 36 万节点、73 万边的
  图，一次运行就远离 200 ms 预算。
- 若把 k 也作为输入，检测长度至少 k 的负环（kLNCC）是 NP-完全的（Guo & Li, 2021）；固定小
  k 可用 color-coding 等 FPT 算法，但复杂度随 k 指数增长，k=5 实用，k=20 很贵。RICH 用位集
  编码和图约简把常数压低。
- meet-in-the-middle 可把 O(b^k) 降到 O(b^(k/2))，但要处理简单性、池唯一性和内存；适合作为
  长路径研究选项。
- beam search / A* 用可采纳上界做实时筛选，复杂度 O(B·b·k)，但没有完备性，需要单独标定召回。
- 通用 GPU 图库不是即插即用：cuGraph 的 `sssp` 明确不支持负权环；负环、简单路径约束和有限
  金额滑点需要自定义内核。

Oasis 的 DFS 在 k≤4、简单环、池只用一次、给定金额区间和 48 步搜索精度内是精确的；这正是它
53 µs / 2.37 ms 的原因。把同一个 DFS 直接推到 20 跳，不会得到可用的实时性。

### 3.3 简单环与复合环

- 任意闭合 walk 都可以分解为若干简单环；在对数权重模型下，如果所有简单环都非负，那么任何
  复合 walk 也非负。因此，找“是否存在正套利”只需要看简单环。
- 复合环的主要额外收益是节省固定成本：把多个环放进一笔交易，少付几次基础 gas。Base 上
  3 跳交易的总 gas 约 0.0058 USD，16 跳约 0.027 USD；可节省的基础开销只有几美分。
- 在非线性价格冲击下，分解不是严格等价；但顺序执行和原子执行都会改变池状态，差异是二阶的，
  尤其在小金额下。
- 因此，生产环境限制为简单环是合理的；长复合 walk 不是近期优先级。

### 3.4 金额优化

对固定费率、每池只用一次的 CPMM 路线，逐跳常数乘积可合成为：

```text
F(x) = A x / (1 + B x)
```

其中 `A` 是整圈边际兑换率（已扣费），`B` 描述深度带来的价格冲击。忽略比例融资费时，内部最优
投入为：

```text
x* = (sqrt(A) - 1) / B
```

净额 `F(x*) - x* - c`，`c` 是固定成本。本文用两个合成三跳路线复核了你给出的表：

| 合成路线 | 极小金额毛差 | 连续最优投入 | 复算净额 | 原表净额 |
| --- | ---: | ---: | ---: | ---: |
| 浅池，价差更宽 | +5.05% | 4.18 USDC | −0.0958 | −0.096 |
| 深池，价差较窄 | +1.08% | 181.39 USDC | +0.7769 | +0.781 |

复算方法：由 `A = 1 + 毛差`、`x*` 反推 `B = (sqrt(A) - 1) / x*`，再算 `F(x*)`；两行分别得到
`B ≈ 5.966e-3` 和 `B ≈ 2.969e-5`。净额与表中数值在舍入范围内一致。第二行相差 0.004，来自
原表对 `A`、`B` 或 `x*` 的保留位数；不改变结论。

这个例子的重点是：**屏幕价差更宽，不等于交易更赚。** 浅池的价差更宽，但最优投入只有 4.18
USDC，扣 0.20 固定成本后为负；深池价差较窄，但能承受 181 USDC 的投入，扣费后为正。长路径会
累积 `B`，把最优投入进一步压小，同时增加每跳固定成本，因此结构上更容易变成粉尘。

对 CL 池，`F` 是分段函数，需要逐 tick 处理流动性变化和整数舍入；用 48 步整数三分搜索是稳健
做法，但每次评估是 O(k)，路线数才是主项。浮点结果只能用于筛选，边界候选必须用整数模型复算。

### 3.5 费用、gas、状态新鲜度

下表按每跳 5 bps 和 30 bps 计算费用拖累 `1 - (1-f)^n`、盈亏平衡所需毛差、Base gas（按
`100k + 200k/跳` 估算，0.005 gwei，ETH USD 1,650.89）以及路线在 q=0.99 / 0.95 下保持不变的
概率。

| 跳数 | 5 bps 拖累 | 30 bps 拖累 | 30 bps 所需毛差 | 估算 gas | gas USD | gas bps @USD 1k | q=0.99 | q=0.95 |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 2 | 0.10% | 0.60% | 0.60% | 0.50M | 0.0041 | 0.04 | 98.0% | 90.2% |
| 4 | 0.20% | 1.19% | 1.21% | 0.90M | 0.0074 | 0.07 | 96.1% | 81.5% |
| 8 | 0.40% | 2.37% | 2.43% | 1.70M | 0.0140 | 0.14 | 92.3% | 66.3% |
| 16 | 0.80% | 4.69% | 4.92% | 3.30M | 0.0272 | 0.27 | 85.1% | 44.0% |
| 20 | 1.00% | 5.83% | 6.19% | 4.10M | 0.0338 | 0.34 | 81.8% | 35.8% |
| 32 | 1.59% | 9.17% | 10.09% | 6.50M | 0.0537 | 0.54 | 72.5% | 19.4% |
| 50 | 2.47% | 13.95% | 16.21% | 10.10M | 0.0834 | 0.83 | 60.5% | 7.7% |

- 30 bps × 20 跳需要约 6.19% 的毛差才能覆盖费用；32 跳需要约 10.09%。
- 在 USD 1,000 名义额上，gas 只占 0.04–0.83 bps；在 USD 100 名义额上会放大 10 倍，长路径的
  薄边际更容易被 gas 吃掉。
- 50 跳估算 gas 10.1M，超过 Base 8M 的 target；虽然低于 40M limit，但会显著增加包含和竞争
  风险。
- 状态新鲜度是乘积：每池 1% 的变更概率，20 跳后只有 81.8% 的路线仍完全不变；每池 5% 时只有
  35.8%。长路径对状态漂移的敏感度是指数级的。

期望净额可以写成：

```text
E[net] ≈ q^n (gross - variable_cost) - (1 - q^n) revert_cost - gas
```

其中 `q` 是单池在“报价到包含”窗口内保持不变的概率。这个模型是本文的推导，不是实测；它说明
为什么长路径需要 hop 相关的安全边际，而不是一个全局固定滑点。

### 3.6 执行概率与竞争

- Base 上的套利竞争有两类：定向搜索（链下发现、提交承诺路线）和概率搜索（反复提交、在链上
  决定是否成立）。AFT 2026 的样本中，概率搜索只占 23% 的活动，却产生 95% 的 spam、消耗 20%
  的 Base gas。
- 长路径的边际更薄，对 priority fee、builder payment 和包含概率更敏感；即使毛差为正，扣掉
  竞争成本后也可能为负。
- 因此，“搜到候选”不是终点；必须把失败/回滚成本、包含概率和 builder 支付计入期望净额。

## 4. Oasis 现状

| 组件 | 当前实现 | 与长路径相关的缺口 | 优先级 |
| --- | --- | --- | --- |
| Rust 图搜索 | `MAX_SUPPORTED_HOPS = 4`；简单环；池只用一次；上界剪枝；48 步三分；**Wave 32 已合入 `ScanCyclesOptions`**（按跳/V2-CL 池类型累加 gas、canonical 基币+旋转去重、`changed_pools` 增量过滤） | 默认 `scan_cycles` 为向后兼容仍走全扫与固定 base gas；不能 `k>4`（研究模式待定） | **P0/P1 已在内核实现**（默认保持关闭） |
| Python 快路径 | 默认 `max_hops = 3`；live runner 用 2；[pool_graph.py](../../src/oasis_agent/fastpath/pool_graph.py) 已透传 `per_hop_gas_cost_in_base` / `v2_hop_gas_cost_in_base` / `cl_hop_gas_cost_in_base` / `deduplicate_cycles` / `changed_pools` | 生产路径比研究内核更短，且上层 `live_shadow_runner.py` 需按需启用增量参数 | 现状 |
| 影子增量 | `cycle_shadow.py` 用 changed pools 过滤受影响路径；Wave 32 起 Rust `scan_cycles_with_options` 原生支持 `changed_pools` 跳过未变环路 | gas/fee 变化、新池、无事件状态变化时仍需周期性全扫 (`changed_pools=None`) | **P1 已在内核实现** |
| 实测样本 | Wave 25：600 块、57 池、1 个正净块、3 个三跳候选 | 归因日志没有完整逐块池状态，不能作为搜索召回真值 | P1 |
| 延迟 | 两腿远程报价 489–1,082 ms；完整费用模型 0/4 在 750 ms 内 | 搜索内核 2.37 ms 不是瓶颈；状态和报价才是 | P0 |
| GPU | 无 | 没有本地对照；无 CUDA 实现 | P3 |

### 4.1 Rust 内核

[graph_arb.rs](../../crates/oasis-core/src/graph_arb.rs#L386)
把上限固定为 4；[scan_cycles](../../crates/oasis-core/src/graph_arb.rs)
对每个 base token 做 DFS，[enumerate_indexed_cycles](../../crates/oasis-core/src/graph_arb.rs)
在闭合前用 `max_rate_upper` 的乘积做剪枝；[ternary_search_indexed](../../crates/oasis-core/src/graph_arb.rs)
做 48 步整数三分。合成基准：6 池两跳 53 µs，34 池、1,868 条四跳闭环 2.37 ms
（[BASE_100TICK_MULTICALL_CAMPAIGN_20261118.md](../verification/BASE_100TICK_MULTICALL_CAMPAIGN_20261118.md#L7-L8)）。

Wave 32 针对长路径评估已补齐两项 P0 机制与一项 P1 机制（通过 `ScanCyclesOptions` 可选启用，默认与历史全扫逐位一致）：

- **按跳数与池类型累加 gas（P0）**：`per_hop_gas_cost_in_base`、`v2_hop_gas_cost_in_base`（覆盖 `V2` / `AerodromeStable`）与 `cl_hop_gas_cost_in_base`（覆盖 `V3` / `Slipstream` / `V4`）可按每条环路的实际跳数和池类型累加执行 gas 并计入三分搜索净收益与输出的 `gas_cost_in_base`。
- **多 base token 规范去重（P0）**：开启 `deduplicate_cycles=True` 后，DFS 按排好序的 `base_tokens` 序严格限制不穿行低序号中间 base token，并在输出端按 `(pool_id, token_in, token_out)` 字典序最小有向旋转去重（同环冲突保留高净额者）。
- **热路径 `changed_pools` 增量过滤（P1）**：传入 `changed_pools` 列表后，DFS 沿栈跟踪命中标志并在闭合前跳过所有未触及变动池的环路与三分搜索。

### 4.2 Python 快路径与影子

[pool_graph.py](../../src/oasis_agent/fastpath/pool_graph.py)
默认 `max_hops = 3`；[live_shadow_runner.py](../../src/oasis_agent/fastpath/live_shadow_runner.py#L1258)
用 2。[cycle_shadow.py](../../src/oasis_agent/arbitrage/cycle_shadow.py#L196-L233)
已经做了变池增量：当某个池在块内发生时，只把包含该池的路径放入 `affected`，再按金额档位生成
报价点。Wave 32 将同样语义接入 Rust 热路径 `changed_pools` 参数后，可直接在整数图内跳过未变动环路；但需要注意“池没变，
路线的最优金额和净额就不变”的假设仅在 gas/fee 与无事件状态均不变时成立，gas 变化、新池上线或特殊捐助/重定基事件仍需配合定期全扫 (`changed_pools=None`)。

### 4.3 Wave 25 的实测

[Wave 25 记录](../verification/WAVE23_REVIEW_FIXES_AND_ANTITOXICITY_20261121.md#L78-L85)
覆盖 600 个唯一 Base 区块、57 个活跃池；只有 1 个采样块出现模型正净额
（本地未入库日志 `reports/research/wave25-20261121/shadow-bybit-600-r2/shadow_attribution.jsonl` 第 559 条）。
该块的 3 个候选全部是三跳，按 bps 最高的一条：

```text
投入          143.020414 USDC
毛差          0.677665 bps  -> 0.009692 USDC
gas           0.005782 USD
净额          0.003910 USDC  -> 0.273388 bps
```

这条候选的 gas 占 0.404 bps，已经超过净额的一半。若把同样的 gas 比例放到 20 跳（约 2.4 bps），
0.27 bps 的边际会直接消失。这是“长路径在薄边际下不可行”的最直观例子。

### 4.4 延迟

[EVENT_QUOTE_RESULTS_20261114.md](../verification/EVENT_QUOTE_RESULTS_20261114.md#L21-L31)
记录两腿完成延迟 489.153、512.512、1,082.297、908.612 ms；两腿在 750 ms 内完成 2/4，但加上
L1 费读取等完整费用模型后 0/4。这个实验只有两腿；按线性外推，20 跳逐跳远程报价会到秒级，
远超 200 ms 块。长路径要实时，必须依赖本地状态和批量报价，而不是逐跳远程 Quoter。

## 5. 几十跳值得做吗

| 场景 | 典型跳数 | 是否原子 | 经济形态 | 对 Oasis 的相关性 |
| --- | ---: | --- | --- | --- |
| 聚合器支付路由 A→N | 可达数十 | 是（单笔内） | 最优执行，可拆单；凸优化/固定成本 MILP | 低；不是无风险套利 |
| 同链原子循环套利 | 2–4 | 是 | 短寿命、竞争强、净额薄 | 高；当前主战场 |
| 多交易 bundle | 2–14 tx | bundle 内原子 | 排序与打包；长 bundle 多数不覆盖 gas | 中；研究 |
| 静态图长环 | 7–11 | 不一定 | 大量 <USD 10 的潜在机会 | 低；研究/可视化 |
| 跨链 N 跳 | 2–N | 否 | 桥和链风险；机会稀少 | 低；超出原子范围 |

直接回答“几十跳”的问题：

- **作为静态图对象，存在。** MMBF 在 Uniswap V2 上找到 7–11 跳的主模态，最长到 30。
- **作为已执行原子套利，罕见。** MeVisor 的 82.21% 少于 3 个 DEX；RICH 引用超过 5 跳少于 2%。
- **作为可覆盖成本的机会，更罕见。** MeVisor 的长 swap bundle 96.5% 不覆盖 gas；静态枚举的
  大量路径低于 USD 10。
- **作为聚合器支付路由，常见。** 但它的目标是把 A 换成 N，不是闭环套利；不应与套利混谈。

因此，近期把工程资源投入 2–4 跳的状态覆盖、延迟和精确复核，比追求几十跳的搜索更有期望价值。
长路径应作为受控研究：用 k≤8 的枚举、严格净额过滤和独立真值，回答“在 Base 上，长路径是否
存在可重复的正净额”，而不是直接上生产。

### 5.1 长路径的判定树

对每个长候选，按顺序过滤：

1. 是否是简单环、池只用一次？不是则分解为简单环或按 bundle 处理。
2. 当前可观测毛差是否高于费用拖累后的所需毛差？不是则丢弃。
3. 最优投入是否高于最小名义额，且净额覆盖 gas？不是则丢弃。
4. 路线在报价到包含窗口内保持不变的 `q^n` 是否高于阈值？不是则丢弃或加安全边际。
5. 能否在固定块状态下用整数模型和 `minOut > 0` 模拟整笔？不能则不可执行。
6. 扣掉失败/回滚、priority fee、builder 支付和竞争后的期望净额是否仍高于准入阈值？不是则丢弃。

这六步比“搜到一条长环”更接近可执行性。

## 6. A/B/C 离线实验

### 6.1 预注册

- **假设 A（B vs A）**：在相同快照、相同金额档位、相同费用模型下，按变池增量扫能把从状态
  到完整复核的 p99 降低至少 50%，且召回不下降。
- **假设 B（C vs B）**：GPU 批量筛选 + CPU 复算能在相同 p99 下把时限内真实正净候选召回提高
  至少 10%，或在不降低召回的情况下把 p99 再降低 30%，且总成本不超过 1.5×。
- **决策规则**：先跑 B vs A；只有 B 达标才值得引入 GPU。C 若不改善“复核后的召回/延迟”，
  只是模拟吞吐更高，则拒绝。
- 预注册阈值、窗口和失败记录；看到结果后不改阈值。整个实验只读、影子、无签名、无广播。

### 6.2 夹具

- 每个固定块保存：块号、块哈希、state root、`BaseTime`、原始 Multicall3 请求/响应字节、
  提供方别名、检索时间、响应哈希。
- V2：reserves；V3/Slipstream：`slot0`、`liquidity`、当前 tick 附近的 tick bitmap 和
  tick liquidity；V4：PoolManager 状态和 hook 身份。CL 池先取当前 tick ±5% 的范围；范围外
  标记为 unknown，不进入精确真值。
- 报价必须绑定固定块；用该块的链上 Quoter 或本地精确模型交叉验证。非零 hook 池默认排除；
  储备/转账税 token 除非精确建模，否则拒绝。
- 真值：小图（10–20 池）用穷举 DFS 到 k=6；大图用 RICH/缩减图 + 人工复核；k=8–12 用带已知
  植入环的合成图测召回。所有原始字节和哈希留存，禁止 look-ahead。

### 6.3 三个实验臂

| 臂 | 做法 | 目的 |
| --- | --- | --- |
| A：CPU 全扫 | Rust 现有内核，k=2..4，全量枚举；k=5/6 另设研究臂 | 基线；测搜索内核占比 |
| B：CPU 增量 | pool→route 倒排索引；只重算含变更池的路线；每 N 块全扫一次；gas/fee/新池触发全扫 | 测增量是否正确、能省多少 |
| C：GPU 筛选 + CPU 复算 | 路线 × 金额档位常驻显存；浮点/上界筛选 top-K；CPU 整数复算 + 固定状态模拟 | 测 GPU 是否提高“复核后”的召回/延迟 |

C 的 GPU 只做筛选：用可证明的上界淘汰，边界候选保留；CPU 复算必须覆盖所有拟执行候选。不要
用 GPU 的浮点结果直接决定签名。

### 6.4 指标

- 从收到状态到完整复核的 p50/p95/p99。
- 时限内真实正净候选的召回；漏掉候选的净额总和。
- CPU 复算后的 false positive rate。
- 每块成本：CPU ms、GPU ms、显存、RPC 次数、字节数。
- 候选在 +1/+2/+5 个后续块的残余净额；回滚/失败成本。
- 分布而非均值；包含失败、空块、RPC 降级和重组。

### 6.5 判定与 Wave 33 A/B 实测结论

- B vs A：p99 降低 ≥50%、召回不降、每块成本可接受 → 采用 B；若增量漏掉新池或 gas 变化，
  加全扫频率，不计入“B 的收益”。
- C vs B：复核后召回 +10% 或 p99 −30%，成本 ≤1.5× → 进入下一阶段小规模 GPU 试点；否则
  记录为“GPU 不适合当前状态/管线”。
- 任何阶段都不以“模拟 tx/s”作为通过标准。

**Wave 33（假设 A 离线固定块快照 A/B 实测已通过）**：[incremental_scanner.py](../../src/oasis_agent/fastpath/incremental_scanner.py)（`IncrementalCycleScanner` + `run_multihop_ab_experiment`）与 [run_multihop_ab_experiment.py](../../scripts/run_multihop_ab_experiment.py) 已完成 Arm B 缓存 + 参数/拓扑/周期 checkpoint 全扫兜底，并通过 [test_incremental_cycle_scanner.py](../../tests/unit/test_incremental_cycle_scanner.py) 单元测试。在 34 池 6 代币 4 跳、120 个固定块快照（每块 1 变动池、`full_scan_every_n_blocks = 200`、`seed = 20261122`、`execution_eligible = false`、`release_permitted = false`）下运行 `--require-pass` 实测结果：

- **召回与零误报逐位一致**：`exact_bit_for_bit_blocks = 120 / 120`、`recall = 1.0`、`missed_candidates_count = 0`、`missed_net_profit_raw = 0`、`false_positive_count = 0`（双臂均产出 `2,693` 条候选）；
- **评估与延迟下降**：评估环路次数由 `224,160` 降至 `26,392`（**`-88.23%`**），块延迟由 Arm A（`mean = 62.59 ms`、`p50 = 61.25 ms`、`p95 = 82.48 ms`、`p99 = 85.56 ms`）降至 Arm B（`mean = 8.36 ms`、`p50 = 5.32 ms`、`p95 = 25.20 ms`、`p99 = 40.22 ms`），**`p99_reduction_ratio = 52.99% >= 50.0%`（`hypothesis_a_passed = true`）**。

### 6.6 不要做什么

- 不要把 GPU 的模拟吞吐和 CPU 的模拟吞吐直接比较；要比“单位时间内复核后的真实机会”。
- 不要把 MeVisor 的 99.71% 汇总精度当作逐笔精度；逐笔只有 48.28%。
- 不要忽略 gas、失败交易和回滚成本；MeVisor 自己就报告 96.5% 的长 bundle 不覆盖 gas。
- 不要把静态枚举的潜在利润叫“可执行利润”；MMBF 明确不计 gas。
- 不要在未定义允许池、跳数、拆单和成本模型时宣称“全局最优”。

## 7. 建议

**P0：先把 2–4 跳做扎实（内核选项已于 Wave 32 合入，上层接入持续推进）。**

- 保持生产 `k ≤ 4`；[graph_arb.rs](../../crates/oasis-core/src/graph_arb.rs) 与 [pool_graph.py](../../src/oasis_agent/fastpath/pool_graph.py) 已新增 `per_hop_gas_cost_in_base` / `v2_hop_gas_cost_in_base` / `cl_hop_gas_cost_in_base` 支持按跳数与池类型累加 gas。
- 已新增 `deduplicate_cycles`（DFS 最小 base rank + 有向最小旋转去重），避免同一环因多个 base token 重复计价。
- 对每个候选记录：跳数、池类型、毛差、费用、L1/L2 gas、净额、状态块、报价来源、复核状态。
- 测量“状态到达 → 本地图更新 → 候选枚举 → 金额优化 → 精确报价 → 模拟”的分段 p50/p95/p99；
  以 200 ms、1 s、5 s 三个时限报告召回和残余净额。

**P1：增量索引（热循环过滤于 Wave 32 合入；状态扫描器与 A/B 夹具于 Wave 33 验收通过）。**

- 已将 `changed_pools` 增量过滤下沉至 [graph_arb.rs](../../crates/oasis-core/src/graph_arb.rs) 的零分配 DFS 热循环并透传至 [ffi.rs](../../crates/oasis-core/src/ffi.rs) 与 [pool_graph.py](../../src/oasis_agent/fastpath/pool_graph.py)，只对触及变更池的环路执行上界预检与三分搜索；
- [incremental_scanner.py](../../src/oasis_agent/fastpath/incremental_scanner.py)（`IncrementalCycleScanner`）与 [run_multihop_ab_experiment.py](../../scripts/run_multihop_ab_experiment.py) 已实现未变候选缓存、每 N 块周期全扫及 gas/fee/参数/拓扑变动自动回退全扫，并在 34 池 120 块固定快照 A/B 测试中达到 `recall = 1.0`、`exact_bit_for_bit_blocks = 120 / 120`、`p99_reduction_ratio = 52.99% >= 50.0%`（`hypothesis_a_passed = true`）。

**P2：长路径研究模式。**

- 增加 `k=5/6`（最多 8）的研究开关，默认关闭；用 RICH 式 color-coding 或 meet-in-the-middle，
  不用朴素 DFS 直接推高 k。
- 在小图上用穷举真值测召回；在合成图上测长环；所有结果标注“研究，不可执行”。
- 只有当长路径在 untouched 窗口上呈现出可重复的正净额，才讨论生产化。

**P3：GPU。**

- 先做 CPU 基线；只有当 CPU 的 p99 已经接近时限、且瓶颈确实在“路线 × 金额”批量评估时，才
  考虑 GPU。
- GPU 只做筛选；保留可证明上界；CPU 整数复算和固定状态模拟是签名前的硬门。
- 用“复核后每分钟真实正净候选数”对比，不用 tx/s。

**P4：展示与审计。**

- 控制台把“池跳 / DEX 跳 / 交易数 / 聚合器路由”分开显示；长路径候选标记“静态/未复核”。
- 所有长路径实验保留原始响应、哈希、块号和失败记录；不把研究数字混入生产 KPI。

## 8. 证据分级与未决问题

| 级别 | 内容 | 用途 |
| --- | --- | --- |
| A：仓库实测 | 53 µs / 2.37 ms；Wave 25 的 143.020414 USDC → 0.003910 USDC；489–1,082 ms 报价 | 基线、回归、容量 |
| B：同行评议/官方文档 | MeVisor、RICH、循环套利、CFMM 路由、Base Denim/费用、cuGraph | 机制、量级、边界 |
| C：预印本 | MMBF、line-graph routing、跨链 N-hops | 长路径存在性与算法参考 |
| D：本文推导 | 费用拖累、gas 估算、`q^n`、合成路线复算 | 设计取舍，不是实测 |
| E：未知 | Base 上 >5 跳的执行频率；200 ms 下单池变更概率；Oasis 状态上的 GPU 对照；包含概率与 builder 支付 | 需要新实验 |

未决问题：

- Base 上真正执行的 >5 跳原子套利有多少？需要 trace 级分类，而不是静态枚举。
- 200 ms 块下，单个活跃池在“报价到包含”窗口内的变更概率是多少？需要逐块事件和状态快照。
- GPU 在 Oasis 的 57 池状态上，能否在候选失效前提高复核后的召回？需要 A/B/C。
- 长 bundle（多交易）在 Base 上扣 gas 后是否还有净额？MeVisor 的结论偏负面，但需要本地证据。
- builder/priority fee 对薄边际长路径的实际侵蚀是多少？需要影子提交和回执，不是估算。

## 9. 参考文献

- [MeVisor: High-Throughput MEV Discovery in DEXs with GPU Parallelism](https://www.ndss-symposium.org/ndss-paper/mevisor-high-throughput-mev-discovery-in-dexs-with-gpu-parallelism/)（NDSS 2026；[PDF](https://www.ndss-symposium.org/wp-content/uploads/2026-f93-paper.pdf)）
- [RICH: Real-Time Identification of Negative Cycles for High-Efficiency Arbitrage](https://dl.acm.org/doi/10.14778/3749646.3749678)（PVLDB 18(11), 2025；[PDF](https://www.vldb.org/pvldb/vol18/p4081-luo.pdf)；[代码](https://github.com/Xtra-Computing/RICH)）
- [An Improved Algorithm to Identify More Arbitrage Opportunities on Decentralized Exchanges](https://arxiv.org/abs/2406.16573)（MMBF，2024）
- [Cyclic Arbitrage in Decentralized Exchange Markets](https://arxiv.org/abs/2105.02784)（2022）
- [Optimal Routing for Constant Function Market Makers](https://arxiv.org/abs/2204.05238)（Angeris et al.）
- [On the complexity of and algorithms for detecting k-length negative cost cycles](https://doi.org/10.1007/s10878-018-00371-0)（Guo & Li, 2021）
- [cuGraph SSSP](https://docs.nvidia.com/cugraph/26.08/api_docs/api/cugraph/cugraph.sssp/)（负权环不支持）
- [Base Denim](https://www.base.org/blog/denim)（200 ms 原生块、BaseTime）
- [Base network fees](https://docs.base.org/specifications/transactions/network-fees)（L1/L2 费用、最低 base fee、gas 参数）
- [To Wait or To Probe: Arbitrage Competition on High-Throughput Blockchains](https://arxiv.org/abs/2606.00720)（AFT 2026；Base 定向/概率搜索）
- [Bunny Hops and Blockchain Stops: Cross-Chain MEV Detection With N-Hops](https://arxiv.org/abs/2511.17527)（跨链 N 跳，机会稀少）
- [Uniswap v3-core](https://github.com/Uniswap/v3-core/blob/main/contracts/UniswapV3Pool.sol) · [Uniswap v4-core](https://github.com/Uniswap/v4-core/src/PoolManager.sol)（tick、hook、PoolManager 语义）
- Oasis 内部：[LONG_TERM_PROFIT_ROADMAP.md](./LONG_TERM_PROFIT_ROADMAP.md#L64-L86)、[graph_arb.rs](../../crates/oasis-core/src/graph_arb.rs#L386)、[ffi.rs](../../crates/oasis-core/src/ffi.rs)、[pool_graph.py](../../src/oasis_agent/fastpath/pool_graph.py)、[cycle_shadow.py](../../src/oasis_agent/arbitrage/cycle_shadow.py#L196-L233)、[Wave 25 记录](../verification/WAVE23_REVIEW_FIXES_AND_ANTITOXICITY_20261121.md#L78-L85)、[事件报价实验](../verification/EVENT_QUOTE_RESULTS_20261114.md#L21-L31)

## 附录 A：费用与 gas 的口径

- 费用拖累：`1 - (1 - f)^n`，表示 n 跳后输出相对输入的名义减少比例；盈亏平衡所需毛差为
  `1 / (1 - drag) - 1`。
- gas 估算：`100k + 200k × n`，是本文的保守占位模型，不是 Oasis 实测；3 跳的 700k 与 Wave 25
  的 3.5017e12 wei（约 700k gas）一致。实际值应按池类型、tick 跨度和执行器开销重新标定。
- ETH 价格使用 Wave 25 归因记录的 1,650.8908 USD；Base 最低 base fee 使用 0.005 gwei。两者
  都是时点值，不构成未来假设。
- `q^n` 是独立同分布假设下的路线不变概率；真实池的变更概率随时间、池和块变化，需用事件数据
  估计后再替换。

## 附录 B：合成路线的复算

```text
A = 1 + gross_edge
B = (sqrt(A) - 1) / x*
F(x) = A x / (1 + B x)
net = F(x*) - x* - 0.20
```

| 路线 | A | x* | 反推 B | F(x*) | net |
| --- | ---: | ---: | ---: | ---: | ---: |
| 浅池 | 1.0505 | 4.18 | 5.966e-3 | 4.28425 | −0.09575 |
| 深池 | 1.0108 | 181.39 | 2.969e-5 | 182.36688 | +0.77688 |

结论：原表的 −0.096 与 +0.781 在舍入范围内可复现；这不证明任何真实路线可赚，只证明该合成
模型的计算是自洽的。

---

本文不改变 `execution_eligible = false`、`release_permitted = false` 或任何资本/风险参数。
Wave 32–33 已完成 P0/P1 内核增强与 Arm A vs Arm B 固定块快照 100% 召回 + `p99` 下降验证；后续研究重点转向在真实 Base 逐块事件与流动性快照上量化各池实测变更率，再评估 `k=5/6` 研究模式或 GPU 候选试点。
