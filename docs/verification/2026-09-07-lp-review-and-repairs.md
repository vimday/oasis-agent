# LP 复核、修复与未完成项

日期：2026-09-07（Asia/Shanghai）。复核起点：`90e19d26bf0d1f933bfbdb5a5e3ee3792bb6259d`，
另有用户已存在的证据包 README/MANIFEST/日志修订及 `test_evidence_package.py`。
保留上述修改和 `.DS_Store`；本轮没有提交、部署、签名、广播、读取 `.env` 或使用资金。

**结论：不通过 LP 全功能交付或资金执行验收。** 本轮补充复核发现并修复实际缺陷；
本地门禁通过不能替代完整服务、固定区块重验、策略观察窗口、经济证据或独立发布批准。
这是对原实现的另一会话复核，以及本会话修复的自查记录；修复部分仍需另一复核人验收。

## 已修复的问题（confirmed）

| 严重度 | 最小触发场景与原后果 | 修复位置及不变式 |
|---|---|---|
| P1 | profitable RECENTER candidate，省略 gate_input，原实现仍输出 recenter | `shadow/lp_runner.py:247`：缺失门禁明确阻止动作，保留原提案供复核；只有明确 True 才选动作 |
| P1 | 时间 100 的区块搭配时间 1000 的池状态；或同时间不同链/块/哈希 | `models/finance.py:259`、`shadow/lp_runner.py:223`：PoolState 增加 block；shadow 强制完整身份与时间相同。旧正常用例原本混用时间，已改为一致快照 |
| P1 | 区块仍在健康时限内，但短期限 fee quote 已过期 | `shadow/lp_runner.py:230`：报价过期写 HALT，不能输出正常决策 |
| P1 | 策略评估失败写 halt 后，下一块或进程重启用健康输入继续 | `shadow/lp_runner.py:218`、`shadow/journal.py:228`：决策 HALT 跨重启锁存；日志写入也禁止解除 |
| P1 | 直接 record_decision 写入 execution_eligible=True、无健康记录、伪造引用或不一致区块 | `shadow/journal.py:166`：校验 schema、观察专用标记、健康记录、区块/时间/证据一致性；读取验证两条链及关联 |
| P1 | 已 SUBMITTED 再 HALT/EXPIRED，consumed 原本变 False；非法 submit 仍改写 transaction_hash | `execution/lp_state_machine.py:227`、`:263`：已提交事实从历史锁存；转移成功后才更新交易身份 |
| P1 | 字符串 "false" 通过 simulation/policy；畸形哈希、超过 uint256/LP fee/tick 边界的意图被接受 | `execution/lp_state_machine.py`：严格 bool、规范化哈希、数值宽度、v4 tick/spacing/fee 上界和跨 range spacing 一致性；本模型仍不是实盘授权器 |
| P1 | OP operator fee 非零或 Ethereum blob 交易时，原生币归属漏计实际 gas | `farming/native_payments.py:94`、`:116`、`:340`：OP 增加同块 operator oracle code/call 证据；Ethereum 计入 blob fee；校验类型、Nitro L1 gas、总额宽度及实际 gas payer |
| P2 | trace 重复出现同额 PositionManager→PoolManager native settle，原谓词仍接受 | `farming/native_payments.py:175`：第二个 settle 立即失败，符合“唯一物理归属”策略 |
| P2 | 低 Decimal context precision 下 normalize() 四舍五入日志金额 | `shadow/lp_runner.py:45`：直接按固定小数格式去除尾零，保留全部有效数字，负零规范化为零 |

原 shadow 测试中 `assert ... if False else True` 是恒真断言，已替换为逐条健康 digest 对照。
新增反例覆盖上述修复；初次新增核心反例为 **9 failed, 37 passed**，另有日志、费用、意图
反例的失败输出保存在证据包。Decimal 精度修复有回归测试，未单独保存修复前失败输出。

## 本轮命令与结果

| 命令 | 实际结果 |
|---|---|
| `make check` | exit 0；ruff/format/mypy 通过（80 源文件）；524 passed + 96 subtests；Foundry 4 套件 32 passed / 0 failed / 0 skipped |
| `make security-check` | 首次受限网络失败；联网重跑最终 exit 0，pip-audit 无已知漏洞，Bandit 无符合门槛问题，secret gate 通过 |
| `make contract-security-check` | exit 0；Slither 80 contracts / 102 detectors，0 results；本轮没有 Solidity 改动 |
| `.venv/bin/python -m coverage run --branch -m pytest` | exit 0；524 passed + 96 subtests |
| `.venv/bin/python -m coverage report --include='src/*'` | 合并语句/分支覆盖 84%；6317 statements / 753 missed，2582 branches / 647 partial；未设可供通过判定的仓库阈值 |
| `git diff --check` | exit 0 |
| `scripts/run_pinned_fork.py --network robinhood --block 55903961 ...` | **失败**；eth_getCode preflight 返回 `metadata is not found, 55903964`，未生成新的成功证据；后续公开 head 查询 curl exit 35 |

最后只调整状态机说明文字，随后 ruff check 和 format check 再次通过。
Foundry lint 输出含提示，不应描述为零提示；Slither 的零结果也不是不存在漏洞的证明。
旧证据包的清单测试通过，只证明当前文件覆盖与 SHA256 一致，不能认证作者、测试执行环境或经济结论。

证据包：`reports/evidence/lp-review-2026-09-07/`。历史包不被改写为新代码的测试结果。

## 尚未完成、不能以本轮修复替代的工作

| 门禁/功能 | confirmed / unresolved | 交付所需证据或代码 |
|---|---|---|
| 持续 LP 服务 | confirmed：LpShadowRunner 仅见于单元测试调用；`scripts/run_shadow_health.py` 是健康观察 | acquisition→reconciled state→fee/range→risk→journal 的持续运行适配器；独立来源、特征来源与 candidates/gate 的快照绑定；配置/代码版本锁定；重启与缺失周期恢复 |
| 执行与签名 | confirmed：LpExecutionLedger 是内存字典、按 intent_hash 索引；修改 intent_id/同 nonce 会换 hash；transition 接受调用者指令 | 持久化 account/chain/nonce 预留、幂等与恢复；独立证据授权；隔离 signer、额度/目标/selector/approval 限制；receipt/finality/余额/NFT 完整核对。不能将本状态模型当作签名策略 |
| 原生币与 batch | confirmed：新增成本/归属是合成证据单测；共享币种多仓位仍拒绝分配；旧 6 NFT 样本不能因此自动得到 owner cash | 有独立付款证据的实际生命周期验证；支持范围外明确拒绝。OP 旧 witness 缺少新增 operator 证据应重采，不能默认零 |
| 合约验证战役 | confirmed：本地基础/模糊/不变式测试通过；本轮未新增完整恶意代币、回调、重入或并发 fork 战役 | 按 V4_HOOK_PRODUCTION_STANDARD 全矩阵验证、独立复核及发布证据。执行器拒绝 swap leg，仅支持现有 REMOVE/RECENTER，不等于通用区间再平衡 |
| Robinhood 生命周期 | confirmed：存在旧 real Permit2 happy-path fork 记录；本轮重验失败 | 可用归档 RPC、独立提供者与 finality；保留 chain/block/hash/runtime/source/retrieval/evidence；不得把旧记录冒充本次成功 |
| 策略经济性 | confirmed：counterfactual.py 比较声明输入；backtest.py 仍是模块占位 | 真正执行的 R00/R01/R10/R11 回放、锁定试验与样本外窗口、路由竞争/弹性、净成本和尾部风险；不能由声明式比较器证明超额收益 |
| shadow / canary / production | unresolved：尚无预先声明窗口的持续策略验收与受限 canary | 所有上游门禁通过后依次运行；观察时长和经济样本不能用更多单元测试代替 |

哈希链依赖可信外部锚点发现整库重写/尾部删除；本轮校验关联不会将 SQLite 变成不可篡改存储。
PoolState.block 是输入身份约束，仍需真实特征采集证据，不能证明调用者提供的数值真实。
本轮未对 `.env` 是否曾泄露作出结论，也没有将该私钥导入工具或研究进程。

**签署结论：拒绝“LP 全部完成 / 生产就绪 / 可动用资金”的验收；接受本轮修复进入后续独立复核。**
