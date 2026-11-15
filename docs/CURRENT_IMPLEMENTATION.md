# 当前实现与验证边界

更新：2026-11-14。这里记录已实现能力、已验证结果和待解决问题。发布资格以具体代码、
配置、当前证据和授权为准。历史验收记录适用于其原始范围。

## 当前工作

| 路线 | 已实现 | 发布或研究边界 |
| --- | --- | --- |
| Base ETH/USDC v4 LP canary | 本机 Docker 一键有界入口，独立策略与签名服务，1–3 轮上限，完整配置范围内的 NAV、双源模拟、收据对账、双 STOP 恢复及只读仪表盘 | 已记录两轮真实持有验收。最近三轮尝试未启动。每次运行单独核定鲜度、费率、原预算、双账本和当次授权。 |
| Base 自主现货 | OKX 热榜、信号、风险与报价，GMGN 可选交叉检查，FOMO 提名接口，回调入场及止损、止盈、跟踪和时间退出的纸面账簿，输入回放及历史收据复核 | 影子模式。原 OKX 组单与广播路径已移除。FOMO 未接入已授权数据。独立 NAV 与风险、交易绑定、双源模拟和结算闭环仍是发布前置条件。 |
| Robinhood v4 LP | 固定区块头寸和报价验证、费用与库存特征、范围决策、现金归因、v4 合约生命周期与原子执行器 | Phase 1 经济研究基准。PONS/USDG 的外部 LP 权限、目标池真实路由与样本外净超额收益仍需证明。 |
| 证据、账本和控制台 | 区块绑定、原始 RPC 回放、SQLite 请求预算和持久风险状态、只读财务状态和受鉴权的观察任务 | 控制台无法授权交易。日志匹配、显示估值和链上资金对账分别呈现，缺失证据保持未知。 |
| 同链闭环套利 | 固定块末报价、真实收据与块末整笔调用离线复核、V4 新池发现与事件触发的定向报价回放 | Elysium 两块全部 50 笔交易与收据通过[根承诺核验](verification/ELYSIUM_NITRO_COMMITMENTS_20261114.md)，单源块末调用见[案例](verification/ELYSIUM_CYCLE_RESEARCH_20261111.md)。赢家精确 prestate、真实前瞻报价和结算仍缺。 |
| Metric 事件型套利研究 | 有界公开 metadata/depth 采集、哈希绑定的离线回放、整数深度与双池闭环筛选 | 单提供者估算，不是链上报价或执行准入。本轮真实两池六个候选因空深度全部拒绝；成本和资本保持未知。见[本轮验证](verification/METRIC_RESEARCH_20261114.md)和[下一阶段目标](strategies/METRIC_EVENT_RESEARCH.md)。 |
| 多链发现与借贷压力研究 | 按 chain ID 隔离的 Metric 目录；合成抵押/借款循环、HF、cap、深度、利息与先偿还再撤款核算 | Ethereum/Robinhood 目录有响应；Base/Elysium 公开 API 报未知 chain ID，各采集保留为失败。没有部署/收益证明；Binance 事件已纠错。见[后续验证](verification/MULTICHAIN_LENDING_20261114.md)。 |
| Metric / V3 跨 venue 报价 | 固定块、双起点与双顺序的离线报价组合；身份、实际输入、成本、资金和来源校验 | Ethereum 单源观察共 22 次免费读取，首笔 Metric quote 回滚后停止；选择器匹配 `FeedStalled()`，具体原因未证实。0/4 完整报价；无原子模拟或收益证明。见[报价验证](verification/METRIC_V3_QUOTES_20261114.md)。 |
| 公共链观察及 Metric oracle 诊断 | 四链固定来源、原预算、字节回放与 STOP；Base Denim 头部 `timestampMs` 严格校验；immutable provider/runtime/config 和 reference guards 的离线核验 | Ethereum/Base 五合约身份已观察，Robinhood 时间边界缺陷已修正并保留原失败，Elysium 固定哈希调用被拒。原块 15 秒配置已知，feed 仍缺；全为单运营商。见[验证记录](verification/PUBLIC_RPC_AND_METRIC_IDENTITY_20261114.md)与[Denim 观察](verification/BASE_DENIM_HEADER_20261115.md)。 |
| Titan 条件状态观察 | 复用有界公共采集，严格整数时间/序列、状态摘要与相邻快照比较 | 三次真实匿名读取覆盖目标 Metric oracle 和 ETH/USDC 池；属于构块者声明，未应用 override、未验证 canonical base 或可执行收益。见[首次观察](verification/TITAN_STATE_20261114.md)。 |
| Metric 构块 feed 解释 | 源码布局固定、目录字节绑定、17 槽解码、毫秒/秒转换与 feed 冲突检查；接入原离线命令 | 原三份快照的 ETH/USDC 声明在目标时刻均为 4 秒旧；仅声明诊断。源码执行代码排除 immutable/CBOR 后匹配，metadata 不同；未验证目标配置或两腿执行。见[feed 验证](verification/METRIC_BUILDER_FEEDS_20261114.md)。 |
| Metric 父块关联与整笔探针 | 原始 header/Titan 关联；始终回滚的两池完整 swap/回调/还款探针及合成余额验证 | 单源父子块已观察，第二来源传输失败后原范围 6/96 STOP；probe 本地 10 项测试、10,000 fuzz 通过，无真实 pinned fork 或可执行收益证明。见[探针验证](verification/METRIC_ATOMIC_PROBE_20261114.md)。 |
| Metric 探针调用与回滚解析 | 固定计划、完整 immutable runtime 重建、四项调用包、原始字节与 request hash/数量校验 | 四种路线与 Solidity 实际字节一致；67 项新增 Python 拒绝回归，RPC 包装和池仍为合成。无网络入口或真实历史状态证明。见[编解码验证](verification/METRIC_PROBE_CODEC_20261114.md)。 |
| Metric 自有本地节点执行 | 合成 genesis 全账户比对、空 probe 的单次受限 override、实际 IPC、前后 state dump 与子进程退出核对 | 四方向真实本地执行及负例通过，历史输入仍拒绝；另在原部署档案范围补得目标块五 runtime，原账本 18/32。见[本地状态验证](verification/METRIC_LOCAL_STATE_20261114.md)。 |
| Metric 历史状态证明 | Prague 块头哈希、账户/槽 MPT 路径与 runtime 核验，接入只读采集和离线回放 | 两个原块锚重算一致，Anvil 合成证明通过。早期两次公开尝试分别因 429 和证明窗口 STOP。新固定父块的空账户证明见下文。见[状态证明验证](verification/METRIC_STATE_PROOFS_20261114.md)。 |
| Metric 两腿有无 Titan 对照 | 固定真实父状态和目标块参数、探针地址的空账户证明、仅两槽 oracle 覆盖 | [真实模拟对照](verification/METRIC_TITAN_PAIRED_20261114.md) 0/8 完成；回读确认两 feed 从 42/48 秒变为 4 秒，池级两组仍报 `FeedStalled()`。[调用树验证](verification/ETHEREUM_AAVE_METRIC_TRACES_20261114.md)显示覆盖后卡在要求与区块同秒的压缩报价源。加第三槽后四种路径均到第二腿，返回均低于本金。 |
| Aave 历史事件与收据 | 历史索引候选、固定块/hash/交易位置、V3 角色解释、底层转账匹配及块末账户读数 | 62 笔实际收据，60 笔 Pool 事件都匹配底层转账；18 笔借款 11,701 WETH，12 笔还款；起止块末债务均为零。完整资金及收益继续对账。见[历史借还款](verification/AAVE_HISTORICAL_FLOWS_20261114.md)。 |
| Aave 缩放及钱包账本 | 固定指数逐步复核、余额/索引残差与收据转移匹配 | 62 步缩放轨迹期末残差为零，ETH 差额定位到四段。原钱包报告缺 131 笔候选的完整收据，最新缺口见下文。见[钱包对账](verification/AAVE_WALLET_LEDGER_20261114.md)。 |
| Aave Core 冻结与实际 oracle | 带交易位置的冻结收据、父块价格及轮次、两笔买回与解包收据 | USDC/WETH 在 26301183 同时冻结，买回与解包的 WETH 和原生余额差核对通过。净收益仍未知。见[冻结与价格验证](verification/AAVE_FREEZE_ORACLE_20261114.md)。 |
| Aave 借卖再抵押 | 固定交易 value、封闭的余额区段、费用与借贷收据 | 一次 500 ETH 卖出及同额 USDC 再抵押已复核，估值价差 19.11%，包含失败 gas。见[卖出回放](verification/AAVE_SALE_CYCLE_20261114.md)。 |
| Aave 五笔余额约束 | 五笔成功收据和十个固定块末余额、同源索引计数复核 | [五笔优先核验](verification/AAVE_FIVE_PRIORITY_TRACES_20261114.md)覆盖 422.0038 ETH 差额，逐块支持每组只计一次，[独立调用树](verification/ETHEREUM_AAVE_METRIC_TRACES_20261114.md)确认六组各有一条随祖先回滚，五块余额对平。该阶段剩 87.1996 ETH 差额和 126 笔收据。最新对账见下行。 |
| Ethereum 前瞻流与 Aave 钱包对账 | 公共 Titan 流首收、同秒槽与最终块证明、冻结候选收据回放 | [前瞻与对账](verification/METRIC_STREAM_AAVE_WALLET_20261114.md)六个预选 slot 有声明，最终区块同秒 0/6，24 条模拟本金不足。Aave 冻结候选收据 204/204，校正后原生差额为零，完整净 PnL 待归因。 |
| Titan 构块归因与 Aave 结项 | Relay 与官方公钥核对，固定规模双腿；可从仓库重放收据、余额与 trace | Titan 确认块 3，区块末同秒 0/3。18 条规模路线本金不足。Aave 原生余额对平，固定估值有数值，策略净 PnL 保留未知。见[构块归因与案例结项](verification/METRIC_TITAN_BUILDERS_AAVE_CLOSE_20261114.md)。 |
| 事件采集与逐过滤器覆盖 | 原始日志范围导入、持久空块/缺口、重组撤销、批次原子性；按所报高度及固定偏移选择未来块 | [Base 12 块](verification/PROSPECTIVE_EVENT_WINDOWS_20261114.md)三个过滤器观察到 1 Swap、0 Mint、0 Burn；Elysium 8 块六个过滤器为空，原 Base 14/32 停止记录保留。 |
| 持久事件 shadow 队列 | 冻结代码/窗口、覆盖与候选原子提交；租约重投、旧 worker 拒绝、正常 CLI 定向报价、重组同步失效 | [合成演练](verification/CYCLE_OUTBOX_20261114.md)覆盖定向报价和重组；Elysium 真实空窗已进入队列，覆盖完整、无报价任务。实时唤醒另见下一行。 |
| 实时 Swap 定向报价 | 冻结触发、费用模型、等预算轮询、原始证据与独立收据复核 | [双链实测](verification/EVENT_QUOTE_RESULTS_20261114.md)。Base 事件组 2/4 两腿在 750 ms 内，轮询 0/4，八条毛差全负，按门槛关闭固定路线。Elysium 改用匹配池的 Aero MetaQuoter，三腿报价两组各 4/4，均超时限且毛差全负，重复收尾块读取超时。 |
| 后续策略 | 对冲、做市、跨市场交易和组合分配的设计与部分通用组件 | 依照[长期路线](strategies/LONG_TERM_PROFIT_ROADMAP.md)逐项验证。当前实盘验收范围仅 Base。 |

## Base LP 验收记录

- 2026-11-14，owner 要求暂停新 Base LP canary。保留既有 STOP、服务记录和原费用账本。
- 2026-11-07，公开 USD 数据策略解决了原 USDC 喂价更新周期与 1,800 秒上限不兼容的问题。新策略使用 Kraken 与 Bitstamp 的公开成交，
  与签名预言机证明分别记录。
  早期一次持有启动完成后，Docker 对签名服务升级为 KILL，退出码为 137。该记录保留。
  见[价格策略及早期验收](verification/BASE_PUBLIC_USD_PRICING_20261107.md)。
- 随后的 `73c2f50` 一轮及 `81bcaeb` 两轮记录均显示策略、签名服务退出码 0，双 STOP
  恢复，历史收据复核通过。两轮记录是持有，新增成交结算为 0。其本地终验文件为
  `reports/review-20261107/base-81bcaeb-two-ticks-terminal-verification.json`，自哈希为
  `76669eb30b296a42998cd7aec760ec94a404663bce64d4413442d093001c1958`。
- `81bcaeb` 三轮请求首次因策略 RPC 子限额在离线阶段拒绝。`1bb8fe0` 调整了完整流程的
  预算校验，随后在线的无签名模拟仍因 dRPC HTTP 429 停止，未启动生产服务。后续
  `9288432` 将限流原因纳入有界入口和前端诊断。本地终验文件为
  `reports/review-20261107/base-rpc-budget-three-ticks-terminal-verification.json`，自哈希为
  `34117149744497db4a2b6d6c1e652818bdcd71e10354cc3b38438559d7d76152`。
- 较早的[付费只读复核](verification/LIVE_PAID_EVIDENCE_20261107.md)确认五笔原有收据和
  一次无签名全封装退出模拟。历史持有、收据和退出模拟支持各自的验收范围。新的成交后
  双账本结算和更长的自主运行仍需独立的真实验收。

当前操作流程、原账本限额和 STOP 事故处理统一见 [Base LP 运行手册](operations/BASE_BOUNDED_RUN.md)。
价格见证、RPC 账单与风险准入是三类不同证据。保守费用估计需与账单核验区分。

## Base 现货下一阶段

1. 用完整影子会话冻结输入、策略和对照。比较 OKX 信号与 GMGN 交叉验证的增量价值，
   记录不可用数据、净交易成本、样本外表现和尾部损失。FOMO 需先有明确接入权限和数据契约。
2. 构造仅允许的 Base 现货交易，固定路由、代码、账户、授权额度、最小到账和过期时间。
   用独立账户风控和同区块双源模拟验收同一份交易，再由独立签名服务执行。
3. 在原累计 RPC 预算下关联发送结果、双源收据、事件、余额和费用。结果不明时冻结新单，
   验证人工接管与恢复，再为单独授权的 canary 提交证据。

[现货运行与回放](operations/BASE_SPOT_SHADOW.md)记录命令、证据范围与拒绝语义。
影子研究运行可以在第一次提供者调用前把冻结计划绑定到采集会话，离线验收只按该计划的窗口
判断观察覆盖。目前只有合成样本，见[预注册验收记录](verification/SPOT_PREREGISTERED_ACCEPTANCE_20261110.md)；
真实影子采集、独立时间证明和可执行退出仍缺。
没有已验证的持续净盈利。合成测试、冻结文件回放和真实签名或广播属于不同验收层次。

## 证据导航

- [外部审计报告原文](../references/EXTERNAL_OASIS_AUDIT_20261115.md)与[逐项复核](verification/EXTERNAL_OASIS_AUDIT_REVIEW_20261115.md)
- [Base Denim 头部时间戳观察](verification/BASE_DENIM_HEADER_20261115.md)
- [Wintermute 对手方观察：归档读取被拒绝](verification/WINTERMUTE_COUNTERPARTY_20261115.md)
- [持续执行及未完成项](strategies/NEXT_STAGE_EXECUTION.md)与[公共只读采集](strategies/PUBLIC_RPC_OBSERVATION.md)
- [Base 发布和恢复边界](operations/LIVE_RELEASE.md)
- [Phase 1 实验锁](strategies/PHASE1_LP_EXPERIMENT_LOCK.md)和[生产准备记录](operations/PREPRODUCTION_COMPLETION.md)
- [RPC 与证据管线](architecture/LP_MARKET_FEATURE_PIPELINE.md)和[控制台读模型](architecture/CONSOLE_READ_MODEL.md)
- [历史证据报告](verification/)及本机 `reports/evidence/`。已提交的原始证据和失败记录应保留。
  历史实现索引可通过 `git show 5cea5f7:AGENTS.md` 查询。
