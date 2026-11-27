# Base 上一块与同头整笔仿真对照

上海时间 11 月 28 日 01:04，Base 免费窗口收齐三个相邻 P／H／C 区块组。两个方向各用 **0.001 ETH** 跑完三池全路径。在六组可比结果中，两种规则的交易提议均为零，直接子块没有费后正例。这批固定路线内净省费用 **0 wei**，观察到的微利漏报为 **0 笔**，自然正例的召回和平均可省费用因分母为零仍未知。[逐状态数字与策略对照](BASE_PRIOR_HEAD_POLICY_METRICS_20261128.json)

## 冻结窗口与口径

冻结于 2026-11-27 17:03:59 UTC，代码基线为 `3c2111d`。首个和末个 WebSocket 头于 17:04:10.448 和 17:04:12.476 UTC 收到。用预先指定的第 1／5／9 个流头作为 P，并取顺序相邻的 H 和 C。其余两个流头也保留原文。官方 HTTP `eth_simulateV1` 的模拟父哈希与 PublicNode WebSocket 哈希逐一相符。PublicNode 的 HTTP 费用参数和资金读数按同一 canonical hash 固定。预检在开始前用两条来源核对链 ID、公开发送方的 nonce 和已双源核过的路由器运行码。[冻结输入及复算入口](fixtures/run_base_prior_head_policy.py) · [既有路由器实读](BASE_WHOLE_SURVIVAL_20261124.md)

两个方向为 WETH → USDC → AERO → WETH 及反向。各方向冻结真实本金、发收账户、路由器和三池、交易 value、nonce、deadline、gas 上限、两个费帽和最低回款。两方向的最低回款为 0.00095 WETH，同向高限对照另固定为 0.00105 WETH。每次模拟独立执行同一方向完整交易，校验全部值和四笔实际转账，含首池实际支付 0.001 WETH。对照组使用同一公开发送方和接收方，余额证据覆盖本金及费用上限。零签名、零广播、零状态覆盖和零资金支出。注意这些是先冻结的对照路线，覆盖范围只有三个池和两种方向。[Aerodrome Router](https://github.com/aerodrome-finance/contracts/blob/main/contracts/Router.sol)

A 在本地收到 H 后使用 P 的可用整笔结果决策，B 等待 H 的整笔结果。结果需在本地收到 C 前齐备且整数净额大于零，回滚或晚到都无提议。另保留即使忽略时延也会怎样决策。C 固定同一整笔信封估计首个直接子块末状态的反事实。用实际模拟 gas 乘模拟块的 base fee 与冻结 tip，叠加固定状态时 L1 大小上界和 operator fee 上界。本轮 operator 费上界为零。此处 P/H/C 都表示所读块末状态，`eth_simulateV1` 在对应状态上构建假想后继块。C 估计与本地到达时钟均只能作为立即后继的诊断，无法观察排序位置、实际入块、最终费用、失败统计参数或机会成本。[Base 费用规范](https://docs.base.org/specifications/transactions/network-fees)

## 逐头结果

| H 高度 | 方向 | P 净额，wei | H 净额，wei | C 净额，wei | P 结果距收到 H，ms | H 结果距收到 H，ms | C 距收到 H，ms |
| ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 63,043,375 | 正向 | −15,569,979,118,989 | −15,570,009,235,029 | −15,569,979,118,989 | 49.09 | 1,070.46 | 189.01 |
| 63,043,375 | 反向 | −22,375,346,110,407 | −22,375,376,230,887 | −22,375,346,110,407 | 321.22 | 1,380.14 | 189.01 |
| 63,043,379 | 正向 | −15,569,979,118,989 | −15,569,979,118,989 | −15,569,979,118,989 | 1,983.90 | 2,914.81 | 17.11 |
| 63,043,379 | 反向 | −22,375,346,110,407 | −22,375,346,110,407 | −22,375,346,110,407 | 2,243.56 | 3,211.19 | 17.11 |
| 63,043,383 | 正向 | −15,570,624,104,179 | −15,569,979,118,989 | −15,569,979,118,989 | 3,971.24 | 4,825.33 | 204.30 |
| 63,043,383 | 反向 | −22,375,991,190,687 | −22,375,346,110,407 | −22,375,346,110,407 | 4,240.55 | 5,106.02 | 204.30 |

A 的 P 结果到达 **1/6** 早于 C；B 的 H 结果到达 **0/6** 早于 C。单笔 H 模拟的在途时间为 278–458 ms，而 H 发出前已耗 612–4,828 ms。后者来自本次有界单源队列，包含先前 P／H／C 诊断及对照。即使取本窗最低在途时间，它也超过三个实测子头间隔中的最大值 204.30 ms。后续应让 P 使用已缓存的验证结果，并让 C 与负控离开实时 H 请求队列，再用同一时钟量化分开后的改善。

18 次经济方向的执行均成功，所测费后净额全负。高最低回款对照 **9/9** 在首池入金前以已核对的 `InsufficientOutputAmount()` 回滚，每次 114,023 gas，模拟网络费 **707,676,997,034–707,823,516,589 wei**。这是若强制发出该对照时的费用量级，经济对照不计这些人为触发的回滚费。真实 Base 执行器的收费空跑及回滚见历史观测。[Base 已入块费用样本](BASE_EXECUTOR_ATTEMPTS_20261128.md)

## 复算与验证

实际使用 30 次 Base 官方请求、11 次 PublicNode HTTP 请求和 3 次 WebSocket 逻辑预留，合计 **44 次，USD 0**，未重试。冻结范围 SHA-256 为 `7fdddfa633e7f637933a90a825fdd482f0380df470f8b4df5ddc9b0abd8f8bf6`。原始请求、响应字节、流、时钟、额度账本与冻结时的捕获程序在采集机 `reports/research/base-prior-head-policy-20261128/`。线下入口重放 FIFO 原始帧、HTTP 请求和行级对账，比对报告逐字节。运行态的经济对照由独立的策略复算入口核算。[策略结果复算](fixtures/replay_base_policy_window.py)

```sh
set -o pipefail
root=reports/research/base-prior-head-policy-20261128
PYTHONPATH=. .venv/bin/python docs/verification/fixtures/run_base_prior_head_policy.py replay "$root" |
  cmp - "$root/stdout.json"
```

下列两步从同一批原始帧和 HTTP 响应生成归一化输入，再用通用策略复算入口输出比较结果。程序把每个路线和 H 绑定到冻结交易摘要、严格相邻的父子哈希和同一单调时钟。费用与微利计数按固定路线和负控分别汇总。

```sh
set -o pipefail
root=reports/research/base-prior-head-policy-20261128
PYTHONPATH=. .venv/bin/python docs/verification/fixtures/replay_base_policy_window.py "$root" > "$root/normalized.json"
.venv/bin/python -m scripts.replay_base_shadow_policy "$root/normalized.json" |
  jq -S . | cmp - docs/verification/BASE_PRIOR_HEAD_POLICY_METRICS_20261128.json
```

补充的八头自然搜索见独立报告。[自然报价及覆盖](BASE_PRIOR_HEAD_POLICY_NATURAL_20261128.md)
