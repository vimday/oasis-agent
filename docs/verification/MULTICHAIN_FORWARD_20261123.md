# Ethereum、BSC 与 Robinhood 多跳套利前瞻影子核验

北京时间 2026-11-22 深夜至 11-23 凌晨。各链使用独立的短窗口。这里的 H 是发现候选时绑定的块末状态，H′ 是整笔模拟完成后首次采到并用于复算的链头。两次均用相同的路由、输入、调用者和临时资金设置。H′ 的交易排序和将来能否包含一笔新交易仍未知。[此前研究](../strategies/MULTICHAIN_MULTIHOP_DISCOVERY.md) · [实施 TODO](../strategies/MULTIHOP_ARBITRAGE_TODO.md)

## 如何决定模拟哪条路线

1. 在链内按新状态更新变动池。记录链、块哈希、来源、接收和完整状态可用时间。缺块、重组、状态失效要让依赖它的候选失效。
2. 在同一快照对闭环计算含池费的边际上界。上界已不盈利时直接淘汰。在剩余图对不同起点、方向、池和资金量计算整数输出，按执行成本后的预期净额排序。同一池被多条腿使用时按执行顺序更新一次池状态。
3. 选择在给定资本和计算时限内排名最高、成本数据完整且估算净额为正的候选。同步记录覆盖的协议、搜索深度、金额区间、被截断数量和未估价资产，以界定“最优”的范围。池费已进输出，净额再扣 gas、融资、直接付款及其他适用成本。跨币种比较需要同一时点可复核的价格。
4. 对入选候选做整笔 EVM 模拟，独立核对最终资产净增量，在 H′ 重跑同一笔。只将 H 和 H′ 两次成本后均为正的候选计入“模拟后仍有净利”。零入选时存活率为空。本轮预注册的亏损对照使用单独分母。签名、执行准入和结算在影子研究之外。[项目章程](../PROJECT_CHARTER.md) · [交易控制框架](../governance/PRODUCTION_CONTROL_FRAMEWORK.md)

## Ethereum · 先核对单一工厂的模型与 EVM

公共 PublicNode 上观察到 26,471,708–711 四个连续块。Uniswap V2 的 WETH、USDC、USDT、DAI 六池只有一组不同的储备状态。每块 6 个三跳和 6 个四跳，共 48 个路线×块观察、288 个有限金额整数报价。最高费后边际乘积 0.991637348237；48 个观察的毛利上界均为零。达标分母为零。11 次 HTTP / 64 个逻辑 RPC，全程匿名。[单厂前瞻报告及逐块时间](ETH_V2_FORWARD_SHADOW_20261122.md)

对照采用 WETH → USDC → USDT → WETH。两次公开 Router02 的整笔模拟分别绑定 26,471,709 和 710。输入 0.01 ETH，两次均返回 0.009916223845337372 WETH，gas 前各亏 83,776,154,662,628 wei；扣 gas 后亏损只会更大。首次模拟完成至下个采样头到达间隔 13.540 秒，至第二次模拟完成为 14.521 秒。这包含研究轮询等待。Ethereum 此臂未测实耗 gas。 [Router02 源码](https://github.com/Uniswap/v2-periphery/blob/master/contracts/UniswapV2Router02.sol)

## Ethereum · 两家 DEX 搜到的正毛差能买多少 gas

第二个窗口为 16:08:12.757–16:08:37.556 UTC。扩大到 14 个资产、Uniswap V2 和 SushiSwap V2 两厂实际有储备的 45 个池。块 26,471,752 和 753 父子相连，储备向量相同。每块按有限池集穷举以 WETH 为起终点、池与中间币均不重复的 2–6 跳路径，各跳数分别为 24、156、820、2,520 和 4,456 条，合计 7,976 条。十档金额 × 两块产生 **159,520** 个整数报价。预注册统一成本情景为 900,000 gas 上限乘下一块 base fee 上界加固定优先费 0.05 gwei，模拟净额门槛为 10¹² wei。本臂新增 23 次公共 HTTP / 287 个逻辑 RPC，两项 Ethereum 研究累计 34 次 HTTP。[Uniswap V2 定价](https://github.com/Uniswap/v2-periphery/blob/master/contracts/libraries/UniswapV2Library.sol) · [两厂资金排名与精确金额证明](ETH_V2_WIDE_SHADOW_20261122.md) · [影子搜索与排名入口](../../scripts/run_eth_v2_wide_shadow.py)

两块均找到同样两种四跳池组合的正毛差，均经 WETH → AAVE → USDC → CRV → WETH。十档中第一名的投入为 0.0001 WETH，费后毛差为 **186,078,803,207 wei**，相对本金为 **+18.6079 bp**。两块仅覆盖最基本的 21,000 gas 就需要 2,594,517,240,000 和 2,553,028,212,000 wei。此投入按冻结预算估算的净额分别为 −169,906,716,696,793 和 −167,906,353,296,793 wei。全部网格均未触发经济候选的整笔模拟，H 后的候选净利存活率没有分母。两条中间边的 USDC 储备尤其浅，分别约 2.32 和 5.01 USDC，较大的投入会直接改变输出。

十档网格留下四个“最优金额可能落在档间”的池组合。对每个组合枚举 170,386–174,500 个 USDC 中间整数输出，逆推实现该输出所需的最小 WETH，再用连续上界封住剩余金额。最优一条前三腿走 Uni、最后一腿走 Sushi。精确最优投入 **49,460,114,820,510 wei**，毛差 **2,860,521,360,232 wei**。这比十档网格第一名高 15 倍以上。其 452 字节影子交易 calldata 含 189 个非零字节、263 个零字节，常规固有 gas 至少 25,076。两个块仅这部分就需 3,098,100,681,440 和 3,048,558,830,672 wei，故计执行前的净额上限分别为 **−237,579,321,208** 和 **−188,037,470,440 wei**。四个边界组合在两个块、所有金额上均不足以支付所选封装的固有成本；其余路线连 21,000 gas 的宽松下界都过不了。两个块是扫描快照，这一研究臂的整笔模拟次数为零。其他 calldata 编码或其他池集仍需重新评估。[已固化的真实储备回归](../../tests/unit/test_eth_v2_wide_shadow.py) · [精确解与原始证据指纹](MULTICHAIN_FORWARD_20261123.json) · [EIP-2028 calldata 成本](https://eips.ethereum.org/EIPS/eip-2028)

随后在原始储备上取消 6 跳枚举上限，穷举所有 WETH 起步、不同中间币和不同池的简单闭环。2–6 跳各档逐项与前瞻计数一致，另找到 2,352 条七跳，每块共 10,328 条路径。七跳最大连续全金额毛利上界为 1,744,309,192,013 wei，两块均低于 21,000 gas 的最低成本。此池集的 WETH 闭环最长 7 跳，这是连通结构给出的结果。该补充是事后回放，扩大不同池长环的实测范围需要引入实际相连的新资产和新池。[证据及各跳上界](MULTICHAIN_FORWARD_20261123.json)

## BSC · 哪些价差能通过第一道门

11 月 22 日约 16:00:15–16:00:24 UTC，BNB Chain 官方公共节点连续返回 135,107,340–359。三种资产为 WBNB、USDT 和 USDC。每个快照固定 12 个经过工厂验证的 Pancake V2/V3 池。每块 128 个有方向的三池组合，共 2,560 个观察，全部在扣池费后、扣 gas 前淘汰。最接近的三跳位于 135,107,353，忽略费用的边际价差为 **+5.1695 bp**；采用实际费档后上界为 **−1.8330 bp**。在预先固定的 V2 控制路线，5 档 USDT × 20 块的有限整数毛差额全部为负。[Pancake V3 费档及地址](https://developer.pancakeswap.finance/contracts/v3/addresses)

逐块用整数分数求解价格约束。20/20 块均取得可独立验证的价格势，使 24 条有向边各自满足 `势(out) ≥ 费后边际汇率 × 势(in)`。沿任意闭环相乘，左右端的势相消，边际倍率至多为一。每块额外穷举 48 个有方向的两腿组合，最好的异池两腿仍为 −1.9406 bp。这个结论覆盖该静态图的任意步数；将其解释为有限交易的上界时，要求路径上的普通池满足报价上界且各池只用一次。双向复用同一池、分流、动态 hook 与新发现的池需要相应的有状态模型。128 个不同资产/池的合成正、负对照识别出 128 跳 +1 bp 正环和无正环两种情况。[有理数证书实现](../../src/oasis_agent/fastpath/marginal_graph.py) · [回归用例](../../tests/unit/test_marginal_graph.py)

块头接收至评分完成 p50/p95 为 140.7/157.9 ms，其中池状态到评分 p95 为 14.1 ms。从评分结束到一次最新头回读 p95 为 128.1 ms，20 次都返回原块。主源 95 次请求成功，独立 dRPC 见证的首个请求返回 HTTP 429 并立即停，故主源读数保留单源标记。此窗口没有可带入整笔净利存活率的候选。[BNB Chain 公共端点说明](https://docs.bnbchain.org/bnb-smart-chain/developers/json_rpc/json-rpc-endpoint/)

## BSC · 真实池状态上的 3、30 和 102 跳串行对照

另一个预设实验从三组币对中各取活跃流动性最大的 Pancake V3 池，取边际乘积较优的方向，再沿同一方向重复 1、10 和 34 轮。实际选中 WBNB → USDC → USDT → WBNB，费档分别为 100、500、500 ppm。每条腿使用前腿实际输出，包括重复进入池后的价格变化。每次输入 0.01 BNB，用临时带余额的测量合约进入正式 V3 SwapRouter。输出代币 WBNB 与 BNB 按可包装单位一比一核算。两次模拟都要求路由返回值与实际 WBNB 余额增量相等。最高一档走了 **102 条串行腿、3 个不同池**。[Pancake SwapRouter 地址](https://developer.pancakeswap.finance/contracts/v3/addresses) · [exactInput 语义](https://github.com/pancakeswap/pancake-v3-contracts/blob/main/projects/v3-periphery/contracts/SwapRouter.sol) · [临时状态覆盖](https://geth.ethereum.org/docs/interacting-with-geth/rpc/objects#state-override-set)

下表以输入的基点为单位。H 时的发现上界含池费、未计 gas。两列整笔毛差额均来自余额核对。两列模型净额扣去实测路由 gas 区间加 calldata 传统固有 gas，乘同一时点公共 gas 报价 0.05 gwei。该口径包括测量包装器调用路由的一段开销，未还原整笔退款、完整外层开销及排序报价。**三个对照在任何 gas 假设前都已经亏损。** [离线复核入口](../../scripts/replay_bsc_depth_shadow.py)

| 串行跳数 | H → H′ | 发现毛利上界 bp | H 整笔毛差额 bp | H 模型净额 bp | H′ 整笔毛差额 bp | H′ 模型净额 bp | 发现至 H / H′ 整笔返回 ms |
| ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 3 | 135107492 → 493 | −4.9953 | −5.0188 | −25.6768 | −5.0188 | −25.6768 | 114 / 345 |
| 30 | 135107494 → 495 | −49.8405 | −52.1786 | −156.4838 | −52.1786 | −156.4838 | 118 / 353 |
| 102 | 135107496 → 497 | −168.4467 | −195.0025 | −537.4634 | −193.1145 | −534.7922 | 134 / 380 |

三个 H 块的实测路由 gas 区间分别为 390,048、2,053,412 和 6,791,266，102 跳复算时变为 6,775,601。六个 H/H′ 都做了同源按编号再查哈希，三对 H′ 都是 H 的直接子块。使用 30/32 次免费读取。选池规则、输入、深度和停止条件都先于请求冻结。这里测的是重复使用流动性时的整笔可执行深度和成本；对 102 个各异池的市场路径、可投入容量或稳定盈利，需要另外取证。

## Robinhood · 从 sequencer 消息对上已执行的池事件

首次 TLS/WS 握手成功并协商 Nitro 压缩，但本地检查把可选响应头当成必需，采集在首帧前停止。保留该次 STOP 后修正检查，在同一免费官方入口的累计额度内增加一次恢复握手。2026-11-22 16:04:02.114–16:04:16.621 UTC，取得 144 帧、136 个连续有序消息，序号 120,740,508–643，解出 371 笔交易哈希。预配置 Nitro 压缩字典成功解码；同一运营方的官方 RPC 另用 4 次请求返回 chain ID 4663、两个块及一个收据。[逐帧验证、保留数据与回放入口](ROBINHOOD_NITRO_FORWARD_20261123.md) · [Robinhood 网络与 RPC](https://docs.robinhood.com/chain/connecting/) · [Nitro feed 协议实现](https://github.com/OffchainLabs/nitro/tree/master/broadcastclient)

序号 120,740,543 和 120,740,643 与 L2 块号、块哈希及时间戳相符。消息中 1 和 4 笔交易的本地 Keccak 哈希也对应 RPC 块内交易位置。消息头的 `blockNumber` 则是父链块号。抽取的一笔成功交易在官方 Uniswap V4 PoolManager 发出 1 条 Swap，收据 gas 费为 2,694,720,936,000 wei。该笔未检出多池闭环，净利未知。尚无 sequencer 签名验证、独立最终性或 feed 与 RPC 首次可见时间的同步对照；本次窗口也没有执行 Robinhood 多跳净利扫描。[抽样交易](https://robinhoodchain.blockscout.com/tx/0xab4c1b4c174cf81dca7b357e0879e86e4797236bbd564dfdc44e6c6dda6254b7)

## 研究边界与下一轮分母

这些实验均使用免费公开源、无签名或广播。RPC 账单增量为零，计算、运维和等待仍有成本。具备发现能力后，采购或 GPU 扩容应按**同池集、同盘口时刻，额外留下多少整笔净利**评估。当前 BSC 窗口头到状态的 p95 约 147 ms，多数用在取得状态上。扩大协议与资产覆盖、按变池触发重算、启动时预建路由，并记录免费源落后或缺口，均比据此假设 GPU 是瓶颈更容易检验。

长路线继续纳入搜索。含 128 个独立池的植入样本说明算法能找到长环；102 跳整笔说明顺序执行与接收输出可在指定 BSC 状态复核。稳定盈利须另外报告留出窗口内按同一本金得到的净额、成功与失败成本、候选总数、持续时长、容量、回撤和尾部。独立来源未证实或费用未识别的候选只保留研究标签。[资金与研究口径](../../.claude/rules/financial-research.md)


## 溯源与复核

采集基于工作树 HEAD `14260a4e224cf9bc0b039bcb1a937afc6482cb1f` 及当时未提交的研究代码。原始请求、响应、账本、feed 帧和失败记录留在忽略目录 `reports/research/multichain-forward-20261122/`。机器记录列出各臂的范围文件、原始结果及派生证书 SHA-256，可在本地同一原始留档上复算。新观测必须另行记录时间、范围和原始用量。实际前瞻窗口均已结束。[可核对的整数结果、区块哈希与 SHA-256](MULTICHAIN_FORWARD_20261123.json)

```sh
.venv/bin/python -m scripts.run_eth_v2_forward_shadow replay
.venv/bin/python -m scripts.run_eth_v2_wide_shadow replay
.venv/bin/python -m scripts.run_eth_v2_wide_shadow diagnose
.venv/bin/python -m scripts.replay_bsc_depth_shadow
```

Robinhood 的逐帧重放命令和对应字典列于[采集记录](ROBINHOOD_NITRO_FORWARD_20261123.md)。BSC 三角扫描及静态证书的离线复算入口在留档目录的 `bsc/replay.py`、`bsc/graph_certificate.py`。对 BSC 整笔证据另注入两个反例，篡改模型净额和改变 RPC 响应各一个，两者均被拒绝。

### 本轮工程检查

- `make check` 重跑退出 0。Python 6,550 项和 138 个子测试通过，10 条警告；Dashboard 检查通过，Foundry 17 套共 125 项通过。首次退出 2，宽图脚本按 Python 模块加载时找不到同目录模块，已修正并单独复核脚本、模块和证据回放入口。
- 另行运行的真实四跳最优金额回归退出 0，覆盖粗网格漏掉最优规模、不同中间币精度、calldata 固有 gas 与全金额拒绝条件。Robinhood 协议及边际证书 7 项焦点回归也通过。
- `make security-check` 退出 0。首次退出 2，随后从原始字节逐一核实两个公开 EVM 运行时指纹和一个上游压缩字典指纹，将说明保存在既有审查基线中。`make contract-security-check` 退出 0；Slither 报告 6 项提示。`make rust-check` 退出 0，23 项测试通过。
- 两个 Ethereum 窗口、BSC 扫描与证书、BSC 串行深度以及 Robinhood 帧均完成离线复核。Ethereum 精确尺寸补算和 BSC 证书都与原输出逐字节匹配。证据清单的 20 个摘要核对通过；文档相对链接与 `git diff --check` 通过。以上工作保存在未提交工作树。
- 运行机保留 `/private/tmp/oasis-multichain-forward-check-{,final-}20261123.log`、`/private/tmp/oasis-multichain-forward-security-{,closed-}20261123.log`、`/private/tmp/oasis-multichain-forward-contract-security-20261123.log`、`/private/tmp/oasis-multichain-forward-rust-20261123.log`。
