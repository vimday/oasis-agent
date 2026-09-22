# 生产准备：已完成工作与实际前置条件

更新：2026-09-15。项目所有者明确取消外部独立安全审计要求。这不是审计通过，也不要求
另行购买审计；仍需工程复核、可执行安全验证、操作发布批准和有效资金约束。
现有 `independent_reviewer` 字段表示工程/晋级复核人，不证明外部审计，不能填写虚构身份。

## 本轮已交付：完整现金窗口

`farming/cash_window.py` 将以下留存证据连接到同一个头寸账：

1. 两源原始 opening/closing 头寸快照，重新执行 wire 解码并验证完全一致。
2. 两源完整 PoolManager 和 ERC-721 事件区间，重建连续区块和头寸/所有权变化。
3. 每个 ModifyLiquidity 动作必须恰有一个双来源现金证据对，按事件顺序给出。
4. 由动作之前的 NFT Transfer 重建实际 owner，再重放其核心结算和 owner 现金证据。
5. 输出逐币种现金、原始出处、各 owner 独立汇总和重建后的头寸账；缺失、重复、不同来源
   集合、金额伪造或快照篡改拒绝。不同 owner 的现金不作为同一账户余额相加。

命令不请求 RPC，也不签名：

```sh
.venv/bin/python -m scripts.verify_cash_window \
  --request /absolute/path/cash-window.json \
  --evidence /absolute/path/evidence.db \
  --report /absolute/path/new-cash-window-report.json
```

请求字段精确为 `pool_manager`、`opening`、`closing`、`core`、`ownership`、`payments`。
前四类证据字段（opening/closing/core/ownership）各为两个完整 EvidenceRef；payments
是每个动作对应的 EvidenceRef 对列表。入口不接受调用者填写金额、事件 ledger 或 owner。
core/ownership 为 `canonical_block_range` v2；opening/closing 为 `position_snapshot` v1；
payments 为已有原生币或 ERC-20 owner-settlement 证据。所有部分必须使用同一来源集合。
报告写入新文件，拒绝覆盖旧输出。

`require_cash_window_successor` 在两个已经 raw replay 的结果间核对相同边界快照和
PoolManager/来源集合；不能跨缺口或重组拼接。它是纯边界检查，不负责采集、持久游标或
重新验证调用方自行构造的对象。该函数还不是无人值守持续账本服务。

输出 `net_pnl=null`、`execution_eligible=false`：现金结算尚不包括所有账户资产、LP 估值、
失败交易 gas、融资成本、token/runtime 风险证明或最终性。空动作窗口不代表全部成本为零。
已有 native/ERC-20 单事件 raw replay 都有测试；本轮整窗联调使用 ERC-20 合成 fixture，
未声称真实账户、native 整窗或长期实测完成。

## 发布证据一致性修复

`GateResult` 的 PASS 证据现在必须绑定同一条链、同一高度、同一区块哈希和同一时间戳；
缺失 block 或 timestamp 拒绝。五个失败回归覆盖缺失、跨高度、跨 hash、跨链和缺失时间戳。
这修复了引用绑定缺口，但评估器仍只检查引用结构；它不替代每种发布证据的 raw replay。

## 尚不能诚实标为完成的工作

| 必要结果 | 当前欠缺 | 完成依据 |
| --- | --- | --- |
| 目标市场适合部署 | 新 hook 池路由、费用弹性与持续完整数据 | 钉扎身份与可执行报价、真实路由和长期数据留存 |
| 可盈利研究通过 | 冻结实验和未触碰最终窗口的全成本净超额收益 | 实际目标数据、样本数/置信区间/回撤/尾损失和拒绝条件 |
| 账户现金及 NAV 闭环 | 实际账户/NFT、连续状态、全部交易成本及独立估值 | 原始回放、逐 token 核账和无法解释残差为零 |
| canary 资金边界 | 参数已齐全：总额 500 / 日损 200 / 累计损失 500 USD；尚待真实估值接入 | 独立重建全部资产、成本及 USD 价值并接入执行阻断 |
| 托管及实盘执行 | 独立 signer 服务、OS 权限隔离、广播状态/回执/核账与退出联调 | 有界配置、重启/超时/未知交易状态测试与真实小额演练 |
| 安全及运维 | 完整目标对抗和故障演练、长期 shadow 验收 | 自动化检查、目标 fork 和实际运行证据；不要求外部审计 |

本轮不生成虚构生产 PASS、默认资金限额或利润；生产配置和启动须由这些真实前置条件
闭合后进行。历史授权启动实盘仍有效，但不提供缺失数值或证明未执行的演练。

## 已确认的所有者资金要求

用户于本轮确认 `.env` 钱包专用于本项目，单日最大亏损 **200 USD**，累计最大亏损
**500 USD**。保存在 `configs/canary.robinhood.pending.json`，金额为精确十进制字符串。
投入总额暂未提供，以 `null` 表示缺失；执行保持关闭。该文件是待完成的所有者要求记录，
尚不是 signer 能够执行的完整风控策略，也不声称上述美元限额已经接入生产交易路径。

## 2026-09-16：所有者限额解析与阈值检查

`risk/canary_instructions.py` 严格读取上述文件，不接受额外字段、重复 JSON 键、浮点金额、
科学计数法、负数或超过六位小数的 USD 字符串。转换为整数微美元（1 USD = 1,000,000）
不使用浮点或舍入；USDG 等代币不能冒充 USD 估值币种。日损必须不高于累计亏损限额。

```sh
.venv/bin/python -m scripts.check_canary_limits \
  --config configs/canary.robinhood.pending.json
```

输出规范化限额和内容哈希；配置不完整返回 2，格式无效返回 1，完整且合法返回 0。
所有状态均为 `execution_eligible=false`；返回 0 只证明所有者要求可解析，不是生产批准。
当前真实配置返回 2，原因 `total_capital_missing`。

`capital_limit_breaches` 对已独立核账的 USD 输入检查总投入、单日亏损和累计亏损；
总投入超过预算拒绝，亏损达到或超过 200/500 USD 即阻断。它是纯阈值计算，尚未部署
成独立生产风控服务。它不从钱包余额猜测预算，不把稳定币数量直接当 USD，也不补造
缺失的 NAV、价格或失败交易成本。上游必须定义并保存日界线与现金流调整；下游必须
持久化 HALT，不能因次日或程序重启自动恢复。当前没有建立这些生产连接。


## 2026-09-16：500 USD 总投入确认与持久停机

后续用户明确总投入上限 **500 USD**。上文的缺失状态是历史进展，已由新确认替代；
当前 `configs/canary.robinhood.pending.json` 三个上限均完整，校验 CLI 返回 0。
`pending` 仍指待完成生产验证，`execution_enabled=false` 保持不变。

`risk/capital_journal.py` 新增 SQLite 持久风控记录，固定绑定账户、chain 和所有者限额摘要。
每笔输入包含 timestamped block、估值证据 SHA-256、整数 USD-micro 投入与损失。
写事务内重读并验证全部哈希链、检查排序与损失单调性、幂等处理重复估值；冲突、回退、
篡改及账户/限额变化拒绝。第二个进程会读取已提交的停机状态。没有任何自动解除接口。

日界线固定 **UTC+08:00**。采用保守的累计已核账损失口径，盈利不补回亏损预算；同日
损失不得降低，新日的损失增加必须被累计损失增加覆盖。日损达到 200 USD、累计损失
达到 500 USD 或投入超过 500 USD 时，停机原因永久保留，即使跨日、重启或后续投入降低。
首次没有估值时返回 `valuation_missing`；输出始终没有执行授权。

这是已测试的持久阈值组件，不是已部署的自动风控服务：它不验证估值哈希对应的真实
资产/价格/成本，也没有与 signer 或广播入口连接。调用方必须独立重放估值、验证完整性
与新鲜度；不能把策略自己声明的美元数值作为可信输入。SQLite 哈希链不能抵抗拥有同一
OS 权限者重写整份历史或回滚备份；独立监控/检查点仍属部署要求。当前逐次完整重验，
没有声称完成长期吞吐或大账本性能验证。现金亏损记录也不自动产生退出交易。

## 2026-09-16：Base / Robinhood 共享资金边界

用户报告 Base 上约 500 USD 的 ETH、Robinhood 上几十 USD 的 ETH；这属于估计，尚未
取得绑定账户、区块和 USD 价格的实时余额证据。本阶段优先评估 Base（8453），保留
Robinhood（4663）的扩展能力。**500 USD 是全部链合计投入上限，不是每链各 500 USD**；
余额可能高于预算，多余余额不会自动成为允许投入额度，也不自动跨链转移资金。

`risk/capital_reservations.py` 新增共享 SQLite 资金预留组件。所有链使用同一数据库与
同一全局上限；`BEGIN IMMEDIATE` 串行处理预算竞争，精确整数 USD-micro 累加；intent
绑定 chain、account 和金额。重复请求保持幂等，参数变更拒绝；submitted/未知状态继续
占用资金，只有显式结算证据摘要才释放，不能凭超时释放。已结算 intent 不可复用提交。
每行摘要在读写事务中复核；账户/预算/链集合与非法状态不一致时拒绝。

组件不是 signer 的交易批准，也不独立验证调用方提供的结算摘要。部署时必须从同一份
所有者指令读取 500 USD 全局限额，按最坏执行成本（包括 gas）预留，并在调用 settle 前
独立复核真实结算。不得为不同链分别创建预算数据库；当前旧版 Base execution monitor
仍未接入这套全局预算和亏损停机，不能直接启用它。单链 CapitalJournal 的日损/累计损失
目前也还需要全账户聚合层，不能各自获得一份 200/500 USD 的损失预算。

新预留组件通过双连接和同时竞争回归：两条链各申请 300 USD 时仅一个成功；450+50 USD
可以同时占用，上限之外的 1 micro USD 拒绝。上述值为合成测试，不是实际资金分配。
摘要检查不抵抗拥有数据库写权限者重算全表或回滚文件；生产仍需独立权限、外部检查点、
真实账户/估值采集和 signer/广播接入。模块尚未作为后台服务运行。

## 2026-09-17：原生币余额与价格原始证据复核

此前的余额估计现有固定区块证据支持：Base 区块 51392358 的专用账户原生余额为
0.219998301547024811 ETH，Robinhood 区块 64631055 为 0.005153577969653555 ETH。
两条链均从两家 RPC 的原始请求/响应独立重建并一致；这不是持续实时余额或全账户 NAV。
Base 同区块 ETH/USD 参考值为 2396.8703，两家 RPC 对同一预言机返回一致，向下取整的
原生币估值为 527.307395 USD。RPC 一致并不代表两个独立经济价格源。
Robinhood 按该价格跨链参考换算约 12.35 USD，不能作为该链已批准的风险估值。

完整来源、区块哈希、请求/响应摘要与剩余条件见
[重放证据](../../reports/evidence/wallet-reference-replay-20260917.json)。
所有者总投入仍为 500 USD，日损 200 USD，累计亏损 500 USD；余额高于预算不扩大权限。
本次 Robinhood 付费 Alchemy 仅使用 4 次调用，此后估值重放不消耗 RPC。

可重复余额采集入口 `scripts/capture_wallet_balances.py` 仅接受公开账户地址与指定 RPC
环境变量，每源最多 5 次调用，持久预算、至少 3 秒间隔、固定区块头复查；源失败时保留
成功同伴证据并返回非零退出码。观察进程不需要读取私钥。离线余额和价格验证分别由
`evidence/wallet_balance.py` 与 `farming/native_valuation.py` 实现；整数估值显式区分
向下资产展示和向上资金承诺舍入，跨链或不同区块不能混算。

尚未证明的条件包括全 ERC-20/LP/债务/挂单资产清单、预言机代理及 sequencer 状态、
当前新鲜度与最终性、全账户亏损聚合和 signer 接入。该证据不授予实盘资格。

## 2026-09-17：替换 Base RPC 后的实际结果

此前 Base RPC 探测失败已由新的 dRPC / Alchemy 无密钥公共端点组合解除。
固定区块 51410447 下，六个合约 runtime 和三个原生 ETH/USDC 池状态两源一致；
选择 fee=500、tickSpacing=10、无 hook 的池完成 12 档双向 exact-input/output 报价，
全部成功且两源一致。采样 0.001/0.01/0.1 ETH 与 2/20/200 USDC；相对最小档的
最大价格影响为 204 ppm（0.0204%），不是总交易成本或获利指标。

独立运行正式观察器入口后，区块 51410550 的状态也通过两源原始证据重放，实际消耗
9/7 次请求。来源配置和命令见 [RPC 使用](ROBINHOOD_RPC_USAGE.md)。报告仍明确
`lp_action=unavailable`：报价基准不等于新 Oasis hook 池的路由、收益或实盘资格。

证据：[报价](../../reports/evidence/base-eth-usdc-quotes-20260917/route-probe.json)、
[观察器](../../reports/evidence/base-observer-public-20260917/1/report.json)。

## 2026-09-17：Base 本地执行测试与盈利研究启动

新增 `run_pinned_fork.py --network base`。Base 区块 51410447 上的真实
PoolManager/PositionManager/Permit2 runtime 完成了本地 mint、四种 swap、collect、
白名单撤销后移除和授权清理；前后区块固定值一致。测试使用本地合成代币和新建池，
不等于目标 ETH/USDC 池实盘或盈利证明。子进程改为最小环境，不继承任意钱包变量；
同一 OS 用户仍可读取文件，因此这不构成生产密钥隔离。

[实验记录](../../reports/experiments/base-lp-profit-20260917.md)明确区分发现数据和
未触碰的样本外窗口。尚无完整已估值账本，不能用 swap 数量、测试费用提取或报价
成功填充盈利指标。本轮未启用真实资金签名、广播或持续后台交易。

## 2026-09-18：真实 Base ETH/USDC 池的本地验证

`run_pinned_fork.py --network base --suite base-pool` 新增真实池/USDC 的本地测试：
固定区块上入池、四种精确成交、费用提取、退出、逐币种现金守恒和授权归零均通过。
撤销 USDC 授权后，失败 mint 的 ETH 转入、USDC 和 NFT 计数完整回滚。
这补充了合成代币测试的覆盖，不代表真实资金成交、盈利或完整生产验收。
[证据及边界](../../reports/base-real-pool-fork-validation-20260918.md)。

## 2026-09-18：净收益汇总重放一致性

完整成本核账现在保留并重放估值输入，而非直接相信调用方的净收益、期初/期末价值或
现金流汇总。已复现并修复篡改汇总值仍被接受的问题，同时覆盖协调篡改现金流和 PnL、
伪造库存的回归测试。旧汇总缺少 `inputs` 时必须从底层证据重建，不能补零晋级。
这只证明派生结果的一致性，不替代上游原始价格、现金、交易成本和全账户证据验证。
[修复报告](../../reports/pnl-replay-integrity-20260918.md)。
