# Ethereum 54 腿路径的成交、块末回滚与直接子块费用拒绝

2026-11-27 北京时间。固定[已核实的 USDC 闭环](ETH_TWENTY_FLOW_20261127.md)，用原交易的公开字段做两次后继状态对照。单独冻结范围，读取原交易和直接子块头各一次，对成交块末与子块末各作一次只读 `debug_traceCall`。免费来源各 2 次、总计 4 次、USD 0。该试验用历史收据和历史状态比较，没有当时搜索器的发现时钟。第一笔实际消耗的 USDC、块末原调用实际使用的 USDC、以及下一块能否进入 EVM，三者需要分别核实。

| 状态 | 观察到的结果 | 成本与币量 |
| --- | --- | --- |
| 成交块 26,489,188 内索引 266 | 完成 54 项变换回到 USDC，再换成 ETH。原执行器读取 USDC 余额为 3 最小单位，首池实际使用 2 | gas 4,349,452；支付 gas 与块 fee recipient 后，发送者与执行器合计可见 **+36,221,727,789,478 ETH wei、−2 USDC 最小单位** |
| 成交块执行完毕的状态 | 同包络读取执行器 USDC 余额为 1，传给同一首池的输入成为 0。首池和根调用均以 `Mooniswap: return is not enough` 回滚 | trace 报 gas 1,568,059。若按当块条件计一次失败纳入，费用为 **88,906,961,705,365 wei**。此次模拟没有实际扣费或资产结算 |
| 直接子块 26,489,189 末 | 区块时间相隔 12 秒、parentHash 完整匹配。原费率上限 56,698,735 wei/gas 小于子块 base fee **60,122,892 wei/gas**，服务返回 `-32000` | 同包络在此费用条件下不具备纳入资格。未得到 EVM 执行或可用的输出金额；调用范围在首次 RPC 拒绝后停止 |

原交易为 EIP-1559，验证了交易哈希和恢复的发送者。模拟使用同一组 chain ID、type、显式 nonce 82,616、from、to、value、gas limit 8,730,000、两项费用、access list 和原 calldata，按哈希选状态，无状态或区块覆盖。费率是原包络的一部分。模拟服务能返回该历史 nonce 的 EVM 轨迹，并未证明原签名交易可再纳入。两个模拟保持相同的外层输入，内部首池调用的金额已随执行器持仓改变。两次 `balanceOf` 的 calldata 相同；首池调用的目标、selector、资产、限额和附加地址也相同。只有输入从 2 变为 0。这里既没有证明首池市场价的变化，也没有证明换一套资本和新的费率可以复制该收益。[Geth 历史调用接口](https://geth.ethereum.org/docs/interacting-with-geth/rpc/ns-debug#debugtracecall) · [EIP-1559 费用约束](https://eips.ethereum.org/EIPS/eip-1559)

原 H 为 `0xfa491c993de11545d6b611df12658159534db904b4003620ec692a2c2e682118`，H+1 为 `0x3560bbae3d8c682b0f88aeb8fff7062abcb78167c830b1bbd35a79cf0f789b5f`。原始响应、请求、时间及 STOP 均保留。可公开的逐源哈希、归因和同包络摘要收录在机器可读报告的 `post_state_experiment`。[证据摘要](ETH_ARCHIVE_SERIAL_SEARCH_20261127.json)

```sh
set -o pipefail
scope=reports/research/eth-open-archive-20261127/lifetime-266
PYTHONPATH=. .venv/bin/python "$scope/verify.py" | cmp - "$scope/verified.json"
```

**对影子实验的影响。** 同时记录策略计划投入、执行器实际可用余额、第一池实际收到的整数及原 fee cap 对直接子块的余量。把库存导致输入为零、首池回滚、费率拒绝、来源超时和价格变差分开归因。若在新状态重新定量或修改费用，给它独立候选编号。真实前瞻三阶段仍需要在收到状态时计时并冻结自有包络。
