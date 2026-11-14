# Aave 历史借还款与底层转账

2026-11-14。基线 `3731b55`。Codex 作者自审，独立审阅待完成。账户为
`0x0cdd9cbb825212c6cc5b0b36239e94218ee661d8`，范围为 Ethereum Aave V3。
冻结计划、原始回复、账本和报告位于 `reports/research/aave-index-20261114/`。

## 实际读取与反证

| 读取 | 实际结果 |
| --- | --- |
| Blockscout 五个 Pool 索引过滤器 | 26300600–26302600，借款、抵押、还款、撤回、清算均返回空数组，累计 10 次。数据仅为索引输出。 |
| Routescan 头寸代币索引 | 同窗口，aUSDC 34 条、variableDebtWETH 30 条、aWETH 0 条，三次读取完整，取得 62 个唯一交易候选。 |
| Alchemy 候选收据 | 开闭头与交易位置固定，62/62 完整，每笔成功，原范围用满 248 次。实收据复核 64 个头寸代币转移，逐笔一致。 |
| Alchemy 余额边界 | 固定 26300877 与 26301547，40/40 完整。两个块末均观察到 `totalDebtBase` 及 WETH 可变债务余额为零。 |
| Alchemy 实现代码 | 固定两个边界的三个实现地址，12/12 完整。使用相同字节码，逐个计算 Keccak。 |
| Routescan 钱包底层转移 | 26300878–26301547，第一笔 USDC 索引命中 200 条页容量，1/2 STOP。保留截断回复，WETH 未查询。 |

所有网络请求使用已核对的匿名免费端点，每个范围在 I/O 前冻结查询及上限，首错停止。
本轮合计 314 次，费用上限 USD 0。三个来源的接口依据见
[Blockscout ETH RPC](https://docs.blockscout.com/devs/apis/rpc/eth-rpc)、
[Routescan 账户索引](https://docs.routescan.io/api/etherscan/accounts)及
[Alchemy 公共端点](https://www.alchemy.com/rpc/ethereum)。Powered by Routescan.io APIs。

原 Routescan Pool 日志脚本因漏传 `now_ms` 在预占前退出，计数 0。正式历史索引入口
复用了正常采集器和预算测试。头寸代币查询采用独立过滤对象，真实运行前用 mock 验证
时间参数、预占与 429 停止。两个旧 STOP 文件和账本均保留。
[旧 Routescan 记录](METRIC_STATE_PROOFS_20261114.md#aave-线索复核)

Blockscout 的空索引与后续在同一窗口、同一合约上复核到的 60 个 Pool 事件不一致。
原因仍未知。新索引入口始终输出 `historical_index_leads`，空数组不用于判断覆盖。
候选去重后按块及交易索引冻结清单，preflight 上限为 248 次。逐笔比较 Routescan
与 Alchemy 收据的头寸代币、发送方、收款方及整数数量，64 行全部匹配。
原 PublicNode `null` 的交易也在这批候选中，现确认是最后一次还款。
[原收据失败](AAVE_RECEIPTS_20261114.md)

## 账户的已观察头寸流

单位按官方地址簿中的 USDC 6 位与 WETH 18 位解释。下表全为已观察事件与同一收据
底层 ERC-20 转账匹配之和，保留每笔日志索引和费用。完整收益仍待账户资金对账。
[Aave Ethereum 地址簿](https://github.com/aave-dao/aave-address-book/blob/main/src/AaveV3Ethereum.sol)

| 操作 | 笔数 | 底层数量 | 首末时间（2026-10-29，UTC） |
| --- | ---: | ---: | --- |
| 抵押 | 20 | 36,868,428.030751 USDC | 20:45:59–21:32:35 |
| 借款 | 18 | 11,701 WETH | 20:48:23–21:31:47 |
| 还款 | 12 | 11,701.033423428300863309 WETH | 21:49:59–22:50:35 |
| 撤回 | 10 | 28,500,000 USDC | 21:43:11–22:35:59 |

首笔借款的 Pool 事件记载了 WETH、500 WETH 本金、可变利率模式与债务人；WETH
底层转给 `0xd01607c3c5ecaba394d8be377a08590149325722`，同一收据显示该地址解包。
该借款的债务代币铸造为 500 WETH 等值多 1 wei，底层收付从独立 Transfer 取数。
还款总量比借款多 0.033423428300863309 WETH。62 笔样本内由账户支付的收据 gas 分量合计
3.695320608908860710 ETH，尚未覆盖现货交易以及所有费用。
[首笔借款](https://etherscan.io/tx/0xcb39901f2228cef793728c4b53b68793705f8eca38618981ac88bf4003fb4aa6)
[最后还款](https://etherscan.io/tx/0xee34ec92ed08f695fda8675a5aabddbc4a71777dafd3b022f7c21675f31cd99d)

| 块末 | 钱包 USDC | 钱包 WETH | aUSDC | WETH 可变债务 |
| --- | ---: | ---: | ---: | ---: |
| 26300877 | 2,099,000 | 0 | 0 | 0 |
| 26301547 | 1,596,791.218642 | 0 | 0.500526 | 0 |

所选收据另有两笔非销毁 aUSDC 转出至 `0xa8053d616cd4a1bd5e033524cf17c6193c76e5ab`，
合计 8,368,692.126402 aUSDC。接收地址归属与最终资金用途尚未核实。aUSDC 的名义
Transfer 净和为 0.500527，终点 `balanceOf` 为 0.500526，名义日志求和相差 1 个基础单位。
另做一遍条件缩放轨迹。按固定 v3.5 公式，抵押向下取整、撤回及借款向上、偿还向下并
限制为现存缩放债务，对非零地址转移读取 `BalanceTransfer` 的缩放数量。60 个受控
Mint/Burn 的余额变化及 `balanceIncrease` 全部匹配。加入两笔转出后，aUSDC 与
variableDebtWETH 的缩放末值分别为 420040、0，和各自块末读数完全一致。辅助计算
留存 `experimental-scaled-replay.json`，作为已选交易和公式条件下的结果。待将公式接入
正常回放，并验证部署与该源码等价。
[Aave TokenMath](https://github.com/aave-dao/aave-v3-origin/blob/8305565ae342f1773c42cd2e4593f175fe5968a0/src/contracts/protocol/libraries/helpers/TokenMath.sol)
[AToken 转移逻辑](https://github.com/aave-dao/aave-v3-origin/blob/8305565ae342f1773c42cd2e4593f175fe5968a0/src/contracts/protocol/tokenization/AToken.sol)

离线重算 62 个收据头与两个边界的 Ethereum Prague 哈希全部匹配。两个边界中 Pool、
aUSDC、aWETH 和 variableDebtWETH 的代理代码哈希及 EIP-1967 实现地址一致。
Pool 指向 `0x728a138a4823392c2efa55e028d434f526fe03cf`，aToken 指向
`0xadc45df3cf1584624c97338bef33363bf5b97ada`，债务代币指向
`0xb27c52c7f84819e3157993489741cb60786ae330`。三个实现的 runtime 分别为
21,146、10,477、7,198 字节，两个边界全字节相同，散列见 `implementations/comparison.json`。
中间升级、实现源码等价、收据 trie 证明及独立余额读数仍待验证。

## 回放入口

`oasis-public-event-index-plan-v1` 固定链、索引源、最多 4096 块、目标合约、精确
事件签名和 account topic 位置。两次预算请求，返回粗略候选；排除错 topic、重复位置、
移除日志和满页。它独立于前瞻日志覆盖。

`scripts.research_aave_receipts` 可复核最多 64 个 capture，拒绝冲突的块哈希和交易
位置。`--cashflows` 固定 reserve、aToken、精度及可选的 variable debt token。
对 Supply、Borrow、Withdraw 和底层 Repay，按代操作者/接收者、token、方向、整数
数量唯一匹配。多义、缺失、aToken 还款或清算均保留单独状态。`--boundaries` 绑定
首笔所在块的父块和最后一笔所在块，严格解码该账户的余额、缩放余额和 Pool 风险视图。
匹配语义见 [Aave BorrowLogic](https://github.com/aave-dao/aave-v3-origin/blob/8305565ae342f1773c42cd2e4593f175fe5968a0/src/contracts/protocol/libraries/logic/BorrowLogic.sol)
及 [ERC-20](https://eips.ethereum.org/EIPS/eip-20)。

```sh
research=reports/research/aave-index-20261114
set --
for receipt in "$research"/alchemy-campaign/tx-*; do
  set -- "$@" --capture "$receipt"
done
.venv/bin/python -m scripts.research_aave_receipts \
  --plan reports/research/aave-history-20261114/analysis-plan.json \
  --cashflows "$research/cashflow-boundaries-plan.json" \
  --boundaries "$research/boundaries/campaign/start" "$research/boundaries/campaign/end" \
  "$@" --output "$research/another-cashflow-report.json"
```

正常 CLI 两次回放逐字节一致，报告 SHA-256
`da6dfca351a7b9b017161afbd741186cf05b8eee960897a084dc8a6ff79e56d4`。接下来用固定
子区间取得钱包 USDC、WETH 及原生转移，闭合资金、swap 方向和买回价格，并以同期预言机
价格核验价差和清算风险。原满页回复不会计入完整账本。

| 本机检查 | 退出码及结果 |
| --- | --- |
| 聚焦回归 | 0，147 项，`focused-reviewed.log` |
| `make check` | 0，6,155 Python、138 子测试、9 前端模块、82 Foundry，`make-check.log` |
| `make security-check` | 0，锁定依赖、Bandit、秘密扫描，`security-check.log` |
| `make contract-security-check` | 0，85 合约、102 detectors、0 findings，`contract-security-check.log` |
| 文档及 `git diff --check` | 0，4 份文档、55 个本地链接、5 段 shell、2 个 CLI help，`docs-check.log` |

全量 Python 保留既有 11 项警告。本机 Python 3.12.4、Node 23.11.0、Foundry 1.8.4
Homebrew。原始响应和辅助计算留在忽略目录。
