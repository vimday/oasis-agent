# Base 整笔模拟交付与 Denim 时间语义

同一 Uniswap V3 → Aerodrome Slipstream 环的四组新父子块中，Base 官方和 dRPC 的整笔收付、费用与净额逐项一致，两路都未在本机收到直接子头前齐备。官方最少晚 **43.1 ms**。同一历史正控在本机 Anvil 的热态整笔往返中位数 **1.429 ms**，冷态首次往返 **25.853 秒**。Anvil 无法用 Base 同秒子块的时间戳重放完整仿真。热态结果量出了已有状态时的计算耗时。[公共配对数据](BASE_SIMULATOR_PAIR_20261128.json) · [固定 fork 数据](BASE_SIMULATOR_FORK_20261128.json)

## 同一历史微利与新窗口

以 Base 8453 的块 **63,043,739** 为历史正控，冻结实际本金 **67,280,448,940,895,129 wei**、已有探针运行码、调用者、nonce、calldata、gas 上限与费率。WETH 起手由整笔调用中的原生本金包入。两家匿名 HTTP 都从首池实付、第二池回款和最终接收人的六条日志核实同一资金流。链 ID 预检后，Alchemy 的匿名端点对这一方法返回 HTTP 401，原范围随即停止。这个 401 只代表该匿名入口的这次调用。[来源与原始字节摘要](BASE_SIMULATOR_PROVIDERS_20261128.json) · [ETH 与 WETH 费用口径](BASE_MIXED_WHOLE_20261128.md)

| 源 | 历史整笔往返 | gas | 按网络费扣除后的 WETH 净额 |
| --- | ---: | ---: | ---: |
| Base 官方 | 328.584 ms | 293,025 | +1,682,277,530,217 wei |
| dRPC | 372.293 ms | 293,025 | +1,682,277,530,217 wei |
| Anvil 本机冷态 | 25,852.552 ms | 300,733 | +1,628,286,212,161 wei |
| Anvil 本机热态，24 次 | 1.333–1.765 ms | 300,733 | +1,628,286,212,161 wei |

新窗在 2026-11-28 02:45:24 UTC 冻结。同一个 WebSocket 按顺序收取 14 个头，预定第 1、5、9、13 个头为 H，分别验证到直接子块 C 的高度与父哈希。每个 H 同时派发两路整笔和一条共享的按哈希费用读取。模拟输出绑定 H 哈希，费用读数也绑定 H。就绪耗时使用两份回包在同机单调时钟上的较晚值，表示完整证据到齐的最早可能时刻，解析复核在之后完成。

| H | 本机接到 H 至接到 C | 官方整笔与费用均到齐 | dRPC 整笔与费用均到齐 |
| ---: | ---: | ---: | ---: |
| 63,217,824 | 216.747 ms | 259.806 ms | 295.854 ms |
| 63,217,828 | 197.387 ms | 268.472 ms | 358.678 ms |
| 63,217,832 | 197.380 ms | 258.941 ms | 359.519 ms |
| 63,217,836 | 217.261 ms | 262.583 ms | 351.186 ms |

官方整笔 HTTP 在途 251.045–258.703 ms，dRPC 为 288.002–350.839 ms，四个配对头都在接头后 10 ms 内发起两路仿真。两路 4/4 到账一致，每笔 301,204 gas，按同一费项模型均为 **−22,401,040,569,431 wei**。C 提供交付截止见证，这四个 C 的经济结果留空。样本仅覆盖固定双池和本金，未测出自然正机会存续、节省失败费或漏过微利的比例。[上一窗口的发现、整笔与子块回测](BASE_MIXED_HEAD_PRIORITY_20261128.md)

## 近端路径的瓶颈

Mac 和 Aleph WSL 的预检均未发现已同步可用的 Base 节点。历史正控临时 fork 在 Mac 回环上启动，固定 Anvil 1.10.0 的构建、原区块和探针。经 PublicNode 做 **102** 次按 canonical block hash 固定的账户状态读取，其中 **57** 次读取 storage slot。完成首次整笔后，代理拒绝所有上游请求，包括自身缓存命中，再执行 24 笔相同包络。热态无任何出站请求或代理访问。25 次局部模拟的实际本金、两腿回款和日志顺序均与公开 RPC 一致。

Anvil 相对公开源多报 **7,708 gas**，落在相同费率下对应 **53,991,318,056 wei** 的成本差。两路公开仿真的下一块时间戳沿用 H 的整秒值，Anvil 默认增加 12 秒。用原始账户状态在本地重放默认参数可重复取得 300,733 gas。唯一变更为显式传入公开仿真的整秒时间时，Anvil 以 `-38021` 拒绝。Denim 按 200 ms 推进，相邻块允许拥有相同的整秒时间戳。本次对照尚未分离时钟与 EVM 实现的 gas 影响，用公开结果比较网络费。[零网络时间戳对照](BASE_SIMULATOR_TIMESTAMP_20261128.json) · [Base 执行层的同秒父子块测试](https://github.com/base/base/blob/main/crates/execution/node/src/engine.rs) · [Denim 链级时间规则](https://github.com/base/base/blob/main/crates/consensus/protocol/README.md)

下一轮以支持 Denim 的 Base 执行客户端作为近端候选，先复核同秒及跨秒父子块的本金、回款、gas 和费用，再测受影响地址及 slot 到齐的时钟。保留官方和 dRPC 的同头对照，分别记录收到头、获得可执行状态、费用到齐、整笔返回与直接子头。固定双池用于核验交付，再在保留 4／58／66 池的独立窗口评估自然候选。以实际可验证机会的端到端时间衡量新增算力或 RPC 支出的效果。

Aleph 预检时 WSL 根卷余 521 GiB，Windows 挂载卷显示余 851 GiB。官方文档列出的最小主网快照约 782.5 GB，解包后更大。近端顺序先走固定状态下的小型原生执行对照，完整同步另需满足磁盘容量并实测跟头延迟。[Base 节点快照](https://docs.base.org/specifications/node-operators/snapshots) · [Base 本地执行环境](https://github.com/base/base/blob/main/docs/guides/DEVNET.md)

## 预算与复算

共留存 **128** 次免费 RPC 额度预留，支出 USD 0。历史探测 6 次，新窗 18 次，含 3 次 WebSocket 控制预算。fork 共 104 次，其中 2 次来自提前停止的启动试验，102 次属于完整实验。第一次公共探测在本地处理历史请求 ID 时停止，未占用额度。其他提前停止的 fork 尝试保留停止原因、配置、原账本、客户端日志和当时的脚本。接口差异包括可选 RPC 探测与本地响应格式。时间戳对照只读既有原件。未签名、未广播。

```sh
set -o pipefail
export PYTHONPATH=.
.venv/bin/python docs/verification/fixtures/replay_base_simulator_sources.py replay \
  reports/research/base-free-simulators-hash-20261128 |
  cmp - docs/verification/BASE_SIMULATOR_PROVIDERS_20261128.json
.venv/bin/python docs/verification/fixtures/replay_base_paired_simulators.py replay \
  reports/research/base-paired-simulators-live-20261128 |
  cmp - docs/verification/BASE_SIMULATOR_PAIR_20261128.json
.venv/bin/python docs/verification/fixtures/replay_base_fork_latency.py |
  cmp - docs/verification/BASE_SIMULATOR_FORK_20261128.json
.venv/bin/python docs/verification/fixtures/replay_base_fork_latency.py \
  --timestamp-control reports/research/base-fork-timestamp-20261128 |
  cmp - docs/verification/BASE_SIMULATOR_TIMESTAMP_20261128.json
```

四份 JSON 均逐字节复算一致，新增 Python 夹具通过 Ruff，新增文档链接已核验。`make check`、`make rust-check`、`make security-check` 与 `make contract-security-check` 均退出 0。Python 6,860 项及 138 个子测试、Foundry 132 项、Rust 30 项通过。本机日志见 `reports/research/base-simulator-integration-20261128/`。
