# 外部审计报告复核：oasis-agent 架构、策略与盈利能力

2026-11-15（Asia/Shanghai）；内部复核，无独立 reviewer sign-off。报告原文保存在
[EXTERNAL_OASIS_AUDIT_20261115.md](../references/EXTERNAL_OASIS_AUDIT_20261115.md)，
来源 `https://share.gemini.google/OsSbefXNQcvV`。本复核只把报告当作外部假设与风险清单，
不把它当作已核验证据、收益证明或执行授权。

## 0. 复核结论

报告最有价值的部分不是它的收益率数字，而是三个结构性判断：

1. 没有聚合器/路由自然流量的自定义 v4 动态费率池，在集中流动性杠杆下很可能负期望；
2. 无上游情报与发射台税率/锁仓解析的盲狙，在反狙击机制下很可能负期望；
3. 通用路由器双跳原子套利在小资金规模下，固定燃气与失败成本会吞掉大部分毛价差。

这三条与我们的路线图方向并不冲突，反而支持“先原子、后非原子；先证据、后资本”的排序。
但报告对**本仓库代码**的具体判断大多没有依据：它明确说无法读取私有工作区，因此其代码
层结论是外部推断。复核后确认：

- 报告说的 Base Denim `timestampMs` 是真实的；本仓库已加入严格校验。
- 报告怀疑的 Flashblocks 残留、`block.number * 2` 在本仓库代码中不存在。
- 本仓库的 spot book 已经拒绝 honeypot 和当前非零税；报告说“完全没有税过滤”不准确。
- 本仓库 Base LP 已按 owner 要求在 2026-11-14 暂停；报告说的“立即撤出 v4 LP 资金”
  在当前运行时已经成立。
- 报告给出的具体收益率、权重、发射台税率和 CEX-DEX 盈亏平衡数字，均是外部模型或
  第三方研究，不是我们的实测数据；不应直接写入资本配置。

## 1. 报告的方法与证据边界

报告在开头明确说明：团队上传的私有工作区附件在检索层不可读，因此它无法读取
oasis-agent 私有源码、内部函数名、实盘交易哈希与历史账单。它的分析基础是：

- 公开披露的 AlphaWink 双腿盘口深度回放与跨所资金费率时钟对齐规范；
- 姊妹系统 AlephTX 的公开工程底座；
- 截至 2026-11-15 的 Base 协议文档与 Uniswap v4 审计案例库；
- 公开链上数据与第三方学术/行业统计。

这意味着报告中的代码层判断只能作为假设；它的数字是模拟或第三方统计，不是我们的
回放、收据或账本。本复核采用的门槛是：只有可复现的测试、固定块链上证据或我们自己的
完整成本核算，才能改变代码、限额或策略权重。

### 1.1 外部来源交叉核对

本轮对三条关键外部事实做了公开来源核对：

| 报告主张 | 公开来源 | 核对结论 |
| --- | --- | --- |
| 0x 分析 84,163 个 v4 Hook，19.4% 安全、54.2% 恶意、26.4% 疑似恶意，部分成交比报价少 50% | [PANews](https://www.panewslab.com/zh/articles/01a0a282-9d0d-7255-baf1-45fcebcb598b)、[BlockBeats](https://m.theblockbeats.info/flash/367098)、[ChainCatcher](https://www.chaincatcher.com/tags/hook)（2026-09-15） | 多家媒体一致转述 0x 的统计；这是 0x 自报研究，0x 同时是聚合器利益相关方，不能当作独立审计 |
| Base Denim 于 2026-11-05/06 激活，200 ms 原生块，Flashblocks 退役，BaseTime 提供毫秒时间 | [PANews](https://www.panewslab.com/zh-hant/articles/01a1ad62-f652-7526-9821-e346170e8080)、[The Block 转述](https://cryptodiffer.com/feed/project-updates/base-cuts-block-times-to-200ms-with-denim-mainnet-upgrade)、[Zombit](https://zombit.info/base-denim-upgrade-200ms-blocks/)（2026-11-05/06） | 与我们在块 57,794,191 观察到的 `timestampMs` 一致；BaseTime 合约本身尚未读取 |
| Uniswap 官方路由在 2026-10-16 收紧 v4 Hook 白名单（代理/自定义 hookData 被排除） | 本轮检索未找到该条官方细则的原文；只找到 2026-10-22 Uniswap Aggregator Hooks 上线报道 | **未核实**。白名单政策方向可信，但具体条款、日期和范围不能引用为事实 |

报告引用的 arXiv 编号（如 2507.13023、2506.04940、2609.14481、2610.03922、
2606.21769）本轮没有逐篇核对；在取得原文与可复现实验前，全部按外部假设处理。

## 2. 逐项核对

### 2.1 Base Denim 与毫秒时间

**报告主张：** Base 于 2026-11-05 激活 Denim，原生区块 200 ms；EVM
`block.timestamp` 仍按整秒递增；精确毫秒通过区块头 `timestampMs` 或 BaseTime
`0x4200000000000000000000000000000000000030` 获取。

**核实结果：部分核实。** 本仓库用公共采集器观察到 Base 块 `57,794,191`：

- `timestamp` = 2026-11-15T13:26:53Z；
- `timestampMs` = 2026-11-15T13:26:53.800Z；
- 头部有 `timestampMs`，没有 `blockTimestampMs`；
- `timestampMs // 1000 == timestamp` 成立。

证据见 [BASE_DENIM_HEADER_20261115.md](BASE_DENIM_HEADER_20261115.md)。Denim 的
具体激活时间与 200 ms 出块来自报告/Base 文档，本轮没有独立复测出块间隔；BaseTime
预部署合约的实际返回值也尚未读取。结论是：头部毫秒字段真实存在，足以支撑证据管线
的严格校验；不能据此声称我们已经验证了全部 Denim 规范。

### 2.2 Flashblocks 残留与块号计时

**报告主张：** oasis-agent 可能仍残留 Flashblocks 监听、`newFlashblocks` 订阅、
`flashblock_index` 谓词，或用 `block.number` 推算秒数；Denim 后会导致数据断流、
除零或冷却窗口缩短 90%。

**核实结果：与本仓库现状不符。** `src/` 与 `scripts/` 中没有 Flashblocks 订阅调用；
`sniping/event_wakeup.py` 使用标准 `eth_subscribe("logs")`，断线后用 HTTP
`eth_getLogs` 回补；没有发现 `block.number * 2` 或按块号估算秒数的执行路径。
`sniping/config.py` 的 `poll_interval_ms=2000` 是 WebSocket 不可用时的回退轮询，
不是把 2 秒当成区块间隔；事件流可用时会被提前唤醒。`max_age_blocks` 当前未被使用，
实际块头鲜度由 `max_age_seconds=120` 约束。

这不意味着 2 秒轮询没有代价：在只有公开 HTTP RPC、没有 WebSocket 的场景，它确实会
落后于 200 ms 区块。但这是延迟限制，不是报告所说的 Denim 协议断裂。

### 2.3 聚合器白名单与自定义 v4 LP

**报告主张：** 未进入 Uniswap 官方路由白名单的自定义 v4 Hook 池失去散户聚合流量，
只剩 MEV 套利对手盘，主动 LP 因此负期望；报告给出的模拟净年化为 -325.2% 至 -452.7%。

**核实结果：外部假设，方向可信，数字未核实。** 我们无法从报告获得白名单快照，也没
有我们目标池的实际路由覆盖数据。本仓库已做两件事：

- Base LP 自 2026-11-14 起按 owner 要求暂停；
- `pool_access` 现在把 `router_whitelist` 列为必检项，缺少双源确认证据时池为
  `UNRESOLVED`、`execution_eligible=False`。

报告的 -325%/-452% 是它的蒙特卡洛参数组合，不是我们的回放结果；在取得我们自己的
路由覆盖、LVR、费率和调仓成本之前，不采用该数字。

### 2.4 集中流动性 LVR 与主动调仓

**报告主张：** ±5%/±2% 窄区间的资本杠杆与 LVR 放大到 40×/100×；高频调仓是负伽马
锁定；链上滞后波动率 Hook 在 CEX 跳变后的第一个 200 ms 仍看到平静期低方差。

**核实结果：外部模型，部分与我们的研究结论一致。** 我们的
[Phase 1 实验锁](../strategies/PHASE1_LP_EXPERIMENT_LOCK.md) 与
[ACTIVE_LP_PLAYBOOK](../strategies/ACTIVE_LP_PLAYBOOK.md) 本来就要求把 LVR、调仓
摩擦、逆向选择分开核算，不把手续费 APR 当净收益。报告提供的杠杆/LVR 公式可作为
我们模型的外部交叉检查，但其具体年化数字依赖波动率、跳跃频率和区间宽度假设，不能
直接引用。当前没有新增收益结论。

### 2.5 发射台反狙击税、锁仓与软跑路

**报告主张：** Clanker、Doppler/Bankr、Zora、Flaunch、Virtuals 等发射台在开盘
10–30 秒收取 80%–99% 衰减税、设置区块竞拍锁或 30 分钟禁售；标准化工厂合约让传统
字节码检测难以识别软跑路；盲狙每 $100 期望亏损 -$46.68 至 -$91.68。

**核实结果：外部协议事实，部分与本仓库现状相关。** 我们的
[spot_book.py](../../src/oasis_agent/launch/spot_book.py) 在解析报价时已经拒绝
`isHoneyPot is not False` 或当前 `taxRate != 0` 的代币，所以“完全没有税/蜜罐过滤”
的说法不准确。但它只检查报价时点的当前税，不检查：

- 衰减税率曲线与当前所处秒/毫秒槽位；
- 卖出是否被禁用、锁仓到期时间；
- 创作者在同笔交易内的 Dev Buy / 关联地址分流；
- 标准化工厂下的软跑路上下文。

因此报告指出的**结构性缺口是真实的**：缺少“发射台安全门”。报告的期望值数字仍是
外部模型，不直接采用；下一步应实现 fail-closed 的 launchpad safety gate，并把阈值
作为 owner 决策，而不是抄报告的数字。

### 2.6 通用路由器双跳原子套利

**报告主张：** 在 $200 Canary 规模下，通用路由器双跳原子套利的全成本盈亏平衡毛价差
约 48.64 bps；即使放大到 $1,000 仍需 17.73 bps；Base 200 ms PGA 下难以突围。

**核实结果：方向与我们的实测一致，具体数字未核实。** 我们的
[事件报价结果](EVENT_QUOTE_RESULTS_20261114.md) 已经记录了 Base 事件组 2/4 两腿在
750 ms 内完成、轮询 0/4、八条毛差全负；Elysium 三腿报价两组各 4/4、均超时限且毛差
全负。这说明“小规模通用路由双跳很难有正毛差”不是报告独有的判断。但 48.64 bps 是
报告的成本假设组合，不是我们的逐笔账本；我们仍以冻结报价、真实收据和失败成本为准。

### 2.7 Wintermute 基准与对手方

**报告主张：** `0x51c7...2a7f` 横跨 28 条链、约 1.21 亿美元 NAV、以太坊 748 亿美元
CEX-DEX 成交、2,430 万美元净留存，并在 Robinhood Chain 持有 PONS。

**核实结果：二手材料，部分观察支持。** 我们的
[Wintermute 对手方观察](WINTERMUTE_COUNTERPARTY_20261115.md) 在 latest 筛查中看到：

- vault runtime code 20,099 bytes，keccak `0x7cb680c6...`；
- PONS `balanceOf` 约 2,189,639.756 PONS（18 decimals）；
- USDG `balanceOf` 约 2,057,687.377 USDG（6 decimals）；
- PONS runtime keccak 与 benchmark 配置的 token0 pin 完全一致；
- 代码中存在 `09b0d50a`、`1626ba7e`、`1c02708d` 选择器。

但采集期间区块头推进了 10 块，不是同一块快照；授权集合没有读到；固定块读取被归档
provider 拒绝。因此“该地址持有 PONS/USDG、代码存在”有 latest 支持，NAV、成交量、
净留存和具体持仓金额仍未验证。

### 2.8 OasisVault 与热路径减重

**报告主张：** 建立 SurfNoTurf 式自有金库 `OasisVault.sol`，集成 Safe 多签、代币/
目标白名单、`killContract()`、ERC-1271、直连池回调与早期中止探测；把证据落盘和
跨进程签名移出热路径，单次互换 Gas 压到 13.5 万以内，探测压到 6.5 万以内。

**核实结果：设计提案，未实施。** 当前仓库已有 `OasisPositionExecutor`、
`OasisDynamicFeeHook`、独立签名 sidecar 与专用 canary 钱包，托管边界是“策略、风控、
签名、广播分离”。新建一个持有全部资产的金库会改变 custody 模型，必须单独做威胁
建模、权限矩阵、Foundry 不变量测试和独立评审。报告的 Gas 目标也没有在我们的真实
交易上复现过。不能因为报告建议就跳过现有 custody 约束。

### 2.9 CEX-DEX 领航与 ERC-1271 意图/RFQ

**报告主张：** 把主力资金转向非原子 CEX-DEX 领航（45%）与 UniswapX V3DutchOrder
Reactor / 0x BaseSettlerIntent / Bebop 意图结算（30%）；前者盈亏平衡 4.08–4.39 bps，
后者 0.54–0.69 bps。

**核实结果：研究方向成立，执行未授权。** 这两类策略都需要：

- 外部 CEX/Perp 实时盘口与统一时钟；
- 非原子对冲的库存、资金费、滑点与部分成交核算；
- 独立成交/仓位对账；
- ERC-1271 签名权限与最后审视（last look）政策。

我们的路线图把非原子/跨市场放在原子之后；当前没有外部场所数据契约，也没有
CEX 对冲账本，不能把报告的权重当成资本配置。报告中的 45%/30% 和收益目标不采纳；
任何权重都是 owner 决定。

### 2.10 热路径同步门控与 Canary 规模

**报告主张：** 同步 RPC 模拟、证据 fsync、跨进程风控与签名往返会错过 200 ms 区块；
$50–$200 的 Canary 会把固定 L1 数据费放大 5–25 倍；用总回滚率熔断会自我拒绝服务。

**核实结果：部分成立，但不改变当前资本边界。** 我们的 Base LP 是低频再平衡，不是
200 ms 抢单；Base canary 已暂停，spot 仍是 shadow；目前没有 200 ms 竞争窗口的实测
延迟数据。固定的 L1 数据费在小额下占比更高，这一点与我们的成本记录一致，但把
Canary 提高到 $500–$1,000 是 owner 决策，不能由报告直接改。回滚率熔断也应区分
“早期中止探测”和“深度执行回滚”，这是后续可以做的 telemetry 改进。

### 2.11 v4 Hook 九类安全缺陷

**报告主张：** 列出 V4-01 至 V4-09 九类缺陷，包括回调缺少 `onlyPoolManager`、
PoolKey 伪造、unlock 会话手续费劫持、Denim 时间碰撞、动态费率标志位遗漏、
`amountSpecified` 符号误用、闪电记账乱序、异步调费抢跑、代理/自定义 hookData
丧失路由资格。

**核实结果：多数已在本仓库的
[V4 Hook Production Standard](../security/V4_HOOK_PRODUCTION_STANDARD.md) 中有对应
条目。** 例如回调认证、权限位、PoolKey 绑定、动态费率标志、金额方向、粉尘舍入、
代理/升级限制都已写入标准。报告没有读取这份标准，所以它的清单更像通用审计清单，
不是对我们实现的逐行发现。我们仍应把“早期中止探测”和“回滚分类”补进执行设计。

## 3. 报告说对的部分

- **路由流量是 LP 经济的前提。** 没有自然流量的动态费率池，费率调节无法创造正现金流；
  这与我们的 PONS/USDG 经济研究门槛一致。
- **发射台反狙击机制改变了盲狙的期望。** 即使当前税过滤存在，衰减税、锁仓和
  创作者上下文仍是缺失的门。
- **小规模原子套利被固定成本支配。** 我们自己的事件报价结果已经支持这一点。
- **200 ms 出块改变了热路径预算。** 即使当前策略低频，未来做市/套利必须把证据落盘
  和签名往返移出关键路径。
- **Base Denim 的毫秒时间字段是真实的。** 我们已经把它纳入证据校验。

## 4. 报告说错、未证实或不适用于当前仓库的部分

- **“仍残留 Flashblocks 监听”**：当前代码没有；只有设计文档提到过。
- **“按 block.number 推算秒数”**：没有找到。
- **“所有互换都走 UniversalRouter”**：不准确。打新路径有 UniversalRouter 多跳，
  但 LP/研究路径也使用 PositionManager、直接池调用和专用执行器；不能一概而论。
- **“自定义 v4 LP 正在实盘”**：Base LP 已暂停；Robinhood LP 是研究基准。
- **“盲狙正在实盘”**：spot 是 shadow；legacy raw-key monitor 需要显式 `--live`，
  且被 AGENTS 明确排除在授权边界之外。
- **“OasisVault 是必须先做的修复”**：这是设计提案，不是已证实缺陷；当前 custody
  分离可能更适合 Phase 1。
- **具体收益率、权重、发射台税率、CEX-DEX 盈亏平衡数字**：外部模型/第三方统计，
  不是我们的账本；不直接采纳。

## 5. 对策略排序的影响

不改变路线图的资本晋级顺序，但增加三个准入条件：

1. **自定义 v4 LP**：必须同时有 router/aggregator 白名单、自然流量/路由覆盖、LVR 与
   调仓成本模型；缺少任一项保持 `UNRESOLVED`。
2. **发射台狙击**：必须有 launchpad safety 证据（当前税、卖出开关、反狙击/锁仓窗口、
   honeypot 模拟、创作者上下文）；缺少任一项拒绝买入。
3. **原子套利**：继续要求直连白名单池、固定块精确模拟、失败/构建者成本核算、双源
   收据；通用路由双跳不作为默认路径。

CEX-DEX 领航与 intent/RFQ 进入“研究待办”，不进入执行准入。报告建议的资金权重
（45%/30%）不采纳；任何配置都需要 owner 批准和独立对账证据。

## 6. 已落地的改动

| 改动 | 位置 | 说明 |
| --- | --- | --- |
| Base Denim 时间戳严格校验 | [base_denim.py](../../src/oasis_agent/evidence/base_denim.py) | `timestampMs` 规范、溢出、秒级/毫秒一致性；Denim 后缺失即拒绝 |
| 块头解析接入 | [cycle_evidence.py](../../src/oasis_agent/arbitrage/cycle_evidence.py#L205-L216) | 所有 Base 块头证据经过同一校验 |
| Router whitelist 必检 | [pool_access.py](../../src/oasis_agent/evidence/pool_access.py#L49-L60) | 缺少双源白名单证据时不可执行 |
| 审计响应路线图 | [LONG_TERM_PROFIT_ROADMAP.md](../strategies/LONG_TERM_PROFIT_ROADMAP.md#L274-L296) | 三类策略准入与 CEX-DEX/intent 研究边界 |
| Hook 标准 | [V4_HOOK_PRODUCTION_STANDARD.md](../security/V4_HOOK_PRODUCTION_STANDARD.md#L79-L81) | 动态费率 Hook 的路由白名单证据要求 |
| Denim 设计更新 | [BASE_LAUNCH_SNIPER_DESIGN.md](../strategies/BASE_LAUNCH_SNIPER_DESIGN.md#L35-L39) | 标准 logs + `timestampMs`，不依赖 Flashblocks 专用接口 |

验证结果：`make python-check` 通过（6232 passed + 138 subtests，ruff/format/mypy
317 files）；`bandit` 0 Medium/0 High；`pip-audit` 无已知漏洞；secret gate 与
`git diff --check` 通过。

## 7. 未落地及原因

- **launchpad safety gate**：需要新的数据契约与 owner 阈值；当前 spot book 只覆盖
  honeypot/当前税。建议作为下一个代码增量。
- **BaseTime 预部署读取**：需要固定块归档 provider 或单读取 latest 计划；当前只验证了
  区块头字段。
- **OasisVault**：改变 custody 模型，必须单独威胁建模、权限矩阵与 Foundry 审查。
- **CEX-DEX 领航 / ERC-1271 intent RFQ**：需要外部场所数据、对冲账本与独立对账，
  当前没有执行授权。

## 8. 下一步优先级

1. **P1：launchpad safety gate。** 定义严格的 `LaunchpadSafetyEvidence`：当前买入税、
   卖出税、卖出开关、反狙击/锁仓到期时间、honeypot 模拟、流动性、创作者上下文、
   观测块与双源；任一未知即拒绝。先接 shadow，不接 live。
2. **P2：BaseTime 预部署读取。** 在固定块或单读取 latest 下验证 `timestampMs()` 与
   `timestampMillisPart()`，补一条真实验证记录。
3. **P3：OasisVault 设计评审。** 只做威胁模型、权限矩阵、ERC-1271/直连回调/早期中止
   探测的设计与测试计划，不部署。
4. **P4：CEX-DEX/intent RFQ 研究准入。** 先定义外部数据契约、双腿账本、库存与资金费
   核算、独立对账，再讨论任何资本权重。

## 9. 限制

本复核基于公开报告、公开链路观察与仓库代码，不包含独立第三方审计、固定块完整
approval 集合、真实 CEX 盘口或我们的完整实盘账本。报告与复核都不构成收益承诺；
所有执行权限保持 false，Base LP 仍暂停，spot 仍为 shadow。
