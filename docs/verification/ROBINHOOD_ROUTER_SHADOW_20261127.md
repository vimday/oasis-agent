# Robinhood 前瞻影子窗口的路由 ABI、实际返款与费用

北京时间 2026-11-27 09:35–09:43 的三个独立取样窗口共完成 **44 次固定本金路径报价，0 次通过最宽松的 gas 门槛**。另选一笔费用门槛下的两跳对照，锁定同一 Universal Router 包络，在发现块、模拟与估费回复时已收到的最新主 feed 块、发现块的直接子块逐次调用。三态均由两家 RPC 模拟成功。实际执行权限为零。所有金额以 Robinhood Chain 4663 的原生 ETH wei 计。[逐源哈希、分母和未解状态](ROBINHOOD_ROUTER_SHADOW_20261127.json)

| 固定 0.0001 ETH 两跳对照 | 发现 H | 初次模拟和估费回复的主 feed 状态 K | H 的直接子块 C |
| --- | ---: | ---: | ---: |
| 区块 | 123,899,187 | 123,899,214 | 123,899,188 |
| Quoter 整路径毛差 | +72,035,689,738 | +72,035,689,738 | +72,035,689,738 |
| 只扣 21,000 × base fee 的乐观净额 | −348,048,310,262 | −348,426,310,262 | −348,090,310,262 |
| 两源完整包络调用、各自估计 gas | 成功，136,591 | 成功，136,591 | 成功，136,591 |
| Quoter 输出减本金及完整估费的**预计净额** | **−2,660,330,674,262** | **−2,662,789,312,262** | **−2,660,603,856,262** |
| 独立现金探针实际收到并退回 EOA 的 ETH | 100,072,035,689,738 | 100,072,035,689,738 | 未测 |

估费用同一包络分别向两源调用 `eth_estimateGas`。取较大估计乘该块基础费，固定优先费为零。估计包含 Nitro 对网络费的估计；没有实际纳入或费用扣款。实际 EOA 的真实成交净额未知。本轮 0 笔正候选，因此该表只进入负控分母，不能推断正机会的生存率。[Arbitrum 估费定义](https://docs.arbitrum.io/arbitrum-essentials/how-to-estimate-gas) · [Robinhood 费用](https://docs.robinhood.com/chain/gas-and-fees/)

## 窗口与时间

- 首段由官方主 feed 到达触发，以现有入口核对 H=123,895,389 与其直接子块。在每个哈希对 20 个池、四个合约运行码和 6 条固定 0.0001 ETH 路线取双源证据，共 12 个有限报价。最佳方向两跳，两个哈希的乐观净额分别为 −393,518,974,653 和 −391,502,974,653 wei。
- 后两个窗口同时接官方主、备用 feed。首轮匹配 132 个同哈希，备用到达差中位 516.710 ms；修正后匹配 131 个，中位 484.109 ms。两次主 feed 内部缺号均为零。两个 feed 的消息来自同一运营方。参与定价的区块另由官方和 PublicNode 两家 RPC 重建 Nitro 块头，核对交易顺序和状态根。[Robinhood 双 feed](https://docs.robinhood.com/chain/run-a-full-node/)
- 两段开始前固定八条路线，包括两跳双向各取 0.0001／0.001／0.01 ETH，另取 20、24 跳各 0.0001 ETH。原阶梯窗口 H=123,897,904 共 8 个报价。其 24 跳兼容旧窗口中曾卡在第四腿的路线，此次全额报价完成，毛差 −6,471,627,176,411 wei。同一两跳方向三个本金的毛差依次为 +83,863,827,725、−1,219,565,653,011、−214,055,313,434,635 wei。增加本金后方向本身已亏。[先前路线与流动性反例](ROBINHOOD_QUOTE_DELIVERY_20261124.md)
- 修正后窗口的 H／K／C 每个有 8 个报价，均未通过门槛。最初双源 H 报价返回至包络固定用 9.024 ms，从报价返回至完整模拟和估费返回为 577.621 ms。由主 feed 原接收时钟选择 K，并用随后消息验证边界。K 比 H 高 27 块。为降低源追赶不及时的影响，先固定 K，再读 C、K。回查 K 所用时间没有倒算进初次完成时钟。初次到达 H 到整笔估费返回为 2.620 秒。

## 工程修正及独立对照

实际部署的 `ExactInputParams` 在路径与本金之间包含 `uint256[] minHopPriceX36`。项目锁定的通用 periphery 接口在该处没有这一动态字段。在原阶梯 H，按通用接口编码的负控即使 Quoter 有正毛差，两个源的整笔调用均回滚。后续同一旧块只修正这个字段，保留原 from、value、gas、nonce、费用、deadline 与路线，官方源整笔返回成功。独立公开源的归档读返回 HTTP 403／−32602，记录该次预留后停止。浏览器区块浏览服务的源码接口亦曾返回 403，后从独立 Sourcify 取得验证源码，部署运行码的 Keccak 与链上固定哈希逐字节一致。[Sourcify 的实际部署与源码](https://sourcify.dev/server/v2/contract/4663/0x8876789976decbfcbbbe364623c63652db8c0904?fields=matchId,sources,runtimeBytecode,compilation) · [官方部署表](https://developers.uniswap.org/docs/protocols/v4/deployments#robinhood-chain-4663)

链专用只读编码器按 `SETTLE`、`SWAP_EXACT_IN`、`TAKE_ALL` 处理同资产闭环。首步给 PoolManager 计入显式 ETH 本金，按实际部署 ABI 的空逐跳价格数组和固定整路最低产出交换，最后将全部原生币信用返还调用人。测试反解每个动作及嵌套 ABI，并拒绝本金未被最低产出覆盖的包络。原始影子包络绑定公开历史 EOA 的当块 nonce 和足额余额，零账户代码，无签名和状态覆盖。[编码与约束](../../src/oasis_agent/evidence/robinhood_quote.py) · [回归](../../tests/unit/test_robinhood_quote.py)

原阶梯首次完成后，在已有主 feed 哈希的高度，官方返回 HTTP 200／null，而 PublicNode 已返回可复算块头。原范围停在该读。修正后的新范围完成三态后，两源对 `debug_traceCall` 均返回 −32601。原包络的独立逐帧 EOA 余额仍缺失。辅助 `eth_call` 只在 H、K 用只读构造器转给同一 Router 完全相同的 calldata 和本金，实测构造器余额并退回同一 EOA。没有预置余额或状态覆盖。辅助调用改变了 Router 看到的付款人，当前只把它计作这两个无 hook 样本的回款对照，保留 C 未测。[现金探针](fixtures/RobinhoodNativeCashProbe.sol)

## 复算

基线每个 RPC 来源各 12 次；两个阶梯和后续独立对照共用原账本，官方累计 30 次、PublicNode 28 次。合计 **82 次免费 RPC，5 个免费 WebSocket 连接**。原始请求、响应、双 feed 帧和获取时钟留于忽略目录。计算半成品、首次失败回复及来源限额均保留。复算校验选定 K 的时钟、每个 HTTP 请求与响应哈希、块头和 feed、代码指纹、八条报价、三态包络和辅助回款。[复算程序](fixtures/replay_robinhood_shadow_window.py)

```sh
set -o pipefail
root=reports/research/robinhood-shadow-nov27
sources=reports/research/multichain-forward-20261122/robinhood/sources
PYTHONPATH=. .venv/bin/python docs/verification/fixtures/replay_robinhood_shadow_window.py "$root" --sources-dir "$sources" |
  cmp - docs/verification/ROBINHOOD_ROUTER_SHADOW_20261127.json
solc-0.8.26 --evm-version cancun --metadata-hash none --optimize --bin docs/verification/fixtures/RobinhoodNativeCashProbe.sol -o /tmp/oasis-robinhood-cash-build
cmp /tmp/oasis-robinhood-cash-build/RobinhoodNativeCashProbe.bin "$root/cash-control/RobinhoodNativeCashProbe.bin"
```
