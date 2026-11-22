# 多链长交易尾部与资金深度核验

北京时间 2026-11-23。预先指定的窗口按 V2、Aerodrome、V3、Pancake V3、Uniswap V4 与 Infinity CL 六种事件形态取样。用交易哈希聚合，V4 和 Infinity CL 按 manager 与 PoolId 计数，标出同池再次使用。按事件数选最长候选，以链节点完整回执、固定块哈希和交易位置验证。可用时再加入调用树及 ERC-20 资金依赖。所有近期采集均用免费匿名接口，未发送交易。[上一轮已成交案例](RECENT_DEX_ARBITRAGE_20261123.md) · [静态长路径对照](MULTICHAIN_TAIL_DEPTH_20261123.md) · [SQD EVM 索引规范](https://docs.sqd.dev/en/portal/evm/api)

## 案例分布与覆盖

| 网络 | 窗口和匹配分母 | ≥5 Swap | ≥10 | ≥20 | ≥50 | 最长匹配 |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| Base | 60,868,904–60,873,000，4,097 连续块，16,103 笔 | 88 | 6 | 1 | 0 | **35 条，2 池** |
| BSC | 135,110,776–135,115,383，两段不重叠并验证连续，4,608 块，89,603 笔 | 1,368 | 205 | 48 | 4 | **80 条，43 池** |
| Ethereum | 8 个预先固定块，205 笔有匹配事件 | 5 | 0 | 0 | 0 | **8 条，8 池**，有两笔 |
| Robinhood | 120,754,500–120,758,595，4,096 块，3,335 笔 | 43 | 7 | 0 | 0 | **15 条，8 池**；早先 20,000 块窗口最长 22 条 |

时间分别为 Base 00:15:56–00:29:35，BSC 00:26:01–01:00:35。Ethereum 为稀疏的整块回执抽样。Robinhood 新窗口为官方 RPC 的日志筛选，15 条的本轮结果仍为事件层。长尾表中 Base 与 BSC 的窗口按块号及父哈希校验了全部覆盖；其他来源的覆盖各自标注。SQD 的 Base 额外 8,192 块和 Ethereum 窗口返回 HTTP 529，均停止；Robinhood 查询的 SQD 数据集名返回 404，另用此前验证的官方 RPC。BSC 80 条样本的同合约定向回看只取到了目标 20,480 块中的首 3,796 块，228 笔匹配，最长 46 条，标记为未完成。各失败与截断的原始响应均保留。在这些窗口中未核到 100 条以上的单笔，更未核到上百跳的纯串行套利。

## BSC · 80 条 Swap 来自 20 个四次交换的调用分组

交易 `0x004c…0bfe` 在 **135,113,015**、索引 **3**，北京时间 00:42:49。官方回执包含 218 条日志，其中 80 条匹配 Swap，来自 43 个不同池地址。其中 38 条为 V2 事件，42 条为 V3 类事件。公开索引给出同块、同交易、同发起与目标的 435 个调用帧。按池地址及已知 V2/V3 Swap selector 核对，调用 multiplicity 与回执逐池匹配，**20 个外层子调用组，每组正好四次匹配交换**。该分组描述调用树，单个组内的资金深度仍由转账来证实。

执行器的可见 ERC-20 变化为 +211,363,271,059,021 WBNB wei、+7,052,201,935,149,049 USDT 最小单位。本笔没有 WBNB 包装或解包日志。外层付款人支付 180,022,000,000,000 BNB wei gas，执行器另外支付 33,289,163,470,889 BNB wei 给另一个地址。该地址和块头 fee recipient 不同。若仅在交易范围合并这些已知变化，得到 **−1,947,892,411,868 BNB 等值 wei 与 +0.007052201935149049 USDT** 的余额向量。币种折价、钱包控制和其他付款保持未决。[80 条记录](https://bscscan.com/tx/0x004c868e409e24143e699f6186b4ef8e6b339602f5e82201a3cf806013420bfe)

同一扩样还复核到 **56 条 / 22 池**，回执 gas 3,786,985。其资金路径仍停留在回执计数层。[56 条记录](https://bscscan.com/tx/0x37de3d32d138e9b16858dce922ac2a08853041e2c6af63132ff5a2bd34801343)

## BSC · 37 条交易里可证的五跳闭环和一跳变现

交易 `0xa2c1…af88` 在 **135,115,034**、索引 **2**。310 个索引调用帧和收据给出多组回调嵌套，其中有 10 段相互区分的 swap 组和单独的变现。逐个核实的一条连线按实际资金顺序为：

```text
Swap log 79 → 77 → 75 → 73 → 71 → 158
           └────── 5 跳 USDT 闭环 ──────┘  兑换 WBNB
```

前四个依赖由执行器接到上一池输出并等量转到下一池支持。USDT 出口为 1,412,615,669,230,532,376 个最小单位，偿还 1,399,229,278,444,666,368 后，差额 **0.013386390765866008 USDT** 被单独换成 **23,908,475,818,923 WBNB wei**。回执记录 2,618,918 gas，实际单价为零。这个金额是其中一条闭环的变现，整笔还包括其他循环和付款。[37 条记录](https://bscscan.com/tx/0xa2c11e2c90a5d5fca43a6da0e349d5daf133bf1b654425adf3496fde020caf88) · [V3 回调与事件语义](https://github.com/Uniswap/v3-core/blob/main/contracts/UniswapV3Pool.sol#L700-L722)

## Base · 35 条日志走两个 V4 池

交易 `0x5b84…b14d` 在 **60,871,104**、索引 **30**，北京时间 00:23:16.400。完整回执有 298 条日志，外层为 EntryPoint v0.6，记录一条 UserOperationEvent。35 条 Swap 都由同一个官方 V4 manager 发出，两个 PoolId 各出现 **25 次和 10 次**。以池 ID 和事件 caller 作指纹，回执呈同样的 **7 事件序列重复 5 次**。索引给出同笔 2,114 个调用帧，保留 15 个带错误的调用。V4 内部 PoolKey、借还款、净额和真正串行深度仍待完整归因。外层收据已知 L2 与 L1 合计 47,988,873,557,407 wei。Operator fee 未核实。[Base 35 条记录](https://basescan.org/tx/0x5b8491be0ae575eceee782601a40a77debe134080f7935f7492f52a33163b14d) · [V4 PoolManager](https://developers.uniswap.org/docs/protocols/v4/deployments)

另外核对了 **16 条 / 11 池** 与 **13 条 / 13 池** 两笔完整回执。13 条样本中，同一中间地址把同一个输入资产的不同份额送往多个池，再收到 WETH、USDC 和其他资产。这些记录适合验证分流识别和长路径误报。Base 的四池速度基线继续保留，事件覆盖增加后和它配对比较。当前样本在 200 ms 规范块中，收据中的位置可用于后续精确交易前状态实验。[16 条记录](https://basescan.org/tx/0x696754492b51d931ccaf9f4f2e2ebce69b2abe6659b271564cb54720485b6612) · [13 条记录](https://basescan.org/tx/0x63a53fa18cfc9e4d67801868590236438af4c022d14533f7918b9199019c3fe2) · [Base Denim](https://www.base.org/blog/denim)

## Ethereum 与 Robinhood 的对照

Ethereum 两个 8 条 / 8 池样本已经来自整块回执。这里只在 8 个块上扫描。Robinhood 本轮得到新的 15 条 / 8 池日志候选，并保留此前完整回执的 22 条样本。此前 Robinhood 的 100/109 池为静态拓扑对照。BSC 的 102 跳为真实池状态上的三池重复整笔模拟，样本扣费后均亏损。两种实验均和本轮的已落块观测分别计量。[Ethereum 8 条](https://etherscan.io/tx/0x41ee7ae699aef6f9519c2326a5d63b282532eec2cbee21dda57c6c37075bbb66) · [Robinhood 15 条](https://robinhoodchain.blockscout.com/tx/0x5ea647b8bf501344133724da25152cf13525a9f2a047e0be10b2283b189e016e) · [Robinhood 22 条既有回执](https://robinhoodchain.blockscout.com/tx/0x98a5143fcf86defcb660b9548f8eea52011856a24526fa2d2564864cc604362a) · [BSC 102 跳模拟](MULTICHAIN_FORWARD_20261123.md#bsc--真实池状态上的-330-和-102-跳串行对照)

USENIX 的 Ethereum 历史研究在 28 个月内识别 3,797,259 个套利闭环，其中 60,506 个使用 5 个或更多交易场所，占 1.6%。论文仅发表了 `5+` 尾部桶。AFT 的 Base 研究另将真实交换、仅探测池和回滚分别计数。后续采样可以借用公开标注地址做定向臂，并保留按连续区块筛选的对照臂。两臂各自记录完整分母和 5、10、20、50、100、200 阈值，再用回执和资金依赖复核。[USENIX Security '23 表 1](https://www.usenix.org/system/files/usenixsecurity23-mclaughlin.pdf) · [AFT 2025 第 4 节](https://drops.dagstuhl.de/entities/document/10.4230/LIPIcs.AFT.2025.28)

## 回放工具与接下来的检验

- 已加入区块绑定的 [Swap 资金回放入口](../../scripts/replay_swap_flow.py)。完整回执输出事件数、独立池数及重访数。地址池调用树按 selector、收据池地址 multiplicity、父调用是否成功和外层分组核对。V4/Infinity 共用地址的池继续按 PoolId 计，仅凭调用地址无法定位到单池。
- 已解析的资金依赖输入以原始回执 SHA-256 绑定，保留各条整数金额与证据引用。可以由唯一的直接 ERC-20 池间转账产生一部分依赖，或加载单独复核的 trace/多段转账分配。执行图核对币种、过额分配和重复凭证。没有证据的中间资金留空；需额外融资解释的循环不赋予串行深度。合成 400 跳、分流合流、回调倒序和祖先调用失败均有回归。[资金依赖模型](../../src/oasis_agent/evidence/swap_flow.py) · [回归测试](../../tests/unit/test_swap_flow.py)
- 长交易研究改为三组对照。按实际资金路径筛重点，单独统计批量短闭环与分流路由，最后按受益地址和完整费用复算。在 Base 使用现有四池臂与真实 V4/跨厂臂做配对回放。100 跳以上继续留独立分母，同时保留 20×4 这种批处理作为重要对照。

原始留档在本地忽略目录 `reports/research/long-dex-20261123/`。三个典型输出为 `base-mainnet/receipts/3.profile.json`、`binance-mainnet/funding-analysis.json` 和 `expanded/binance-mainnet/flow-analysis.json`。已落块的回执和 trace 用于回顾，尚未重建交易前状态或测定可获利的执行时机。

## 提交前验证

`make check`、`make security-check`、`make contract-security-check`、`make rust-check` 均退出 0。全量检查通过 6,598 个 Python 测试、138 个子测试与 125 个合约测试，Rust 通过 23 个测试。Python 有 10 条警告，Slither 报告 6 项既有发现。8 笔完整回执、BSC 两份调用与资金结果、4 份改动文档的本地链接均已核对。原始检查日志存于本机 `/private/tmp/oasis-long-flow-{check,security,contract,rust}-20261123.log`。
