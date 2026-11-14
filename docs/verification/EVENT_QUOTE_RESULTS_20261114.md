# Base 与 Elysium 的实时 Swap 报价实验

2026-11-14，基线 `efa0b93`，Codex 实现及自审。
[实验预注册](EVENT_QUOTE_PROTOCOL_20261114.md)。
本机原始档案在 `reports/research/event-quote-20261114/`。请求、响应和 WS 帧保留原始字节、
哈希及两类本机时钟。每组原 SQLite 账本在发送前占用额度。启动使用 PublicNode，
收尾由 Base 官方端点和 Kinetiq RPC 分别提供第二来源。运营商不同，底层节点独立性尚未验证。
[PublicNode](https://publicnode.com/)
[EIP-1898](https://eips.ethereum.org/EIPS/eip-1898)

## Base 90 秒实测

首次尝试在匿名 `eth_chainId` 请求超时后停止，1 次请求、0 条订阅。三个公开来源各做一次
免费连通诊断。随后按同一冻结计划打开本轮唯一的正式比较窗口，保留原停止账本。
正式窗口 450 个头、14 条 Swap。回拉区间 57365935–57366408，没有额外 Swap。
事件组抽取首 4 个不同 Swap 块，四笔均由第二来源匹配成功收据和精确日志位置。
四个轮询锚点亦通过第二来源块哈希复核。两组各使用 16 次只读请求，
身份和对账另用 22 次主源、17 次第二来源，另占 4 个 WS 连接及订阅额度。
[Uniswap V3 Base 部署](https://developers.uniswap.org/docs/protocols/v3/deployments/v3-base-deployments)

| 组 | 两腿完成 | 750 ms 内完成 | 两腿完成延迟 ms | 毛差 bps | 模型净差 bps |
| --- | ---: | ---: | --- | --- | --- |
| 事件 | 4/4 | 2/4 | 489.153、512.512、1082.297、908.612 | −14.371 至 −13.280 | −17.378 至 −16.289 |
| 固定轮询 | 4/4 | 0/4 | 2028.265、2051.131、1840.626、3154.908 | −13.665 至 −12.643 | −16.675 至 −15.651 |

延迟从实时帧交付或固定轮询计划时刻起算。事件组接收到任务调度为 1.830–3.644 ms，
额外 L1 费读取在事件后 899.522–2256.926 ms 完成，因此按两腿加全套模型费回包衡量，
事件组也为 0/4。仅第一轮询与两个事件的启动时限相交，使用的是同一个更晚块 57365965，
分别在事件后约 716、708 ms 启动，均在事件后 2.73 秒以上才完成两腿。这两个配对
共享同一个轮询样本。费用模型记录区块 base fee、固定 tip、gas 单位和 L1 上界；
原子执行、真实排序、失败成本和净 NAV 没有观测值。

四笔事件均符合收据复核条件，毛差全负，未出现按时且正的模型净差。
按预注册门槛**关闭这条固定方向、固定 0.01 ETH 的 Base 路线**。
本窗口证明事件触发有时能在 750 ms 内取得两腿报价，对其它路线或长期收益保留未知。
两组按相同请求量预算比较。轮询需额外等待最新块请求，事件组的观察已经给出块哈希，
可以并行补块头。这一优势是本试验的管线差异。

## Elysium 90 秒实测与路线身份

429 个头、5 条实时 Swap，4 笔抽样成功收据和触发字节与另一个来源对齐。
HTTP 在 6423622–6424072 共查到 6 条 Swap，其中新增一条在实时订阅尾部外的块
6424061。补回的日志只有查询证据，实时接收时刻保持未知。窗内无丢失记录，
本次未发生真实断线或重组。实测合计主源 9 次、第二来源 12 次、WS 4 个额度。

后续在 6426880 做了 21 次免费身份读取。固定块号首尾相同，开头块哈希与 PublicNode
一致。V4 Quoter 的 `poolManager()` 对应观察的管理器。把零 hook、3000 fee 和
60 spacing 的 HYPE/KNTQ PoolKey 编码，可以重算原 PoolId。
关键阻塞在第二腿，池 `0x4d5cca6bf05af4fd6c59714b2f7caa6dbfeeca79` 的
`factory()` 返回 `0xefd7a089d3057f05189b05f74ffcf30610c42891`，费率为 4300 pips。
官方 QuoterV2 返回 `0x1707327f626496a7b5b3872e7e4d2879df7d7a9f`。因此这份官方
Quoter 无法证明原第二腿的报价。第三腿确认 volatile、资产对与工厂 getter。
这次身份探测结束时，首个覆盖窗口的 HYPE 报价分母仍为零。
[Uniswap V3 Elysium 部署](https://developers.uniswap.org/docs/protocols/v3/deployments/v3-elysium-deployments)
[Uniswap V4 部署](https://developers.uniswap.org/docs/protocols/v4/deployments)
[Kinetiq Elysium 应用文档](https://dev.elys.run/docs)

## Elysium 三腿实时对照

Aero 官方应用静态配置匹配目标池工厂，给出 MetaQuoter
`0x1ddfec5058180c207f55bb77d95acdd0d8227868`，在链上确认 registry 与指定池。
按固定源码 `quoteExactInputSingleV3` 使用池地址、输入币和数量，解码后检查价格边界。
第一次元数据探测的地址抄录少一位，RPC 拒绝后原范围停止。修正后在块 6428962
完成 15 次免费查询，0.01 HYPE 三腿报回 0.009829817928246993 WHYPE，
扣额外成本前约 −170.182 bps。Slipstream 的优先费相关 MEV 税可能因调用上下文不同而缺失，
真实费用与净额保持未知。
[Aero 公共应用](https://app.aero.xyz/)
[Aero 固定 Quoter 实现](https://github.com/dromos-labs/metadex-slipstream-public/blob/f99f2095918cab69eafeffa2c3d0a8762de737b7/contracts/periphery/lens/Metaquoter.sol)

[后续预注册](ELYSIUM_EVENT_QUOTE_PROTOCOL_20261114.md)冻结了新的 90 秒窗口。415 个头、
5 条实时 Swap。两组各 16 次读取，三腿均 4/4，时限内 0/4。轮询在时限内与事件配对 0 次。
Elysium 用数值块高报价，另外核对哈希块头、收据及两个来源的窗口边界。

| 组 | 三腿完成延迟 ms | 毛差 bps |
| --- | --- | --- |
| 事件 | 1144.277、1489.102、1560.596、1786.139 | −226.845 至 −201.785 |
| 固定轮询 | 2126.349、2210.420、4017.885、1449.589 | −240.020 至 −208.884 |

原窗口在结束后第二来源链 ID 超时时停止。单独对账的 23 次读取初始匹配两个来源的
6430447、6430866 块头，日志回拉复核 5/5，第二来源收据 4/4、轮询锚点 4/4。
随后主源对最后块的重复读取超时，独立收尾探测又在主源首次链 ID 遇到连接错误。
保留三份原账本和失败状态，完整关停条件未达成。失败的自动审批 403 未发出请求，
服务恢复后已执行该实时窗口。

## 重放与范围

```sh
r=reports/research/event-quote-20261114
.venv/bin/python -m scripts.research_event_quotes replay --out "$r/base-transport-recovery"
.venv/bin/python -m scripts.research_event_quotes replay --out "$r/elysium"
.venv/bin/python -m scripts.research_event_quotes replay --out "$r/elysium-live"
.venv/bin/python -m scripts.research_event_quotes replay --out "$r/base"
```

正式命令可从数据库回放原窗口，重复输出逐字节一致。额外 Elysium 部署及事后复核的脚本
和字节仅在本机留存。HTTP 范围回补和重组拒绝有合成回归，真实窗口尚未包含断线或重组。
本轮的归因、压缩槽时间和 trace 校正纯逻辑已从研究脚本移入模块；没有为这项重构增加 Aave 链读。
Metric 公开流挂起，Aave 账本结项且 `net_pnl=null`。Base LP 继续暂停，资本、签名和广播为零。
经济评估继续保留，当前无可推广的净 NAV 样本。

| 本机留存对象 | SHA-256 |
| --- | --- |
| Base 结果 | `2c603a994f90f32623977921336c19dd5d3446a677e867190c05271da9fea18c` |
| Base 原始 SQLite | `70989b804e0f5538188abd4ad4402ddf641c4543cd2a301035cc9cc96d9bc7d1` |
| Elysium 结果 | `ed3fd6bbb7c93f3a0f92a54028d54064e8b1c57e3e50e58ae0b70194bfbccec8` |
| Elysium 原始 SQLite | `210d57a4b166d04e89d8c2a443650e9046e5e1c4a3fdbee3a6485a7e9b1d5f0e` |
| Elysium 实时报价原始 SQLite | `7b351b21db5c3f170341ea9234f3401c06bf6c6e2132bb1c72ff7e5aa7eec205` |
| Elysium 实时报价离线回放 | `d81284edbdee65ddf1d7385db4158a867aa9221461cf4f87327c0b96a3415530` |
| Elysium 事后复核原始 SQLite | `5c94eb4cf213fa4771aedc0f75654a080c23cf9cced4a4c8940a20fbc94a13f6` |

| 检查 | 结果 |
| --- | --- |
| 聚焦回归 | 96 项通过 |
| `make check` | 6,223 项 Python、138 项子测试、9 个前端模块、82 项 Foundry 测试通过 |
| `make security-check` | 依赖审计、Bandit 中高严重度及密钥扫描通过 |
| `make contract-security-check` | Slither 检查 85 个合约，0 项发现 |
| 文档与证据 | 63 个相对链接可达，`git diff --check` 通过，原 Aave 和 Metric 报告复算与旧稿逐字节一致 |
