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
| 同链闭环套利 | 固定块末报价、真实收据与块末整笔调用离线复核、V4 新池发现与事件触发的定向报价回放 | Elysium 三个块头和两笔收据跨端点一致，单源块末无签名调用成功。合成窗口验证了定向报价。赢家精确 prestate、真实前瞻采集和结算仍缺。见[验证与复现](verification/ELYSIUM_CYCLE_RESEARCH_20261111.md)。 |
| Metric 事件型套利研究 | 有界公开 metadata/depth 采集、哈希绑定的离线回放、整数深度与双池闭环筛选 | 单提供者估算，不是链上报价或执行准入。本轮真实两池六个候选因空深度全部拒绝；成本和资本保持未知。见[本轮验证](verification/METRIC_RESEARCH_20261114.md)和[下一阶段目标](strategies/METRIC_EVENT_RESEARCH.md)。 |
| 后续策略 | 对冲、做市、跨市场交易和组合分配的设计与部分通用组件 | 依照[长期路线](strategies/LONG_TERM_PROFIT_ROADMAP.md)逐项验证。当前实盘验收范围仅 Base。 |

## Base LP 验收记录

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

- [Base 发布和恢复边界](operations/LIVE_RELEASE.md)
- [Phase 1 实验锁](strategies/PHASE1_LP_EXPERIMENT_LOCK.md)和[生产准备记录](operations/PREPRODUCTION_COMPLETION.md)
- [RPC 与证据管线](architecture/LP_MARKET_FEATURE_PIPELINE.md)和[控制台读模型](architecture/CONSOLE_READ_MODEL.md)
- [历史证据报告](verification/)及本机 `reports/evidence/`。已提交的原始证据和失败记录应保留。
  历史实现索引可通过 `git show 5cea5f7:AGENTS.md` 查询。
