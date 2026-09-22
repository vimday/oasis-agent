# 当前实现与验证边界

更新：2026-09-16。本文是维护入口；历史报告只证明其记录的代码、配置和区块。
系统仍处于研究、观察和受控执行组件阶段，尚未证明持续盈利或生产运行资格。
项目所有者于 2026-09-15 取消外部独立安全审计要求；这不构成审计通过，也不替代自动化安全验证与操作发布批准。
本次复核结果见 [工程复核报告](../reports/engineering-review-20260915.md)。

## 功能地图

| 领域 | 已实现的工程能力 | 尚未成立的结论或待完成项 |
| --- | --- | --- |
| RPC 与证据 | 区块钉扎、原始信封留存、内容哈希与来源校验；SQLite 持久请求预算 | 两个来源标识不自动证明基础设施独立；生产可用性与长期数据完整性需持续验证 |
| 持续行情 | 有界 pool-state 观察；双来源 slot0/Swap 原始回放、成熟窗口、延后样本、哈希链接检查点恢复 | 不是无人值守的完整补数系统；检查点最多 1000 个窗口，恢复/追加重放成本随历史增长 |
| LP 特征与 shadow | 整数特征、费用带、范围/库存与再平衡决策；原子幂等消费、代码/配置锁定、断流 HALT | 尚未完成生产级上游行情、现金、风险、执行的持续闭环；决策不授权交易 |
| 头寸与现金 | 双来源原始头寸快照；owner 原生币/ERC-20 现金校验、按 token 归属、原生支付与 gas 核账；双来源原始头寸/完整事件/动作时 owner/现金窗口整合 | 不等于全账户 NAV 或目标池长期净收益核账；共享 token、第三方支付等歧义必须拒绝或显式解决 |
| 样本外研究 | R00/R01/R10/R11 walk-forward、purge、成本/路由弹性、回撤/尾损失；v2 逐折比较 hold 与各基线 | 仍缺生产目标未触碰样本外数据、置信区间与可部署盈利证据；fixture 不支持盈利声明 |
| LP 执行准备 | 有界 RECENTER/REMOVE calldata；双来源模拟、角色绑定 runtime pins；账户/nonce 日志与恢复证明 | 模拟和准备不授予签名或发布权限；目标部署、独立证据复核及完整资金约束仍是发布前置条件 |
| 签名组件 | 独立 calldata 策略、持久 nonce 预留；本地 Unix socket 摘要签名适配器、签名恢复验证 | 适配器不是密钥守护服务；默认 CLI 无生产 provider；仍需隔离部署、账户限额、运维与独立审查 |
| 抢开盘与 meme | 多链目标配置、token reconnaissance、v3 holders 风险诊断与原始双来源持仓采集/复核 | 不是盈利的多链自动 meme 机器人；历史 Base live monitor 仍是独立原始密钥路径，不能作为统一托管生产入口 |
| API 与前端 | 模块化观察控制台、整数保真、只读证据/决策/执行日志；操作鉴权、双人确认、受控 campaign、进程组停止和恢复 | 审批元数据不替代独立发布证据；API 保持执行门禁关闭；本次未重新进行完整手工浏览器验收 |
| Solidity | 确定性有界费用 hook、LP 加仓白名单、原子 RECENTER/REMOVE executor；官方 core/periphery/Permit2 本地生命周期与回滚测试 | 当前 hook 不启用 afterSwap；动态状态发布器设计尚未实现；完整恶意 token/重入/目标 fork 安全战役仍缺；外部独立安全审计已按用户要求豁免 |
| 多市场套利 | 架构、策略与风险规范及初步研究组件 | arbitrage 仍是脚手架，不是可执行跨市场套利系统 |

## 已有链上证据如何解释

Robinhood 测试网的 35 笔测试交易和主网钉扎 fork 的合成 token 生命周期分别证明当时的
测试生命周期与 runtime 兼容性；本次没有重新采集或广播。PONS/USDG 的失败报价与
hookless ETH/STOCKHOOKS 的成功直接报价只适用于各自记录的区块和 PoolId，不能证明
新 Oasis hook 池获得路由、成交量或净利润。具体标识和历史结果保留在
[AGENTS.md](../AGENTS.md) 及 `reports/evidence/`。

## 当前链与资金范围

用户报告 Base 有约 500 USD ETH、Robinhood 有几十 USD ETH；尚未实时核验。当前优先评估 Base，
总投入 500 USD 按跨链合计约束。新增共享资金预留组件已经通过跨链并发测试，但旧 Base
live monitor 尚未接入，不可将钱包余额或独立链配置视为额外预算。

## 当前发布阻塞项

1. 目标池真实路由、持续完整行情和现金/头寸核账，以及经过冻结实验的样本外净超额收益。
2. 完整目标安全验证、独立审查、持续 shadow 验收和可复现的故障退出演练。
3. 隔离 signer 服务、发布批准和独立监控。已确认专用钱包、总投入 500 USD、日损 200 USD、累计亏损 500 USD；限额解析和持久亏损停机记录已实现，尚未接入真实 NAV、signer 和广播闭环。
4. 将历史 Base live monitor 与统一账户、资金、证据、签名和核账边界整合后再考虑生产使用。

`.env` 有私钥不能证明它属于独立 canary，也不能替代上述边界。观察进程应只获得所需的
RPC 配置；签名服务独立持有密钥。默认 shadow runner 使用最小环境，campaign 仅显式
传入必要的 RPC 变量。该环境隔离不阻止同一操作系统用户读取文件，仍需独立系统权限隔离。

## 维护入口

- [生产准备和完整现金窗口](operations/PREPRODUCTION_COMPLETION.md)：本轮入口、输入格式、审计豁免与剩余条件。

- [贡献规范](../CONTRIBUTING.md)：锁定依赖、全量检查与安全门禁。
- [服务接口](architecture/SERVICE_INTEGRATION_INTERFACES.md)、[控制台读模型](architecture/CONSOLE_READ_MODEL.md)：API 与展示所有权。
- [行情管线](architecture/LP_MARKET_FEATURE_PIPELINE.md)、[RPC 使用](operations/ROBINHOOD_RPC_USAGE.md)：采集、预算、检查点和恢复。
- [执行准备](architecture/LP_EXECUTION_PREPARATION.md)、[签名传输](security/LOCAL_SIGNER_TRANSPORT.md)：受限接口与尚未实现的部署边界。
- [Phase 1 蓝图](strategies/ROBINHOOD_V4_AUTOLP_BLUEPRINT.md)、[托管模型](security/CUSTODY_AND_AGENT_KEY_MODEL.md)：权威目标与晋级条件。

`docs/superpowers/` 内尚存设计是未完成的目标规范，不能据标题或任务勾选认定已发布。
已被章程和蓝图替代的 2026-09-03 初始设计/脚手架计划已删除；原始证据、历史验证报告、
仍待实现的动态费用控制器和头寸核账计划保留。测试数量应以日期报告为准，不在多个入口重复维护。

## 2026-09-17 余额核实增量

已增加有预算的双源原生币余额采集、原始 RPC 离线重建和同区块整数 ETH/USD 参考估值。Base 与 Robinhood 的真实固定区块余额、Base 预言机参考已复核；详见[生产准备记录](operations/PREPRODUCTION_COMPLETION.md)。这不等于完整 NAV、持续行情或实盘放行。
