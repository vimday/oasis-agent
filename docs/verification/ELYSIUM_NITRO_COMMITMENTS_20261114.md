# Elysium 触发块与赢家块的 Nitro 承诺核验

2026-11-14，基线 `78b0a07`，Codex 作者自审。范围为 Elysium 99901 的
3628115–3628116 两块，原始材料在 `reports/research/elysium-commitments-20261114/`。
原触发、赢家、块末调用和失败探测均保留。[原始案例](ELYSIUM_CYCLE_RESEARCH_20261111.md)

## 块头、交易和收据

PublicNode 的全块收据实验事先固定两个块哈希，依次取链号、开头块头、按哈希的
`eth_getBlockReceipts`、收尾块头。每块各四次匿名免费请求，8/8 完成。
重算两块的 RLP 块头哈希，再检查两份收据列表的完整顺序、累计 gas、全块日志索引和
bloom，按位置 RLP 编码作 MPT 键，重算两份 `receiptsRoot`。
原留存的官方完整块另重建交易编码、恢复签名发送方、重算逐笔哈希与
`transactionsRoot`，交叉核验同位置的交易和收据身份。交易编码只在内存中使用。
[PublicNode Elysium](https://elysium.publicnode.com/)
[区块收据接口](https://ethereum.github.io/execution-apis/api/methods/eth_getBlockReceipts/)
[Offchain Labs 收据编码](https://github.com/OffchainLabs/go-ethereum/blob/master/core/types/receipt.go)
[EIP-1559](https://eips.ethereum.org/EIPS/eip-1559)

| 块 | 完整验证的交易和收据 | 关键交易位置 | 失败收据 |
| --- | ---: | ---: | --- |
| 3628115 | 27 | 触发索引 26 | 0 |
| 3628116 | 23 | 赢家索引 5 | 索引 9、22，收据 gas 分别为 35,894、24,036 |

失败收据只证明同块存在失败执行及对应 gas。是否有相同策略、是否为竞争者、链费用归因
和整个机会窗口的成本仍待核对。交易和收据的承诺绑定到固定的 L2 块哈希，
底层服务独立性及 L1/L2 终局证据仍待取得。

两个块首笔都是 Nitro 类型 `0x6a`，其事务数据符合
`startBlock(uint256,uint64,uint64,uint64)`。两笔的消息参数均为父链基准费 0、
父链块参数 50,849,150、时间增量 0 秒，各自的 L2 参数匹配 3628115、3628116。
3628116 的块头派生父链号字段是 50,849,152，保留这两个字段各自的来源和数值。
赢家之前索引 0–4 都成功，已验证其日志中没有赢家三池的发出地址。
需要执行 trace 或状态见证才能说明这些交易的读写影响。
[ArbOS 接口](https://github.com/OffchainLabs/nitro-precompile-interfaces/blob/main/ArbosActs.sol)
[Nitro 内部交易编码](https://github.com/OffchainLabs/go-ethereum/blob/master/core/types/arb_types.go)

## 父块状态证明

单独冻结五个目标账户在 3628115、3628116 两端的证明和 runtime 查询，预算 26 次。
`eth_getProof` 使用历史块号参数，证明只能在根据固定 Nitro 头哈希重算的
`stateRoot` 下通过，代码再用证明中的 code hash 核对。
第一笔父块账户证明返回 RPC `-32000` 和历史状态不可用。原实验在 3/26 停止，
退出 3，原响应与 STOP 保留。离线复核保留失败且不给出部分已验证状态。
真实账户证明、精确赢家 prestate、Nitro 整笔重放和原生结算仍未完成。
[EIP-1186](https://eips.ethereum.org/EIPS/eip-1186)

| 留存对象 | SHA-256 |
| --- | --- |
| 触发块 capture | `4cf781c6d9b613b3be6c8e93e6fcfd62413fd0f4d479bb3cd6aeca007f44b7e3` |
| 赢家块 capture | `d045f3196f3ee8ec3e3a5218c5072c3de3dd6d1674931aa9a3cdfca1f80b53c6` |
| 父块证明失败 capture | `b710fb99a89d733e89a130086f714e4b7744ebf818ad52cb8181d92079addbb2` |
| 核验报告，两次回放逐字节相同 | `8a3a306d2484b11a77ffbad2ec2edffd1b987be8b257a950f7811d4cb6cb96d5` |

## 复现

```sh
historical=reports/runs/elysium-evidence-20261110
run=reports/research/elysium-commitments-20261114
.venv/bin/python -m scripts.replay_cycle_case \
  --plan configs/cycle-research.elysium.example.json \
  --primary "$historical/official" --secondary "$historical/boxa-secondary" \
  --block-end "$historical/official-sim-1" \
  --nitro-receipts "$run/campaign/trigger" "$run/campaign/winner" \
  --output "$run/another-review.json"
```

聚焦检查包含合成 Nitro 账户证明和交易签名，签名夹具在本机即时生成。
早期合成测试遗漏必填的空 slots 数组，修正夹具后通过。省略 `--nitro-receipts`
执行旧入口得到的报告与上轮留存报告逐字节一致。

| 本机检查 | 结果 |
| --- | --- |
| 聚焦回归 | 退出 0，118 通过 |
| `make check` | 退出 0，6191 个 Python 测试、138 个子测试、9 个前端模块、82 个 Foundry 测试通过。Python 报告 11 个警告 |
| `make security-check` | 退出 0，Bandit 中高严重度 0，密钥扫描通过 |
| `make contract-security-check` | 退出 0，Slither 检查 85 个合约及 102 个检测器，报告 0 项 |

本轮累计 11 次匿名免费读取。下一步需要有历史状态或交易 witness 的来源以推进重执行，
其余多链研究继续见[阶段清单](../strategies/NEXT_STAGE_EXECUTION.md)。
