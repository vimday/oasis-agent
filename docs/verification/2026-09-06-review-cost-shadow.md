# 交叉复核、全成本证据约束与 shadow 健康运行

状态：研究验证推进，**自动 LP 仍不具备上线验收条件**。本轮无提交、部署、签名、广播或资金使用。

## 复核另一位 AI 的工作

以 `9059f12026ace6c4532a4e4f19471dffc5d434a6` 为本轮续接基线，保留此前的依赖锁、双链 fork 和 CostModel 必填项。

发现并修复 RPC 回归：`58515f6` 把异常捕获收窄到 ValueError/TypeError，遗漏 OSError（包括 URLError、超时），网络异常因此不能按预算重试，并可能将带凭据的异常文本传到上层。四个回归用例先失败、修复后通过；未知写方法仍不重试。保留 HTTP(S) 白名单、禁止重定向与异常脱敏。

前一报告的“bandit 清零”只能解读为 **Medium/High 为零**，不能解读为所有严重级别无发现。本轮保留完整扫描指标，Low 仍存在。扫描结果不是独立审计。

## 实际收付款

已有 `owner_payments.py` 支持严格的单 NFT、直接 owner→PositionManager、ERC-20 increase/decrease/collect 路径。需要核心返回 delta、成功代币调用、收据 Transfer、完整块 Transfer、块前后余额和未变化的代币运行代码相互一致。

本轮补充整条 `replay_captured_position_datasets(..., payments=...)` 集成验证：按照修改事件发生时的 NFT 所有人归属付款，拒绝重复付款证据；事件后 NFT 转让不会改写原付款人。每种币单独对账，不将异币数量相加。

**边界未扩大成“所有人已结算”**：mint/burn、原生币、第三方付款/退款、多仓位净额、NFT 转移对价尚不支持。现有 Base 实链样本是多 NFT 交易，继续保持逐仓位 owner 金额 unresolved。单仓位成功用例仍是合成测试，不能充当真实账户结算证明。

## 全成本

新增 `farming/full_cost.py`，在 NAV/owner cashflow 会计结果上检查十类成本披露：gas、失败执行、再平衡、对冲、融资、机会成本、协议/hook、IL、LVR、库存风险。

- 不允许缺项静默归零；零或不适用也需要证据和理由。
- 必须统一计价 token、decimals 和整数基本单位；证据须在同一完整规范区块序列上，块哈希也必须匹配。
- 明确额外支出和已体现在 NAV/现金流中的费用。IL/LVR/协议扣款不能重复扣除。
- 拒绝重复成本类别、重复额外支出证据、旧 external_cost 聚合值与新明细混用。
- 缺项或 owner/估值未闭合时 `net_pnl=None`；即使研究核算完整，`execution_eligible=False`。

这补齐了**研究成本覆盖约束**，尚未实现成本收据与独立价格证据自动采集/验证。旧 `PositionAccountingReplay.net_pnl1` 仍是所提供成本假设下的会计值；报告全成本必须经过新增覆盖检查。不得将手工输入或证据引用标签当作价格正确性的独立证明。

## Fork

保留另一 AI 的 Robinhood 55780568 与 Base Sepolia 46455055 通过证据。本轮为运行器增加测试后的同高度头复核，保留 postflight 原始 RPC 字节。

L2 的 L1 编号、时间戳和代码哈希不是唯一状态证明：同一代码可以存在于多个块。前后同高度块哈希复核能检测这两次读取间可见的钉点变化，但不能替代 RPC 独立性或共识最终性。Foundry 在 Nitro 上的 block.number/blockhash 语义限制继续存在。

本轮 Base Sepolia 46460917 通过；Robinhood 55903961 首次因公共 RPC TLS 中断失败，同区块有界重试通过；两次成功运行的 postflight 块哈希均匹配。结果见 `reports/evidence/closure-2026-09-06/*fork-reviewed*/`，失败尝试保留，不以历史成功覆盖本次失败。所有 fork 创建的测试币与 hook 仅存在于本机测试 EVM，未证明已有目标池的准入或经济可行性。

## Shadow

新增 observation-only SQLite 哈希链日志和 `python -m scripts.run_shadow_health`：

- 唯一动作 HOLD/HALT，没有 signer、交易执行或广播依赖。
- 陈旧/未来数据、错误链、缺块/重组、时钟倒退导致 HALT。
- HALT 跨进程重启锁定；策略参数不能在原日志上悄悄改变。
- RPC 采集失败也写 HALT，不记录异常中的敏感文本。
- SQLite 事务串行追加；验证日志哈希链。此机制检测内容篡改，不能防御有文件权限者删库、截断后重写或整体替换；外部不可变锚点仍待实现。

实测：Base Sepolia 3 次 HOLD（其中重复观察同一块，不是 3 个不同区块）；Robinhood 初版采样跨越 15 块，HALT；改为补齐连续高度后 55903961、55903962、55903963 三块均 HOLD。短时健康观察**不是策略 shadow 验收**：没有长时观察窗口、策略信号/模拟执行、收益基准、损失限额演练和独立监控。

## 验证和独立安全包

本轮命令结果和源码/证据 SHA256 清单保存在 `reports/evidence/closure-2026-09-06/review/`。

- `make check`：Python 372 passed + 70 subtests；mypy 70 源文件；Solidity 18 项/组通过，无失败、无跳过；格式与 lint 通过。
- coverage：Python src 87%（4790 个语句，609 未覆盖）；不是安全证明或专门的新增代码覆盖门槛。
- `make security-check`：锁定 dev 依赖 pip-audit 未发现已知漏洞；Bandit Medium/High 为零。
- Slither 0.11.6，22 contracts、102 detectors、6 Informational，无 Low/Medium/High。新增 `make contract-security-check` 使用 `--fail-low`，保留 Informational 报告；CI 安装相同哈希锁中的工具执行此门禁。
- [detect-secrets 1.5.0](https://github.com/Yelp/detect-secrets) 使用 `--no-verify` 扫描当前源码、脚本、测试、配置和合约，8 个候选逐项记录分类（公开 git/SHA256/ABI、Base58 字符表、故意无效的测试值）。未扫描 Git 历史；临时环境工具尚未纳入项目锁和 CI，不能声称 CI secret gate 已闭合。

**独立安全验收未完成。** 此包是供外部复核的输入，本轮实现者的自查和独立工具不构成外部独立审计。复核人仍需针对清单绑定的源码、部署身份、恶意代币/回调、重入、权限撤销、数值边界、真实 Permit2、恢复/退出、计价与费用覆盖做独立复现和签署结论。

## 后续放行仍需的证据

1. 多仓位交易所有实际付款/收款人、每币净额与逐仓位归属无残差。
2. gas/L1 数据费/失败交易/对冲/融资等逐笔凭证、可靠价格与避免重复计算的成本账；样本外被动 LP 和静态费基准比较。
3. 固定策略、预先声明时长/风险门槛的持续 shadow，及 RPC quorum/finality、运行代码变化、限损/安全退出验证。
4. 外部独立安全复核，专用 secret CI gate 和发布批准；在此之前不启用资金执行。
