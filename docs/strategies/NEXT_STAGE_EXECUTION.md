# 下一阶段持续执行清单

2026-11-14，起点 `237c98b`。用户要求持续推进全部既定规划；本清单用于逐项验收，
不把源码存在、合成通过、观察成功、经济有效和实盘发布混为同一种完成。
长期策略依赖仍遵循[长期路线](LONG_TERM_PROFIT_ROADMAP.md)。

| 交付 | 当前证据 | 完成验收与后续依赖 |
| --- | --- | --- |
| 通用只读链证据采集 | [正式入口](PUBLIC_RPC_OBSERVATION.md)已实现冻结请求、原预算预占、原始字节、首尾块头、失败停止、崩溃/并发拒绝、离线重放；四链有实际尝试 | 已支持 code/storage/call、收据及日志范围；[真实事件尝试](../verification/EVENT_COVERAGE_20261114.md)在锚点时间检查停止；独立源、真实前瞻窗口继续补齐 |
| 多链部署与数据源档案 | [Ethereum/Base 五个 Metric runtime 已观察](../verification/PUBLIC_RPC_AND_METRIC_IDENTITY_20261114.md)；原 Ethereum 范围补得[目标块五部署 runtime](../verification/METRIC_LOCAL_STATE_20261114.md)，累计 18/32；Robinhood/Elysium 保留失败 | 每链分别确认 proxy/config、数据源能力和独立性；未知 API 不推断不存在部署 |
| Metric provider / feed | 原块 immutable provider、pyth-lazer oracle、15 秒配置已观察；raw getter ReadDisabled 后停止；Titan 三次公开状态已[按源码布局解释 17 个 feed 槽](../verification/METRIC_BUILDER_FEEDS_20261114.md)，官方目录确认 ETH/USD 与 USDC/USD 编号 | 原块 price 数据、目标块配置、source shaping、canonical parent hash 与整笔模拟仍缺；4 秒旧是声明诊断，不得解释原 FeedStalled 或当成成交证明 |
| 同链精确模拟 | 双 venue 单跳组合、Elysium 块末调用回放；[完整付款探针](../verification/METRIC_ATOMIC_PROBE_20261114.md)、[调用/回滚解析](../verification/METRIC_PROBE_CODEC_20261114.md)和[自有 Anvil 合成状态执行](../verification/METRIC_LOCAL_STATE_20261114.md)通过 | caller/recipient/extension/价格界、完整 runtime、付款回调及本地前后状态已核对；真实目标 pool/provider/config、独立上下文、历史状态装载和 pinned 整笔两腿 replay 仍缺 |
| 主动事件触发与 shadow | Elysium 合成事件与定向报价；逐过滤器覆盖、空块/缺口与重组失效、原子批次及[持久离线报价队列](../verification/CYCLE_OUTBOX_20261114.md)已接入正常入口；现货冻结窗口接口 | 真实事件窗口、maker/oracle 触发；冻结窗口与等预算轮询对照 |
| Aave 历史借还款 | [收据位置与 V3 事件角色解释](../verification/AAVE_RECEIPTS_20261114.md)已实现；单源块头列出目标交易，receipt 为 null 后停止；循环及退出模型仍为合成 | 独立历史收据、代理身份、全部借还款与转移、债务指数、余额、外部资金和冻结时点；完整账本与残差 |
| Elysium 精确 prestate | 两笔收据与三个块头；块末无签名调用 | 交易前状态与区块内前序调用，完整重放、失败样本及成本，不能以块末成功替代 |
| 独立经济评估 | 合成成本/资金/压力逻辑；未证明持续净收益 | untouched 窗口、独立价格、净 NAV、基准、容量、尾损、失败与未覆盖样本 |
| LP 与现货共用控制 | Base 有界 LP、现货 shadow、独立 signer / 风控基础 | 逐个具体策略完成 NAV、双源精确模拟、结算/恢复证据；另行取得当次资本发布授权 |
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
maker/oracle 触发、真实窗口与同预算轮询仍待补齐。Aave、Elysium 和经济评估条目继续保留，
不能以队列工程或合成正报价替代完整债务账本、真实可执行利润与收益验收。
