# Aave 事件角色与历史收据观察

2026-11-14（Asia/Shanghai）；基线 `c620665`，作者/内部自审 Codex，无独立 reviewer
sign-off。[此前借贷研究](MULTICHAIN_LENDING_20261114.md)已经区分 oracle–DEX 价差、
循环负债、退出资金与已实现利润；本轮补齐历史收据采集和协议事件解释入口。
本机材料位于 `reports/research/aave-history-20261114/`，未提交原始通信或第三方源码副本。

## 冻结设计与实际结果

从先前保留的地址网页中提取出 17 条 2026-10-29 交易/块号线索，属于网页可见部分，
不是完整交易历史。标注 Aave 的那条内部转账具体标签是 ETH Staking Contract，地址
`0xd01607c3c5ecaba394d8be377a08590149325722`；不能据标签认定 Supply 或 Borrow。

采集前冻结 design-lock：先核验单笔
[交易](https://etherscan.io/tx/0xee34ec92ed08f695fda8675a5aabddbc4a71777dafd3b022f7c21675f31cd99d)
及所在块 26301499，再解释账户角色；免费匿名 PublicNode，原范围总限 24 请求、每次
capture 60 秒、零重试。第一步只观察历史头，再以返回 hash 冻结收据计划。反证包括
缺收据、块/索引冲突、未知协议版本、事件非规范编码、第三方代操作及不完整现金流。
本次没有经济验收窗口或资本提案，不计算完整债务、HF 或净 NAV。

| 观察 | 结果 |
| --- | --- |
| 历史块头 | 2 次请求完整；block 26301499，timestamp 1793314235，即 2026-10-29 22:50:35 UTC |
| block hash | `0x6e6fc480eb64b46592feb209e405ae805a8b48d09e8e08ec7f02763161fc5eef` |
| 块内交易列表 | 返回 194 个交易 hash，目标在索引 162；这是单提供者观察，无独立共识证明 |
| 固定收据 capture | 前两次链 ID/块头一致，第 3 次 receipt 返回 HTTP 200、result null；停止，未请求闭合头 |
| 原预算 | 累计 5/24，STOP 保留，无重试、替换账本或付费调用 |
| 离线结果 | incomplete_capture；receipts 为空，event_flow_totals 为 null，net_profit 为 null |

收据响应 SHA-256 为 `3fe22fb2b53166fad994fc5514af096699a460b906ef15fd35b9c42f3e5eae93`。
缺收据不能解释为交易失败、没有借款或没有机会。尚未独立验证网页的资金转移数量和
该账户的完整 Supply/Borrow/Repay/Withdraw 历史。网页交易页在搜索工具中不可读取；
旧 bgd-labs 地址簿路径的连接器读取失败，后由 Aave 官方文档及 aave-dao 路径取得现行
地址簿。现行地址簿不是该历史块实现地址的证明。

## 工程能力与语义

公共采集器增加历史整数块号观察和固定收据计划。后者绑定 source、chain ID、块号/hash
及最多八个交易 hash；链 ID 和首尾块头开销为三次请求，每笔收据另一次。验证目标在
块内交易列表中的位置、收据和每个日志的同一块/交易/索引、去重、removed、status、
gas 数量与乘法溢出。当前仅接受有 topic 的日志，匿名日志会拒绝而不会丢弃。
有效的 status=0 收据可作为失败成本观察，不能含已提交日志。null 收据停止采集。

事件 ABI 固定到
[aave-v3-origin IPool](https://github.com/aave-dao/aave-v3-origin/blob/8305565ae342f1773c42cd2e4593f175fe5968a0/src/contracts/interfaces/IPool.sol)。
实际分析计划选择当前[官方地址簿](https://github.com/aave-dao/aave-address-book/blob/main/src/AaveV3Ethereum.sol)
列出的 V3 Ethereum pool `0x87870bca3f3fd6335c3f4ce8392d69350b4fa4e2`。没有假定所有 Aave
版本共用此 ABI，也没有核验该历史块代理实现和协议代码。

- Supply 归属 onBehalfOf；Borrow 的债务账户与实际收款调用者分别记录，支持识别代理和
  credit delegation 的角色差异。借款不是收入。
- Repay 区分负债账户与代偿者，并单列 aToken 偿还，不能一律算作账户现金流出。
- Withdraw 区分头寸账户与收款者；Liquidation 同时记录两个资产及两种数量。
- ReserveDataUpdated 的利率和指数以 RAY 整数记录；不据单点指数重建整个计息历史。
- 汇总是每个 chain/account/reserve 的**事件流量**，精度未知保持 null。不用 supply
  减 withdraw 当余额，不用 borrow 减 repay 当当前债务，不将撤回利息或融资当利润。
- 收据 gasUsed × effectiveGasPrice 只是一项原生币费用；另列谁付 gas，不冒充全部费用。
  转账、原生资金、初末债务、实现身份和完整历史各自保持未验证。

本轮也复核了 Titan 输入：Pydantic Literal[1] 会接受 JSON true 并转成 1，违反仓库
严格类型要求。两个链 ID 字段已改为严格整数区间，并新增 true/false/float/string/错链
回归。原三次真实采集的输入是整数 1，离线兼容回放仍通过，未改写其原报告。

## 复现与材料

`oasis-public-receipt-plan-v1` 包含 source、chain_id、block_number、block_hash、transactions。
`oasis-aave-receipt-analysis-v1` 包含 chain_id、account、pools。计划须在联网前固定。
采集使用[公共只读入口](../strategies/PUBLIC_RPC_OBSERVATION.md)，以下解释命令完全离线：

```sh
research=reports/research/aave-history-20261114
.venv/bin/python -m scripts.research_aave_receipts \
  --plan "$research/analysis-plan.json" --capture "$research/campaign/selected-receipt" \
  --output "$research/another-offline-report.json"
```

可重复 --capture 接最多 16 个原始收据 capture；重复交易、错链或非法 ABI 拒绝。
不完整 capture 仍可留存诊断，但清空事件汇总；输入拒绝或输出已存在退出 2。

| 材料 | SHA-256 |
| --- | --- |
| design-lock.json | `89d0f0ebbd527e33eebcf10cf6e6e2d6e7eb8ce2fc2f7760e444ba4e587c72a5` |
| IPool.sol（连接器源码副本） | `9e30d4aba1048c4bc095769e8b84db9c7d10ff5c152d74f89d6f05595f3bb11a` |
| 现行 AaveV3Ethereum.sol | `e210d350bc5b9abaace20b85b1135ed18477ad62c5946e0e1be0f1dce1a649a5` |
| historical-header/capture.json | `e6081826a4892cb5a5bcf4890553a0eb250bb03c86a92e262726b8c71773b301` |
| selected-receipt/capture.json | `7637731e108bd59928c54b92d7306e3b20015cf2fe6bf42258650379fa2c4519` |
| receipt-analysis-final.json | `f60f8f8a6765b7ef9b4b003f96c2652cf2b801be695a9e1ac4b62552cfa3da8f` |

156 项相关回归通过；新增内容包括收据位置、null、失败 gas、委托借贷、代偿、aToken
偿还、双资产清算和非规范事件拒绝。初次 mypy 的 list 不变性与 import 顺序问题已修正。
最终审阅额外拒绝 Python 整数解析会接受的带符号/空白日志数据；增加三项回归，完整
门禁在该修正后重跑，保留此前日志。

| 检查 | 结果 / 本机日志 |
| --- | --- |
| 聚焦回归 | 156 通过，退出 0；`focused-final.log` |
| make check | 退出 0；5,890 Python、138 子测试、9 前端模块、70 Foundry 通过；`make-check-final.log` |
| make security-check | 退出 0；依赖审计、Bandit、秘密扫描通过；`security-check-final.log` |
| make contract-security-check | 退出 0；80 合约、102 detectors、0 findings；`contract-security-check.log` |
| 文档与正常入口 | 32 本地链接目标和 help 通过；合成完整收据 CLI 通过；真实不完整报告重复回放逐字节一致；既有 Titan 三快照兼容回放通过 |
| 提交审阅 | diff --check 通过；仅任务的 13 个源码、测试与文档，原始研究文件及日志未入 Git |

下一安全动作是取得独立历史收据与代理/代码证据，再补全未选择交易、aToken/债务 token
转移、计息和外部现金流。原已停止的范围不自动恢复。本次工程交付不宣称已验证用户
地址的借贷套利利润，也不改变 Base 的资金/发布边界。
