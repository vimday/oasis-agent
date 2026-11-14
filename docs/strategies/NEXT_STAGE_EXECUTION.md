# 下一阶段持续执行清单

2026-11-14，起点 `237c98b`。用户要求持续推进全部既定规划；本清单用于逐项验收，
不把源码存在、合成通过、观察成功、经济有效和实盘发布混为同一种完成。
长期策略依赖仍遵循[长期路线](LONG_TERM_PROFIT_ROADMAP.md)。

| 交付 | 当前证据 | 完成验收与后续依赖 |
| --- | --- | --- |
| 通用只读链证据采集 | [正式入口](PUBLIC_RPC_OBSERVATION.md)已实现冻结请求、原预算预占、原始字节、首尾块头、失败停止、崩溃/并发拒绝、离线重放；四链有实际尝试 | 当前支持 code/storage/call 及观察头；日志范围、独立源、前瞻持久游标继续补齐 |
| 多链部署与数据源档案 | [Ethereum/Base 五个 Metric runtime 已观察](../verification/PUBLIC_RPC_AND_METRIC_IDENTITY_20261114.md)；Robinhood/Elysium 保留失败 | 每链分别确认 proxy/config、数据源能力和独立性；未知 API 不推断不存在部署 |
| Metric provider / feed | 原块 immutable provider、pyth-lazer oracle、15 秒配置已观察；raw getter ReadDisabled 后停止；[Titan 三次公开状态](../verification/TITAN_STATE_20261114.md)覆盖目标池并观察到更新 | 原块 price 数据、source shaping、构块者状态与 canonical base 的关系及整笔模拟；不得用新时点声明解释原 FeedStalled |
| 同链精确模拟 | 双 venue 单跳组合、Elysium 块末调用回放 | 固定完整 caller/recipient/extension、付款与余额；整笔两腿模拟及状态变化，双源核验 |
| 主动事件触发与 shadow | Elysium 合成事件与定向报价；现货冻结窗口接口 | 链级持久游标、去重/断线/重组缺口、maker/oracle/LP/swap 触发；冻结窗口与等预算轮询对照 |
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
