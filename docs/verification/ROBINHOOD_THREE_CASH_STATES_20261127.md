# Robinhood 首个完成三态独立回款核对的前瞻窗口

北京时间 2026-11-27 15:30，链 ID 4663。对固定的 **0.0001 ETH** 两跳负控，官方和 PublicNode 在三个状态各完成一次原 EOA 包络模拟、一次完整 gas 估计和一次独立 ETH 退款核对。两源三态的退款都是 **100,021,353,709,798 wei**。毛差只有 21,353,709,798 wei，低于约 2.81 万亿 wei 的估计网络成本。三态的交易发送者 nonce 均为 84,644，可见原生币余额均为 47,394,516,545,374,728 wei，覆盖本金与冻结的最坏 gas 上限。该批 24 个路线/状态组合得到 21 个有限报价，3 个回滚，通过最低费用筛选的自然候选为零。[三态逐源证据](ROBINHOOD_THREE_CASH_STATES_20261127.json)

| 按 ETH wei 计 | 发现块 H | 首次完整模拟及估费返回时的主 feed 块 K | H 的直接子块 C |
| --- | ---: | ---: | ---: |
| 高度 | 124,085,182 | 124,085,209 | 124,085,183 |
| 完整包络在两个来源 | 均成功 | 均成功 | 均成功 |
| 独立退款，含本金 | 100,021,353,709,798 | 100,021,353,709,798 | 100,021,353,709,798 |
| 基础费，wei/gas | 20,006,000 | 20,000,000 | 20,028,000 |
| 双源 gas 估计 | 140,507 | 140,507 | 140,507 |
| 估计网络成本 | 2,810,983,042,000 | 2,810,140,000,000 | 2,814,074,196,000 |
| **退款减本金及估计网络成本** | **−2,789,629,332,202** | **−2,788,786,290,202** | **−2,792,720,486,202** |

完整包络固定 EOA、nonce、路由、gas 上限、基础费用上限、零优先费、deadline、本金和最低输出。三态均验证费率上限、期限、nonce 和原生币库存。回款读数来自独立的只读构造器，内部 Router calldata 与本金保持相同，Router 看到的调用人改为临时构造器。构造器把实收 ETH 退给原 EOA，各读数与对应状态的 V4Quoter 整路输出完全一致。顶层原 EOA 包络本身只返回空字节，对该原包络的直接余额真值仍标为未知。估费使用原 EOA 包络，按 `eth_estimateGas × baseFee` 计算，结果是模拟条件下的估计，未产生实际网络费用。[探针源码](fixtures/RobinhoodNativeCashProbe.sol) · [Arbitrum 估费方式](https://docs.arbitrum.io/arbitrum-essentials/how-to-estimate-gas)

**观测时钟。** 收到 H 主流消息后预留 900 ms 等 RPC 发布；双源完整报价在收帧后 2.912 秒到齐。报价返回至包络冻结为 10.112 ms，至首个整笔模拟和估费全部返回为 597.901 ms。用实际接收时钟截取 K，并由下一个已到达的主流消息夹住截止时刻。K 比 H 高 27 块。随后先回查直接子块，再回查已冻结的 K。后续回查耗时没有倒填至发现时钟。主、延迟备用 feed 各取得 136 个不同序号，其中 132 个同哈希配对，主流内部缺号为零。[官方 sequencer 配置](https://docs.robinhood.com/chain/run-a-full-node/)

**长路的状态条件。** 固定的 20 跳在 H 的 0.0001 ETH 投入产生 −2,828,245,641,861 wei 毛差。24 跳在 H/K/C 都返回 `UnexpectedRevertBytes(NotEnoughLiquidity(poolId))`，归于第四腿 `0xb7ef…05c1`。这个池在上午的同本金 24 跳路径可以完整报价。两次 H 读到的活跃流动性同为 23,714,791,908,981,046,884，tick 却由 116,024 变为 115,299。两窗对照足以拒绝以活跃流动性单个字段缓存“整路可成交”的判断；当时进入第四腿的实际中间数量及跨 tick 可用深度仍需分段重放。[上午的同路线对照](ROBINHOOD_ROUTER_SHADOW_20261127.md)

**旧子块和预算。** 先对上午留存的子块 123,899,188，按此前 H/K 的相同构造器参数向新的匿名 Triport 来源仅作一次哈希固定读取。服务返回 HTTP 400、RPC −32602，原文为 “invalid or unsupported block parameters”。旧 C 的回款保持未知，拒绝原因不足以判定其归档深度。此后另起新市场窗口，在独立预注册上限内各使用官方、PublicNode 25/30 次只读请求。合计 51 次免费 HTTP RPC、两个免费 WebSocket 连接，旧账本保留。没有签名、广播或充值。[Triport 匿名端点](https://triport.io/docs/robinhood/public)

原始请求、响应、时钟与账本保留于忽略目录。复算覆盖双 feed 帧与独立块头承诺、H 至 C 父子关系、K 的收帧边界、路由选择、冻结包络、状态相关资金与 nonce、全路报价和第四腿回滚、两源整笔调用/估费及 ETH 退款。[复算程序](fixtures/replay_robinhood_three_cash_states.py)

```sh
set -o pipefail
root=reports/research/robinhood-cash-three-state-20261127
sources=reports/research/multichain-forward-20261122/robinhood/sources
PYTHONPATH=. .venv/bin/python docs/verification/fixtures/replay_robinhood_three_cash_states.py "$root" --sources-dir "$sources" |
  cmp - docs/verification/ROBINHOOD_THREE_CASH_STATES_20261127.json
```

## 15:42 后续分段核对：第四池能吃下多少 ETH

原窗口收尾后，固定同一条 24 跳路径和 100,000,000,000,000 wei 本金，分别报价前 1、2、3、4 腿和全路；再将第三腿输出的全部 ETH 作为第四腿的唯一输入。先向官方源回查固定哈希，再预注册一个新 sequencer 窗口，对首个有交易的主流区块使用官方和 PublicNode 两源核对。两源在新窗口的所有分段与档位响应逐字节一致。[分段证据、档位与原始响应哈希](ROBINHOOD_FOURTH_LEG_CAPACITY_20261127.json)

| 路径与方向相同 | 上午 H，123,899,187 | 下午 H，124,085,182 | 新窗口，124,091,614 |
| --- | ---: | ---: | ---: |
| 第一腿输出，代币 `0x020b…18b4` 原子单位，18 位 | 未知 | 3,418,053,365,573,764,899 | 3,484,680,483,239,666,790 |
| 第二腿输出，代币 `0x5fc5…d168` 原子单位，6 位 | 未知 | 172,248 | 170,933 |
| 第三腿输出即第四腿输入，ETH wei | 未知 | 99,381,415,795,914 | 98,962,916,720,681 |
| 以原中间量单独报价第四腿 | 未知 | 第四池流动性不足 | 第四池流动性不足 |
| 前四腿 / 全 24 腿报价 | 未知 / 成功 | 两者均指向第四池 | 两者均指向第四池 |
| 到最近下方 tick 网格线所需含费 ETH，wei | 379,342,265,630,532 | 689,656,662 | 689,656,662 |
| 第四腿独立输入档位 | 未测 | 10 档全拒绝，最小为 10,000,000,000 wei | 16 档，最高成功为 500,000,000 wei，最低拒绝为 1,000,000,000 wei |

下午与新窗的第四池状态 `sqrtPriceX96`、tick、费用和活跃流动性完全一致。两次上游产出的 ETH 不同，各次都在第四腿复现同一 `NotEnoughLiquidity(poolId)`。新窗用 500,000,000 wei 输入时能换出 50,250,136,989,643 个目标代币原子单位，同一方向在 1,000,000,000 wei 时已不能全额成交。接近 0.0001 ETH 的路线需求比该区间大五个数量级。孤立报价只度量这一腿的容量，未生成新的整笔净利观测。[新窗区块与逐档输出](ROBINHOOD_FOURTH_LEG_CAPACITY_20261127.json)

按 `zeroForOne` 方向、tickSpacing 207、含协议费的总费率 11,343 ppm 进行整数复算。取现价至最近下方网格线的 token0 数量并向上舍入，再补齐费用。新窗抵达 tick 115,299 所需毛输入为 689,656,662 wei。链上该 tick 的 `liquidityNet` 与当前活跃流动性都为 23,714,791,908,981,046,884，下穿后的紧邻区间流动性为零。上午到最近网格线所需毛输入约为下午的 550,045 倍。更远的已初始化区间需要另行遍历；上述金额只描述抵达第一条网格线的用量。现有两档实测把新窗正输出可报价边界夹在 500,000,000 与 1,000,000,000 wei 之间，尚未逐 wei 找到极限。[Uniswap v4 SwapMath](https://github.com/Uniswap/v4-core/blob/main/src/libraries/SwapMath.sol) · [跨 tick 更新规则](https://github.com/Uniswap/v4-core/blob/main/src/libraries/Pool.sol)

影子筛选可以把逐腿的真实整数输入、方向、价格和下一段初始化深度一同缓存。遇到超出首段容量的输入，先读取后继 tick 深度或直接用该中间量做单腿报价，再决定是否分配整路模拟算力。这个例子也给出对照：现价附近的活跃流动性整个时段相同，单靠它无法预测固定规模的全额成交。下一次纳入更多池子时，可记录这种按方向和规模的筛选省去了多少完整报价，同时抽查被筛掉路线，测漏报率。

上午 H 再读时官方源返回 RPC −32000，明确指出历史状态不可用，按首次拒绝停止；上午表中仅使用既有整路双源报价和池状态。下午归档与新窗口分别在原账本追加三、六次免费只读请求，含该次失败。官方累计 30/30，PublicNode 29/30。独立探针三态估值和原窗口的双源 25/30 计数仍对应各自截止点。[分段离线复算](fixtures/replay_robinhood_fourth_capacity.py)

```sh
set -o pipefail
root=reports/research/robinhood-cash-three-state-20261127/fourth-capacity
sources=reports/research/multichain-forward-20261122/robinhood/sources
PYTHONPATH=. .venv/bin/python docs/verification/fixtures/replay_robinhood_fourth_capacity.py "$root" --sources-dir "$sources" |
  cmp - docs/verification/ROBINHOOD_FOURTH_LEG_CAPACITY_20261127.json
```
