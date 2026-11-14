# Aave V3 Core 冻结、预言机与单笔买回对账

2026-11-14，基线 `c0bf3e3`，Codex 作者自审。范围是 Ethereum Core 市场和用户给出的
账户 `0x0cdd9cbb825212c6cc5b0b36239e94218ee661d8` 在 2026-10-29 的选定交易。
原始证据、冻结计划和本机日志保存在 `reports/research/aave-regimes-20261114/`。
[前轮钱包对账](AAVE_WALLET_LEDGER_20261114.md)

## 两个不同的冻结时点

Aave 风险报告给出的首笔冻结交易在 Ethereum 26301157 块索引 245，
时间为 21:42:11 UTC。该收据没有目标 V3 Core Configurator 的储备冻结事件；
USDC 和 WETH 在 26301156、26301157 两个块末的储备配置相同，冻结位均为假。
[风险报告与首笔交易链接](https://governance.aave.com/t/binance-hot-wallet-exploit-aaves-response/25954)

用官方地址簿的 Configurator 和 `ReserveFrozen(address,bool)` 签名查询
26301158–26301493 的公开索引。返回 32 条、三个交易候选。另取完整收据确认其中
26301183 块索引 127 的交易在 21:47:23 UTC 冻结了目标 USDC 和 WETH，日志索引
分别为 312、316。地址提供者、Pool 和 Configurator 的绑定与冻结块前后配置读数一致。
两个资产的冻结位均由假变真，暂停位均保持假。
[Core 冻结交易](https://etherscan.io/tx/0x405bee93def8c9114e28a5338d03946b9d1c8e4dd6ff318973ce71723c0038bd)
[官方地址簿](https://github.com/aave-dao/aave-address-book/blob/main/src/AaveV3Ethereum.sol)

| 储备 | 父块末 LTV | 冻结块末 LTV | 两端清算阈值 |
| --- | ---: | ---: | ---: |
| USDC | 75% | 0% | 78% |
| WETH | 81% | 0% | 84% |

已选收据中的最后一笔借款早于该交易 15 分 36 秒，在 26301105 块借出 500 WETH。
冻结后所选收据仍包含还款。上述 LTV 是储备配置，具体账户的有效借款上限和健康因子
仍需要抵押、模式及精确交易前状态。
[最后一笔已选借款](https://etherscan.io/tx/0xb4c760a7c686132316db6601b30e209a82aaa02a22106f19a4cae18796f9bf01)
[配置位布局](https://github.com/aave-dao/aave-v3-origin/blob/8305565ae342f1773c42cd2e4593f175fe5968a0/src/contracts/protocol/libraries/configuration/ReserveConfiguration.sol)

## 协议估值与实际收付

固定块读回的地址提供者、oracle 及资产价格源与官方地址簿相符。oracle 基础币种是
零地址表示的 USD，单位为 1e8。两处样本内，每个源的 `latestAnswer()` 都与 Aave 的
`getAssetPrice()` 相同；WETH 源同时返回了同值的 round。USDC 样本只核对源值和精度，
轮次及价格年龄留空。这里使用仓库既有的 1,800 秒 WETH 研究读数上限。
[Aave Oracle](https://aave.com/docs/aave-v3/smart-contracts/oracles)
[Chainlink 历史轮次](https://docs.chain.link/data-feeds/price-feeds/historical-data)

| 观测点（父块末） | USDC/USD | WETH/USD | WETH round 年龄 |
| --- | ---: | ---: | ---: |
| 26301104，最后一笔样本借款的父块 | 0.99977974 | 2281.37914283 | 0 秒 |
| 26301493，WETH 收入交易的父块 | 0.99977974 | 2270.26764105 | 1560 秒 |

22:49:35 UTC 的收据显示，该账户付出 100,000 USDC 并收到 43.478260869565217392
WETH。观察到的数量比约为 2,300 USDC/WETH。以同一父块的 USDC 估值换算是
2,299.49340199 USD/WETH（E8 向下取整），比该父块的 WETH 债务估值高约 1.29%。
交易发送方是另一地址，因此这里只归因账户的两种代币收付，路由、其他资产转移、参与者
归属与全部成本继续待证。这是实际买回样本与父块借贷估值的比较，交易前的精确状态、
CEX 成交价、借出后的卖出价格和全周期收益均未知。
[收付交易](https://etherscan.io/tx/0x1fd2ea4dcc5c4c1b8cf7ff98dd288b24ff3ce38b483d6287a5316b0650be3f6d)

22:53:59 UTC 的第二笔收据由该账户直接调用 WETH 合约，唯一的 `Withdrawal` 事件扣掉
同额 WETH。固定块末余额显示 WETH 从 43.478260869565217392 降至零，扣掉
0.003601052 ETH 的收据 gas 后，原生余额相邻块末增加 43.474659817565217392 ETH。
购买进账的 WETH 块末差、解包 WETH 差和解包 ETH 扣 gas 后差均为零。四个块末的
WETH runtime Keccak 一致；本轮保留实际 runtime 与 WETH9 参考语义，未做源码重编译等价核验。
[解包交易](https://etherscan.io/tx/0x4a2a8638b79ac61a7468d49ad302480dc3c55da8b84268d00e00877d0bba16fc)
[WETH9 源码](https://github.com/dapphub/ds-weth/blob/master/src/weth9.sol)

将两笔收据附加到原钱包离线入口后，204 个索引候选已有 73 笔完整收据，仍缺 131 笔。
原始索引按 ERC-20 Transfer 比较，WETH9 的解包是独立的 Withdrawal 事件，所以原始
Transfer 与余额之间仍显示 43.478260869565217392 WETH 差额，以上事件和余额解释了
这组买回及解包的差异。原生全窗余额与索引仍差 509.203424865618933788 ETH，
交易级 trace、关联资金及独立来源均待复核。

复用本轮的 26301493、26301494 两个 ETH 余额，把原先最大区间的差额进一步定位
到 26301494 单块。该块实际净增 419.585755213036901787 ETH，索引内部转账合计
741.219956847238535990 ETH，相差 321.634201634201634203 ETH。该数恰等于
同块另一笔交易内一组重复出现的两条同额、同发收方索引转账的合计。trace ID 各不相同，
报告保留原金额，待用执行 trace 或状态差确认两组是否都落地。
[优先追踪的交易](https://etherscan.io/tx/0x8e47af73769d93575dd82fd549d516bb5147a7ddf471a038792a0e9e352a7ca5)

全窗有六组这种索引形状，涉及五笔交易。若每组仅有一笔入账，解释量为
422.003817649918046486 ETH，仍余 87.199607215700887302 ETH。该敏感性计算
保留所有候选行，记录于 `trace-priority.json`，未据此修改原账本。

## 留存与复现

原钱包 STOP 保持有效。新实验只采集两笔包装资产目标、冻结历史观察和预言机读数。
Alchemy 原范围上限 160，实际 153 次固定只读调用，全部完整。另有两次 Routescan
免费索引查询定位事件，合计 155 次匿名读取，费用上限为 0。初次本机定位命令缺少
Python 导入路径，在创建范围及网络请求前退出。修正路径后完成唯一一次定位请求。
[Alchemy 公共入口](https://www.alchemy.com/rpc/ethereum)
[Routescan 日志查询](https://docs.routescan.io/api/etherscan/logs)

```sh
research=reports/research/aave-regimes-20261114
.venv/bin/python -m scripts.research_aave_incident \
  --plan "$research/incident-plan.json" --campaign "$research/campaign" \
  --borrow-receipt reports/research/aave-index-20261114/alchemy-campaign/tx-37 \
  --output "$research/incident-repeat.json"
```

正常离线入口以相邻块号、父哈希、收据交易位置绑定样本。对冲突和多义证据拒绝，
余额残差会写报告并退出 3。各回放两次输出一致，事件报告 SHA-256 为
`e6bdd32b178303b75fefd708830b6b52753d0756e38528adb1d4737056bf1344`。
下一步对比早期卖出与父块 WETH/USD。已选 26300899 的卖出线索中，外部索引标记
500 ETH，收据显示包装及进入账户的 1,339,879.335724 USDC。需结合原交易 value、
分腿费用、确切资金归属和父块价格完成归因。原生资金差额、其他退出费用和独立来源
继续按[阶段清单](../strategies/NEXT_STAGE_EXECUTION.md)维护。
[卖出收据线索](https://etherscan.io/tx/0x60f6b6398484ae04f32c96d67b7f4f6cc9e479fc4f4925a580f5bcad771df124)

## 本机验证

| 检查 | 退出码及结果 |
| --- | --- |
| 聚焦回归 | 0，144 项，`focused.log` |
| `make check` | 0，6,175 Python、138 子测试、9 前端模块、82 Foundry，`make-check.log` |
| `make security-check` | 0，依赖审计、Bandit、秘密扫描，`security-check.log` |
| `make contract-security-check` | 0，85 合约、102 detectors、0 findings，`contract-security-check.log` |
| 文档和 diff | 0，3 份文档、48 个本地链接、1 段 shell 及 CLI help，`docs-check.log` |

Python 套件保留 11 项既有警告。冻结的 Python 文件哈希、两次回放记录和原账本核对
留存于本机研究目录。
