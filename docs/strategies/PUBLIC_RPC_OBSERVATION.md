# 公共链证据与 Metric reference 诊断

本入口将只读网络采集与离线解释分开。四个 PublicNode 别名分别绑定 Ethereum 1、Base
8453、Robinhood 4663 和 Elysium 99901，属于同一运营商，不构成独立双源。
另有固定的 Titan AP 公共状态来源，仅允许专用观察计划；它不提供链 ID/块头证明。
`mevblocker_ethereum` 另绑定 Ethereum 的免费匿名 MEV Blocker base URL，仅开放同一只读
方法集合；服务标签不代表底层节点已独立验证，失败不触发来源回退。首次父块观察失败
保留在[探针记录](../verification/METRIC_ATOMIC_PROBE_20261114.md)。
实际观察、失败及哈希见[验证记录](../verification/PUBLIC_RPC_AND_METRIC_IDENTITY_20261114.md)。

## 冻结采集

先核实端点仍免费、无需认证，再为具体研究填写 scope；以下仅为格式示例，过期示例不会发出请求。
新建目录只适用于新的独立范围，不得用于重置失败或耗尽的原预算。

```json
{
  "schema": "oasis-public-rpc-scope-v1",
  "purpose": "deployment_identity_observation",
  "expires_utc": "2026-11-14T00:00:00Z",
  "sources": [{"source": "publicnode_ethereum", "max_requests": 16}],
  "max_capture_seconds": 60
}
```

`oasis-public-rpc-head-plan-v1` 只接受 source、chain_id、block_tag（latest/safe/finalized）；
block_tag 也可为严格非负整数，用于观察指定历史块；返回块号必须相符。
它用两次请求观察链 ID 与块头。tag 不提供独立最终性保证。
随后冻结 `oasis-public-rpc-read-plan-v1`：source、chain_id、block_number、block_hash、reads。
每个 read 指定 method 和 target；eth_call 额外指定 data，可选 sender；eth_getStorageAt
额外指定完整 32 字节 slot。没有 value、state override、任意 URL、认证或广播参数。
每次固定读取额外消耗三请求（链 ID、首尾块头），全程使用 EIP-1898 requireCanonical。
不支持该参数的提供者会停止，不自动降级到块号。

```sh
.venv/bin/python -m scripts.capture_public_rpc init --scope "$scope" --root "$campaign"
.venv/bin/python -m scripts.capture_public_rpc capture \
  --root "$campaign" --plan "$plan" --name deployment
.venv/bin/python -m scripts.capture_public_rpc replay \
  --capture "$campaign/deployment" --output "$report"
.venv/bin/python -m scripts.capture_public_rpc replay \
  --capture "$campaign/deployment" --exports-only --output "$export"
```

网络前检查原账本身份、完整请求预算和范围期限，逐次预占；失败同样计费。
首个传输、HTTP、RPC、内容、上下文或资源错误立即停止并持久化 STOP。崩溃遗留 ACTIVE
拒绝自动继续；并发 writer 拒绝。采集文件只新建，原始 request、response、时钟、hash、
预算计数和失败均保留。单响应上限 2 MiB，总响应上限 32 MiB，最多 120 秒。
退出 0 为完整 capture，3 为已保存的不完整 capture，2 为输入或操作拒绝。

replay 完全离线，核对精确请求、字节和计数，不重新请求网络。失败采集可以验证完整性，
仍报告 incomplete；不把失败上下文导出为可用结果。完整 RPC error 可保留诊断。
本地时钟不等于独立时间见证；哈希不是签名；解析导出另作 parsed evidence，不能继承
原始传输验证、源独立性或交易权限。

## Metric 诊断

`scripts.research_metric_oracle` 读取 `oasis-metric-oracle-plan-v1`，固定链、块/hash/cutoff、
来源、pool/provider/oracle/reader 地址和各 runtime hash、pool immutables hash、oracle kind、
base/quote feed ID 与离线查找预算。先核验部署身份及 immutable provider，再检查实际
reference age 配置。仅支持已研究的 1.1.0 pyth-lazer / chainlink 接口语义。

```sh
.venv/bin/python -m scripts.research_metric_oracle \
  --plan "$diagnosis_plan" --export "publicnode_ethereum=$export" --output "$diagnosis"
```

价格读取使用 `price(bytes32,address)`，guard 使用 `priceGuard(bytes32)`；不尝试禁用的
raw getter。refTime 为秒，mid 为 E8，spread0 单位 0.01 bps；检查零/未来/过期时间、
零价、饱和 spread 及上下界。合成测试验证 15 秒边界与 16 秒拒绝，实际配置从证据读取。
quote feed 为零时是单腿模型。缺任一必需回复保持 unavailable，不以错误选择器推断 feed。

零 origin 的 eth_call 只是模拟读取路径。reference guards 通过也不证明 source shaping、
经济报价对、真实调用者权限、流动性、两腿原子成功或盈利。诊断所有执行准入标志均为 false。

[Titan 状态研究](../verification/TITAN_STATE_20261114.md)另行解释构块者状态声明。采集器
只保留该公开接口的原始字节；分析输出状态摘要，不应用收到的 override，也不提交候选交易。
可成对提供 `--feed-plan` 和 `--feed-catalog`，按已审阅源码布局解释 Metric 的 oracle diff；
目录原始字节哈希、feed/token/symbol 与年龄边界均固定在计划内。它保留毫秒 reference，
依源码取整为秒；未知槽、代码/完整状态覆盖、其他账户存储修改和缺必需 feed 均拒绝部分
价格，feed 退序和同时间不同值也拒绝。结果仍是构块者声明，不是签名更新、目标块权限、
canonical 状态或可执行收益；格式和原始回放见[feed 验证](../verification/METRIC_BUILDER_FEEDS_20261114.md)。

`scripts.research_metric_context` 通过 `--plan`、`--titan-plan`、`--titan-capture` 和最多四个
`--header-capture` 离线关联固定父子块。计划绑定原 Titan 两个文件 hash 和必需来源，
拒绝重复/跨链/不符块号、来源分歧、父子关系或时间矛盾；缺源退出 3，输入拒绝退出 2。
缺第二源时保留明确的单源观察，矛盾时清空共同上下文，所有执行/最终性标志保持 false。
本地始终回滚的两池探针不在 RPC 采集方法集合内；没有新增任意 override 或提交入口。

[历史收据计划](../verification/AAVE_RECEIPTS_20261114.md)固定块/hash 和最多八笔交易，
交叉核对块内交易位置及日志；null 或错位立即停止。Aave 事件解释单列账户、代操作者、
收款人和融资流量，不能代替完整债务或现金流账本。

## 日志范围与持久覆盖

`oasis-public-log-range-plan-v1` 指定 source、chain_id、start_block、end_block、parent_hash、
address、topics 和 locked_utc。仅接受连续 1–16 块和 1–4 个精确前缀 topic；不接受 OR、
通配符或任意 URL。所有块的秒级时间须晚于声明的锁定时间，且不能晚于本机响应时钟。
N 块需要 N+3 请求。空日志数组可以形成覆盖，null、移除日志、错位/重复/乱序事件、
祖先或关闭块变化均停止原 campaign。首次真实尝试及边界见[事件覆盖验证](../verification/EVENT_COVERAGE_20261114.md)。

```sh
.venv/bin/python -m scripts.import_public_range \
  --capture "$campaign/swap" --store "$evidence_db" --chain-store "$chain_db" \
  --stream pool.swap --output "$import_report"
.venv/bin/python -m scripts.replay_cycle_shadow \
  --cycle-plan "$cycle_plan" --window "$window" --store "$evidence_db" \
  --capture "pool.swap=$campaign/swap" --chain-store "$chain_db" --output "$shadow_report"
```

第二个示例只传一个过滤器；其余必需过滤器未提供时结果应为 coverage_incomplete，不能报价。
完整运行逐个传入冻结计划对应的 `--capture FILTER=DIR`，也可保留原有 `--refs` 离线入口；
同名输入不能混用。导入与回放不请求网络。链存储按 chain/stream/filter 记录空块、缺口、
连续前沿及原始证据引用；重组撤销受影响的覆盖，保留历史来源。旧数据库不能从全局 tip
补造覆盖。该覆盖不是独立最终性证明，也不是后台任务的投递游标。

## 持久离线报价队列

为既有 shadow 命令加上 `--queue-session NAME --chain-store "$chain_db"`，可将冻结窗口、
覆盖记录、报告和候选任务放进同一事务。session 名称绑定完整配置、窗口和相关策略/证据源码指纹，不能在原名称上
换配置或清除失效状态。任务只允许已保留输入的历史报价复核；没有实时 RPC 或交易权限。

```sh
.venv/bin/python -m scripts.replay_cycle_shadow \
  --cycle-plan "$cycle_plan" --window "$window" --store "$evidence_db" --refs "$refs" \
  --chain-store "$chain_db" --queue-session "$session" --output "$producer_report"
.venv/bin/python -m scripts.process_cycle_shadow_queue run-one \
  --chain-store "$chain_db" --store "$evidence_db" --session "$session" \
  --worker offline-worker --export "synthetic=$quote_export" --output "$worker_report"
.venv/bin/python -m scripts.process_cycle_shadow_queue show \
  --chain-store "$chain_db" --store "$evidence_db" --session "$session" --output "$queue_view"
```

`synthetic` 是示例别名，实际使用冻结计划中的来源；报价输入须通过原有来源、区块、身份和
cutoff 检查。`run-one` 必须提供报价导出文件，只计算一个任务。租约默认 30 秒，
`--lease-ms` 上限 60 秒，至多八次领取；崩溃后过期重投同一任务 ID，旧 attempt 不能提交。
完成表示离线结果已持久化，结果可为 unavailable；不代表交易或盈利。

`show` 退出 3 表示窗口已失效，正常观察为 0；输入、租约或完整性拒绝为 2。
重组会同步撤下已完成结果的当前展示，保留历史字节。输出文件写入失败不会撤销已提交的
队列状态，重启应检查原数据库。租约的本机处理时钟与冻结行情 cutoff 分开，旧窗口始终是
历史复核。细节与合成验收见[队列验证记录](../verification/CYCLE_OUTBOX_20261114.md)。
