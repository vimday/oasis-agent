# Base Denim 真实交易整笔模拟与父哈希复核

11 月 28 日上海时间，Base 官方和 dRPC 各完整重放一笔同秒成交和一笔跨秒回滚。两者都返回规范父哈希和正确的下一高度，整笔 gas 和日志逐项匹配已核收据。官方独立的 `web3_clientVersion` 返回 `reth/v2.5.2-9e380dd/x86_64-unknown-linux-gnu/base/v1.5.6`。这是公共端点的版本自报，与 Aleph 上的进程身份分别记录。[逐组响应证据](BASE_RETH_DENIM_RECENT_20261128.json) · [Base RPC 方法](https://docs.base.org/base-chain/api-reference/ethereum-json-rpc-api/eth_simulateV1) · [版本报告接口](https://docs.base.org/base-chain/api-reference/ethereum-json-rpc-api/web3_clientVersion)

| Denim 对照 | 规范父块 → 成交块 | 链上时钟，毫秒 | 双源模拟与原收据 gas | 已验证 USDC 转账净额 |
| --- | --- | --- | ---: | ---: |
| 同秒，两池闭环 | 63,025,054 → 63,025,055 | 1,795,795,386,400 → 1,795,795,386,600 | **189,066** | **+36,333 最小单位** |
| 跨秒，回滚 | 63,007,311 → 63,007,312 | 1,795,791,837,800 → 1,795,791,838,000 | **27,850** | **0** |

重放从规范父块末状态开始，复用实际交易的发送人、目标、nonce、calldata、value、访问列表和 gas 上限与费率。签名恢复与交易哈希在本地按留存原件重算。只向公共端点发送未签名调用对象，开启验证，沿用规范块环境与状态。两个端点的原始父、子头均经 RLP 和 Keccak 重算，确认 200 ms 间隔。整笔模拟的外层父哈希、块号、整秒时间戳、总 gas 上限、接收费用地址与子块 base fee 均逐项匹配。模拟块哈希由模拟块内容派生。[原收据、交易位置和状态差分](BASE_EXECUTOR_ATTEMPTS_20261128.md)

同秒交易在成交块索引 3，执行器收到的 USDC 与原收据相符，仿真按 gas 和有效费率计算出 **13,427,298,673,128 ETH wei** 的 L2 执行费。此前已核的原始收据另列 8,935,667,014 wei L1 数据费。原生前后差分合计扣 13,436,234,340,142 wei。跨秒交易在索引 53，仿真同样回滚且无日志，L2 执行费 **734,711,574,100 wei**，已核原始收据另列 57,116,863,850 wei L1 数据费。原生差分合计扣 791,828,437,950 wei。仿真响应支持核对逐币到账与执行费，模拟的完整费用和 USDC 变现仍缺证据。两例的父块末状态足以重现原始日志和 gas。其他交易须单独核对同块前序的状态依赖。[同秒原交易](https://basescan.org/tx/0x39e1096ab88cb4b68539e05edfa098f2d84a0b98d3308fe865df810eef072786) · [跨秒原交易](https://basescan.org/tx/0x04ac22bfd2dc8c4b14244b66acd665fb16074d74306b26d2b326dff6e7944a45)

Aleph 只读盘点未发现正在运行的 Base/Reth 进程或常用 RPC 监听，常用数据路径下未见已同步库。根卷空余 **499 GiB**，低于官方最小主网快照约 **782.5 GB** 的压缩体积。此前在 Aleph 留存的 Base Reth 1.5.6 原生响应绑定的是内置 Base 创世父块。近期规范状态的 Aleph 复核仍待可用的标准 Reth 数据或已同步节点，本次公共结果单列。[Aleph 原生创世验证](BASE_RETH_PARENT_20261128.md) · [Base 主网快照](https://docs.base.org/specifications/node-operators/snapshots)

正式范围只用 **15 次**免费匿名 RPC 预留，官方 8 次、dRPC 7 次，支出 **USD 0**。第一次冻结的交易哈希笔误在离线原件核对时已拦截，两源账本均为零。原件、预注册范围、账本及两份停止记录分别留在 `reports/research/base-reth-denim-recent-remote-20261128/` 与 `reports/research/base-reth-denim-recent-remote-corrected-20261128/`。下列命令可逐字节复算。

```sh
set -o pipefail
PYTHONPATH=. .venv/bin/python docs/verification/fixtures/replay_base_reth_denim_recent.py \
  reports/research/base-reth-denim-recent-remote-corrected-20261128 |
  cmp - docs/verification/BASE_RETH_DENIM_RECENT_20261128.json
```
