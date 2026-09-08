# 独立审计报告：Oasis Agent Phase 1 LP 系统（AI 独立审计员）

日期：2026-09-07（Asia/Shanghai）
审计员：Oasis Agent 内置独立审计 AI（只读审查，未参与本轮实现改动）
范围：合约层（`OasisDynamicFeeHook` / `OasisPositionExecutor` / `OasisHookFactory` / `IOasisPositionManager`）、
Python 层（`execution` / `shadow` / `reporting` / `risk` / `uniswap_v4` / `farming` / `models`）、
测试资产与门禁状态。
方法：只读静态审查；关键公式与常量与本地 pin 的规范 `v4-core` / `v4-periphery` 源码逐项交叉核对；
全部 high/medium 发现由主审计员亲自复核源码与行号。
对照标准：`docs/security/V4_HOOK_PRODUCTION_STANDARD.md`、`docs/security/CUSTODY_AND_AGENT_KEY_MODEL.md`、
`AGENTS.md` 工程标准。

## 结论先行

1. 实现质量总体高：hook 权限面最小、回调认证严格、动态费率语义与规范一致；Python 侧 fail-closed
   与证据哈希链机制扎实；`fees.py` / `pool_id.py` / `events.py` 与 pin 规范实现逐项一致（含复合费率
   公式与 PoolId 编码）；未发现任何隐式密钥、签名或广播路径。
2. 本报告是 **AI 独立审计**（机器复核），是证据而非保证；按
   `CUSTODY_AND_AGENT_KEY_MODEL.md` 与 `closure-acceptance-checklist`，它**不替代**第三方指定复核人
   签署，不放行任何生产部署、canary 或资金执行。
3. 门禁保持关闭：`execution_eligible` 恒 `False`、`lp_readiness` WP0–WP8 未授权。本报告为
   WP5/WP7/WP8 提供审计输入，但**不构成放行**。
4. 发现按严重度分级：High 0 项（已核验实现逻辑层面）；Medium 5 项（成本口径、证据门槛一致性、
   并发序列化、测试覆盖缺口）；Low 若干（文档化与边界校验）。对抗性测试覆盖（§8）仍是通往生产的
   主要未完成证据。

---

## 一、合约层审计矩阵

### 1.1 权限与回调合规（标准 §3）

| 要求 | 状态 | 证据 |
|---|---|---|
| 权限位只含已实现回调 | 通过 | `getHookPermissions()`：仅 `beforeInitialize` + `beforeAddLiquidity` + `beforeSwap`，全部 return-delta 关闭；`REQUIRED_HOOK_FLAGS` 与 `OasisHookFactory.REQUIRED_HOOK_FLAGS` 一致 |
| 回调认证 PoolManager | 通过 | `onlyPoolManager` 在 `beforeInitialize` / `beforeAddLiquidity` / `beforeSwap` 全部生效；`_checkPoolManager` 对非 PoolManager 调用 revert |
| 未实现回调直接调用失败 | 通过 | `afterInitialize` 等 6 个未启用回调显式 `revert HookNotImplemented()`，不静默返回 |
| CREATE2 地址低 14 位匹配权限掩码 | 通过 | `OasisHookFactory.deploy` 部署前校验 `actualFlags != REQUIRED_HOOK_FLAGS` 即 revert；部署后校验 `address(hook) != predicted`；`OasisDynamicFeeHookInvariant.t.sol` 覆盖 |
| 禁用回调不被依赖 | 通过 | 移除（remove）无回调：白名单撤销或暂停不阻塞合法 owner 退出（§3 要求） |

### 1.2 Permissioned-LP 合规（标准 §3）

| 要求 | 状态 | 证据 |
|---|---|---|
| 仅从 pin 的 PositionManager 读所有权 | 通过 | `beforeAddLiquidity` 强制 `sender == address(POSITION_MANAGER)`，`WrongPositionManager` 拒绝直接核心调用 |
| tokenId 只来自 PositionManager salt 约定 | 通过 | `params.salt → uint256(tokenId)`；`tokenId == 0` 拒绝（`InvalidPositionSalt`） |
| `ownerOf` + `getPoolAndPositionInfo` 交叉核对 | 通过 | PoolKey 与范围（packedLower/packedUpper）双向比对，`PositionPoolMismatch` / `PositionRangeMismatch` |
| 白名单只影响新增、不阻塞退出 | 通过 | 无 `beforeRemoveLiquidity` 回调；`OasisPositionManagerIntegration.t.sol` 验证撤销白名单后原 owner 仍可移除 |
| 守护者只能减权 | 通过 | `guardianRevoke` 只能移除白名单；`emergencyPause` 只能暂停；均不能加白名单、不能升上限 |

### 1.3 动态费率语义（标准 §4）

| 要求 | 状态 | 证据 |
|---|---|---|
| 动态费 sentinel 与 override 标志 | 通过 | `feePips | LPFeeLibrary.OVERRIDE_FEE_FLAG`；返回值 `ZERO_DELTA`；与 `LPFeeLibrary.sol`（pin）一致 |
| LP 费经济上限 | 通过 | `MAX_CONFIGURED_FEE = 100_000`（10%，≤ 规范 `MAX_LP_FEE = 1_000_000`）；`MAX_LP_FEE` 为协议经济上限而非 uint24 线容量 |
| 阈值按指定代币与模式显式区分 | 通过 | `exactInput = amountSpecified < 0`；`specifiedCurrency0 = zeroForOne == exactInput`；token0/token1 阈值分离，不做跨币种原始单位比较 |
| 确定性：同输入同费率 | 通过 | 纯函数，无外部调用/无存储扫描/无循环；`stateHash` 含 chainid/address/poolId/config/riskPaused |
| 报价与执行路径一致 | 通过 | `beforeSwap` 调用 `previewFee`；测试含真实 PoolManager 调度 + Swap 事件 fee 重建（`_assertNoFillSwapLog`） |
| 暂停回退 base fee | 通过 | `riskPaused ? baseFee : ...`；不变量测试覆盖暂停分支 |

### 1.4 管理 / 应急设计（标准 §7）

| 要求 | 状态 | 证据 |
|---|---|---|
| 无 upgradeable 代理、无任意调用 | 通过 | 无 proxy；hook/executor 均无可任意 target 的调用面；executor 仅执行显式 intents |
| 配置有界、版本单调 | 通过 | `configurePool`：base ≤ medium ≤ high ≤ MAX、阈值严格递增、`ruleVersion` 严格递增、绑定单池不可变 |
| 暂停不阻塞安全退出 | 通过 | 暂停仅冻结新增与高费率（回退 base），移除路径不受 hook 影响 |
| 守护者最小权限 | 通过 | 见 1.2；executor 侧 guardian 只能 `emergencyPause` |

### 1.5 有界原子执行器（`OasisPositionExecutor`，蓝图 4.3/6.5）

| 要求 | 状态 | 证据 |
|---|---|---|
| 只执行两种显式 owner 签名 intent | 通过 | `RECENTER`（烧旧铸新+盈余取回）与 `REMOVE`（全量取回）；`execute` 仅 `onlyOwner` |
| 无 swap 裸腿 | 通过 | `maxSwapInput != 0 → SwapLegUnsupported`；pin 的 PositionManager 的 `modifyLiquidities` 不支持 swap action |
| nonce 严格递增、deadline 到期保护 | 通过 | `nonce <= lastNonce → StaleNonce`；`block.timestamp > deadline → DeadlinePassed`；60 秒窗口免疫校验者时间操纵 |
| 池绑定不可变 | 通过 | 首次成功执行后 `boundPoolId` 不可变，`PoolBindingImmutable` |
| 净值恒非负 | 通过 | bundle 以 `TAKE(OPEN_DELTA)` 收尾；负净 delta 由 pin 的 PositionManager 整体 revert（fail-closed） |
| 无重入面、事件先于唯一状态变更调用 | 通过 | emit 先于 `modifyLiquidities`；pin 的 PositionManager 不回调 executor |

---

## 二、Python 层审计矩阵

### 2.1 无密钥 / 无广播 / fail-closed（标准 §2、宪章 5/6）

| 模块 | 审计结论 |
|---|---|
| `execution/lp_signer_policy.py` | 纯静态策略，无密钥无签名接口；解码 calldata 与 intent 全字段比对；simulation/preflight 证据要求 **≥2 提供者 + 单一解释**；`calldata_hash` 三处互相绑定；gas/fee/金额全上限 |
| `execution/lp_signer_sidecar.py` | 唯一签名边界；无 provider 即拒；只签自身构造的 EIP-1559 摘要；`value==0`、access-list 禁、r/s/y_parity 与 EIP-2 low-s 校验；签名必须恢复策略账户；journal 只存哈希 |
| `execution/lp_journal.py` | SQLite 哈希链账本；nonce 单调；intent_hash 唯一；`HALTED → CLOSED` 需双提供者 `HaltRecoveryProof` 且 `closes_exposure` 全真；transaction_hash 一经引入不可变；SIGNED/SUBMITTED 后禁止 EXPIRED |
| `shadow/producer.py` | 显式 `trust="single_provider_research"`；payload 序列化强制 `execution_eligible=False`（构造与属性双保险） |
| `shadow/lp_runner.py` | 决策链 `execution_eligible` 恒 False、`strategy_qualification` 恒 "unresolved"；健康与决策双闩锁；fee_quote 过期即 halt；异常只记固定原因 |
| `shadow/service.py` | 重启时以代码哈希（9 文件）+策略+min_benefit+canary 锁定 journal 身份；header/input 证据与 cycle 内容逐字节一致，否则 HALT |
| `reporting/lp_readiness.py` | 恰好 9 gate、键集合精确匹配、重复键拒绝；PASS 必须证据完整且无 findings；过期视为不通过；`execution_eligible` 仅 stage==BOUNDED_PRODUCTION |
| `evidence/pool_access.py` | 恰好 7 项 canonical 检查；非 UNKNOWN 必须有完整 provenance 且 **≥2 提供者**；`execution_eligible=True` 仅当全 PASS |
| `shadow/features.py` | `AgreedInteger` 要求 **≥2 来源 + 单一解释 + block 严格匹配**；未来块拒绝；价格上界 2^160 |

### 2.2 与 pin 规范实现的交叉核验（全通过）

| 模块 | 核验结论 |
|---|---|
| `uniswap_v4/fees.py` | `MAX_LP_FEE=1_000_000`、`DYNAMIC_FEE_FLAG=0x800000`、`OVERRIDE_FEE_FLAG=0x400000`、`REMOVE_OVERRIDE_MASK=0xBFFFFF`、`isDynamicFee` 用 `==`、`isValid` 用 `<=` —— 与 pin `LPFeeLibrary.sol` 逐项一致；复合费率 `pf+lf−pf·lf/1e6` 与 pin `ProtocolFeeLibrary.calculateSwapFee` 逐位一致；`MAX_PROTOCOL_FEE=1000` 与 pin 一致 |
| `uniswap_v4/pool_id.py` | `keccak256(abi.encode(PoolKey))` 5×32 字节布局（地址左填充 12、fee 无符号、tick_spacing 有符号）与 pin `PoolId.sol` 一致；tick_spacing 1..32767、int24 边界与 pin `TickMath`/`PoolManager` 一致 |
| `uniswap_v4/events.py` | 三个事件签名与 pin `PoolManager.sol` 一致（含 Swap 事件末字段为总费率）；符号扩展严格校验 |
| `farming/transaction_costs.py` | Nitro `gasUsedForL1 ≤ gasUsed` 防重复计数；blob gas 131072 倍数；OP 链上版本化 operator fee；全部 block pin |

### 2.3 经济口径核验

| 模块 | 审计结论 |
|---|---|
| `risk/lp_policy.py` | `cap == min(四上限)`；矩阵响应只减敞口；未知 trigger 抛异常而非静默放行 |
| `farming/strategy.py` | `min_fee ≤ base_fee ≤ max_fee ≤ MAX_LP_FEE`；band 上界严格递增、premium 单调非降且每项 ≤ term_cap；`worsening` 门控阻止取绝对值放行；clamp 保证 [min,max] |
| `farming/backtest.py` | 观测唯一事件序、dataset_hash 绑定、lock 块号先于全部观测、双 provider 证据一致、`unexplained_residual` 必须为 0、fold 数 ≥ 最低折叠、R11 各指标超限即拒绝 |
| `models/finance.py` | CostModel 六项成本全部非负；`AssetPair.__add__` 校验同 token 对与 decimals（不混加不同资产）；TokenAmount 基单位 int + decimals uint8 |

---

## 三、发现清单（严重度分级，行号已复核）

### Medium

| # | 位置 | 发现 | 影响与建议 |
|---|---|---|---|
| M1 | `farming/backtest.py:34-55`、`farming/counterfactual.py:88-98` | 回测/反事实净 PnL 成本口径缺 **opportunity_cost** 显式科目（`LpReplayObservation` 无该字段；`other_cost1` 虽保证非负但仅含其他显式成本；`net_pnl1 = fee+incentive+inventory − gas − rebalance − adverse − other`）。工程约定要求成本模型含非负 `failed_tx_cost` 与 `opportunity_cost` | R11 超额收益未扣除资金占用成本，样本外"全成本净收益"口径不完整。建议为 `LpReplayObservation`/`CounterfactualRun` 增加 `failed_tx_cost1` 与 `opportunity_cost1`（默认 0 亦可）并纳入 net_pnl1，同时更新实验锁声明 |
| M2 | `reporting/lp_readiness.py:76-85` | PASS 门禁证据只要求 provenance 完整（request/raw/interpretation 哈希），**不要求 ≥2 提供者**，与 `pool_access.py`/`features.py` 的双提供者门槛不一致 | 发布级 gate 可由单提供者证据支撑"通过"。建议 PASS 要求双提供者 + 单一解释，与池访问判定对齐；或显式声明发布 gate 的证据标准 |
| M3 | `execution/lp_signer_sidecar.py:393,316-351` | `account_nonce <= max_signed_nonce` 检查 → provider 签名 → `journal.append` 未包在单个 DB 事务内（append 自带短事务，但检查与签名在其外），多进程/多实例部署存在 TOCTOU：同一 nonce 可能被签名两次 | 单进程串行部署安全；若 HA/多实例，需在检查+签名+落账全程持 `BEGIN IMMEDIATE` 或复用 `lp_journal.reserve` 的竞态保护。建议在部署拓扑文档中显式约束为单实例，或在 `handle()` 加写锁 |
| M4 | 合约对抗测试（§8） | 重入、malformed/非空 hookData、恶意代币全类别（fee-on-transfer/rebasing/ERC777/黑名单/暂停/代理/异常 decimals）、flash/JIT/donation/同块排序、边界 tick、原生币路径、部分 tick 跨越、fee 精确边界（0/1 pip/100%/无效 flag 组合）均未覆盖 | 生产放行（WP5）前必须补齐的最小对抗集合；现有证据仅覆盖"返回 false/撒谎成功"两类代币行为 |
| M5 | 组合断链 | `OasisPositionExecutor.t.sol`/`OasisPositionManagerIntegration.t.sol` 使用 `MockPermit2Transfer`；走真实 Permit2 的 `OasisRealPermit2Lifecycle` 不经过 executor —— **executor × 真实 Permit2 端到端组合从未被测试**；fork 生命周期缺 fee change/pause/失败演练且不在默认 CI 门禁 | 增加 executor × 真实 Permit2 的 fork 生命周期测试并入门禁；补 fee 变更与暂停/退出演练 |

### Low

| # | 位置 | 发现 |
|---|---|---|
| L1 | `shadow/features.py:28-41` | `AgreedInteger.value` 未与证据 `content_hash`/`interpretation_hash` 重新绑定（信任边界在构造方）；建议与 `lp_signer_policy.py` 的显式内容哈希绑定对齐 |
| L2 | `shadow/service.py:182-192` | 代码指纹清单未含 `features.py`（`produce_pool_state` 是决策输入生成代码），其变更不触发 `lp_policy` 失效 |
| L3 | `shadow/producer.py:220` | `reference_sqrt_price_x96 or price_history[-1]` 的 `or` 回退会掩盖"参考价缺失"，`oracle_deviation` 归零；仅靠 research trust 标签缓解 |
| L4 | `shadow/producer.py:223-226` vs `features.py:117-118` | `swap_state` 用默认 28 位精度，`produce_pool_state` 用 `prec=80`；极端 token 下 inventory_skew 存在量化误差（研究路径，不影响执行） |
| L5 | `farming/strategy.py:226-230,118` | `_to_bps` 隐含 bps→fee pips 100 倍换算、`score*10_000` 分数刻度；内部一致但需文档化，防上游按"bps 数值"喂入差 100 倍 |
| L6 | `models/v4.py:23` | PoolKey 允许 fee ∈ (1_000_000, 0xFFFFFF] 非规范值（静态费应 ≤1e6、动态费恰为 0x800000）；pool_id 计算不受影响 |
| L7 | `uniswap_v4/events.py:71` | hooks 地址取低 20 字节未校验高 12 字节为 0（对比 indexed 地址有填充校验） |
| L8 | `risk/policy.py:18-34` | `min_worst_case_profit` 仅校验 finite 未要求非负（arbitrage 路径，非 LP 主路径） |
| L9 | `models/finance.py:52-59` | `LpPerformance` 无 `__post_init__` 一致性校验（net_pnl 可与收入/成本不一致） |
| L10 | `execution/lp_signer_sidecar.py:451` | `recover_signer` 异常未捕获，不落 denied 审计记录 |
| L11 | 合约测试 | 5 处无参数 `vm.expectRevert()`（`OasisLifecycleBase.sol:182`、`OasisRealPermit2Lifecycle.t.sol:103/117/128/141`）不验证 revert 原因 |
| L12 | `shadow/lp_runner.py:218-221`、`journal.py:228-229` | HALT 闩锁无显式重置路径（人工轮换 journal），需运维文档化 |
| L13 | `OasisPositionManagerIntegration.t.sol` | 仅 1 个测试函数；缺 later increase / NFT transfer / alternate recipient / direct-core calls / `InvalidPositionSalt` / `ForbiddenWhitelistAccount` / `ZeroAddress` / `NoStateChange` |

---

## 四、测试资产评估（标准 §8）

| 类别 | 评估 |
|---|---|
| 确定性测试 | 扎实：权限位/selector/OVERRIDE 标志、真实 PoolManager 调度 + Swap 事件 fee 重建、band 边界、双向精确输入/输出、owner/guardian 权限、暂停、ruleVersion 单调 |
| 不变量模糊 | 部分：ci profile runs=512/depth=128/fail_on_revert；但 handler 仅含治理操作，**缺 per-currency 守恒、回调认证、delta 滞留**三类标准不变量 |
| 固定分叉生命周期 | 部分：mint/双向 swap 4 模式/collect/revoke/unwind/per-token 对账已覆盖且 pin 严格（L1 号+L2 时间戳+三 codehash）；fork 版缺 fee change/pause/失败演练，且不在默认 CI 门禁 |
| 对抗性测试 | 主要缺口：见 M4 |
| 差分测试 | 缺失：PoolId/fee math 无独立实现对拍（本项目与 pin 逐项一致可视为部分补偿） |
| 弱化/跳过 | 未发现 skip/xfail/only；存在 5 处无参数 `vm.expectRevert()` 与 2 处 MockPermit2 显式声明弱化（L11/M5） |

Python 测试：`test_lp_durable_journal.py`、`test_shadow_lp_runner.py`、`test_lp_signer_policy.py` 覆盖最扎实
（并发双连接不能双占 nonce、HALT 跨重启锁存、链篡改检测、单源证据拒绝、伪造 execution_eligible 拒绝等）；
`test_live_shadow_producer.py` 覆盖 execution_eligible 构造拒绝 + payload 钉死 false + 篡改冻结字段不泄漏。

---

## 五、门禁复核结果（2026-09-07 全仓复跑）

| 门禁 | 结果 |
|---|---|
| `make python-check` | 通过：634 passed + 96 subtests；ruff/mypy/format 全净（本轮修复 signer sidecar 与 2 个脚本的 6 处 lint、10 处 mypy 类型错误，均回归验证） |
| `make security-check` | 通过：pip-audit 无已知漏洞；bandit Medium/High 0；secret gate 通过 |
| `make contract-check` | 通过：5 套件 40 passed，0 failed，0 skipped；forge fmt/build/lint 通过 |
| `make contract-security-check` | 通过：slither（80 contracts, 102 detectors）0 result |

门禁日志与审计报告已归档至 `reports/evidence/independent-audit-2026-09-07/`（MANIFEST.sha256 校验通过）。

## 六、未解决 / 门禁状态（保持关闭）

| 项 | 状态 | 说明 |
|---|---|---|
| `execution_eligible` | 恒 `False` | producer payload、runner 决策、readiness 评估三处独立强制，本轮审计复核确认无旁路 |
| `lp_readiness` WP0–WP8 | 未授权 | 全部 gate 无有效 PASS 证据；本报告仅提供审计输入 |
| 独立第三方复核 | 未委托 | 本 AI 审计为第一方机器复核；按宪章不替代指定复核人签署 |
| 对抗性测试战役 | 未完成 | M4/L11/L13 需补齐后才能支撑 WP4/WP5 |
| 净收益口径 | 待修正 | M1 需补 opportunity_cost 后再评估样本外超额收益 |
| canary / 生产托管 | 未开始 | WP6/WP7/WP8 均未到放行条件 |

## 七、审计员声明

- 本报告基于 2026-09-07 工作区源码与本地 pin 依赖（`contracts/node_modules/@uniswap/*`）静态审查，
  未运行广播、未接触密钥、未改动任何实现代码（审计报告、证据包与门禁修复除外）。
- 行号对应审查时点的工作区内容；后续任何代码变更使本报告部分失效时，应重新审计对应条目。
- 审计是证据，不是保证：通过项不代表未来无缺陷，未覆盖项不得推断为安全。
- 本 AI 审计为第一方机器复核，不替代第三方指定复核人签署；WP0–WP8 未授权，`execution_eligible` 恒 `False`。
