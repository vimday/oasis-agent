# LP 行情、现金、研究与执行边界

2026-09-14。四条链路的已实现增量和剩余条件；不是生产发布声明。

## 可验证的数据流

1. `run_pool_state_observer` 的 market 配置采集同一连续区块范围的两源 slot0 和 Swap，
   保留原始请求响应、验证 runtime/PoolKey/区块一致性，然后构造 MarketWindow。
2. 固定成熟尾窗决定参与 markout 的成交集合；新的成交明确计入 deferred。后续窗口
   必须有一致的重叠状态，并覆盖上一窗口截止点之后的成交。缺口不能被新 head 掩盖。
3. `replay_owner_cash_quorum` 根据显式事件、PoolKey、区块与 owner，从两源留存证据
   分别重建核心结算、trace、Transfer 和余额变化。原生币路径使用已有链 gas 规则。
4. 回测 v2 分别比较 R11 与持币、R00、R01、R10；每个评估 fold 都必须超过至少一个
   quote base unit，且不得低于冻结的 `minimum_r11_excess1`。这比旧规则严格，旧结果
   的 accepted 不能沿用。引擎版本已变更，须重新审查实验锁后再运行正式测试。
5. 签名服务在 SQLite 写事务内重新检查审计链，再预约 nonce 和调用外部签名器；
   审计记录被修改时停止。费用、资本、期限与 runtime 等原有限制继续生效。

上述接口没有新增广播路径；现金或市场 quorum 不能自行提升 release gate。

## 现金离线验证入口

```sh
.venv/bin/python -m scripts.verify_owner_cash \
  --request /absolute/path/cash-request.json \
  --evidence /absolute/path/evidence.db \
  --report /absolute/path/new-cash-report.json
```

请求必填 `references`（两源结算 EvidenceRef）、`header`（BlockSnapshot）、
`pool_manager`、`pool_key`、`entry`（尚未归因的 PositionLedgerEntry）及 `owner`。
验证器按 PoolKey 选择原生币或 ERC-20 路径，重新读取核心父证据与支付 wire bytes，
比较重建结果和原始解释。即使攻击者修改金额并重算内容哈希，仍无法通过此重放。
报告含带 token/decimals 的两币金额及父证据；不覆盖旧报告，不发起 RPC。

支持范围是已有 exclusive transaction resolver，包含直接 PosM 增减/收取的有限路径。
多仓混合、第三方支付、非标准 token、未知链 gas 政策等仍拒绝。quorum 报告是独立
现金核验结果，尚未自动填充跨周期 position ledger 或为全部失败交易定价。

## 样本外验收的精确含义

新增门槛阻止“跑赢持币却输给静态 LP”和“靠其他 fold 利润掩盖失败 fold”。校准与
评估不能切开同一区块；尾部平均损失向上取整，避免整数舍入低估损失。
`r11_excess_lower_bound1` 仍是最差 fold 的累计超额收益，**不是统计置信区间**。
固定阈值、更严格回测和单元测试不能证明收益，也不能证明数据未被研究者提前查看。
现有 lock/dataset hash 绑定不能替代外部时间戳、完整试验次数记录及不可见的最终测试集。
本轮没有开启或调参一个新收益假设，未生成样本外盈利报告。

## 当前阻断与下一阶段输入

| 链路 | 当前可运行部分 | 仍需完成 |
| --- | --- | --- |
| 持续行情 | 有界两源窗口采集、成熟期计算、原始重放恢复游标、持久化请求预算 | 低成本增量订阅/缓存、缺口回填、足够长的连续实测；现有采集上限不保证快速链可持续重叠 |
| 持仓现金 | 原生币/ERC-20 两源 raw replay、分币金额与 gas 分离 | 指定真实账户/NFT 的连续 opening/closing lineage、token attestation、独立估值和全交易成本合并 |
| 样本外研究 | 更严格的四组 fold 比较、块边界隔离、保守尾损舍入 | 正式冻结实验、完整可执行目标数据、隔离最终测试集、置信区间与多重试验控制 |
| 受限实盘 | 有界意图/模拟与外部 signer 基础、nonce 并发保护、运行中审计复核 | 正式外部 signer 后端、独立证据验证、发布批准、预算与日损控制闭环、签名后核账及真实 canary 演练 |

本轮未更改 live 配置、预算上限、外部账户或资金，也未启动后台交易任务。


## 2026-09-14：实盘授权后的启动核查

用户已授权启动实盘，授权无需重复请求。实际启动仍缺独立交易账户用途、资金与亏损
上限，以及已配置的外部签名后端。现有签名 CLI 使用无密钥 provider，不会产生签名。
仅存在 `.env` 私钥变量不能证明该钱包独立、额度合适或发布证据完整。

本地自动化可以使用独立签名进程持有密钥：策略提交有界意图，签名进程独立复核后
自动签名。签名进程必须有专属环境/文件权限；不能让 web、数据采集或研究进程继承
同一份私钥环境。签名后仍需独立广播状态跟踪、回执核账、日损停止与退出路径。
本轮没有使用或输出私钥，也没有迁移密钥，没有启动实盘。


## 2026-09-15：独立签名传输适配器

新增 `execution/socket_key_provider.py`，可供可信 sidecar 显式连接本地独立密钥服务。
已通过真实 Unix socket + 固定测试密钥的完整策略检查/签名恢复联调，具备文件权限、
响应大小/期限、账户与摘要绑定和签名格式检查。后端异常不重试，未决 nonce 阻断下一次
签名。默认 CLI 仍无 provider；真实密钥服务、密钥迁移与部署没有执行。协议和操作
边界见 [LOCAL_SIGNER_TRANSPORT](../security/LOCAL_SIGNER_TRANSPORT.md)。


## 2026-09-15：完整双来源现金窗口

`verify_cash_window` 从 raw opening/closing、完整 core/NFT 事件和逐动作现金证据重建头寸账，
动作时 owner 由 NFT 历史派生，按 owner/token 分账。提供已重放窗口的相邻边界检查；
缺失、重复、来源集合变化和伪造解释均拒绝。仍缺持续账户采集/持久游标、全账户 NAV、
估值和全成本闭环。入口与限制见 [生产准备](../operations/PREPRODUCTION_COMPLETION.md)。
外部独立安全审计已按项目所有者本日要求豁免；历史报告保持原样，不回写为审计通过。
