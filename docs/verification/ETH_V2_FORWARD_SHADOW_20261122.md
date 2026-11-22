# Ethereum V2 前瞻影子观察

UTC 2026-11-22 15:59:27.561 至 16:00:11.977。四个连续 Ethereum 区块、六个 Uniswap V2 池、
每块六条三跳和六条四跳闭环。48 个路由区块观察中，0 条通过费后边际筛选，0 条可能超过观测
区块的最低 gas 成本。288 个预先固定的路线与输入组合均无正毛差额。六池储备在这四个区块
完全相同，这段数据只有一个独立价格状态。

## 范围和方法

预登记范围固定链号 1、Uniswap V2 正式工厂和 Router02，以及 WETH、USDC、USDT 和 DAI。
只考虑从 WETH 开始和结束、不重复中间资产与池的原子串行路线。输入网格为 `0.0001`、
`0.001`、`0.01`、`0.1`、`1`、`10` WETH。无凭据 HTTPS 主源为 PublicNode。上限为 12 次
HTTP POST、80 个逻辑 RPC 及 110 秒，本轮实际 11 次 HTTP、64 个逻辑 RPC、0 美元。每次网络
请求前在原始 SQLite 账本持久预占。原始数据是单一供应商的近期状态观察。 [PublicNode](https://ethereum.publicnode.com/) · [Uniswap V2 官方部署](https://developers.uniswap.org/docs/protocols/v2/deployments)

每轮解析收到的 `latest` 头，只把 `latest` 用于发现。池查询使用 `blockHash` 和
`requireCanonical: true`，按块号回查头与状态根，并对四个相邻头验证父哈希。首轮按工厂
`getPair` 确认六池，读取 `token0`、`token1` 和运行时字节，六池字节摘要一致。每轮使用
各池 `getReserves`，保留代币地址、精度、储备和合约字节摘要。 [EIP-1898](https://eips.ethereum.org/EIPS/eip-1898)

V2 精确报价逐跳使用 `floor(997 × reserveOut × amount / (1000 × reserveIn + 997 × amount))`。
每跳去掉取整后是 `a × amount / (b + c × amount)`，该函数单调递增，因此去掉所有取整得到
真实整数输出的上界。复合后仍可写作 `A × amount / (B + C × amount)`。若 `A <= B`，任意
正输入的毛差额都小于零。否则连续毛利上界为 `(sqrt(A) - sqrt(B))² / C`，用整数平方根向上
包住上界。再用 `21,000 × baseFeePerGas` 作为普通交易最低成本，价差与 gas 均按 WETH wei
和等值 ETH wei 对照。`21,000` 是保守下界，实际 swap 成本还取决于 calldata、EVM、优先费
和其他费用。本轮四个头的最高费后边际乘积约 `0.991637348237`，48 条路线均无需完整成本估计
即可排除。 [Uniswap V2 报价公式](https://github.com/Uniswap/v2-periphery/blob/master/contracts/libraries/UniswapV2Library.sol)

| 块号 | 区块时间 UTC | 收到头 UTC | 储备读完 UTC | base fee wei/gas | 最低成本 wei |
| --- | --- | --- | --- | ---: | ---: |
| 26,471,708 | 15:59:23 | 15:59:27.972 | 15:59:28.951 | 119,151,904 | 2,502,189,984,000 |
| 26,471,709 | 15:59:35 | 15:59:42.453 | 15:59:42.932 | 134,021,308 | 2,814,447,468,000 |
| 26,471,710 | 15:59:47 | 15:59:56.960 | 15:59:57.456 | 128,311,575 | 2,694,543,075,000 |
| 26,471,711 | 15:59:59 | 16:00:11.474 | 16:00:11.977 | 129,864,719 | 2,727,159,099,000 |

区块哈希由早到晚如下。原始证据还保留各头部的 parent hash、state root 和完整响应。

- `0x36cb9b074b42fe1dfbe6127894697fc7cfaee682d8f146e3a882aa65095e549a`
- `0x973e30da663ca641d9fa889fd61b70c732a263d73d36d09322a9c378849cee8a`
- `0x3475ed1056ce88a7680f2fbda0b63c0dd923b2a72cf0d60fd6335cef028277d6`
- `0x1b120845b55082a58ce4a6176b68162cb76406008fc522aa7c6ea04379c0aa47`

## 完整 Router02 影子执行

第二轮按预先固定的 `0.01 ETH` 输入排序，选其最大输出的 WETH → USDC → USDT → WETH。
路线用到的池依次为 `0xb4e16d0168e52d35cacd2c6185b44281ec28c9dc`、
`0x3041cbd36888becc7bbcbc0045e3b1f144466f5f` 和 `0x0d4a11d5eeaac28ec3f61d100daf4d40471f1852`。
在块 26,471,709 用 `eth_call` 执行正式 Router02 的 `swapExactETHForTokens`，`amountOutMin`
为 0，模拟地址 `0x000000000000000000000000000000000000d35c` 同时作为 sender 和 recipient，
在临时状态将其余额设为 2 ETH、代码设为空，供 EVM 模拟使用。真实池状态仍绑定该块。完全
相同的 caller、calldata、value 和 override 在模拟结束后首次采到的新头 26,471,710 再执行一次。
400,000 是调用的 gas 上限，本轮没有 gas 实耗或估算。无资金转账、签名或广播。 [Router02 源码](https://github.com/Uniswap/v2-periphery/blob/master/contracts/UniswapV2Router02.sol) · [Geth 状态覆盖](https://geth.ethereum.org/docs/interacting-with-geth/rpc/objects#state-override-set)

| 影子执行块 | 发送 UTC | 返回 UTC | Router02 返回原整数 |
| --- | --- | --- | --- |
| 26,471,709 | 15:59:43.149 | 15:59:43.420 | `[10000000000000000, 17104139, 17085631, 9916223845337372]` |
| 26,471,710 | 15:59:57.671 | 15:59:57.941 | `[10000000000000000, 17104139, 17085631, 9916223845337372]` |

两次整笔模拟均返回 `0.009916223845337372 WETH`，与精确报价一致。每笔不含 gas 的亏损为
`83,776,154,662,628 wei`。第一次模拟完成至后续头接收间隔 13.540 秒，两次模拟完成相隔
14.521 秒。经济达标样本为 0，条件在达标候选上的整笔模拟成功率、跨块存活率及实盘成交率
均无分母。负控的模拟执行成功为 2/2。

## 复核

原始请求、响应、UTC 和单调时钟、预算及解析结果保留在 Git 忽略目录
`reports/research/multichain-forward-20261122/ethereum/`。离线执行
`.venv/bin/python scripts/run_eth_v2_forward_shadow.py replay`，重新匹配原始预算计数、11 组
请求/响应哈希，解码并重算样本，核对与留存的 `result.json` 完全一致。

| 保留件 | SHA-256 |
| --- | --- |
| `scope.json` | `c1431437f15782537103176704eef08de23806861f3147739c46f6d567e82f05` |
| `result.json` | `625fa1924a8e2bdae174a412cd37ac982c20650826fa4607c7cad6a72449998e` |
| `original-budget.sqlite3` | `1d2ed2fc5ec6c746a7581c0d3a894ebf485092fc715cecf4e9036c993cb29ee0` |
| 所有 `.meta.json` 原字节按文件名排列，以单个 LF 相连 | `6b7bd0ae66e5e615d75a75ceaa986cd0b3bf1a5e9ea5ab010509ee1a6ed92854` |
