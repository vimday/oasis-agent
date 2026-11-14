# Titan 构块状态：设计、实现与首次观察

2026-11-14（Asia/Shanghai）；实现基线 `fb60bdf`。作者/内部自审 Codex，无独立 reviewer
sign-off。此项承接 [Metric provider 诊断](PUBLIC_RPC_AND_METRIC_IDENTITY_20261114.md)。
研究材料在本机忽略目录 `reports/research/titan-state-20261114/`。

## 事先固定的范围

假设：构块者公开状态可能包含一般链上 latest 读取缺失的 Metric 条件更新。首次实验
只验证数据入口、身份声明、单位和覆盖，不评估价格或收益，不构造交易。
本机 design-lock 在采集前固定来源、方法、3 请求总限、30 秒期限、0 重试与反证条件；
采集前另存完整代码 hash 和参数计划。未使用已停止的 Ethereum 诊断账本。

数据来源是 [Titan 官方 taker 接口](https://docs.titanbuilder.xyz/propamms/takers)公布的
AP 公共匿名入口。复用公共采集器的原 SQLite 预算、预占、STOP、崩溃与字节回放机制，
仅允许一个固定的状态查询方法，最多三次。Titan 来源不能用于链 RPC 计划，也不能
传入任意方法、认证、overrides 或提交参数。耗尽后不能新建账本重采。

分析计划只选择此前观察过的 oracle 地址
`0x30a88bf894d1b806125a23ebf23d715496fd7102`，要求声明 maker 为 Metric；未选择的地址
仍保留在覆盖统计中。阈值事先固定为 snapshot 最多老 15,000 毫秒、最多未来 1,000
毫秒，target slot 最多老 36 秒、最多领先接收 12 秒。它们检查构块状态声明，不能证明
其中各 feed 的 reference 时间，也不是修改协议的 15 秒 guard。

验收：合法消息原样留存、确定性解析；整数时间/序列不损失精度；未知、缺失、退序、
同版本不同状态及资源超限均可见。无 untouched 经济窗口、资本、基准收益或晋级门槛；
任何经济结论须另立完整实验。首次错误停止并保留，不能以放宽 schema/鲜度重试获得成功。

## 实际观察

2026-11-14 03:25:06.599–03:25:07.894 UTC，共三次请求、全部 HTTP 200；原预算 3/3。
每次响应有 15 个 venue 声明。选择的 Metric oracle 均列出 17 对资产及 19 个 priced
地址，包括先前研究的池 `0xe9c19e5bd10e5096519fe1d2a9d783f79b8211da` 与 WETH/USDC 对。
这些是提供者覆盖声明，不是逐池代码、权限或流动性的独立证明。

| 次序 | 接收 UTC | 本地接收时间减提供者 snapshot 时间（ns） | 响应字节 |
| --- | --- | --- | --- |
| 1 | 03:25:07.457 | 94,188,647 | 37,071 |
| 2 | 03:25:07.683 | 32,212,375 | 37,486 |
| 3 | 03:25:07.894 | 23,186,570 | 37,486 |

上述差值受两端时钟影响，不能称为经独立测量的网络或 feed 延迟。声明目标为 Ethereum
block 26410517、beacon slot 15650224，对应目标 timestamp 1794626711，属于当时构建中的
状态。两次相邻比较均为 observed_update；snapshot 跳跃不证明中间没有漏更新。
每个被选择 venue 的 override 涉及五地址、17 个 storage slot，未出现 code override。
它同时涉及 oracle 以外的账户，因此不能将整个对象不加审查地用于资金模拟。

结果为 observed_declarations。没有 canonical base block hash、独立提供者/链证明、feed
reference 鲜度、精确调用者模拟、成交深度、成本或净收益。没有应用状态 override、签名、
提交 bundle、广播、部署或生产配置变更。未解释旧块 FeedStalled 的原因。

## 代码和复现

新增 `arbitrage/titan_state.py` 验证消息形状、整数与地址、资产对/序列对应、状态上限；
保留未知 venue。状态只输出摘要，余额、nonce、code 和 storage 原始值不变成执行参数。
相邻快照区分更新、重复、消失、覆盖变化、上下文/序列退序和同版本冲突。
当前没有持续订阅、持久事件游标或断线后补齐，因此不报告完整订单流。

公共 capture plan 的 schema 为 `oasis-titan-state-capture-plan-v1`，source 固定 titan_ap、
chain_id 固定 1、samples 为 1–3。分析 plan 的 schema 为 `oasis-titan-state-analysis-v1`，
包含 chain_id、metric_quote_addresses 和上述三个鲜度参数。

```sh
research=reports/research/titan-state-20261114
.venv/bin/python -m scripts.research_titan_state \
  --plan "$research/analysis-plan.json" --capture "$research/campaign/snapshots" \
  --output "$research/another-offline-report.json"
```

命令完全离线，输出必须是新文件。结果即便不完整也保留缺口；输入损坏或覆盖输出退出 2。
本机真实材料未提交，CI 使用合成数据。网络采集的 init/capture/replay 使用
[公共采集入口](../strategies/PUBLIC_RPC_OBSERVATION.md)，须先冻结并审阅具体范围。

| 材料 | SHA-256 |
| --- | --- |
| design-lock.json | `0fc930b12c199edd64635551d7b243002ed721e1c1d22bc274f9a4c34b570509` |
| 官方 taker Markdown | `80b37dc8371636ad8421a80fe177c716e75358e1198f6c698d1bb1fa67bea8eb` |
| capture.json | `8e6de6973c54107f1d29eac2bdec89f8e41eedd35f2b8e2c9921549d1dcbfcaf` |
| analysis-plan.json | `7681ca9f101e01f23c9498cb36408b60bc74f562a82dd120576288006860b9a8` |
| diagnosis-final.json | `e0f2e26bbd2021184762f6de53222ec178485634aaed22d54d23711e9d4cbf1c` |

44 项新增合成测试覆盖超大序列、秒/ns 分离、鲜度边界、恶意 override、空快照、字段
冲突、预算/429/格式失败、原始回放和 CLI 不覆盖。与公共采集及 Metric 两模块合计
160 项通过；初次未格式化代码的两处行长由 formatter 修正。

| 检查 | 结果 / 本机日志 |
| --- | --- |
| 聚焦回归 | 160 通过，退出 0；`focused-final.log` |
| make check | 退出 0；5,823 Python、138 子测试、9 前端模块、70 Foundry 通过；`make-check.log` |
| make security-check | 退出 0；依赖审计、Bandit 与秘密扫描通过；`security-check.log` |
| make contract-security-check | 退出 0；80 合约、102 detectors、0 findings；`contract-security-check.log` |
| 文档和真实离线入口 | 29 个本地链接、CLI help 通过；诊断重复运行逐字节一致，输出已存在时拒绝 |
| 提交审阅 | diff --check 通过；只有源码、测试与文档，原始状态及研究材料不入 Git |

下一步分别推进原始链事件/债务账本与持久 shadow 游标。Titan 的未来精确模拟还需固定
canonical base、目标 runtime/config、逐账户 override 语义、完整付款/收款者、整笔两腿
及净成本。公共状态可见性不会改变该策略的研究阶段或实盘授权。
