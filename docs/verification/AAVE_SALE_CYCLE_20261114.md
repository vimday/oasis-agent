# Aave 借出、链上卖出与再抵押

2026-11-14，基线 `72dc0fe`，Codex 作者自审。研究范围为 Ethereum Core 和账户
`0x0cdd9cbb825212c6cc5b0b36239e94218ee661d8` 在 2026-10-29 的早期交易。
原始采集、冻结计划及日志留在 `reports/research/aave-sale-20261114/`。
[前序冻结与买回验证](AAVE_FREEZE_ORACLE_20261114.md)

## 一次借出、卖出、再抵押和再次借出

| UTC | 块 | 观测 |
| --- | ---: | --- |
| 20:48:23 | 26300889 | Aave 新增 500 WETH 债务。收据显示网关收到并解包 500 WETH；账户相邻块末的 ETH 增量加本笔 gas 等于 500 ETH。 |
| 20:48:35 | 26300890 | 另一地址向账户发送 1 ETH。收据成功，交易 value 确认为 1 ETH。 |
| 20:49:11 | 26300893 | 相同入口尝试卖出 500 ETH，收据 status 为 0，没有提交日志，仅计 0.440832 ETH gas。 |
| 20:50:11 | 26300898 | 同一补款地址再直接发送 0.3 ETH。该块是成功卖出的父块。 |
| 20:50:23 | 26300899 | 钱包交易 value 为 500 ETH。WETH Deposit 与输入量同额，USDC Transfer 及块末钱包余额确认到账 1,339,879.335724 USDC。付出 0.36279325 ETH gas。 |
| 20:51:47 | 26300906 | 将同额 USDC 存入 Aave，收据的 Supply 与底层 USDC 转账唯一匹配，钱包 USDC 余额由全部卖出所得降至 0。 |
| 20:53:11 | 26300912 | 同一账户又借出 500 WETH，网关收到并解包同额。 |

首次借款、卖出、再次抵押三个相邻块边界中的 ETH、WETH 和 USDC 残差全为零。第一笔
借款块末到卖出父块的 ETH 变动恰好等于两笔补款减失败 gas，稳定币和 WETH 余额不变。
卖出块末到再抵押父块的三项余额不变。本组七笔选定交易由账户支付的收据 gas 合计
**0.98010265 ETH**，包含失败成本。补款计入资金流，未将其记作策略收入。
[第一笔借款](https://etherscan.io/tx/0xcb39901f2228cef793728c4b53b68793705f8eca38618981ac88bf4003fb4aa6)
[失败尝试](https://etherscan.io/tx/0x7e56dd5576366b648c2e8e17536681d7ea5247bebff431e4cb2e524a6f2c1807)
[卖出](https://etherscan.io/tx/0x60f6b6398484ae04f32c96d67b7f4f6cc9e479fc4f4925a580f5bcad771df124)
[再次抵押](https://etherscan.io/tx/0x7e07a508c39670a5b220350622eff6078d5b1b843b93a4ae6c6b583d3feae434)
[再借款](https://etherscan.io/tx/0x0673e2c404cfccfd1bd4827cc6cdcf931f439885617e63e3c74acbba9df86131)

同额、顺序和边界余额支持这次资金循环。没有完整内部 trace、逐交易资金来源证明或全窗
钱包对账。二次借款为新增债务，后续买回、偿还、抵押撤出仍需要按实际数量逐笔匹配。
全窗原生残差及五笔优先 trace 见[前序记录](AAVE_FREEZE_ORACLE_20261114.md)。

## 卖价与同一父块借贷估值

500 ETH 实收 1,339,879.335724 USDC，即 **2,679.75867144 USDC/ETH**，未扣账户 gas。
父块 26300898 的 Aave 估值是 USDC **0.99977974 USD**，WETH **2,249.316 USD**。
同块 WETH 源的 round 与协议值一致，更新距父块时间 84 秒。按该块的 USDC 估值折算，
实际收付约 **2,679.16842780 USD/ETH**，高于父块 WETH 估值约 **19.11%**。
块内各交易前状态、同期 CEX 可成交价、真实边际容量和完整成本仍未验证。
[Aave Oracle](https://aave.com/docs/aave-v3/smart-contracts/oracles)
[WETH9 源码](https://github.com/dapphub/ds-weth/blob/master/src/weth9.sol)

交易收件地址与 0x 公布的 Ethereum AllowanceHolder 地址相同。分析以原始 value、
包装和到账日志为准，尚未核对路由部署等价性或各池交易、路由费和支付给构块者的成本。
[0x 协议合约](https://docs.0x.org/docs/core-concepts/contracts)

## 采集与离线核验

在旧钱包 STOP 保持有效的情况下，冻结了两个新查询范围。卖出交易及父块、价格源和
余额采集 43 次，借款与再抵押边界和六笔目标交易的收据、value 采集 58 次。两批共
101 次匿名、免费只读请求，全部完整，预占原始账本后发出。每个范围首错即停。
[Alchemy 公共端点](https://www.alchemy.com/rpc/ethereum)

在旧收据计划中可选 `include_transactions: true`，每笔增加一次
`eth_getTransactionByHash`。解析交易 hash、所在块及索引、chain ID（若返回）、原生
value、输入字节和角色，比较时检验交易与收据的 sender/recipient。保留原始响应，
报告仅输出 selector 和对账数据。旧计划回放原有请求序列。
[Ethereum JSON-RPC 交易对象](https://ethereum.github.io/execution-apis/api/methods/eth_getTransactionByHash/)

```sh
sale=reports/research/aave-sale-20261114
.venv/bin/python -m scripts.research_aave_sale \
  --incident-plan reports/research/aave-regimes-20261114/incident-plan.json \
  --sale-plan "$sale/reviewed-sale-plan.json" \
  --cashflows reports/research/aave-index-20261114/cashflow-boundaries-plan.json \
  --sale-campaign "$sale/campaign" --link-campaign "$sale/links-campaign" \
  --output "$sale/replay-repeat.json"
```

冻结计划包含七笔交易身份和已选合约地址。缺证据或冲突退出 2，余额差保留报告并退出 3。
两次正式回放输出逐字节一致，报告 SHA-256
`70ce4d6ccbbba96530e8bb854f5486c767f911c13d83246d89d6189caf666a3e`。
另有九次 Elysium 匿名免费只读请求，复核赢家和五笔前序收据。全轮累计 110 次免费请求。
下一步完善全窗买卖与原生结算，并推进[多链其他验收项](../strategies/NEXT_STAGE_EXECUTION.md)。
[Elysium 前序收据](ELYSIUM_CYCLE_RESEARCH_20261111.md#2026-11-14-赢家前序收据)

## 本机验证

| 检查 | 结果 |
| --- | --- |
| 聚焦回归 | 退出 0，168 项，`focused.log` |
| `make check` | 退出 0，6,180 Python、138 子测试、9 前端模块、82 Foundry，`make-check.log` |
| `make security-check` | 退出 0，依赖审计、Bandit 和秘密扫描，`security-check.log` |
| `make contract-security-check` | 退出 0，85 合约、102 detectors、0 findings，`contract-security-check.log` |
| 文档及 diff | 退出 0，4 份文档、53 个本地链接、2 段 shell、CLI help，`docs-check.log` |
| 旧回放入口 | 原 Aave 冻结和价格报告逐字节一致，Elysium 原块末调用回放通过 |

Python 全量保留 11 项既有警告。选定交易互换的反例被离线入口拒绝。
