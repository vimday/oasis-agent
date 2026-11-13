# 多链研究、Binance 案例纠错与借贷场景验证

日期：2026-11-14（Asia/Shanghai）；基线提交 `78eb13e`。作者/内部自审：Codex，
无独立 reviewer sign-off。代码提交由本文件 Git 历史定位。
[锁定设计与下一阶段交付](../strategies/MULTICHAIN_LENDING_EXPERIMENT.md)。

## 事件纠错和用户地址

2026-10-29 的 Binance 热钱包事件已获
[Binance 官方确认](https://www.binance.com/en-AU/support/announcement/detail/d3306d9dca5243548c813b605e1d1751)。
上一轮未找到事件、引向 2025 Bybit 的说明不准确；在
[原记录](METRIC_RESEARCH_20261114.md)追加了纠正，而未删除旧状态。

[Aave 风险响应](https://governance.aave.com/t/binance-hot-wallet-exploit-aaves-response/25954/1)
记录当晚 21:42–23:04 UTC 执行多网络冻结。冻结限制新增 supply/borrow，repay、withdraw、
liquidation 仍可运行；报告称 oracle 正常、市场未 pause。因此用户所述的“卖出 ETH →
稳定币抵押 → 借 ETH → 再卖”值得研究，但在冻结后不能继续假定同样循环可用。
冻结与暂停的边界不同，撤款还依赖剩余流动性。

[AaveOracle](https://aave.com/docs/aave-v3/smart-contracts/oracles) 的每资产 source 和市场
base currency 决定债务估值；[Chainlink 数据源](https://docs.chain.link/data-feeds/data-sources)
说明 crypto price 可包含多个供应商的 CEX 和/或 DEX 数据。应将机制表述为 oracle–DEX
可执行价格分离，而不是将所有 Aave 市场永远绑定到一个 CEX 价格。

用户提供的 [0x0cdd…661d8](https://etherscan.io/address/0x0cdd9cbb825212c6cc5b0b36239e94218ee661d8)
在 [Bitquery 当晚研究](https://www.bitquery.io/investigations/binance-hack-who-profited)中被列为
最大 ETH 卖方：26 笔、扣 gas/tips 后约 $3.46M。这是**提供者归因结果**：方法是逐笔相对
同分钟 Hyperliquid 价格，稳定币按 $1；并承认外部购币成本可能不在内。不能直接称为
我们独立验证的借贷闭环利润，也不能用 26 笔 swap 推断 26 次借款。

地址网页 HTTP 200 原文已保留，能看到当晚 KyberSwap/CoW 等相关原生币流入，以及
22:50:35 UTC 的 Aave 标签转账。它们只是继续查证的线索，地址标签不能替代协议事件。
交易分页页 HTTP 403，未重试；搜索工具的地址页失败及两个 Routescan 交易页 cache miss
也保留为查证缺口。尚未取得完整 Supply/Borrow/Repay/Withdraw 原始日志、前后余额、债务
指数与外部现金流，因此不下完整借款次数、本金、成本或净 NAV 结论。

源码语义固定到 aave-v3-origin `8305565ae342f1773c42cd2e4593f175fe5968a0`：

- [ValidationLogic](https://github.com/aave-dao/aave-v3-origin/blob/8305565ae342f1773c42cd2e4593f175fe5968a0/src/contracts/protocol/libraries/logic/ValidationLogic.sol)：supply/borrow 的 frozen/paused/cap、repay/withdraw 的 paused 检查。
- [GenericLogic](https://github.com/aave-dao/aave-v3-origin/blob/8305565ae342f1773c42cd2e4593f175fe5968a0/src/contracts/protocol/libraries/logic/GenericLogic.sol)：抵押、债务与 HF 关系。
- [AaveOracle](https://github.com/aave-dao/aave-v3-origin/blob/8305565ae342f1773c42cd2e4593f175fe5968a0/src/contracts/misc/AaveOracle.sol)：资产 source、base currency 与 fallback。

这是源码研究 pin，不是 10 月 29 日 Ethereum 部署的 runtime 或历史配置证明。新增计算器
明确是简化场景，不冒充上述协议的 bit-exact 利息、eMode/isolation 或执行仿真。

## 多链公开目录观察

固定顺序 `[1, 8453, 4663, 99901]`、每链 32 池、无认证 HTTP、总共四请求；无 RPC。
第一捕获遇 Base 404 停止。第二捕获仅请求第一捕获未到达的两链，遇 Elysium 404 停止；
没有重试任何失败或成功请求，没有修改旧 capture。它们是两个不同时钟的独立捕获，
均 `incomplete`，严格 replay 均拒绝，不能拼成“成功的四链市场快照”。

| 链 | 请求 UTC | 结果 |
| --- | --- | --- |
| Ethereum 1 | 2026-11-13 18:32:02 | 200；观察 32 池，提供者声明 total=158 / nextOffset=32；单页解析为 16 对资产，10 对有多池，1 池暂停 |
| Base 8453 | 2026-11-13 18:32:02 | 404 `Unknown chainId: 8453`；该接口可用性缺口 |
| Robinhood 4663 | 2026-11-13 18:33:17–18 | 200；观察 32 池，total=610 / nextOffset=32；12 对资产，5 对有多池，11 池暂停 |
| Elysium 99901 | 2026-11-13 18:33:18–19 | 404 `Unknown chainId: 99901`；该接口可用性缺口 |

成功响应的分组仅作原始单页研究检查，不改写失败 manifest，也未发布完整 universe 报告。
未按 total 继续分页。官方[部署文档](https://docs.metric.xyz/reference/supported-chains-and-deployments)
列有 Base/Elysium；公开目录不支持它们不等于没有合约或机会，更不等于 EVM 算术不同。
当前没有取得跨 venue 可执行报价、独立 oracle 或链上权限证明。

## 合成借贷场景结果

例子：1 SYNETH、卖价 2600 SYNUSD、入场 oracle 2300 USD、target LTV 75%、LT 80%、
4 轮、20,000 SYNUSD 外部退出准备金、25 SYNUSD 非利息成本、债务增长 1.0001 倍。
都是虚构参数与地址；不是历史市场快照，也不改变真实资本授权。

最终债务本金 `2692746728804347826`（18 位），抵押 `9601141493`（6 位）。
各轮卖出与本金守恒，初始库存出售所得与借款出售所得分开核算。

| 替代退出场景 | 模型结果 |
| --- | --- |
| 买回价 2300，oracle 不变 | 条件超额相对恢复初始库存为 1082.204685 SYNUSD；相对只卖初始库存为 782.204685；不是已实现收益 |
| 买回仍为 2600 | 扣成本及利息后相对两基准均 −25.700117 SYNUSD |
| ETH oracle/买回价升到 4600 | HF≈0.6200，出现可清算状态；拒绝给退出终值 |
| 抵押 oracle 跌到 $0.80 | HF≈0.9921，拒绝给退出终值 |
| 去掉外部退出准备金 | 即使 gross equity 正数，也因退出现金不足拒绝闭环 |

其他回归覆盖冻结停止循环但允许偿还、pause、cap、累计深度、撤款流动性、未知成本、
币种精度变化、四链同算术隔离、无穷循环比例假象和保守舍入。未建模盘中路径、清算
损失、逐笔失败与 gas 支付；本金不清零就不是结算，不生成任何交易或签名。

## 证据和复现

本机忽略目录 `reports/research/multichain-lending-20261114/` 保留原始观察和日志，不提交网页
或环境私密材料。capture 中每个 request/body 的哈希与时钟可单独复查。

| 文件 | SHA-256 |
| --- | --- |
| metric-universe/capture.json | `08659bc0bb17679daf63c44a0a886a79edafed785814499d4403073ed31f4b71` |
| unattempted-chains/capture.json | `af5566238cd4ff05d232a8f88543384bf30e892119f97d1ad83bcbe1e93db71a` |
| address.html | `131bddfc75456ee1caf5f77e751f637736fb6605c4009f03d40ddbcc354d04d2` |
| transactions-1.html（403） | `bea392f2c269652b3435dde0a027c1fdce6ed0d94e2d6d4a5d5c9cf499becb7b` |
| lending-synthetic.json | `97e7d6788632fa919da797e10d3342fa159482931047d7b4bfba0036c4e8daaf` |

实现：`metric_universe.py`、`lending_loop.py`，CLI `discover_metric`、`research_lending_loop`；
`research_metric` 提取共享有界采集/回放函数，保留原入口与安全边界。
可运行命令见实验记录。相同实现与计划应确定性输出；实施哈希跨版本变化是预期。

## 检查与当前阻断

聚焦三模块 108 项通过（新增 57 项）。初次新增捕获测试错误地两次进入同一个 httpx
context，导致 2 项测试失败；修正测试 harness，生产采集没有放宽。初次静态检查发现
dict 风格及一个 Any 返回类型，已修正。检查日志在上述本机目录。

| 检查 | 结果 / 本机日志 |
| --- | --- |
| 三模块聚焦回归 | 108 通过，退出 0；`focused.log` |
| `make check` | 退出 0；5,663 Python、138 子测试、9 前端模块、70 Foundry 全部通过；`make-check.log` |
| `make security-check` | 退出 0；依赖无已知漏洞、Bandit 无 medium/high、秘密扫描通过；`security-check.log` |
| `make contract-security-check` | 退出 0；80 合约、102 detectors、0 findings；`contract-security-check.log` |
| 文档与入口 | 56 个本地链接目标通过；两个新增入口 help 通过；合成例子两次输出逐字节一致 |
| 原 Metric 捕获兼容回放 | 退出 0，仍为 0/6 成本完整、没有正收益结论；`prior-metric-replay.json` |
| 两个不完整真实捕获 | replay 均退出 2，未发布报告；`incomplete-replay-a.log`、`incomplete-replay-b.log` |
| `git diff --check` 与提交审阅 | 通过；仅本任务源码、测试、示例及文档，原始观察和日志不入 Git |

下一安全动作：用独立、显式预算的原始链证据补齐地址
借还款与 Elysium 精确 prestate；按每条链的可用数据源接独立 DEX 与精确 caller 模拟，
再冻结前瞻 shadow 窗口。当前没有付费 RPC、资金操作、部署或实时守护进程变更。
