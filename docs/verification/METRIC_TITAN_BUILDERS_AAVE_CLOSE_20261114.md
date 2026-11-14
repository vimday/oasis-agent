# Titan 构块归因、Metric 规模复核与 Aave 案例结项

2026-11-14，基线 `a505eb5`。Codex 实现及自审。Ethereum 调查使用匿名免费只读来源。
Base LP 维持暂停。Base/Elysium 虚拟策略观察与多链研究保留。
[前一轮原始采集记录](METRIC_STREAM_AAVE_WALLET_20261114.md)

## Titan 构块与报价落地

将原六个 slot 的区块哈希、父哈希、区块高度与 Titan Relay 的已交付 payload 对齐，
并核对 `builder_pubkey` 在 Titan 官方发布的公钥表中。公开表归属及 relay 输出是
来源声明，区块头与 oracle 槽以 Ethereum 状态承诺复核。执行块的 `miner` 只保留为
辅助线索。另三块未在所查询的 relay 返回匹配 payload，归属保留未知。
[Titan 官方公钥](https://docs.titanbuilder.xyz/builder-public-keys)
[Titan Relay 数据](https://titanrelay.xyz/)
[Titan 的冒名构块记录](https://titanbuilder.substack.com/p/one-hundred-thousand-times-titan)

| 区块 | slot | Relay 和公钥吻合 | 严格时钟顺序的公开声明 | 区块末同秒 |
| --- | ---: | --- | --- | --- |
| 26413085 | 15652800 | 是 | 是 | 否 |
| 26413088 | 15652803 | 是 | 否 | 否 |
| 26413090 | 15652805 | 是 | 否 | 否 |

按这三块统计，有本机在目标秒前收到的同秒三槽声明 3/3，区块末同秒 **0/3**，
首个合格帧的压缩报价原始存储字匹配 **0/3**。若进一步要求发送方时钟不晚于本机
收包，分母为一，落地 **0/1**。尚未验证目标块中间状态或 Titan 在后续时段的落地率。

固定这三块、原父块哈希及各 slot 的首个本机合格帧，分别对两个腿序测试 2、100、
10,000 USDC。18 条全到第二腿并触发本金不足。各档六条结果如下，数值为扣 gas 前。

| 本金 | 本金差额 USDC | 本金损失 bps |
| ---: | ---: | ---: |
| 2 USDC | 0.006016–0.007266 | 30.080–36.330 |
| 100 USDC | 0.300820–0.363323 | 30.082–36.332 |
| 10,000 USDC | 30.711263–36.960383 | 30.711–36.960 |

六个配对的 10,000 USDC 档均较 100 USDC 档恶化，约 0.628–0.629 bps。
这三个块的路线判为负样本并停止推广。样本是同一个短窗口的单端点条件模拟，
真实排序、政策成本、其他规模和其他时段仍未验证。
[三槽诊断与探针语义](ETHEREUM_AAVE_METRIC_TRACES_20261114.md)

## Aave 一页结项

冻结窗口内的 204 笔候选收据齐全。用正式入口从固定块头、原始收据、余额、原索引
及两轮 trace 复算，原生 ETH 残差为 **0**。两笔直接补款合计 1.3 ETH，来自同一个
不同地址，列为可明确识别的外部资金划转。**3.254496360518749927 ETH** 是钱包期初
至期末的净增，包含各类现金流与费用，无法整笔归为外部资金划转。窗口内该地址支付
的已匹配 gas 为 20.257927909813355166 ETH，其中失败交易 gas 为 2.35587565 ETH。
余额已反映这两项支出。
[借卖与补款原始验证](AAVE_SALE_CYCLE_20261114.md)
[钱包余额与收据](METRIC_STREAM_AAVE_WALLET_20261114.md)

对开闭两端统一使用已留存买回父块的 Aave 估值，USDC 为 0.99977974 USD，
ETH/WETH 为 2,270.26764105 USD。按已跟踪的钱包资产、Aave 抵押和债务，窗口
净资产变动约 **−494,709.09 USD**。再扣两笔已识别补款后为 **−497,660.43 USD**。
该数字衡量单地址、固定价格下的净资产变化。其他大额资金划转的归属、逐笔交易与
外部资金拆分、利息的完整归因仍未闭合，案例结项保留 `net_pnl=null`。此账本用于
复核历史个案，研究精力转向实时事件。

## Base 与 Elysium 实时首收

沿用原窗口冻结的池地址与 topic，每链开一次免费 WebSocket，观测 90 秒。
Base 收到 446 个不同块头和 8 条池日志（6 Swap、1 Mint、1 Burn）。Elysium 收到
381 个不同块头和 6 条池日志（3 Swap、3 liquidity），均来自已选 HYPE/KNTQ v4 池。
两链的第一笔 Swap 均与各自区块和成功收据的 logIndex、交易位置、地址、topic 和 data 一致。
Base 样本块 `57347910` 的日志比同源块头晚 0.427 ms 被本机收到，Elysium 样本块
`6403615` 的日志早 552.881 ms。相对区块标称整秒，分别晚 586.108 ms 和 226.307 ms。
相对本机墙钟的个别其它日志会早于区块标称秒，时间数据仍需独立校准。它们是接收边界
测量，尚无实际报价启动/完成时延。实时定向报价、断线缺口和等预算轮询保持待验收。
WebSocket 原始帧和两笔收据在本机留存，两笔复核另用 8 次免费 HTTP。
[原窗口基线](PROSPECTIVE_EVENT_WINDOWS_20261114.md)
[PublicNode 公共端点](https://publicnode.com/)

## 仓库回放

以下命令依赖本机原档案和原 SQLite 账本，保留原始目录与文件身份。解析、校验与
对账逻辑均提交到仓库。Metric 的规模扩展原始输入与输出仍存本机实验范围。

```sh
research=reports/research
r="$research/metric-builder-aave-close-20261114"
.venv/bin/python -m scripts.research_metric_stream \
  --research "$research" --relay "$r" --output "$r/builder-review.json"
.venv/bin/python -m scripts.research_aave_closure \
  --research "$research" --output "$r/aave-review.json"
```

两个正式回放分别重复运行，输出逐字节一致。Titan 官方公钥与 relay 共 7 次，
规模模拟及父块共 21 次，所有 HTTP 请求免费。Aave 结项回放无新增链读取。

| 本地证据 | SHA-256 |
| --- | --- |
| `builder-a.json` | `510f9aa5759280937d50ad8bab230464a7bf6e38e71188454031bb072734a72f` |
| `metric-size/summary.json` | `41e2be6400d9b41dfc639a94f8b39529f714ecb4ca78e174bb1b036874826724` |
| `aave-c.json` | `557b8442982f1202b7cb42dca0cca5c3adf7e71fcaefad7edac1be410fca4320` |

| 检查 | 结果 |
| --- | --- |
| 相关回归 | 84 项通过 |
| `make check` | 退出 0，6,215 项 Python 测试、138 项子测试、9 项前端模块、82 项 Foundry 测试通过 |
| `make security-check` | 退出 0，依赖审计、静态分析及密钥扫描通过 |
| `make contract-security-check` | 退出 0，Slither 分析 85 个合约，0 条发现 |
