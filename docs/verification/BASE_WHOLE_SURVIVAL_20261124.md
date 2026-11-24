# Base 连续发现与同包络整笔负控

北京时间 2026-11-24。连续发现使用常规 `--base-newheads` 的 4/58/66 池影子入口。
整笔负控使用另一组已留存的两个直接父子块，通过免费 RPC 执行发现报价与相同包络的
`eth_simulateV1`。发现窗口、固定负控和历史落块分别保留各自分母。两个范围均为只读。
[并行取数 A/B 与连续采集](BASE_DUAL_HEADER_OVERLAP_20261124.md) · [Base 模拟接口](https://docs.base.org/base-chain/api-reference/ethereum-json-rpc-api/eth_simulateV1)

## 连续采集与缺测分母

| 区块 | 取数开始前排队，ms | 66 池状态就绪，ms | 常规发现完成，ms | 模型正候选，4/58/66 池 |
| ---: | ---: | ---: | ---: | ---: |
| 61,476,050 | 14.610 | 1,094.525 | 1,176.241 | 0/0/0 |
| 61,476,051 | 966.150 | 1,876.423 | 1,954.393 | 0/0/0 |
| 61,476,052 | 1,751.018 | 2,573.034 | 2,646.199 | 0/0/0 |
| 61,476,053 | 2,475.348 | 3,296.758 | 3,361.498 | 0/0/0 |

时间起于同一采集进程的 WebSocket 应用层接收。四块连续，三对实际直接子块。范围
用满 23 个免费预留，4/4 状态复算均通过。66 池每块仍有 36 个尚未闭合的定量区间。
正候选整笔仿真数和同包络直接后继仿真数均为零，正净额存续率没有分母。并行取数在
独立同头 A/B 两块分别缩短 715.190 和 129.573 ms，四块连续臂后段的主要等待已经
发生在上一个块处理完之前。
[状态交付和覆盖](BASE_DUAL_HEADER_OVERLAP_20261124.md)

新增离线审计核对归因哈希链、总结、对应状态清单、接收时钟及重放块身份。逐块输出三组
模型候选数、金额未决区间、正常排名、首次到状态与发现时间、直接子块与缺块状态。
上一块固定路线模型复价保留原字段，整笔模拟和同包络后继为独立的缺测计数。
[正常归因审计入口](../../scripts/replay_base_shadow_survival.py)

以原留存状态对 144 个未决区间分别补查 33 个固定整数金额，共 4,752 个试算点。236 个能
按原模型报出整条路，均扣费为负。WETH 路最接近盈亏线的是 −3,542,217,466,529 wei，
USDC 路为 −7,115 个最小单位。其余 4,516 个点受留存的流动性报价范围约束，无法获得
完整路径报价。同一路线在不同区块分别计数。扩大同哈希 tick 覆盖后才能缩小这些未决区间。

## 两个已核状态的三跳整笔负控

冻结 **61,475,576 → 61,475,577**、投入 0.001 原生 ETH、Aerodrome Volatile 三池路线
WETH → USDC → AERO → WETH、0.00095 WETH 最低输出和调用参数。发送者是已留存
历史交易的公开地址，同一地址接收 WETH，未加载任何账户密钥。两源核对 Router 字节码、
`getAmountsOut` 各腿整数、三个实体池、发送地址余额和固定费用 getter。按对应区块数字
调用 Base 官方 `eth_simulateV1`，验证结果的 `parentHash` 和 `number` 绑定预选状态。
两个输入状态使用同一 calldata、金额、最低输出、gas cap、费率参数和 deadline，模拟走
下一虚拟块执行环境。源方法使用真实库存，`validation=true`，没有合约、余额或区块覆盖。
[Aerodrome Router](https://github.com/aerodrome-finance/contracts/blob/main/contracts/Router.sol)

| 指标，除标明外均为整数 wei | H | H+1 |
| --- | ---: | ---: |
| 双源发现毛差 | −25,450,128,614,859 | −25,450,128,614,859 |
| 发现净额，以冻结 gas 上限和费率上限计算 | −162,968,115,954,001 | −162,968,115,954,001 |
| WETH 模拟净入账 | 974,549,871,385,141 | 974,549,871,385,141 |
| 模拟 gas 用量，gas | 432,471 | 432,471 |
| L2 执行费，含固定 tip | 2,596,049,892,930 | 2,595,183,221,046 |
| L1 data fee 上界 | 17,987,339,142 | 17,987,339,142 |
| 状态的 operator fee | 0 | 0 |
| 取 L1 上界的整笔情景净额 | **−28,064,165,846,931** | **−28,063,299,175,047** |
| 发现双源完成至官方模拟回复，ms | 294.300 | 285.961 |

完整输入和逐腿返回均重现同块双源报价。WETH 的规范 Transfer 净额与最后一腿返回一致。
成本按模拟 gas 和虚拟块 base fee 加冻结 tip、固定块的 L1 数据费上界及 operator fee 推算。
没有使用模拟者的原生币余额差作为 gas 证据。L1 上界由 oracle 按 unsigned type-2 长度
计算，nonce 给出长度上界。该净额是无其他融资、链下付款或库存成本的实验情景，两个
模拟没有发生链上扣款。计时起于两源报价完成，本次负控是在状态归档后单独执行，
所以连续发现的实时时钟只见前表。[Base 费用](https://docs.base.org/specifications/transactions/network-fees) · [OP Stack operator fee](https://specs.optimism.io/protocol/isthmus/predeploys.html#gaspriceoracle)

固定对照耗用原账本 12/12 次，官方 8、PublicNode 4，费用 USD 0。留存的每次请求、
响应、HTTP 元数据和接收时钟可单独审计。正式成功归因还需要连续窗口内出现正候选，
以及整笔模拟和下一状态的覆盖。本轮只有负对照证明链路可通。

## 复算

四头归因输出、负控程序、冻结方案、原始账本和结果位于忽略目录
`reports/research/base-whole-survival-20261124/`。SHA-256 如下。

| 文件 | SHA-256 |
| --- | --- |
| `continuous-shadow.json` | `bde0d02ac5816b6fe866a1d4b0a2e83b933e47157c8cfd86505a0164191c2af8` |
| `residual-grid.json` | `3113d73f63f018f3e03142e03c13fc05d11c4edfab32bbbeb423423299c98cca` |
| `plan.json` | `46e392525439cea117ea4589e36d7ef27c502b0d9d676c5c3d6b2966ebf290bd` |
| `run/report.json` | `4bd17e41dcb3873ddf6c17ec2552d4cea195d6e212d6a62fa32e674c8a03734c` |
| `control.py` | `6ea197613a62e9295f8b354903a00fc907b8f67624b333379aaf9b3c01fe2c34` |

```sh
set -o pipefail
scope=reports/research/base-whole-survival-20261124
.venv/bin/python -m scripts.replay_base_shadow_survival \
  reports/research/base-newheads-overlap-four-20261124/run | cmp - "$scope/continuous-shadow.json"
PYTHONPATH=. .venv/bin/python "$scope/control.py" replay | cmp - "$scope/control.log"
PYTHONPATH=. .venv/bin/python "$scope/residual_grid.py" | cmp - "$scope/residual-grid.json"
```

## 合并验证

四链离线复算均通过。Base 连续归因、整笔负控和金额补查，Ethereum 两份资金报告，
BSC 两份资金报告及 Robinhood 收据复算与保留输出逐字节一致。

| 检查 | 退出码 | 结果 |
| --- | ---: | --- |
| `make check` | 0 | Ruff、mypy、前端语法、6,658 项 Python 测试及 138 项子测试、127 项 Foundry 测试 |
| `make security-check` | 0 | 静态安全及秘密材料门禁通过 |
| `make contract-security-check` | 0 | Slither 检查 98 个合约，保留 6 项已有提示 |
| `CARGO_NET_OFFLINE=true make rust-check` | 0 | 30 项 Rust 测试 |

原检查输出保留在忽略目录 `reports/research/multihop-whole-stage-checks-20261124/`。
