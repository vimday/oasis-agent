# Base 实时发现、最新头调度与三时点负控

北京时间 2026-11-27。两个免费实时影子窗口均同时复算四池、58 个地址池和加入 8 个 V4
池后的 66 池。两窗口共保留 32 个 WebSocket 头，12 头完成状态和 Rust 双图复算。
12 头均无模型正候选，实际候选的整笔模拟和同包络后继分母均为零。另一个独立窗口的
固定三跳负控在三个真实状态上整笔仿真通过，计入可观察费用后均为负。
[前次连续发现和两状态负控](BASE_WHOLE_SURVIVAL_20261124.md)

## 实时影子

两个窗口各预注册 35 秒墙钟上限、USD 0、匿名 PublicNode WebSocket、PublicNode HTTP
及 Base 官方 HTTP。每次 RPC 先占用原账本额度，遇首次失败停止。CEX 侧使用本地合成
夹具，只将链上模型纳入结果。组合状态要求 WebSocket 见证、两 HTTP 源头一致和全部
子调用完成。地址池及 V4 均受已留存名单、固定 L1/L2 模型成本及 CL tick 覆盖限制。

| 北京时间窗口 | 保留头 | 完整状态 | 4/58/66 池模型正候选 | 原账本实际 / 上限 |
| --- | --- | --- | --- | ---: |
| 09:36:08.845–09:36:16.815 | 8 个连续头，62,764,970–977 | 8 | 0 / 0 / 0 | 43 / 49 |
| 09:41:41.044–09:41:47.351 | 24 个连续头，62,766,630–653 | 4 | 0 / 0 / 0 | 23 / 29 |

顺序臂使用 3 个 WebSocket 控制额度、16 次 PublicNode HTTP、24 次官方 HTTP。
最新臂分别为 3、8、12。两个窗口均无源拒绝或重试。顺序臂 4 池每头有 4 个未闭合定量
区间、58 池每头 58 个、66 池首 5 头各 59 个及末 3 头各 58 个。最新臂每个扫描头分别
为 4、52、52。资金规模、逐腿精确报价、整笔净额与后继存续的正候选分母均缺失。

| 调度 | 块 | 入队至取数开始，ms | 收帧至状态，ms | 收帧至发现完成，ms |
| --- | ---: | ---: | ---: | ---: |
| 顺序 | 62,764,970 | 1.258 | 1,048.059 | 1,144.969 |
| 顺序 | 62,764,971 | 923.263 | 1,720.543 | 1,766.462 |
| 顺序 | 62,764,972 | 1,602.906 | 2,398.530 | 2,440.785 |
| 顺序 | 62,764,973 | 2,243.618 | 3,027.015 | 3,066.741 |
| 顺序 | 62,764,974 | 2,893.422 | 3,685.691 | 3,731.414 |
| 顺序 | 62,764,975 | 3,497.446 | 4,352.426 | 4,394.430 |
| 顺序 | 62,764,976 | 4,211.479 | 5,005.230 | 5,046.004 |
| 顺序 | 62,764,977 | 4,863.702 | 5,662.638 | 5,704.371 |
| 最新 | 62,766,630 | 3.830 | 957.753 | 1,004.654 |
| 最新 | 62,766,635 | 21.790 | 821.680 | 854.669 |
| 最新 | 62,766,639 | 87.492 | 1,313.784 | 1,454.536 |
| 最新 | 62,766,646 | 77.599 | 1,459.570 | 1,501.748 |

顺序臂当场见证 7 对直接父子，状态和发现赶上直接子头均为 0/7。最后一头的直接子未纳入
冻结窗口。最新臂的 4 个扫描头都在原始 WebSocket 窗口见到直接子，状态及发现赶上各为
0/4。其发现晚于对应直接子收帧 675.780–1,292.665 ms。最新臂把积压转成 13 个被更新头
替代和 7 个窗口结束未派发的覆盖缺口。短排队仍伴随约 0.82–1.46 秒的完整状态交付。
两个窗口出自不同高度，延时是描述性现场读数；子头到达以本地单调时钟为准。Base 主网
Denim 的原生出块间隔为 200 ms。[Base Denim](https://www.base.org/blog/denim)

常规入口使用 `--base-newheads --base-newheads-overlap-headers --base-newheads-max-observed 24`
启用最新头调度。收到头时先保留原帧；扫描空闲时取此刻队列内最新的已见证头。原始流
父子冲突仍停止。因调度跳过状态时，Rust resident 对照按新完整快照重建。调度副本包含
每帧收取、派发、替代或窗口结束未派发，摘要绑定其哈希。离线审计单独显示状态快照间的
后继关系与原始帧里的直接后继时限，并把调度器与状态交付共用的原始时钟重新核对。
[常规入口](../../scripts/run_multicall_100tick_campaign.py) ·
[调度和重放](../../src/oasis_agent/evidence/base_newheads.py) ·
[归因审计](../../scripts/replay_base_shadow_survival.py)

## 同包络三时点负控

单独冻结 09:47:29.935–09:47:33.834 实时范围。实际使用官方 HTTP 11 次及 PublicNode
WebSocket 连接/控制 3 次，共 14/14，USD 0。固定路由 WETH → USDC → AERO → WETH，
三腿为 Aerodrome Volatile。发送者取已留存的公开历史地址，投入 **1,000,000,000,000,000
wei 原生 ETH**，接收资产为 WETH，最低输出 950,000,000,000,000 wei，gas cap 550,000，
费率上限 250,000,000 wei/gas，tip 1,000,000 wei/gas。采用前置读取的 nonce `0x717272`、
空访问列表、H 区块秒时间戳 +60 的 deadline，三个状态下合同地址、发送方、收款方、calldata、
金额和费用参数完全相同。无需签名，模拟启用严格校验，原始请求无状态或区块覆盖。
[Aerodrome Router](https://github.com/aerodrome-finance/contracts/blob/main/contracts/Router.sol) ·
[Base 模拟接口](https://docs.base.org/base-chain/api-reference/ethereum-json-rpc-api/eth_simulateV1)

H 和 H+1 首先由 PublicNode 连续两帧见证，再各自以独立的 Base 官方完整头验证。H 整笔
响应收到后 6.486 ms 发出官方 `latest` 请求，366.609 ms 收到头，头在 H 的高度 +11。
精确到整笔回报那一瞬的其他本地头没有采样，回报后首次官方观测的 latest 只有该源见证。
每次整笔模拟回报都检查返回虚拟块编号及父哈希，以绑定实际模拟基准。H+1 独立开始，整笔
回复早于 H。H 到实际子头收帧仅 231.035 ms。

| 实际基准状态 | 62,768,376（H） | 62,768,377（H+1） | 62,768,387（H 模拟回报后首次官方 latest） |
| --- | ---: | ---: | ---: |
| USDC 各腿整数输出，6 decimals | 1,697,446 | 1,697,446 | 1,697,544 |
| AERO 各腿整数输出，18 decimals | 4,178,467,362,287,574,192 | 4,178,467,362,287,574,192 | 4,178,709,326,662,369,048 |
| WETH 收款净入账，wei | 980,949,693,851,774 | 980,949,693,851,774 | 981,006,497,779,545 |
| 扣费前差额，wei | −19,050,306,148,226 | −19,050,306,148,226 | −18,993,502,220,455 |
| 整笔模拟 gas | 432,471 | 432,471 | 432,471 |
| 模拟 L2 费用，wei | 2,594,826,000,000 | 2,594,826,000,000 | 2,594,826,000,000 |
| L1 data fee 上界，wei | 2,411,012,494 | 2,411,012,494 | 2,411,012,494 |
| Operator fee（按 gas cap 查询），wei | 0 | 0 | 0 |
| 按上述费用的情景净额，wei | **−21,647,543,160,720** | **−21,647,543,160,720** | **−21,590,739,232,949** |
| 本地见证至报价 / 整笔回复，ms | 1,375.567 / 1,896.217 | 907.810 / 1,401.112 | 368.328 / 734.356 |

时间从 H/H+1 的 WebSocket 收帧或第三列官方头回复起算。所有三腿的 Router 报价与整笔
返回的整数一致，最后一腿也与规范 WETH Transfer 的收款净入账一致。价格已包含池费用，
L2 以模拟用量及虚拟块 base fee 加固定 tip 计算，L1 上界按冻结 unsigned type-2 包络的
实际字节长度查询。路线使用自有资金假设，没有 flash 借款，不估链下付款、库存机会成本或
实际竞争成功率。模拟没有链上扣款；该路线在三时点的毛差已经为负，并且预先固定为负控。
[Base 费用规范](https://docs.base.org/specifications/transactions/network-fees)

| 状态 | 哈希 |
| --- | --- |
| H | `0xb539dd156cf72ffe6f1ef6e0b9e1a8e2c54546983967bfaaf35b3b838c0d1d76` |
| H+1 | `0xbfcc326aa9acb4834d9ed1a4e06dc91f1b5302d654f36b8ec1b2da3147b1486d` |
| 回报后官方 latest | `0x084219c4ee92a5ea4eeeb2c284d590988b0bfffe40ac334408542d3a18c6129b` |

## 复算与原始文件

三个范围各自保留忽略目录的预注册、原始请求及响应、源额度、开始和结束、执行状态。
以下为 SHA-256。文档内的相对目录基于 `reports/research/`。

| 范围 | 文件 | SHA-256 |
| --- | --- | --- |
| `base-realtime-opportunity-20261127` | `preregistered.json` | `ef2f2c39b76dffd2d625436548bea2490dbc3291f6b6f7c70533e48b8e5320db` |
| 顺序臂 | `run/shadow_attribution.jsonl` | `943be4319024b0669aa5b53f4d3bf6a7499e964516062963a10a97965a5f0808` |
| 顺序臂 | `arrival-deadlines.json` | `892f7481dc8e7964787fe22efbe680943a02d4b63d7c1310f16043d70d256571` |
| `base-latest-head-opportunity-20261127` | `preregistered.json` | `0ba5413258654c77e86087b69d22c19259a2e41e5c0b0659defaa801cfd1c687` |
| 最新臂 | `run/base_newheads/schedule.json` | `010bc278fa8eda0b7c3033796782096560b6ef37a30376994b17e194bde843e5` |
| 最新臂 | `run/shadow_attribution.jsonl` | `7a01d9d49943ba365a40ba4ae6f47cfce753990f8ff47e1c23c59eb77c24ffd7` |
| 最新臂 | `arrival-deadlines.json` | `38e6f33b6045fd829b48bab8c594f49f4fbaf45f63ffc192fc21fb1037dc9693` |
| `base-live-envelope-control-20261127` | `scope.json` | `8f4e7829bb21067b1b6cc21a5501fad5d68dcbbf480b3f6b60a75eaa70067e55` |
| 负控 | `run/envelope.json` | `1c1c369514bf5b8dddf5a2009c43d1775fdcd07537afb11492f4b4b53bf1dce5` |
| 负控 | `report.json` | `42feef181c6227c48d6b1ac8357714b7c48dda078db884f92fa899150a26e86c` |
| 负控 | `verified.txt` | `d84f991b93b50aa945457b163d071e4adc29015f3da08d8b2f15818722a88c98` |

```sh
set -eo pipefail
root=reports/research
for name in base-realtime-opportunity-20261127 base-latest-head-opportunity-20261127; do
  .venv/bin/python -m scripts.replay_base_shadow_survival \
    "$root/$name/run" --arrival-deadlines | cmp - "$root/$name/arrival-deadlines.json"
done
scope="$root/base-live-envelope-control-20261127"
PYTHONPATH=. .venv/bin/python "$scope/verify.py" | cmp - "$scope/verified.txt"
```
