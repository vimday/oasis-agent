# BSC 和 Base 多跳交易前状态回放

北京时间 2026-11-23。使用匿名免费端点、历史只读 RPC 和本地 EVM。BSC 回放目标块 135,115,034、索引 2，Base 回放目标块 60,871,104、索引 30。两条链的父块、成交块与下一块已由不同端点核对哈希及父子关系。区块结束状态和块内交易前状态分别记录。历史样本没有 Oasis 实时采集或候选发现时钟，以下数字按可核对的链上状态断面排列。[上一轮的资金流与样本选择](LONG_SWAP_TAIL_20261123.md)

## BSC · 父块亏损、块内翻正、下一块再次亏损

区块索引 1 的前序交易分别触及 5 跳路线的第一池和第五池，两个 RPC 回执一致。目标交易内部在五跳前还两次交换第一池和变现池。父块末、精确交易前、支路实际执行时与成交块末分别取证。闭环沿 USDT → `0x25d8…bb00` → `0x6081…8f7f` → USDC → WETH → USDT，费率依次 2,500、500、500、500、100 ppm，六个池的币种和地址均已用固定状态和工厂映射核对。[块内前序交易](https://bscscan.com/tx/0x7f7038389b46f3475a82d0542a2cc295ff09937b2e50a8bc75edf2ff5d3d2359) · [目标交易](https://bscscan.com/tx/0xa2c11e2c90a5d5fca43a6da0e349d5daf133bf1b654425adf3496fde020caf88) · [Pancake 官方部署清单](https://github.com/pancakeswap/pancake-v3-contracts/blob/main/deployments/bscMainnet.json)

| 状态断面 | 固定投入 1.399229278444666368 USDT 的五跳毛差 | 额外一跳收到的 WBNB |
| --- | ---: | ---: |
| 父块末，公开状态对照 | −0.009179712192131452 USDT | 无正差额 |
| 精确交易前，先本地执行块内索引 0、1，再用官方 Quoter 报价 | +0.015804614987385687 USDT | 28,227,439,169,127 wei，独立报价 |
| 整笔本地模拟完成，从其中的五跳支路及变现转账读回 | +0.013386390765866008 USDT | 23,908,475,818,923 wei |
| 成交块末，按原额重新报价 | −0.004725517014086641 USDT | 无正差额 |
| 下一块末，按原额重新报价 | −0.005972090923359275 USDT | 无正差额 |

实际支路保留精确交易前报价的 **84.70%**，兑换后少 **4,318,963,350,204 WBNB wei**。整笔执行经 310 个 Foundry 调用帧，所有 114 条日志的地址、topics、data 和顺序与规范回执相同。Foundry 1.10.0 按同块前序顺序回放，保留原 EIP-7702 授权。根调用 2,878,218 gas 加 121,100 intrinsic，扣 367,900 执行 refund 和 12,500 EIP-7702 非空账户 refund，复算出回执的 **2,618,918 gas**。实际 gas 单价为零。模拟的是已落块交易；正常影子机会的付费假设仍待独立测量。[EIP-7702](https://eips.ethereum.org/EIPS/eip-7702#set-code-transaction) · [Pancake QuoterV2](https://github.com/pancakeswap/pancake-v3-contracts/blob/main/projects/v3-periphery/contracts/lens/QuoterV2.sol)

整笔 37 条 Swap 还含其他支路。执行器的普通 WBNB 转账净额为 −6,027,138,070,031,066 wei，匹配 6,384,841,952,825,896 wei 包装与 357,703,882,794,830 wei 解包后，WBNB 净变动为零。执行器原生 BNB 余额变动也为零；两个地址分别收到 339,818,688,655,089 与 17,885,194,139,741 BNB wei。链上可见支付与五跳毛差分列，收款地址控制、额外结算及整笔策略净利仍未证实。下一块的原始完整调用返回 `no success route`，独立 Quoter 提供了可比的路线亏损值。

匿名 Blast 的普通归档读取成功，debug 接口返回 401。BSC 官方端点出现 `missing trie node`，PublicNode 的历史读取受限，dRPC 返回 429 或历史状态不可用，SQD 状态差分请求返回 529。Foundry 通过只读代理使用 Blast 已核对的数字块标签及同一累计账本，精确交易回放使用 608 次读取，独立交易前报价完成时累计 621 次，均为 $0。原始失败和本地工具适配保留在同一留档。

## Base · 精确复现 35 条 V4 Swap，后续按 nonce 拒绝

dRPC 用成交块哈希和索引 30 重放原始调用，包含 **2,114 个调用帧、298 条有序日志及 7,684,317 gas**，全部日志与 PublicNode 规范回执逐字节对齐。索引 29 也匹配。仅使用父块状态重放成功，但其中一条日志的数据与回执不同。索引 31、成交块末和下一块末重放原操作均在 EntryPoint v0.6 以 `AA25 invalid account nonce` 回滚，消耗 70,031 模拟 gas，均未触发 Swap。同一操作的后续回滚测到的是权限状态，市场状态由下列查询另测。[Base 原交易](https://basescan.org/tx/0x5b8491be0ae575eceee782601a40a77debe134080f7935f7492f52a33163b14d) · [EntryPoint v0.6](https://github.com/eth-infinitism/account-abstraction/blob/v0.6.0/contracts/core/EntryPoint.sol)

35 次 Swap 对应两个从 calldata 解出的 PoolKey，重算 PoolId 匹配收据。两池共用同一 Zora Post Hook，费率 10,000 ppm、tick spacing 200。池对为 `0x1111…fc69 / 0xcbe4…2c6a` 与 `0x66fc…8af9 / 0xcbe4…2c6a`。前者 25 次 `zeroForOne=false`，后者 10 次 `zeroForOne=true`。StateView 显示索引 30 前与父块相同，索引 31 和下一块末两个池的 sqrt 价格均改变，两池流动性各自不变。当前记录未证实可独立出资并回到同一资产的套利闭环。该样本的策略净利与寿命留空。[Base V4 部署](https://developers.uniswap.org/docs/protocols/v4/deployments#base-8453) · [Zora Post Hook 地址](https://github.com/Uniswap/routing-api/blob/main/lib/util/hooksAddressesAllowlist.ts)

整笔账本中，外层执行费 47,981,274,932,484 ETH wei，L1 数据费 7,598,624,923 ETH wei，GasPriceOracle 的 Operator fee 为 **0**。EntryPoint 向 bundler 结算 58,393,651,207,320 ETH wei，扣外层费用后 bundler 可见增加 **10,404,777,649,913 ETH wei**，与同笔余额状态差一致。UserOperation 的付款由 paymaster 层承担。该捆绑服务结余按角色单列，未计为 V4 套利余额。[OP Stack 费用规范](https://specs.optimism.io/protocol/isthmus/exec-engine.html#operator-fee)

## Base 四池对照 · 边际正价差在有限金额报价后消失

沿用现有 WETH/USDC 四池定义。12 个有向两池组合的边际费后扫描出现两个正值，分别为 Slipstream 1 → 2 的 **+13.0848 bps** 与 Uniswap V3 → Slipstream 2 的 **+6.8098 bps**。两个组合在 0.1 WETH 下都用实际首腿输出接第二个官方 Quoter，两个池不同。父块末、精确索引 30、索引 31 与下一块末返回相同的有限金额输出。父块末和下一块末的较优边际组合第二腿通过 PublicNode 复核。[仓库四池定义](../../src/oasis_agent/fastpath/live_shadow_runner.py) · [Slipstream 合约](https://github.com/aerodrome-finance/slipstream)

| 状态 | Slipstream 1 → 2 估计净额 | Uniswap V3 → Slipstream 2 估计净额 |
| --- | ---: | ---: |
| 父块末 | −8,614,338,909,139,091 WETH wei | −7,823,951,989,862,630 WETH wei |
| 精确交易前和紧接其后 | −8,614,340,293,139,091 WETH wei | −7,823,953,373,862,630 WETH wei |
| 下一块末 | −8,614,342,344,639,091 WETH wei | −7,823,955,425,362,630 WETH wei |

毛差分别为 −8,610,713,012,195,365、−7,820,326,092,918,904 WETH wei。净额估算使用项目 500,000 gas 模型、固定 2,000,000 wei tip 与 512 字节 L1 费用上界。报价真实跨了初始化 tick，薄流动性的第二腿吞掉边际价差。这里的 L2 gas 是模型，四池未发单。Base 精确历史 trace 请求的本地耗时为 540 ms；该计时不含实盘发现、状态传输或落块等待。

## 长尾新增样本 · 11 笔完整回执

| 新增窗口 | 完整校验的块 | 有匹配 Swap 的交易 | ≥5 | ≥10 | ≥20 | ≥50 | ≥100 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Base 60,885,193–60,889,288 | 4,096 | 12,794 | 120 | 9 | 3 | 1 | 0 |
| BSC 135,106,680–135,110,757 | 4,078 | 84,223 | 1,376 | 213 | 58 | 7 | 0 |

BSC 原目标窗口在 48 MiB 响应预算处截断。只统计截至 135,110,757 的完整 NDJSON 行，逐块核对高度和父哈希。Base 两个额外窗口、BSC 两个额外窗口和 Ethereum 窗口被 SQD 以 HTTP 529 停止。这些窗口不纳入分母。此轮已校验 Base 的 50 / 30 / 20 / 19 条及 BSC 的 80 / 66 / 64 / 60 / 59 / 52 / 50 条完整回执。[SQD EVM 查询](https://docs.sqd.dev/en/portal/evm/api)

- 新的 **BSC 80 条 / 42 池**样本里，80 笔都可按地址池和金额绑定到 ERC-20 转账，60 条唯一直接转账边组成 20 段各四跳的路径。**60 条 / 32 池**样本的 45 条转账边同样对应 15 段各四跳。另有 **64 条 / 2 池**，可解码的地址池子集未见串行转账边。缺少的 singleton、分支或中间结算仍保留未知。[BSC 80 条](https://bscscan.com/tx/0xb7eddffa1279089a6c93efae1037d91db98685c6b34e3f5904ac3dba128c4e17) · [BSC 60 条](https://bscscan.com/tx/0x10f4f551a5b96afa904394d8fb9449eaea69deea73eca3037ce56048f61d6464)
- 新的 **Base 50 条 / 2 池**样本复用上述两个 Zora Hook PoolId，2,822 个调用帧将 50 条日志分入两条外层分支，分别 5 条和 45 条。**20 条 / 20 池**样本来自官方 Universal Router，236 帧包含多个外层分支，地址池子集可证两跳。Zora 与通用路由样本都可用来防止把 Swap 条数计成串行深度。[Base 50 条](https://basescan.org/tx/0xbc860b989bc68c8f449e3a6d4a59bdc28a0cd898c6d652fd74db80393c961246) · [Base 20 条](https://basescan.org/tx/0x4f05bfa344adcf8b3cd6707f2a74fcc632a7b2299d4b2284717cc7db4136d456)

目前仍未核实几十跳的纯串行资金路径，也未核实单笔 100 条 Swap。下一轮保留阈值 5、10、20、50、100、200，并继续优先找去掉批量和重复用池后仍有长资金依赖的样本。

## 回放与验收

新增[离线交易位置审计入口](../../scripts/replay_pretrade_trace.py)，绑定连续三块、成交位置、回执和原调用。在 Geth 形态的调用树里跳过回滚祖先，按调用位置重建有序日志。将 RPC 拒绝、账户 nonce 拒绝、执行和完全匹配分开记录。保留 EIP-7702 授权等原调用字段，拒绝状态覆盖。原字节与用量留在本地忽略目录 `reports/research/pretrade-tail-20261123/`。该目录中的 `bsc-local-fork-pinned/cast-extended.stdout` 与 `forge-exact-quote.stdout` 分别是整笔回放与精确交易前报价，SHA-256 分别为 `c49191810d0dcaf2ecbfc043050109baf9529b3c7f895289ae11308829974724` 和 `249ecef242622cb88a2ab16fac082dd2b878205a260ef1d3683d87a7d1c35ffb`。

`make check`、`make security-check`、`make contract-security-check` 均退出 0。全量通过 6,600 个 Python 测试（另有 138 个子测试）和 125 个合约测试，Python 报告 10 条警告，Slither 保留 6 条既有发现。检查日志在本机 `/private/tmp/oasis-pretrade-{check,security,contract}-20261123.log`。
