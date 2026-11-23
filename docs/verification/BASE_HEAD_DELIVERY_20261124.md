# Base 同源 newHeads 和轮询头部交付

2026-11-24 北京时间 01:03:03.249 起，用同一进程的单调时钟观察 3 秒。PublicNode 的
WebSocket `eth_subscribe("newHeads")` 与同来源的 HTTPS `eth_getBlockByNumber("latest")`
在订阅确认后同时观察。轮询预定六个 500 ms 槽，全部完成。关窗后在 Base 官方 RPC 按高度
逐块读共同区间，验证区块哈希、父哈希、状态根、时间和相邻父子关系。[采集和离线入口](../../scripts/compare_base_head_delivery.py)

冻结计划允许一次 WebSocket 连接、两条 WebSocket RPC、七条 PublicNode HTTPS RPC、
最多十七条 Base 官方 HTTPS RPC。实际耗用 **24/27** 个账本预留，分别为 **3 / 7 / 14**。
三次链 ID 检查均为 8453，固定高度查证 13 次。匿名公共端点费用 USD 0。计划保留首个失败，
没有重试。本轮没有失败或重复头。[此前固定哈希状态交付对照](BASE_STATE_DELIVERY_AB_20261123.md)

| 共同区间 | WebSocket newHeads | 500 ms 轮询 |
| --- | ---: | ---: |
| 样本全部可用头部 | 15 | 6 |
| 61,315,038–61,315,050，共 13 个官方核验高度 | 13 | 6 |
| 同一臂观测到两端的直接父子关系，共 12 对 | 12 | 0 |
| 共同区间漏块 | 0 | 7 |
| 所有头部含 Base 毫秒时间戳 | 15/15 | 6/6 |

六个共同高度均通过精确哈希配对。下表是两臂接收时刻的差值，正数表示本机先完成
WebSocket 消息接收。六个差值的中位数是 **82.958 ms**，最大值 **240.246 ms**。共同区间以两臂
首次与末次所见高度的交集定义，两条额外的 WebSocket 尾部观测留在原始证据中。

| 高度 | 轮询减 newHeads，ms |
| ---: | ---: |
| 61,315,038 | 105.993 |
| 61,315,041 | 46.568 |
| 61,315,043 | 59.924 |
| 61,315,046 | 14.880 |
| 61,315,048 | 160.624 |
| 61,315,050 | 240.246 |

本轮数据覆盖 PublicNode 的头部交付与 Base 官方区块身份复核。66 池取数、路径优化、整笔模拟
及交易结果仍需独立计时。短窗口证明的是本次共同区间的覆盖与时差。

## 离线复算

忽略目录 `reports/research/base-head-delivery-20261124/` 留存预注册文件和 `run/` 中的
冻结计划、原始 SQLite 账本、原始 HTTP 与 WebSocket 字节和本机时钟。预注册 SHA-256
为 `4b41ec3796b857d9c8852da00f5910eeb29ca0c6983af4704652f375fb341d8e`。
原始 SQLite 证据 SHA-256 为
`19a90974912d2d2780517d873cf987d7c5f5ddb2429ae59f9e499c6dba521a1e`。
摘要 SHA-256 为 `388043312d638682bc22c3b497232c6d8cbd41620d3749740b80263e07ae5906`。

```sh
.venv/bin/python -m scripts.compare_base_head_delivery replay \
  --out reports/research/base-head-delivery-20261124/run
```

回放返回 0，重建摘要与保留字节一致。四项离线回归覆盖同哈希配对、漏块和直接父子统计、
元数据篡改、首个 HTTP 429 后停止及保留失败响应、同高哈希冲突和相邻父哈希冲突。
[测试](../../tests/unit/test_base_head_compare.py)

## 已见证区块的同哈希历史状态复算

共同区间的最后一个块 **61,315,050** 在北京时间 01:03:05.650 由 `newHeads` 收到。
01:08:15.571，独立冻结范围对该块读取历史状态。两家来源按高度确认原头的哈希、父哈希、
状态根和时间戳，随后以 `requireCanonical: true` 按同一哈希读取池状态和 PoolManager
运行码。共完成 **2 次 PublicNode 与 3 次 Base 官方免费请求**，均成功。

复算从同一地址池快照取出四池对照，并合并八个 V4 池。4、58、66 池的 WETH/USDC
三跳搜索分别覆盖 **12/12、1,774/1,596、1,940/1,776** 条路线，逐组通过 Rust
legacy/resident 对照。固定 L1/L2 成本模型下没有正候选，三组分别仍有 **2/2、41/41、
41/41** 个定量未闭合区间。整笔模拟数为零。历史读请求启动至完整解码用时 **1,463.058 ms**。
该耗时来自头部收到约五分钟后的读取。实时样本见下一节。

预注册、执行脚本、源请求与响应、原始账本以及本轮结果留存于忽略目录
`reports/research/base-witnessed-head-state-20261124/`。预注册 SHA-256 为
`f480d95a22e91746b36d0f3e0ab0a7de7b1ae36d3deff285c9e72290fede690f`，状态清单
SHA-256 为 `ea63803f0b3cada74e7656d12baf98bd9b8e336a65f97874ce00fcc770011ea1`。
常规混合回放返回 0，重建 58 个地址池与 8 个 V4 池。

```sh
.venv/bin/python scripts/run_multicall_100tick_campaign.py --replay-mixed-base \
  reports/research/base-witnessed-head-state-20261124/run/attempt-00000001/manifest.json
```

## 单个实时新头驱动同哈希状态

北京时间 01:12:35.646，PublicNode WebSocket 在链 ID 和订阅确认后交付的首个头为
**61,317,900**，哈希 `0xe008f8be5b18dd2a37fd8b3f8f344434cacd4a389e85211b8bdd23a1134fcd19`。
驱动把该帧的单调接收时刻传给同进程状态流程，并在连接打开期间完成读取。两个来源按高度
各确认哈希、父哈希、状态根和时间戳，随后只对该哈希发 `requireCanonical: true` 查询。
冻结范围只读一个头，实际耗用 **8/8** 个免费预留，其中一次 WebSocket 连接、两条控制 RPC、
两次 PublicNode HTTPS 和三次 Base 官方 HTTPS。全部成功。

| 从应用收到 WebSocket 帧起 | 用时，ms |
| --- | ---: |
| PublicNode 区块头已接收 | 253.476 |
| 58 个地址池响应已接收 | 531.935 |
| Base 官方区块头已接收 | 632.318 |
| PoolManager 运行码响应已接收 | 892.550 |
| 八个 V4 池响应已接收 | 1,184.899 |
| 66 池完整状态就绪 | **1,188.130** |
| 4、58、66 池三组 Rust 搜索和 legacy/resident 对照完成 | **1,199.246** |

地址池传输和官方块头传输重叠 **265.578 ms**。三组的 WETH/USDC 路线数分别为
**12/12、1,774/1,596、1,940/1,776**，固定 L1/L2 模型正候选均为零，定量未闭合区间
分别为 **1/1、3/3、3/3**。单个样本在 750 ms 后取得完整状态。时钟止于状态及模型搜索，
整笔模拟数为零。

状态就绪至完成全部三组对比只占 **11.116 ms**，约为该样本总用时的 **0.93%**。
即使消除这段计算，750 ms 门槛仍超出 **438.130 ms**。本样本的后半段由官方块头、
PoolManager 运行码与 V4 状态顺序响应主导。下一轮在同样的独立来源和状态发布校验下，
对照固定哈希的运行码及 V4 读取能节约多少等待时间，再依据整笔复核的及时率衡量节点或 GPU 增量。

预注册、驱动、两本原始账本及两个来源的请求字节与应答留在忽略目录
`reports/research/base-live-head-state-20261124/`。预注册 SHA-256 为
`0a9d438188ceba4b28dd6c1c58846e6bb204560723d66dda8af6b1f3d74e6de2`，
WebSocket 请求、帧和接收时钟的 SQLite 证据 SHA-256 为
`19f81c2adbf5fd83c12030091fdd782676794d5b77952411547bb7115116a446`，
混合状态清单 SHA-256 为
`356938d41a1b90ec6ea89f5698ccbbeb82d32fea0d3385b9afffddf771e13d1a`。
同一时钟的离线回放及常规混合状态回放均返回 0。

```sh
PYTHONPATH=. .venv/bin/python \
  reports/research/base-live-head-state-20261124/live_head.py replay
.venv/bin/python scripts/run_multicall_100tick_campaign.py --replay-mixed-base \
  reports/research/base-live-head-state-20261124/run/mixed/attempt-00000001/manifest.json
```
