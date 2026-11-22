# 链上多跳套利技术调研

Updated 2026-11-22. 研究记录，不构成交易授权或收益承诺。本文中的 Oasis 数字均为只读、影子或
合成证据；`execution_eligible = false`、`release_permitted = false`。外部数字以论文原文为准，
推导数字在文中标注为“本文计算”。

## 0. 结论先行

1. **几十跳在静态图上存在，但已执行、扣费后为正的原子套利仍以短路径为主。** MeVisor 统计
   30,646 笔历史套利，82.21% 涉及少于 3 个 DEX；它允许最长 16 笔交易的搜索，实际发现 2–14 笔，
   其中 14 笔只有 3 个 bundle、平均利润约 USD 350。RICH 默认 `k = 5`，并引用既有工作指出加密
   市场超过 5 跳的环少于 2%。MMBF 在 Uniswap V2 静态图上找到的主模态是 7–11 跳，但该文明确
   不考虑 gas，且大量机会低于 USD 10。2026 年 BSC builder 的 963 万条 cycle 也显示 2–3 swap
   为主、10–20 跳只是小尾部，且更长路径没有系统性更高利润。
2. **“几十跳”至少混用了三种对象。** 聚合器的支付路由（A→N，可拆单，不是无风险套利）、多交易
   bundle（MeVisor 的 14 是交易数，不是简单环跳数）、静态图上的潜在长环（多数是粉尘）。不先
   区分口径，就会把“搜得到”误当成“执行得了”。
3. **长路径的瓶颈不是搜不到，而是扣完费用、gas、状态漂移和竞争后还剩多少。** 在 Base 上，
   30 bps × 20 跳的费用拖累约 5.83%，盈亏平衡需要约 6.19% 的毛差；32 跳的 L2 gas 约 6.5M，
   按最低 base fee 约 USD 0.054，在 USD 1,000 名义额上约 0.54 bps。gas 不是主要约束，费用拖累
   与状态新鲜度才是。
4. **Oasis 当前 Rust 内核上限 4 跳，34 池、1,868 条四跳环路 2.37 ms。** 真正的瓶颈是状态读取与
   远程报价：历史事件实验两腿报价 489–1,082 ms，加完整费用模型后 0/4 在 750 ms 内完成。
   Wave 32–33 已把按跳 gas、canonical 去重和 `changed_pools` 增量过滤下沉到 Rust，并在 120 块
   固定快照 A/B 上取得 120/120 召回与 p99 −52.99%；下一步是真实 Base 快照、路径库与 k=5/6
   研究，GPU 仍应排在最后。
5. **建议把长路径限定为研究模式。** 生产仍以 2–4 跳为主；k≤8 的按需枚举只用于研究、可视化和
   对照实验。A/B/C 的判定指标应是“从收到状态到完整复核的 p95/p99、时限内真实正净候选召回、
   漏掉候选的净额”，不是模拟吞吐。
6. **路径枚举与路径管理已经工程化，但“路径库存”不等于“执行跳数”。** 典型实现是 DFS + 最大
   跳数 + 池不复用；CPMM 路线可以用虚拟池参数逐跳合成，并得到闭式最优投入；路径库用
   `DashMap`/`DashSet` 做 pool→path 倒排索引和哈希去重。跨链套利则是另一条非原子路线：
   一年 242,535 笔、USD 868.64M 成交量，66.96% 用预置库存、中位 9 s，桥接中位 242 s。
7. **2026-11-22 链上抽样：Robinhood 的“几十跳”是实测存在的。** 20,000 个连续区块（38 min）内
   出现 4 笔 ≥20 Swap、2 笔 22 Swap；其中 21/20/19/15 Swap 的多笔在扣 gas 后仍有 +USD 1–2 的
   WETH/USDG 残差。Base 的 500 块（100 s）样本最大只有 4 Swap。**本轮抽样仍未出现“上百跳”；
   20–22 跳是本轮抽样中可复核的上限。**

## 1. 先把“跳”的口径定清楚

| 口径 | 定义 | 典型数量 | 经济含义 | 主要证据 |
| --- | --- | ---: | --- | --- |
| 池跳 | 一次池内 swap | 2–4；长尾可到 10 以上 | 一个池的状态、费用、价格冲击 | RICH、MeVisor |
| DEX 跳 | 跨一个 DEX 或协议 | 82.21% 少于 3 | 执行路径的协议数 | MeVisor 图 2 |
| 交易数 | 一个 bundle 内的 tx 数 | 2–14 | 排序、原子性、bundle 级 gas | MeVisor 表 IX |
| 聚合器路由 | A→N 的支付路径，可拆单 | 可达数十 | 最优执行，不是无风险套利 | CFMM routing |
| 路径库跳数 | 路径管理器存储的候选路径长度 | 可变，几十到上百 | 搜索/库存规模，不等于执行 | swap-path、2026 行业观察 |
| 跨链跳 | 桥或链切换 | 2–N | 非原子，带桥和链风险 | N-hops |

- MeVisor 的 14 是 bundle 内交易数，不是 14 个池的简单环；它的 path length 统计的是交易数。
- RICH 的 `k` 是环的边数，默认 5；它找的是限跳简单负环。
- MMBF 的 path length 是 token 图上的边数，包含 loop 和 non-loop；不计 gas。
- 聚合器的“几十跳”通常是 A→N 的支付路由，允许拆单和合流；把它叫作“套利”会混淆目标函数。
- “路径库跳数”可以远大于执行跳数：库中可以存上百跳的候选，执行时只挑净额为正的短子路径，
  或把长路径拆成多笔交易。2026-11 的“几十上百跳”更可能指这一层；若要证明单笔原子简单环
  达到上百跳，需要 trace 级证据。
- 协议层没有 4 跳硬上限。Uniswap V3 的 path 是变长 bytes：`20N + 3(N−1)` 字节（`N` 个 token、
  `N−1` 个费率），SwapRouter 逐段解码执行；真正的限制是 gas、calldata、tick 跨度和执行风险，
  不是编码格式。

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

### 2.7 跨链套利：库存 vs 桥（arXiv 2501.17335）

这篇论文（v2 标题 *Cross-Chain Arbitrage: The Next Frontier of MEV in Decentralized Finance*，
v1 为 *Pandora's Box*）研究的是**非原子**的跨链套利，不是单链原子环。它给出一个利润-成本
模型，并用 2023-09 至 2024-08、9 条链的数据做了实测。

模型（两链、CPMM + 完全流动的第二市场）：

- 机会按 Poisson 到达，到达率 `λ`；两市场相对价格 `p > 1`；token B 的相对漂移 `μ < 0`；
  桥接时间 `Δ`。
- 无摩擦时，最优卖出 `(√p − 1) R_A`，利润 `(√p − 1)^2 R_A`。
- 需要桥接时，最优卖出 `(√(p e^{μΔ/2}) − 1) R_A`，期望利润
  `(√(p e^{μΔ/2}) − 1)^2 R_A`；边际桥接成本
  `C_BR = (√p − 1)^2 − (√(p e^{μΔ/2}) − 1)^2`。
- 持有库存的成本（每单位交易）为 `−μ/λ`；库存量 `(1 − √(1/p)) R_B`。
- 定理：当且仅当 `(−μ/λ)(1 − √(1/p)) < C_BR` 时持有库存；等价地，`λ` 需高于阈值
  `−μ (1 − √(1/p)) / C_BR`。桥接时间阈值
  `Δ < −(2/μ) ln(√p / (1 + √M))`，
  `M = (√p − 1)^2 + (μ/λ)(1 − 1/√p)`。

方法与边界：作者从 Allium 提取 9 条链的 swap/aggregator 事件，处理 5.3 亿+ 笔交易，匹配
跨链交易对并用桥交易分类；**只识别两跳跨链环**（每条链一条腿），依赖边际差、时间窗和实体
匹配等严格启发式，因此结果是下界；新协议、未覆盖的桥、长尾 token 定价和库存风险都可能缺失。
它不是多跳路径研究，不能用它来支持“几十上百跳”的执行频率。

实测：

| 指标 | 数值 |
| --- | ---: |
| 已执行跨链套利 | 242,535 笔 |
| 成交量 | USD 868.64M |
| 研究期增长 | 5.5× |
| 库存执行占比 | 66.96% |
| 库存中位结算 | 9 s |
| 桥接中位结算 | 242 s |
| 前 5 地址交易占比 | >50% |
| 单地址最高日成交量占比 | 约 40%（Dencun 后，0xCA74） |

要点：

- 跨链套利的关键变量是**机会频率、桥接时间、token 贬值**，不是单纯的价差。机会足够频繁时，
  预置库存更便宜；桥接足够快时，桥接更便宜。
- 66.96% 的成交用预置库存、9 s 结算，说明“库存 + 快速结算”在跨链里占主导；桥接 242 s 的
  延迟成本很高。
- 模型忽略了费用、gas、桥费、失败和竞争；它是机制性结论，不是可直接套用的净额模型。
- 对 Oasis：Base 单链原子套利不需要桥，也不应把跨链库存风险混入当前 NAV/风险边界。若未来
  考虑跨链，必须单独建库存、桥、链风险和权威，不沿用单链原子环的准入。

### 2.8 2026 年 BSC builder MEV：短跳为主，长尾 10–20 跳

arXiv 2602.15395（2026-02）追踪 BSC 上两个主导 builder（48Club、Blockrazor）在 2025-04 至
2026-02 的套利活动。数据集包含 963 万条 builder 执行的套利 cycle、250+ factories、
23,000+ pools。

| 指标 | 数值 |
| --- | ---: |
| builder 执行的套利 cycle | 9.63M |
| 覆盖 pools | 23,000+ |
| 两大 builder 出块占比 | >87% |
| 两大 builder MEV 利润占比 | 约 90%+ |
| 稳定币 + wrapped 资产路由量占比 | 约 66.7% |
| 48Club 在双 builder 净利中占比 | 约 75% |

路径结构：

- 2–3 swap 路线占多数；48Club 几乎全部在 4–5 swap 内结束；Blockrazor 的尾部到 10–20 跳，但只占
  小部分流量。
- 路径长度与每 swap 利润的相关性接近零；更长路径没有系统性更高利润，Blockrazor 的长尾点
  大多贴近横轴。
- 3 秒块时间下，简单的三角/两跳稳定币路线占优；长多跳在延迟和 gas 上处于劣势。

这给了“几十跳”一个 2026 年的实测锚点：**10–20 跳的真实执行尾部存在，但利润集中在 2–3 swap；
公开数据里没有单笔上百跳的统计**。它和 MeVisor 的 2–14 笔 bundle、RICH 的 5 跳默认值一致，
也说明长路径更适合作为研究/尾部策略，而不是主策略。

### 2.9 2026 年多路径路由：k-shortest + 凸分配 + KKT 证书

arXiv 2607.22540（2026-04）给出一个可落地的多路径路由架构：

- 图：有向 token multigraph，弧携带 AMM 交换函数；pool-simple 约束在路径构造时显式维护。
- 候选生成：gas-aware marginal k-shortest loopless paths，边分数 `w_a = −log π_a + γ g_a`，把
  gas 直接嵌入图搜索；分数非负时用 Yen + Dijkstra，有负边际时用 Bellman–Ford。
- 分配：在 `k=20` 条候选路径上做 separable concave maximization（simplex + per-pool
  price-impact cap），KKT 给出 water-filling / 边际输出相等。
- 证书：用 KKT 乘子 `λ*` 作为阈值，一次最短路查询判断“是否存在被遗漏的 pool-simple 路径能
  改进当前解”；在多数 epoch 中证书确认候选集充分。
- 实测：对四个生产聚合器的 WETH–USDT 报价，六个 trade size；median shortfall <5 bps，top-3
  排名 >57% 的 epoch。

对 Oasis 的启示：这条路线的强项是“候选集 + 凸分配 + 可证明的遗漏检查”，适合 A→N 的支付路由
或未来的拆单/合流；它不是简单环套利的直接替代。`k=20` 是候选路径数，不是 20 跳。Oasis 当前
的原子环搜索可以先借用“gas-aware 候选排序”和“遗漏路径证书”的思想，但不必引入整套凸分配。

另一条 2025 年的工作用 STAP（standardized total arbitrage profit）把 DEX 效率写成凸优化问题；
在 11 token/18 pool 的 Uniswap V2 图上，它用 STAP 比较线图路由与 DFS 对效率的影响。这说明
凸优化不仅能做单笔路由，也能做全局效率基准；但它的图规模远小于生产 DEX，不能直接外推。

### 2.10 路由摩擦与可执行流动性（arXiv 2609.19013）

这篇 2026-07（2026-09 修订）的论文直接量化了“毛差 → 净额”的压缩。它从 29 个 Ethereum/Base
token-pair family、71 个 sibling pools 中抽取 13,768 笔 family-transactions，重建交易前状态和
逐笔路由成本。

| 指标 | 数值 |
| --- | ---: |
| 样本 | 13,768 family-transactions |
| token-pair families | 29 |
| sibling pools | 71 |
| 毛多 venue 收益下界 | 34.85% |
| 计 access cost 后上界 | 6.89% |
| 被 access cost 消除的正毛差状态 | 80.45% |
| 实际发生 multi-pool activation 的机会占比 | 1.203% |
| 170 条 eligible integrated routes 捕获的可行收益 | 94.8% |

要点：

- 毛差很大不代表可执行；access cost（gas、费用、跨 venue 激活成本）会消掉大部分正毛差状态。
- 真正的 multi-pool activation 只占机会人口的 1.203%；大多数机会只用一个池或根本不执行。
- Base 的低成本 regime 会实质性扩大可执行 integration；这对 Oasis 选择 Base 是支持性证据，
  但不等于长路径会变多。
- 方法边界：family-transactions、sibling pools、抽样；不是全量 trace census，不能外推到所有
  token/pool。

### 2.11 链上实测：Base 与 Robinhood Chain 的长跳分布（2026-11-22）

本节是只读链上抽样，不是全量 census。方法：对固定区块范围用 `eth_getLogs` 抓 V2/V3/V4 `Swap`
事件，按交易分组，统计每笔交易的 Swap 数和不同池数；对长样本再用 ERC-20 `Transfer` 计算
“同一地址既发出又收到同一 token、且净额为正”的候选，并用 receipt 的
`effectiveGasPrice × gasUsed` 扣 gas。没有做 trace 级完整 PnL，也没有签名/广播。

**Robinhood Chain（4663）**：20,000 个连续区块（2026-11-22 03:28:09–04:06:26 UTC，约 2,297 s），
30,158 条 Swap 日志、25,829 笔含 Swap 的交易。

| 单笔 Swap 数 | 交易数 | 占比 |
| ---: | ---: | ---: |
| ≥2 | 2,452 | 9.49% |
| ≥3 | 1,045 | 4.05% |
| ≥4 | 373 | 1.44% |
| ≥5 | 173 | 0.67% |
| ≥8 | 32 | 0.124% |
| ≥10 | 18 | 0.070% |
| ≥15 | 7 | 0.027% |
| ≥20 | 4 | 0.015% |
| ≥22 | 2 | 0.008% |

**Base（8453）**：500 个连续区块（2026-11-22 04:06:02–04:07:42 UTC，约 100 s），1,160 条 Swap
日志、1,034 笔含 Swap 的交易。

| 单笔 Swap 数 | 交易数 | 占比 |
| ---: | ---: | ---: |
| ≥2 | 103 | 9.96% |
| ≥3 | 21 | 2.03% |
| ≥4 | 2 | 0.19% |
| ≥5 | 0 | 0 |

注意：两者时间窗不同（38 min vs 100 s），不能直接比较频率；Base 需要更长窗口才能估计长跳尾部。

**长样本与候选净额**（WETH 按 USD 1,650.89、USDG 按 USD 1 计；仅为时点估算）：

| 交易 | 链 | Swap/池 | 同 token 正残差 | gas ETH | 候选净额（未含其它成本） |
| --- | --- | ---: | --- | ---: | --- |
| `0x57110d3c…` | Robinhood | 21/6 | WETH +0.000663、+0.000539；USDG +0.267966 | 0.00014449 | 约 +USD 2.01 |
| `0x1f1ed9d0…` | Robinhood | 20/4 | WETH +0.000584、+0.000340、+0.000221 | 0.00011484 | 约 +USD 1.70 |
| `0x16052cdc…` | Robinhood | 19/11 | WETH +0.000806 | 0.00006858 | 约 +USD 1.22 |
| `0x0d888e98…` | Robinhood | 15/4 | WETH +0.000367、+0.000220；USDG +0.365543 | 0.00012207 | 约 +USD 1.13 |
| `0x98a5143f…` | Robinhood | 22/4 | URANUS +4517.53；SPCX +0.001034 | 0.00008081 | 未定价 |
| `0xc7eb380b…` | Robinhood | 10/4 | WETH +0.000633 | 0.00017339 | 约 +USD 0.76（PONS 燃烧未计） |
| `0x1268e5ae…` | Base | 6/6 | USDC +0.005034 | — | 不是套利，是 ERC-4337 用户路由 |

要点：

- **“几十跳”在 Robinhood 上是真实的执行现象**：38 分钟窗口里有 4 笔 ≥20 Swap、2 笔 22 Swap；
  其中 21/20/19/15 Swap 的多笔在扣 gas 后仍有 +USD 1–2 的 WETH/USDG 残差。这是目前最强的
  2026 年链上证据，强于公开论文的 10–20 跳尾部。
- **但 20+ Swap 不等于 20+ 跳简单环**：最长样本里有股票代币（NVDA/GLD/SPCX）、长尾 token
  （URANUS/SCHIFFY）和 PONS 燃烧；有的是多资产路由/做市，不是干净的循环套利。
- **Base 的 100 秒样本最大 4 Swap**：Base 的长跳尾部在这段窗口里没有出现；需要更长窗口，
  不能据此说 Base 没有长跳。之前看到的 Base 6-Swap 交易是 ERC-4337 用户操作，不是套利。
- **候选净额很小**：+USD 1–2 的级别，符合竞争性 MEV 的薄边际；没有看到“几十上百跳带来
  大额利润”的证据。本轮抽样中上百跳仍未出现，20–22 跳是可复核的上限。
- **方法限制**：同一 token 正残差是启发式，未排除所有路由器/做市商；最终受益人、PONS 燃烧、
  股票代币定价和 L1 数据费未完成 trace 级核算。WETH/USDG 价格是时点假设，不是未来假设。

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
- 2.10 的实测进一步说明，access cost 会消掉 80.45% 的正毛差状态，真实 multi-pool activation
  只占 1.203%；这与本文的费用拖累模型方向一致，也说明“毛差排名”不能替代净额复核。

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
- 2.10 的 1.203% multi-pool activation 也提醒我们：绝大多数“机会”不会走到多池执行；能走到
  执行的少数，才是复核和竞争分析的对象。

### 3.7 路径枚举与路径管理

#### 3.7.1 有界 DFS

[ccyanxyz/uniswap-arbitrage-analysis](https://github.com/ccyanxyz/uniswap-arbitrage-analysis) 给出
了一个教学级实现：token 是节点，池是边；递归 DFS 从 `tokenIn` 走到 `tokenOut`，用 `maxHops`
限制深度；每层用 `pairsExcludingThisPair` 保证同一个池不被重复使用；当回到起点且路径长度
大于 2 时记录一个环；按利润保留 top-N。它还显式过滤掉一侧 reserve 小于 1 个 token 的池。

优点：实现简单，天然支持“简单环 + 池只用一次”，可以在 k≤4–6 时穷举；缺点：最坏复杂度是
`O(b^k)`，k=20 时不可用；它没有把 gas 计入，也没有处理 CL tick 和 V4 hook。该仓库自己也
注明“Didn't cover the gas fee”，所以它只能作为枚举与定价的教学基线，不能作为净额证据。

#### 3.7.2 虚拟池与闭式最优投入

同一个仓库给出了 CPMM 任意长度路径的合成方法。单池（reserve `R_in, R_out`，费率因子
`r = 1 - fee`）的输出是：

```text
y = R_out * r * x / (R_in + r * x)
```

把 A→B、B→C 两个池依次复合，可以得到一个虚拟的 A→C 池；继续迭代，就能把任意长度的
CPMM 路径合成为 `F(x) = A x / (1 + B x)`。于是最优投入可以直接用
`x* = (√A − 1) / B`，不需要对每条路径做几十步三分搜索。

这对长路径很重要：每条路径的定价从 `O(k × iterations)` 降到 `O(k)`，瓶颈回到路径数量本身。
但它只适用于固定费率、每池一次、CPMM 型路线；CL 池要逐 tick 处理，V4 hook 可能改变费率或
结算量，不能直接用闭式公式。对 CL/V4，仍要用整数三分/二分 + 固定状态模拟。

#### 3.7.3 路径数据库

[cakevm/swap-path](https://github.com/cakevm/swap-path) 是一个 Rust 的路径管理器（README 标注
“first draft”，不是生产库）。它的结构值得借鉴：

- `SwapPath` 保存 `tokens`、`pools`、`pools_map` 和 `swap_path_hash`；`push_swap_hop` 逐跳扩展；
  `len()` 返回跳数。
- `SwapPathsContainer` 用 `DashSet<SwapPathHash>` 做全局去重，用
  `DashMap<PoolId, HashMap<SwapPathHash, SwapPath>>` 做 **pool → paths 倒排索引**；
  `add`/`remove_pool`/`get_pool_paths_vec` 分别对应加入路径、删除池相关路径、按池查询。
- `TokenGraph::build_swap_paths(pool, max_hops)` 在新增池时枚举经过该池的路径；
  `find_all_paths` 是迭代 DFS（`VecDeque` + `PathState`），检查池不复用，限制 `max_hops`，
  并有 500,000 次迭代的 sanity cap；同时生成反向路径，特殊处理 WETH/native/wrapped token。

流程就是笔记里说的：新池加入 → 更新图 → DFS 查找可达路径 → 存入路径库；删除池 → 清掉所有
包含该池的路径。`DashMap`/`DashSet` 提供并发读写，pool→path 索引让“某池变了，哪些路径受
影响”变成一次查询，而不是全表扫描。

需要补的工程边界：

- `max_hops` 是 `u8`，但路径数随 k 指数增长；500k 迭代 cap 会报错，不会静默截断，这是对的，
  但生产上还要加 `max_paths`、最小流动性、token allowlist 和 per-pool 路径上限。
- 路径哈希要绑定 token 序列、pool 序列和方向；同一 token 对的不同池是平行边，不能只按
  token 对去重。
- 删除池要同时删除正向和反向路径；新增池要处理“新池让旧路径变短/变便宜”的情况，不能只
  追加新路径。
- 路径库的更新延迟、内存和路径数是长路径实验的核心指标，不是附带指标。

#### 3.7.4 Rust 侧的实现参考

[degenbot-pathfinding](https://docs.rs/degenbot-pathfinding/latest/degenbot_pathfinding/) 给出了
一个更接近生产形态的 Rust 设计：token/pool 用 `u64`，内部重映射为 `u32`；CSR 扁平邻接
（`adj_offsets`/`adj_flat`）；同一 token 对的平行池折叠为 bundle，用 per-bundle 计数器懒展开；
迭代 DFS 支持 min/max depth、per-depth pool-type filter 和基于 valid-depth 的 lookahead 剪枝。

对 Oasis 的直接启示：

- Rust 热路径用 CSR + `u32` 索引，避免 `HashMap<&str, usize>` 的字符串哈希开销；
- 池方向和池身份都要进入边，不能只存 token 对；
- 路径库用 pool→path 倒排索引，配合路径哈希去重；
- 全量重建和增量更新必须能对拍，增量漏路径要能被测试抓住；
- 线图路由在 100 token/200 pool 的 Uniswap V2 图上，Python 约 0.1 s、Rust 约 0.01 s；复杂度
  `O(|E|(Σd_i² − 2|E|))`，比 DFS 高，但在该规模可接受。

### 3.8 凸优化、排序与过滤

笔记里提到“按价格 > 状态匹配度 > 路径条件排序，或者叠加更多因素做凸面求解”。这三层可以
拆成：

1. **价格**：边际汇率乘积 / 毛差。便宜，但只看价格会选到“价差宽、深度浅”的路线；3.4 的
   合成例子已经说明，浅池的 5.05% 毛差扣固定成本后为负，深池的 1.08% 反而为正。
2. **状态匹配度**：池状态是否来自同一个块、是否在报价到包含窗口内保持不变。这应该是**硬
   过滤**，不是软排序；一条价格很好但状态过期的路径，净额期望可能为负。
3. **路径条件**：跳数、池类型、hook、gas、allowlist、准备金/转账税。这些是约束，不是打分项。

更稳的排序键是“净额上界”：

```text
upper_net = gross_upper
          - fee_cost
          - gas_upper
          - staleness_buffer
          - revert_buffer
```

先按 `upper_net` 排序，再对 top-K 做精确整数复算和固定状态模拟。这样既保留价格信息，也
不会把深度、gas 和状态风险漏掉。

**凸优化适合哪一层？** CFMM 路由在忽略固定成本时是凸优化；加入固定成本后是混合整数凸问题
（MICP）。它适合“给定 A→N、允许拆单/合流”的最优执行，也适合把多个池的流动性联合分配；
它不直接解决“简单环 + 每池一次 + 固定成本”的离散枚举。对 Oasis 的原子环搜索，先用图枚举
产生候选，再用闭式/三分做金额优化；如果未来支持拆单/合流，再把金额分配交给凸优化/MICP。
2.9 的 k-shortest + 凸分配 + KKT 证书是这一层的具体实现；`k=20` 是候选路径数，不是 20 跳。

2026 年的结构套利论文（arXiv 2608.20377）指出，多周期套利的收益可能来自“拆分-合并”：
单条腿不赚钱，组合起来才赚钱；论文同时指出，这类多样拓扑在数百个池合约的规模上司空见惯。
它提醒我们：**不要把“单环盈利”当作唯一形态**。但这类组合需要更复杂的模拟、排序和风险
归因，不能直接塞进当前的简单环内核。

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
| 聚合器支付路由 A→N | 可达数十 | 是（单笔内） | 最优执行，可拆单；凸优化/固定成本 MICP | 低；不是无风险套利 |
| 同链原子循环套利 | 2–4 | 是 | 短寿命、竞争强、净额薄 | 高；当前主战场 |
| 多交易 bundle | 2–14 tx | bundle 内原子 | 排序与打包；长 bundle 多数不覆盖 gas | 中；研究 |
| 静态图长环 | 7–11 | 不一定 | 大量 <USD 10 的潜在机会 | 低；研究/可视化 |
| 路径库候选（2026 行业口径） | 几十到上百 | 不适用（候选） | 搜索/库存规模；执行时再筛短子路径 | 中；路径管理 |
| 跨链 N 跳 | 2–N | 否 | 桥和链风险；机会稀少 | 低；超出原子范围 |

直接回答“几十跳”的问题：

- **作为静态图对象，存在。** MMBF 在 Uniswap V2 上找到 7–11 跳的主模态，最长到 30。
- **作为已执行原子套利，罕见。** MeVisor 的 82.21% 少于 3 个 DEX；RICH 引用超过 5 跳少于 2%。
- **作为可覆盖成本的机会，更罕见。** MeVisor 的长 swap bundle 96.5% 不覆盖 gas；静态枚举的
  大量路径低于 USD 10。
- **作为聚合器支付路由，常见。** 但它的目标是把 A 换成 N，不是闭环套利；不应与套利混谈。
- **2026 年 BSC 的实测**：9.63M 条 builder cycle，2–3 swap 为主；10–20 跳是尾部，且更长路径
  没有更高利润。这给“几十跳”提供了执行锚点，但仍未到“上百跳”。
- **2026-11-22 Robinhood 实测**：20,000 块/38 min 内 4 笔 ≥20 Swap（2 笔 22），多笔扣 gas 后
  +USD 1–2；最长样本混有股票代币和 PONS 燃烧，不全是简单环。Base 500 块/100 s 最大 4 Swap。
  这把“几十跳”从论文尾部推进到了可复核的链上执行样本，但仍未看到“上百跳”。
- **2026-11 的“几十上百跳”**：按你的行业观察，路径搜索/路径库已经到几十到上百跳。本文把它
  记为“路径库存/路由规模”，不是“单笔原子简单环执行跳数”。2026 年的结构套利论文也提到，
  多池/拆分-合并拓扑在数百个池合约的规模上常见；这支持“路径规模上百”，但不等于单环上百跳。
  公开证据的利润主体仍集中在短路径：MeVisor 最大搜索 16 笔交易、实际 2–14 笔；BSC 的 10–20
  跳只是尾部；RICH 默认 5 跳；MMBF 静态图最长 30 跳。若要把“上百跳执行”升级为事实，需要
  trace 级池跳计数和净额证据。
- **跨链是另一条轴**：arXiv 2501.17335 的 242,535 笔、USD 868.64M、66.96% 库存、9 s vs 242 s
  说明跨链套利是库存/桥接问题，不是单链原子环；N-Hops 论文还显示，跨链可行性随跳数增加
  呈幂律下降。Oasis 当前不应把它纳入原子执行范围。

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

### 6.7 路径库验收项

路径库不是“能存就行”，要能被对拍和审计：

- **加入池**：新增池后，枚举经过它的所有 ≤ `max_hops` 路径；路径集合与全量重建一致。
- **删除池**：删除池后，路径库中不再有任何包含该池的正向/反向路径。
- **按池查询**：`pool → paths` 的结果与全量扫描一致；同一 token 对的不同池作为平行边保留。
- **去重**：同一 token/pool 序列插入两次只保留一条；不同 pool（平行边）保留两条。
- **上限**：`max_hops`、`max_paths`、迭代上限、最小流动性、token allowlist 都要有显式错误；
  溢出时 fail closed，不静默截断。
- **增量 vs 全量**：固定块快照上，增量更新后的路径集合、每条路径的报价和最优金额与全量重建
  逐项一致。
- **性能**：加入/删除池的 p50/p95 延迟、路径数、内存、每块更新耗时；长路径实验必须报告这些
  数字，而不是只报告搜索内核时间。
- **候选集充分性**：如果引入 k-shortest/凸分配，记录 KKT/上界证书的覆盖率；证书未覆盖的遗漏
  路径要保留在日志里，不能当作“无机会”。

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
- 路径库：pool→path 倒排索引 + 路径哈希去重 + 添加/删除池 + 全量重建对拍；结构可参考
  `cakevm/swap-path` 的 `DashMap`/`DashSet`，但不要照搬其 first-draft 的 500k 上限。

**P2：长路径研究模式。**

- 增加 `k=5/6`（最多 8）的研究开关，默认关闭；用 RICH 式 color-coding 或 meet-in-the-middle，
  不用朴素 DFS 直接推高 k。
- 候选集生成可参考 gas-aware k-shortest（Yen + Bellman–Ford）与 KKT 遗漏证书；`k` 是候选
  路径数，不是跳数。
- 长路径枚举加 `max_paths`、最小流动性、token allowlist 三个闸门；溢出时报错，不静默截断。
- Robinhood 长跳 replay：对 20–22 Swap 候选做固定块 trace/PnL，核对最终受益人、PONS 燃烧、
  股票代币价格和 L1 费；只读，不执行。
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
| A：仓库实测 | 53 µs / 2.37 ms；Wave 25 的 143.020414 USDC → 0.003910 USDC；489–1,082 ms 报价；Wave 33 的 120/120 召回与 p99 −52.99% | 基线、回归、容量 |
| A2：链上抽样（2026-11-22） | Robinhood 20,000 块/2,297 s：25,829 笔含 Swap，≥20 Swap 4 笔、22 Swap 2 笔；候选 +USD 1–2。Base 500 块/100 s：1,034 笔，最大 4 Swap | 长跳存在性、尾部频率 |
| B：同行评议/官方文档 | MeVisor、RICH、循环套利、CFMM 路由、Base Denim/费用、cuGraph、Hermes（2025） | 机制、量级、边界 |
| C：预印本/工程参考 | MMBF、line-graph routing、跨链 N-hops、跨链套利（2501.17335）、BSC builder MEV（2602.15395）、多路径路由（2607.22540）、线图路由（2504.15809）、DEX 效率（2508.03217）、路由摩擦（2609.19013）、cakevm/swap-path（first draft）、ccyanxyz（教学实现）、degenbot-pathfinding 文档、2608.20377 | 长路径存在性、枚举与路径管理 |
| D：本文推导 | 费用拖累、gas 估算、`q^n`、合成路线复算 | 设计取舍，不是实测 |
| E：未知 | Base 上 >5 跳的执行频率；Base 上的 10–20 跳尾部；单笔 100+ 跳的执行证据；200 ms 下单池变更概率；Oasis 状态上的 GPU 对照；路径库规模/更新延迟；包含概率与 builder 支付 | 需要新实验 |

未决问题：

- Base 上真正执行的 >5 跳原子套利有多少？需要 trace 级分类，而不是静态枚举。
- 2026 年 BSC builder 的 10–20 跳尾部，在 Base 上是否存在？需要 Base trace 级统计。
- Robinhood 20–22 Swap 候选的最终受益人、PONS 燃烧和股票代币定价，需要 trace 级 replay。
- Base 更长窗口（≥1 天）的 10+ Swap 尾部是否存在？当前 100 s 样本未见。
- Base 上 multi-pool activation 的 1.203% 是否适用于 Oasis 的 57 池？需要本地 trace 级统计。
- 单笔 100+ 跳是否有公开 trace？目前没有；需要具体交易哈希和路径计数。
- 2026-11 的“几十上百跳”是路径库/路由规模，还是单笔执行的池跳数？需要 trace 级计数。
- 路径库在 Base 57 池、k=6/8 时的路径数、更新延迟和内存是多少？需要固定块快照基准。
- 200 ms 块下，单个活跃池在“报价到包含”窗口内的变更概率是多少？需要逐块事件和状态快照。
- GPU 在 Oasis 的 57 池状态上，能否在候选失效前提高复核后的召回？需要 A/B/C。
- 长 bundle（多交易）在 Base 上扣 gas 后是否还有净额？MeVisor 的结论偏负面，但需要本地证据。
- 跨链库存/桥接是否应进入 Oasis 的研究范围？当前不进入单链原子执行。
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
- [Cross-Chain Arbitrage: The Next Frontier of MEV in Decentralized Finance](https://arxiv.org/abs/2501.17335)（2025；v1 为 *Pandora's Box*；库存 vs 桥接模型与一年实测）
- [ccyanxyz/uniswap-arbitrage-analysis](https://github.com/ccyanxyz/uniswap-arbitrage-analysis)（DFS + 虚拟池 + 闭式最优投入，教学实现）
- [cakevm/swap-path](https://github.com/cakevm/swap-path)（Rust 路径管理器；`DashMap`/`DashSet`；first draft）
- [degenbot-pathfinding](https://docs.rs/degenbot-pathfinding/latest/degenbot_pathfinding/)（CSR + 迭代 DFS + 深度/池类型过滤）
- [Hermes: Scalable and Robust Structure-Aware Optimal Routing for Decentralized Exchanges](https://doi.org/10.1109/Blockchain67634.2025.00034)（IEEE Blockchain 2025；treewidth 路由）
- [If It Walks Like an Arbitrage: Protocol-Agnostic Detection with Decidable Structural Equivalence](https://arxiv.org/abs/2608.20377)（2026；多周期拆分-合并拓扑）
- [MEV in Binance Builder](https://arxiv.org/abs/2602.15395)（2026；v1 PDF 的 9.63M cycles、>87% 出块、10–20 跳尾部）
- [Multi-Path Routing in AMM Exchange Networks](https://arxiv.org/abs/2607.22540)（2026；k-shortest + 凸分配 + KKT 证书）
- [A Line Graph-Based Framework for Identifying Optimal Routing Paths in Decentralized Exchanges](https://arxiv.org/abs/2504.15809)（2025；线图路由复杂度与实测）
- [Measuring DEX Efficiency and The Effect of an Enhanced Routing Method](https://arxiv.org/abs/2508.03217)（2025；STAP 与凸优化）
- [Routing Frictions and Executable Liquidity in Fragmented Markets](https://arxiv.org/abs/2609.19013)（2026；毛差→净额压缩、Base 低成本 regime）
- [Uniswap v3-periphery Path.sol](https://github.com/Uniswap/v3-periphery/blob/main/contracts/libraries/Path.sol)（变长 path 编码）
- 笔记提到的 `advock/CFMM-Convex-Optimization` 在当前 GitHub API 返回 404；本文按 CFMM 凸优化
  文献（arXiv 2204.05238）讨论同一方法，不把它作为已读仓库证据。
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

## 附录 C：DFS 与虚拟池

单池 CPMM（费率因子 `r`）：

```text
y = R_out * r * x / (R_in + r * x)
```

两池复合后仍是 `A x / (1 + B x)` 的形式；逐跳迭代即可得到整条 CPMM 路径的 `A`、`B`。最优
投入 `x* = (√A − 1) / B`。这把每条路径的金额优化从 `O(k × iterations)` 降到 `O(k)`，但路径
数仍按 `O(b^k)` 增长；长路径的瓶颈是枚举，不是单条路径的定价。

有界 DFS 的伪代码（与 ccyanxyz 的实现同构）：

```text
dfs(path, used_pools, max_hops):
  if len(path) > max_hops: return
  for pool in pools_connected_to(path.last):
    if pool in used_pools: continue
    if reserve_too_small(pool): continue
    next = other_token(pool, path.last)
    if next == path.start and len(path) >= 2:
      record(path + [pool])
    elif next not in path.tokens:
      dfs(path + [pool], used_pools + [pool], max_hops)
```

关键不变量：池只用一次；简单环不重复 token；金额优化与路径枚举分离；枚举上限、路径数上限和
最小流动性是硬闸门。

## 附录 D：链上抽样复现

- Base：chain 8453；RPC `https://base.drpc.org`；区块 60,649,934–60,650,433（500 块，
  2026-11-22 04:06:02–04:07:42 UTC）；V2/V3/V4 Swap topics；5 块一组查询以避开公共 RPC
  结果上限。
- Robinhood：chain 4663；RPC `https://rpc.mainnet.chain.robinhood.com`；区块
  120,349,565–120,369,564（20,000 块，2026-11-22 03:28:09–04:06:26 UTC）；500 块一组查询。
- 长样本完整哈希：
  - `0x57110d3c1a28867544969054504e0abb875060908a6780dd29534faf0895d7e2`（21/6）
  - `0x1f1ed9d04f5b876afbfb24676b0164b03f7e8e98f453aa0f8a37e114fdba7ac1`（20/4）
  - `0x16052cdcaa4222019094abb68fbae637627f4372317acc4e3af1c953f46daa57`（19/11）
  - `0x0d888e98af87f59e09cfdac46181e2ed67c4bd1f0ed8742b0172b6093cb422b1`（15/4）
  - `0x98a5143fcf86defcb660b9548f8eea52011856a24526fa2d2564864cc604362a`（22/4）
  - `0xc7eb380bad9c243ac61317ede2e6295bfdd2a6d614acc8a23db14685b34065f7`（10/4）
  - `0x1268e5aea8b9001209b45ca78d53bf173666be76d4a77396c838fca72fc37934`（Base 6/6）
- Swap topics：V3/Slipstream `0xc42079f9…`；V2/Aerodrome `0xd78ad95f…`；V4 `0x40e9cecb…`；
  Transfer `0xddf252ad…`。
- 候选净额：同一地址在 `Transfer` 中既发出又收到同一 token，且 `received - sent > 0`；排除
  Swap 池地址、零地址和 `0xdead`；用 receipt `effectiveGasPrice × gasUsed` 扣 gas。未做 trace、
  未扣 L1 数据费、未验证最终受益人。
- 该抽样只覆盖两个固定窗口，不是全量 census；不能外推到其他时段或其他链。

---

本文不改变 `execution_eligible = false`、`release_permitted = false` 或任何资本/风险参数。
Wave 32–33 已完成 P0/P1 内核增强与 Arm A vs Arm B 固定块快照 100% 召回 + `p99` 下降验证；后续研究重点转向在真实 Base 逐块事件与流动性快照上量化各池实测变更率，核查 Base 的 10–20 跳尾部与 100+ 跳 trace，并在 Robinhood 上对 20–22 Swap 候选做 trace 级 replay，评估路径库（pool→path 倒排索引）与 `k=5/6` 长路径研究模式，再决定是否进入 GPU 候选试点。
