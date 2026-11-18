# 下一阶段持续执行清单

2026-11-14，起点 `237c98b`。用户要求持续推进全部既定规划；本清单用于逐项验收，
不把源码存在、合成通过、观察成功、经济有效和实盘发布混为同一种完成。
长期策略依赖仍遵循[长期路线](LONG_TERM_PROFIT_ROADMAP.md)。

| 交付 | 当前证据 | 完成验收与后续依赖 |
| --- | --- | --- |
| 通用只读链证据采集 | [正式入口](PUBLIC_RPC_OBSERVATION.md)已实现冻结请求、原预算预占、原始字节、首尾块头、失败停止、崩溃/并发拒绝、离线重放；四链有实际尝试 | [Base/Elysium 固定未来日志窗口](../verification/PROSPECTIVE_EVENT_WINDOWS_20261114.md)完整回放；独立源及时间、Ethereum/Robinhood 前瞻验证继续补齐 |
| 多链部署与数据源档案 | [Ethereum/Base 五个 Metric runtime 已观察](../verification/PUBLIC_RPC_AND_METRIC_IDENTITY_20261114.md)；原 Ethereum 范围补得[目标块五部署 runtime](../verification/METRIC_LOCAL_STATE_20261114.md)，累计 18/32；Robinhood/Elysium 保留失败 | 每链分别确认 proxy/config、数据源能力和独立性；未知 API 不推断不存在部署 |
| Metric provider / feed | 原块配置、Titan 槽位、[真实父块代码与配置及覆盖回读](../verification/METRIC_TITAN_PAIRED_20261114.md)已核验。目标块两槽覆盖后 Pyth 年龄 4 秒，[调用树](../verification/ETHEREUM_AAVE_METRIC_TRACES_20261114.md)证实自定义报价源仍要求压缩 feed 同秒 | 旧块错误归因、其他池/源配置、完整 builder 环境和实际流继续验证 |
| 同链精确模拟 | 双 venue 单跳组合、Elysium 块末回放、[完整付款探针](../verification/METRIC_ATOMIC_PROBE_20261114.md)及本地合成验证；Ethereum 同父状态对照 0/8 完成，三槽补充实验 4/4 到第二腿且本金均不足。[六块前瞻先导](../verification/METRIC_STREAM_AAVE_WALLET_20261114.md)的 24 条固定规模路线亦全部本金不足 | Titan 确认块同秒落地 0/3，规模阶梯 18 条全负，此窗口拒绝，固定公开流路线挂起；新结构性来源单独预注册 |
| 主动事件触发与 shadow | Base 前瞻窗口取得 1 Swap，Elysium 路线真实空窗已进[持久离线报价队列](../verification/CYCLE_OUTBOX_20261114.md)；合成事件定向报价、重组失效与现货冻结窗口已验证；[双链 WebSocket 首收](../verification/METRIC_TITAN_BUILDERS_AAVE_CLOSE_20261114.md)取得真实非空事件及收据 | [90 秒双链实测](../verification/EVENT_QUOTE_RESULTS_20261114.md)量得 Base 事件组 2/4、轮询 0/4 两腿按时，固定路线毛差全负并关闭。Elysium 的 Aero 三腿实时报价均超时限且毛差全负，原范围收尾复核停止。真实断线及重组场景待核验 |
| Aave 历史借还款 | [借卖再抵押](../verification/AAVE_SALE_CYCLE_20261114.md)复核选定现金流及父块 oracle；[五笔优先核验](../verification/AAVE_FIVE_PRIORITY_TRACES_20261114.md)补齐收据；[调用树](../verification/ETHEREUM_AAVE_METRIC_TRACES_20261114.md)确认 422.0038 ETH 祖先回滚多计；[后续对账](../verification/METRIC_STREAM_AAVE_WALLET_20261114.md)校正剩余 87.1996 ETH，冻结候选收据 204/204 | 冻结案例结项，已识别补款和固定价格净资产报告留存；完整策略净收益未知。见[构块归因与案例结项](../verification/METRIC_TITAN_BUILDERS_AAVE_CLOSE_20261114.md)。 |
| Elysium 精确 prestate | [两个块的交易根与收据根](../verification/ELYSIUM_NITRO_COMMITMENTS_20261114.md)已重算，ArbOS `startBlock` 参数、完整前序和两个同块失败收据已核对 | 匿名历史证明在第一笔停止；系统交易和前序的读写状态、精确 prestate、整笔模拟与原生结算待验证 |
| 独立经济评估 | 合成成本/资金/压力逻辑；未证明持续净收益 | untouched 窗口、独立价格、净 NAV、基准、容量、尾损、失败与未覆盖样本 |
| LP 与现货共用控制 | Base 有界 LP 按 owner 要求暂停，保留原记录；现货 shadow、独立 signer / 风控基础 | 逐个具体策略完成 NAV、双源精确模拟、结算/恢复证据；另行取得当次资本发布授权 |
| 后续多市场与组合 | 长期路线中的 perp、做市、股权关系及分配提案 | 依赖前序真实分布与对冲/偿还控制；先补研究和模拟，不用提案替代实证 |

执行时更新本清单的事实和链接，不删除失败观察。完成一个工程交付后继续下一依赖；
每个可提交增量执行聚焦回归、全库与安全门禁、审阅、commit/push 和远端核对。
缺少真实证据的条目保持未完成，尚未授权的资金/发布动作仍须在可审阅提案齐备后办理。

## 当前固定实验：provider 诊断和采集入口

基线：上次 22 请求的原预算、原始失败与块 `26410155` 不修改，不重试原 quote。
这次诊断研究读取 provider/oracle 身份、只读配置和 feed 状态，与原报价采集分别记录。
先从保留的 getImmutables 定位 immutable provider，再使用独立冻结的只读请求计划。
仅选择已核实免费、无需认证的公开端点；请求上限在原 campaign 账本累计，首次异常
停止，进程崩溃留下运行标记并拒绝自动继续。没有付费 RPC 或资金操作。

反证包括：源代码与部署不对应、ABI/区块冲突、feed 状态不可得、caller 路径不同、
oracle 年龄/guard 无法解释回滚。不能放宽价格鲜度以得到正报价。采集器的合成验证
覆盖失败、截断、时间倒退、重组、重复运行与并发；真实观察只证明其具体读取范围。

## 当前事件管线增量与下一依赖

逐过滤器覆盖及原始日志范围已接入正常 shadow 回放，见[事件覆盖验证](../verification/EVENT_COVERAGE_20261114.md)。
Base 三块观察实验在锚点阶段因块时间比本机响应时钟晚 30 ms 而停止，未发出日志查询；
原 14/32 账本和失败不重试。后续真实采集先解决独立时钟与数据源证据，不能修改旧结果。
新观测以冻结规则选择水位之后的块。Base/Elysium 两个窗口完成单源覆盖，未放宽未来时间判定。
区块窗结束后的拉取延时已经量到，独立时钟与事件到报价的可执行时延仍需另行测量。

持久候选 outbox 已完成正常入口与合成验收：冻结配置/窗口、原子覆盖与候选、租约重投、
进程退出恢复、旧 worker 拒绝和重组失效均已验证。下一研究步骤回到 Metric 的已保留
构块者状态与 canonical base 的关系，以及精确两 venue 模拟；不重试已停止的原采集。
源码布局与目录解释已完成；目标 26410517 与 parent 26410516 的单源块头已关联，第二
来源传输失败后新原范围 6/96 STOP，未补读或模拟。整笔探针已通过合成支付/回调验证。
调用包与回滚结果已接上离线 CLI，四方向 Solidity 字节交叉一致；仍未取得真实模拟回包。
自有零账户 Anvil 的固定调用、完整状态核对和原始 IPC 回放已通过，属于合成 genesis，
拒绝历史声明。原五部署地址在目标块的 runtime 已补齐；pool/provider/config 并未补读。
原父子块头已重算哈希并绑定 `stateRoot`，账户/槽及 runtime 证明已接入采集和回放，
Anvil 合成证明通过。[公开尝试](../verification/METRIC_STATE_PROOFS_20261114.md)在匿名
父块读取 429（2/38）及新近 `safe` 块的证明窗口拒绝（5/21）后各自停止。下一项是
独立上下文、目标代码/配置、有历史 trie 的来源、完整读集装载及真实 pinned replay；
逐账户余额/nonce 含义和实际目标池调用权限仍未知，不能将本地探针用例标为完成实证。
后续新范围从 Alchemy 取得探针地址的真实排除证明，父子块头与原 PublicNode 观察一致。
同一父状态的公开节点对照已执行，价格层留下 `FeedStalled()`，0/8 两腿完成。原停止记录保留。
后续 Ethereum 调用树确认 Metric 两条 Pyth feed 通过后仍需要同秒的压缩报价。三槽覆盖后四种路线均到第二腿，本金皆不足。
五笔 Aave 原生调用树确认六组每组一次有效入账、一次祖先回滚，五块中该类差额已对平。
Base 实时 Swap 与同预算轮询已实测，关闭本次固定路线。Elysium 已实测同池三腿报价，收尾与故障场景待验，
maker/oracle 触发和经济评估继续待证。Aave 账本结项，策略净收益仍未知。

## 2026-11-18 全路线 CTO 推进与 P1–P4 工程闭环

在保留上述全部失败记录、停止账本与风控硬边界的前提下，Wave 1–Wave 4 已完成全既定工程规划并合并至 `main`：
见[自主机会引擎与四路并行升级核验](../verification/AUTONOMOUS_OPPORTUNITY_ENGINE_20261118.md)与
[CTO 全路线推进与 P1–P4 终验记录](../verification/CTO_FULL_ROADMAP_COMPLETION_20261118.md)。

1. **外部审计后续 P1–P4 工程交付**：
   - P1：`LaunchpadSafetyGate` 经 `make_launchpad_safety_evaluator` 接入 `SpotAgent._try_entry` 买前防线；
   - P2：`BaseTime`（`0x4200000000000000000000000000000000000030`）已在 Base 块 `58978574` 完成只读实测交叉核验（`header_timestamp_ms == predeploy_timestamp_ms == 1794986090400`、`timestampMillisPart() == 400`）；
   - P3：`OasisIntentVault.sol` 完成双平面托管、域隔离按 epoch 可撤销 `ERC-1271`、以及 `IReactorCallback` 原子结算与 `<65k` gas 早期中止；
   - P4：只读无密钥 `UniswapX` Base `Dutch_V3` 客户端、`HedgeGroup` Saga、`AlephTX` SHM 桥与 Stage 7 组合分配器接入统一机会雷达 `v2` 与只读控制台 `/api/v1/radar/*`。
2. **明确未声称项与下一实证门槛**：所有策略 `execution_eligible` 仍为 `false`；尚未证明 untouched 实盘窗口持续正净收益或触发任何实时交易授权。后续资本晋级仍须按[长期路线](LONG_TERM_PROFIT_ROADMAP.md)与[发布边界](../operations/LIVE_RELEASE.md)独立取得双源 pinned 模拟、24h 影子归因与当次发布审批。
