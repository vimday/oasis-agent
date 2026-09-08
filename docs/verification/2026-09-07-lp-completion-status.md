# LP 全面复核：继续修复与未完成项

2026-09-07。本记录接续真实测试网生命周期报告；不覆盖历史证据包，也不声明全面交付。

## 本轮已确认修复

- 执行日志重放复核 schema、规范载荷、原始 intent 的经济哈希/chain/config/nonce、证据哈希、交易身份和单账户未决动作约束。重新计算日志哈希也不能令语义无效的记录通过。整个数据库被重写仍需要外部锚点才能发现。
- POLICY_APPROVED / SIGNED 可 HALT；已签名交易身份和 nonce 预留继续保留，不能用超时或重启释放。HALTED 的恢复授权器仍未实现，保持关闭。
- 费率策略拒绝字符串布尔值、非枚举方向和 int256 越界金额，避免误选输入币种或错误应用折扣。
- 四格反事实比较要求共同持币基准。报表拒绝 NaN/Infinity/负金额及非 Decimal 金额，代币精度严格为 uint8 整数。
- 官方 core/periphery 与真实 Permit2 runtime 本地测试增加：暂停加撤权仍能完整退出；NFT 转移后原 owner 不能操作、新 owner 不能继承旧白名单但能 burn 退出；代币返回 false 或虚报转账成功时 mint 原子回滚。虚假代币行为通过 Foundry mockCall 注入，不声称完成完整恶意代币/重入活动。

以上 Python 缺陷均先运行失败回归，再修复通过。

## 本轮验证

- `make python-check`：562 passed，96 subtests；lint/format/mypy 均通过。
- `make contract-check`：40 passed，0 failed，0 skipped，5 suites。
- `make contract-security-check`：Slither 80 contracts / 102 detectors / 0 results。
- `make security-check`：通过；最终 Python 改动后重新运行 Bandit 与 secret gate。
- `coverage run --branch --source=src/oasis_agent -m pytest`：562 passed，96 subtests；总覆盖率 83%。仓库没有 pytest-cov 插件，改用已安装 coverage 工具执行完整测量。
- 原 Robinhood 测试网 35 笔收据再次离线复放通过；本轮没有新增链上签名或花费。该复放不是独立 RPC 或最终性证明。
- `git diff --check` 通过。保留既有依赖弃用与 Foundry 缓存/预处理警告；Solc 编译、测试、lint 的退出状态均为成功。

## 后续实现与真实分叉

- `farming/backtest.py` 已从空占位改为可执行的证据绑定四格重放：带 purge 的 walk-forward、
  R00/R01/R10/R11、线性 routing elasticity、逐项成本、最大回撤、expected shortfall、逐 fold
  R11 相对持币下界和预注册接受阈值。每个事件要求两个不同 provider 对同一解释达成一致。
- `scripts/run_lp_backtest.py` 提供严格 JSON CLI，拒绝重复键、未知字段和浮点值，输出仍明确
  `execution_eligible=false`。
- `execution/lp_signer_policy.py` 规范解码 Solidity executor calldata 并绑定 intent、双来源模拟、
  risk decision、账户、目标、nonce、代码哈希、资本、gas、区块和 deadline；无密钥和广播接口。
- 持久日志的 HALT 恢复只在最终区块、成功收据、零剩余流动性、逐币对账和撤销授权均有完整
  证据时释放 nonce；恢复交易哈希单独保留。
- `evidence/pool_access.py` 实现 `accessible / permissioned / unresolved` 三态准入；只有身份、初始化、
  hook、token、外部 add、owner remove 和双向 routing 七项全通过才允许 `accessible`。
- `reporting/lp_readiness.py` 实现 WP0–WP8 顺序门禁、证据到期和独立 reviewer 要求。
- Robinhood 主网状态固定分叉：chain 4663，block 56,905,986，hash
  `0xe597d4063f37931181e224716c3bfaf843b20fb265e2bef70bf869979d41397d`。真实链上
  PoolManager / PositionManager / Permit2 runtime 上的本地合成 token 生命周期 1/1 通过，
  `broadcast=false`，preflight/postflight pin 一致。证据位于
  `reports/evidence/lp-fork-robinhood-56905986-2026-09-07/`。

原始门禁日志位于 `reports/evidence/lp-continuation-2026-09-07/`。

## 全部 LP 的交付缺口（仍然有效）

| 蓝图 | 已存在能力 | 尚未达到的验收条件 |
|---|---|---|
| WP0 正确性 | ABI/整数/账户守恒与安全输入测试 | 所有路径的完整独立复核 |
| WP1 池准入 | 区块固定采集、部署身份、研究证据 | 选定经济池的双提供者准入、token authority、真实路由深度档案 |
| WP2 回放记账 | 池/仓位重建、逐币 owner cash、全成本模块 | 生产目标池历史回放与 fork 对账全闭环 |
| WP3 研究 | 可执行四格 walk-forward、弹性模型、预注册阈值与风险统计 | 生产目标池的完整输入数据、竞争路由模型、已锁定 untouched OOS 结果 |
| WP4 合约执行 | hook、原子 recenter/remove、Permit2/NFT 生命周期、无密钥 signer 静态策略 | 独立审计、隔离 signer sidecar/HSM、生产角色接线 |
| WP5 对抗验证 | 单元/fuzz/invariant、故障注入、Robinhood 主网状态固定生命周期 | 完整 malicious token/reentry/reorg/sequencer/keeper 并发 campaign 和独立 reviewer |
| WP6 实时 shadow | 证据绑定消费者、持久决策日志 | 连续市场/仓位特征与对账生产者、真实外部 routing、预定时长/SLO |
| WP7 canary | 隔离测试网 35 笔生命周期 | 独立 signer、最终性与恢复退出验证器、真实策略 canary 和损失预算闭环 |
| WP8 生产 | fail-closed 原则和模块 | 上述各门禁通过后的生产批准与逐级扩容 |

当前结论：LP 研究与受限测试能力继续增强，但全部 LP 功能尚未交付。测试网自有 token 的交易不能证明外部路由、策略净收益、生产资金安全或长期可靠性。缺口必须通过实现和证据关闭，不能改写为通过。
