# Ethereum 两厂宽图前瞻观察

UTC 2026-11-22 16:08:12.757 至 16:08:37.556。14 个预选资产、两个正式 V2 工厂、45 个活跃池。
两个连续区块各搜索 7,976 条不同池闭环和 79,760 个路线与金额组合。每块仅两档路线组合有正
毛差，均未达到最宽松的 `21,000 × baseFee`。对四条连续毛利上界超过该下界的曲线，离线找了
所有整数输入的最佳毛差，并验证这四条连固定模拟调用的 calldata 最低成本也无法覆盖。两个块
的池储备向量相同，本轮提供一个独立储备状态。

## 冻结范围和操作

- 资产为 WETH、USDC、USDT、DAI、WBTC、LINK、UNI、AAVE、SHIB、PEPE、LDO、CRV、ENS、COMP。
  每厂查询 WETH 星形和 14 组预选交叉对，共 27 组。每个工厂的槽位结果通过 `getPair`、
  `token0`、`token1`、储备和运行时字节核对。45 个活跃池来自 Uniswap 26 个、Sushi 19 个。
  有些实际池很浅，资产知名度和单池深度是两项独立条件。 [Uniswap 官方部署](https://developers.uniswap.org/docs/protocols/v2/deployments) · [Sushi 官方 SDK 工厂](https://github.com/sushiswap/sushiswap-sdk)
- 搜索从 WETH 开始和结束的 2 至 6 跳单路径，允许并联报价池，要求池和中间资产不重复。固定输入
  `0.0001`、`0.001`、`0.003`、`0.01`、`0.03`、`0.1`、`0.3`、`1`、`3`、`10` WETH。DEX
  手续费和逐跳整数取整已计入。 [Uniswap V2 公式](https://github.com/Uniswap/v2-periphery/blob/master/contracts/libraries/UniswapV2Library.sol) · [Sushi V2 公式](https://github.com/sushiswap/v2-core/blob/master/contracts/libraries/UniswapV2Library.sol)
- 样本绑定链号、头哈希和状态根。发现头之后，每个池读取按 `blockHash` 和 `requireCanonical: true`
  发出，然后按块号复核。两个头均是采集期间新收到的实时头。全部 23 次 HTTP、287 个逻辑 RPC
  都成功。冻结上限为 36 次 HTTP、420 个逻辑 RPC。上轮原账本的 11 次只读核对，本轮原账本
  另记实际 23 次，源累计 34 次，费用 $0。没有失败重试或切换供应商。 [EIP-1898](https://eips.ethereum.org/EIPS/eip-1898) · [PublicNode](https://ethereum.publicnode.com/)
- 只有网格最佳净差达到 `10^12 wei` 才运行完整影子调用。成本预算为 `900,000 × (下一块
  baseFee 的 12.5% 上调上界 + 0.05 gwei)`，模拟使用固定 sender、临时余额和只绑定这两厂的
  `observe(address[],address[],uint256)`。若达到门槛，下一采样头用同一 calldata、caller 和
  value 回验。该预算只覆盖设定气价下的交易 gas，不含竞争性报价和额外费用。 [EIP-1559](https://eips.ethereum.org/EIPS/eip-1559)

| 区块 | 块时间 UTC | 头收到 UTC | 状态就绪 UTC | baseFee wei/gas | `21,000` 基础成本 wei |
| --- | --- | --- | --- | ---: | ---: |
| 26,471,752 | 16:08:11 | 16:08:13.039 | 16:08:21.499 | 123,548,440 | 2,594,517,240,000 |
| 26,471,753 | 16:08:23 | 16:08:35.651 | 16:08:37.556 | 121,572,772 | 2,553,028,212,000 |

完整块哈希分别为 `0x84bbe967f199ff92feacf450d044b11e19b6cec9fef5a921d0387197aae26c81`
和 `0x0b24a98e6ceda4c7f3b5edb5dc2224547455a86aa14ab1f231a48cef8714725f`。各跳数每块分别有
24、156、820、2,520、4,456 条不同池路径。

## 排名和金额

两块的首选路线均为 WETH → AAVE → USDC → CRV → WETH，前三腿走 Uniswap，末腿走 Sushi。
网格首选金额只有 `0.0001 WETH`，毛差 `+186,078,803,207 wei`，约 18.61 bps。两块扣保守
预算后分别为 `−169,906,716,696,793 wei` 和 `−167,906,353,296,793 wei`。第二条正毛差为
同一币种序列走四腿 Uniswap，同额输入毛差 `+156,198,734,151 wei`。其余输入档的全图最优
毛差均为负，其中 `0.001`、`0.003`、`0.01` WETH 分别为 `−2,156,150,747,714`、
`−13,365,133,834,220`、`−45,028,760,043,300 wei`。按中间资产划分的网格首位是 AAVE、
USDC、CRV，它们对应同一条路线，不能独立相加。后续为 SHIB `−4,807,526,676 wei`，USDT
及 LINK 各 `−109,046,468,532 wei`。完整资产和金额排名在 `result.json`。

最优路线的两处中间池只分别有 `2.317011 USDC` 和 `5.014095 USDC` 储备，解释了最佳规模低于
最低网格档。发现候选是块后储备价差，经济门槛通过数为 0，因此本轮未触发执行器 `eth_call`。
正候选条件模拟成功率、跨块保留率和链上成交率均无分母。没有签名、广播或动用真实资金。

## 网格之外的整数诊断

四条曲线共享 AAVE/USDC 和 USDC/CRV 的 Uniswap 池，只在第一腿和最后一腿选择不同工厂。
连续路径为 `A×x/(B+C×x)`，正毛额只可能在 `0 < x < (A−B)/C`。先用该区间上端界定原始
USDC 中间单位的遍历上限。每个整数中间输出 `y`，向前逐跳用
`ceil(1000×reserveIn×y / (997×(reserveOut−y)))` 反推达到它的最小输入。末段输出由实际的
整数 USDC 中间值决定，所以每个实际可达的中间值用最小输入比较毛差即可穷尽盈利区间。四条
分别遍历 `174,500`、`174,048`、`170,839`、`170,386` 个 USDC 原始单位。该诊断只重放先前
的区块状态，冻结范围及在线决策不变。

完整路径 `observe` calldata 都为 452 字节，`msg.value` 在 calldata 外，因而固有 gas 与金额
无关。按保守下界 `21,000 + 4×零字节 + 16×非零字节`，一条路线为 25,064 gas，其余为
25,076 gas。以下剩余执行 gas 用连续全输入毛利上界折算，并要求严格正净差。负数代表即使
尚未运行 EVM 也已亏损。

| 工厂序列（U=Uniswap，S=Sushi） | 整数最优输入 wei | 整数最优毛差 wei | 连续毛差上界 wei | calldata 固有 gas 下界 | H 剩余 gas | H′ 剩余 gas |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| UUUS | 49,460,114,820,510 | 2,860,521,360,232 | 2,860,521,360,269 | 25,076 | −1,923 | −1,547 |
| UUUU | 49,325,484,311,945 | 2,844,239,422,922 | 2,844,239,422,928 | 25,064 | −2,043 | −1,669 |
| SUUS | 48,486,016,094,260 | 2,743,076,087,836 | 2,743,076,087,846 | 25,076 | −2,874 | −2,513 |
| SUUU | 48,351,287,009,122 | 2,727,118,697,790 | 2,727,118,697,793 | 25,076 | −3,003 | −2,645 |

以 UUUS 为例，最佳规模约 `0.000049460 WETH`，确实遗漏在网格以下，但两个头的该 calldata
基础成本分别为 `3,098,100,681,440` 和 `3,048,558,830,672 wei`，均高于连续上界。其余所有
路线由通用 `21,000 × baseFee` 已排除。全额排除结论绑定这批池、两个头、单路径模型和这里的
调用封装。替代执行器、其他类型 AMM、共享池拆单及交易排序均超出本轮观察。

## 重放与保留件

原始请求、响应、UTC 和单调时钟、SQLite 原预算账本、范围、完整池状态和结果在忽略目录
`reports/research/multichain-forward-20261122/ethereum-wide/`。不联网复跑采集解码和金额诊断。

```sh
.venv/bin/python scripts/run_eth_v2_wide_shadow.py replay
.venv/bin/python scripts/run_eth_v2_wide_shadow.py diagnose
```

影子代码通过 `solc 0.8.26+commit.8a97fa7a.Darwin.appleclang` 本地编译，编译选项为
`--optimize --via-ir --bin-runtime --metadata-hash none --evm-version cancun`。本轮没有达标
路线调用该运行时，混合代码的可执行性尚未通过真实池的状态覆盖模拟验证。

| 保留件 | SHA-256 |
| --- | --- |
| `scope.json` | `44e45e2e267a5de824764f4dc2a303cfe76932c93e432bc3c4bf4fe51ac227a7` |
| `result.json` | `b72c27766c63c6f264b6a201b44ef35f3fc3885cc1a50f82e0068110cd0d623b` |
| `diagnostics.json` | `40ffe06779e58f29f56f571288dae604eb12b1299c281015e18daa34ffde4546` |
| `original-budget.sqlite3` | `35c53147a0ec39fa772af65b278b0f6e5ab73245ddc5819a46cdc092ac747010` |
| `probe-runtime.hex` | `f6dbeb1509363173b064d8ff6109c0a47e34c11a2796945c9dcf54d99edc71fb` |
| 所有 `.meta.json` 原字节按文件名排列，以单个 LF 相连 | `840757189f6bc4d9cd241625ff78d7a156ff445aca9499e01baa5639293cb1f8` |
