# Base 同头地址池增量与 V4 取数对照

北京时间 2026-11-27。三个匿名免费范围共用 55 个 RPC / WebSocket 预留，费用 USD 0。
58 地址日志过滤遭源拒绝后停止。新范围使用两个独立 HTTP 源和原始 WebSocket 头
一致的区块日志 Bloom，在三个连续的相同父子头比较保守地址池增量与全量取数。
三个配对的 58 池原始 ABI 值与 4/58/66 池模型结果均一致。增量地址传输少用
35–47 ms，组合状态仍都晚于实际直接子头到达。
[此前连续窗口](BASE_REALTIME_OPPORTUNITY_20261127.md) ·
[EVM 日志 Bloom 实现](https://github.com/ethereum/go-ethereum/blob/master/core/types/bloom9.go)

## 原始拒绝

15:31:47.294–15:31:52.538，第一个范围见证 62,871,661 和直接子块 62,871,662。
种子状态完成 4/58/66 池读取，三组模型正候选均为零。对第二头的首次
`eth_getLogs({blockHash,address:[58]})` 返回 HTTP 403，JSON-RPC `-32602`，
正文给出的匿名地址上限为 10。该范围按预注册在拒绝后停止，未开始增量读。
实际用量 14/29，由 PublicNode HTTP 5、官方 HTTP 6、WebSocket 控制 3 构成。
直接子头间隔 195.416 ms，种子组合状态 909.878 ms。后续范围没有再发
`eth_getLogs`，各自保留原始账本。

## 同头 Bloom 增量

15:34:28.830–15:34:33.299，连续观察 **62,872,469–62,872,473**，首块只初始化，
随后三个头完整配对，第五个仅用于核对实际直接子头时钟。五个原始头均保留，前四头
无调度跳过。配对各臂仅发一份同源 PublicNode 地址 Multicall，双源头、Base 官方
V4 运行时和完整状态作为共享证据。实际 26/26，PublicNode HTTP 11、官方 HTTP
12、WebSocket 控制 3。每 HTTP 源 250 ms 最小预留间隔，任一首拒绝停止。
[EIP-1898 哈希固定](https://eips.ethereum.org/EIPS/eip-1898)

每个头先核对两份 HTTP 头和 WebSocket 观察的高、哈希、父哈希和状态根。
增量臂只复用前一完全匹配状态里 Bloom 确认无日志的地址池 `getReserves`、
`slot0` 或 `liquidity` 原 ABI 字节。Bloom 可能命中的地址全部读对应状态。
费用、两侧代币实际余额、BaseTime、链 ID 和 L1 费用探针每头实读。Bloom
阴性只证明没有该地址日志，静默的存储变化仍需全量校验。每对在发布前
强制比较所有 283 个完整 ABI 结果、58 个池状态与余额、状态哈希及 Rust 两锚
候选和未闭合定量区间。V4 的 8 池每头完整实读并共用，66 池比较没有增量
V4 覆盖。四池沿用同一请求的控制子集，无独立四池网络耗时。

| 配对头 | 地址先后 | Bloom 可能发日志，58 池 / 4 池 | 实读子调用，全 / 增 | 地址 HTTP 传输，全 / 增 ms | 地址线程 CPU，全 / 增 ms |
| --- | --- | ---: | ---: | ---: | ---: |
| 62,872,470 | 全→增 | 1 / 0 | 283 / 180 | 178.535 / 143.072 | 39.431 / 43.115 |
| 62,872,471 | 增→全 | 0 / 0 | 283 / 178 | 203.643 / 156.752 | 41.291 / 41.436 |
| 62,872,472 | 全→增 | 0 / 0 | 283 / 178 | 205.580 / 165.389 | 41.303 / 42.396 |

每对全量地址请求及响应共 228,049 字节，增量为 137,937 / 136,209 / 136,209
字节，减少 39.5–40.3%。Bloom 筛选单独用 0.757–1.606 ms 线程 CPU。
地址 CPU 还包括 ABI 复原和原始归档的工作量。在同源交替先后的三对中，缩小
传输没有缩短完整 66 池状态就绪时间。

| 配对头 | 本地排队 ms | 本地收头至双源头完成 ms | V4 runtime / StateView HTTP ms | 完整状态，两臂同值 ms | 直接子头本地间隔 ms |
| --- | ---: | ---: | ---: | ---: | ---: |
| 62,872,470 | 852.101 | 1,130.644 | 278.431 / 335.149 | 1,781.673 | 206.309 |
| 62,872,471 | 1,585.428 | 1,863.879 | 271.094 / 349.830 | 2,531.622 | 215.039 |
| 62,872,472 | 2,326.535 | 2,603.397 | 302.218 / 283.932 | 3,226.036 | 221.373 |

两份头的 HTTP 分别在配对头耗时 147–157 ms（PublicNode）及 272–275 ms（官方）。
共用的官方 V4 路径先验证 PoolManager 运行时代码，再以一次 Multicall 取 74 个
子调用，包括 slot0、活动流动性、bitmap 与已初始化 tick，以及时间和费用凭证。
slot0 是合包内的一项，未单独观测其耗时。三对两臂到完整状态的 200/750/2,000 ms
覆盖都是 0/3、0/3、1/3，实际直接子头早于两臂就绪均为 3/3。种子状态 1,048.483 ms。
这是本应用层的 monotonic 时钟，窗口中两臂存在原定同源排队。

每个配对的 4/58/66 池两锚合计未闭合区间为 2/9/9，正模型候选为零。
整个配对范围没有自然正候选可冻结调用包络，整笔与直接子块净额均无分母。
策略研究的交易风险、真实成交和执行收益保持未实测。
[Base 原生 200 ms](https://www.base.org/blog/denim)

## 同头 V4 串行与重叠取数

15:38:23.261–15:38:26.815，另一个冻结范围见证 **62,873,641–62,873,643**。
对前两头，在双 HTTP 区块见证一致后，每臂执行同一固定哈希下的 runtime 和
74 个 V4 子调用。串行臂先验证代码再送状态，重叠臂在首个响应前允许另一
只读请求进队，两者均须在完整运行时代码和所有状态通过后才计作交付。
次序交叉，旧 250 ms 每源 pacing 保留。实际 15/15，PublicNode HTTP 2、
官方 HTTP 10、WebSocket 控制 3。两个头的每对运行时、V4 响应字节和重建
池状态摘要完全一致。

| 块 | 次序 | 串行臂内 ms | 重叠臂内 ms | 实际在途 overlap ms | 直接子头本地间隔 ms |
| --- | --- | ---: | ---: | ---: | ---: |
| 62,873,641 | 串行→重叠 | 539.696 | 658.649 | 7.784 | 185.847 |
| 62,873,642 | 重叠→串行 | 565.495 | 540.117 | 28.205 | 262.219 |

重叠臂分别慢 118.954 ms 和快 25.378 ms。收帧起算，两块串行交付分别
927.670 / 2,772.317 ms，重叠交付分别 1,586.453 / 2,206.700 ms。由于试验
在同一来源交叉跑两臂，这些含双源头、前臂和排队的实时时钟用于核对截止，
臂内数才是取数比较。两对都晚于直接子头，单源限速下没有稳定的端到端改善。
这一控制仅复算 8 个 V4 池状态，策略扫描和同包络模拟归入前一窗口分母。

## 留存与复算

可复用的 [Bloom helper](../../src/oasis_agent/evidence/log_bloom.py) 校验地址宽度、
256 字节 Bloom 及见证一致。现有 [常规 Base 归因复算 CLI](../../scripts/replay_base_shadow_survival.py)
新增 `--address-log-bloom`，从完整混合快照核验后的原始 HTTP 与 WebSocket 头给出
地址池可能与排除集合，并携带见证哈希。既有输出默认不附加该字段。四头常规
归档复验，可能 emitter 数为 2/1/0/0，四头均绑定三份原文。这里的实验 fixture
复算新范围的完整/增量原始字节和时间。生产实时混合入口未切换状态取得方式。

| 范围，均位于 `reports/research/` | 文件 | SHA-256 |
| --- | --- | --- |
| `base-incremental-state-ab-20261127` | `scope.json` | `7057d844ce80e4ef066ad6d14468e3aed8d978a921e338b643fd227548ac1643` |
| 原拒绝 | `62871662-logs.response` | `bef3431d8abc18c1652b2930e7850defdf432f531d830d89aaf7a1e7f08d794f` |
| 原拒绝 | `STOP.json` | `726cb1e549cfd08217319b622263c480726ba7ae1fb34f6c9c46062c74adf634` |
| `base-bloom-state-ab-20261127` | `scope.json` | `77ec8b3cef2823afbcfd174ccdb5fe764174f3758825782bcbb45cd5989b4a0d` |
| Bloom | `timings.json` | `7a99ef15073fbb8c23e0702b781f2a8c239636b323486a67bc3eafd15526cc41` |
| Bloom | `result.json` | `20676169ac9978367afd3faeb0fb86a0d0f839c5aa39ac7966b273f8c67f38d4` |
| 常规入口 | `normal-bloom-replay.json` | `2bd65343289505bfa5cdd1a31f8d556073dae01509378599678f0c22d2a9f9fb` |
| `base-v4-transport-overlap-20261127` | `scope.json` | `fdbea5bd5097ac8feb0fdccfbc1c353f0fefdac29a9bad48a292b391ce0e50af` |
| V4 | `result.json` | `9c4b218415541ba23357ab9449cdc4d9427286c68f000a5d2d626aa7ab50455a` |

```sh
set -o pipefail
root=reports/research
bloom="$root/base-bloom-state-ab-20261127"
v4="$root/base-v4-transport-overlap-20261127"
PYTHONPATH=. .venv/bin/python docs/verification/fixtures/replay_base_bloom_state_ab.py \
  replay "$bloom" | cmp - "$bloom/result.json"
PYTHONPATH=. .venv/bin/python docs/verification/fixtures/replay_base_v4_transport_overlap.py \
  replay "$v4" | cmp - "$v4/result.json"
.venv/bin/python -m scripts.replay_base_shadow_survival \
  "$root/base-latest-head-opportunity-20261127/run" --address-log-bloom \
  | cmp - "$bloom/normal-bloom-replay.json"
```
