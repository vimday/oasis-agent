# Base 四池与扩大池集前瞻比较

北京时间 2026-11-23 17:26，在 Base 规范块 **61,178,149–61,178,154** 取同一块哈希下的
四池及扩展池状态。六块头部毫秒时间相隔 200 ms，五个子块的 `parentHash` 链接均吻合。
同一 PublicNode 端点返回块头、状态和逐腿报价，`BaseTime` 毫秒、链 ID、固定哈希及
状态读数均按仓库解码器核对。样本对来源独立性和 L1 最终性仍无证明。
[Base Denim](https://www.base.org/blog/denim) · [EIP-1898](https://eips.ethereum.org/EIPS/eip-1898) ·
[PublicNode 免费 Base RPC](https://base.publicnode.com/)

## 池集和预算

首次固定发现块 **61,177,380**，得到 11 币、56 个达到发现储备门槛的池。四池对照中的两个
Slipstream 池也在同块工厂映射和余额结果内，活跃流动性为正，USDC 余额分别为
**3,684.253023** 和 **558.569177**，低于宽池集的 5,000 USDC 门槛。初版要求控制池也通过
宽池门槛，因此本地停止。当时 4 次 RPC 都成功，原始范围、响应和 STOP 全部保留。

同一预算的恢复阶段从留存的固定哈希响应重建两个薄池身份，保留四池对照，并构建
**56 个储备达标池加 2 个薄控制池**的扩展比较。两阶段共 **20/26** 次免费匿名请求，
包括 4 次链身份及发现、12 次区块头及同哈希 Multicall、4 次 Quoter。恢复阶段首次请求最新块，
随后按上一样本的下一高度读取。扩展池为 23 个 Uniswap V3、13 个 Aero CL2、
11 个初代 Slipstream 类池和 11 个 Aerodrome Volatile。发现结果来自已知工厂，
V4 PoolId、其他 V3 厂、稳定曲线和未纳入资产留作后续覆盖。
[池集发现与 Multicall 解码](../../src/oasis_agent/fastpath/multicall_universe.py)

## 同哈希比较

所有池集使用同一批状态、费用输入和 WETH/USDC 锚。范围为 0.001–1 WETH 或 5–2,000 USDC，
不限最低毛差，筛选模型净额至少 1 最小单位的 2–3 跳路线。集中流动性使用真实 `slot0`、
活跃流动性、费和余额生成限深合成区间，完整初始化 tick 未取得。表中环路计数按锚和方向给出。

| 池集 | 每块 WETH / USDC 环路 | 六块总环路 | 模型正候选 | 未闭合定量区间 |
| --- | ---: | ---: | ---: | ---: |
| 四池对照 | 12 / 12 | 144 | 0 | 0 |
| 56 个达标池，事后同快照复算 | 1,448 / 1,270 | 16,308 | 0 | 0 |
| 58 个扩展池 | 1,774 / 1,596 | 20,220 | 0 | 0 |

扩展池集的两锚 Rust 内核合计中位约 0.047 ms，计入输入和 FFI 的中位约 0.774 ms。
四池分别约 0.0048 ms 和 0.152 ms。六块中，排除块号和时间戳后仅一次观察到池模型字段变化。
重放时全扫与增量 WETH 环路计数分别为 10,644 和 1,934，USDC 为 9,576 和 1,768。
两锚逐块候选一致 6/6。没有正例可估回召，预注册 P99 假设未通过。
[离线 A/B 入口](../../scripts/run_multihop_ab_experiment.py)

## 报价完成、子块读回及费用

冻结的反例路线为 Uniswap V3 → Slipstream，从 WETH 走 USDC 再回 WETH。
首块和其直接子块各逐腿链上报价一次。输入均为 1,000,000,000,000,000 WETH wei，
返回均为 999,457,653,515,359 WETH wei，与两块同金额模型一致。毛差为
−542,346,484,641 WETH wei。按每块 base fee 5,000,000 wei、固定 tip 2,000,000 wei、
500,000 gas 及 512 字节 L1 费用探针，L2 估算为 3,500,000,000,000 wei，L1 探针为
983,657,162 wei，模型净额为 **−4,043,330,141,803 WETH wei**。
两笔报价合计 4 个腿响应，模型正候选和整笔交易模拟均为 0。实际交易 gas、
真实 calldata 的 L1 费、operator fee、资金成本、失败成本和排序付款均留空。
[OP Stack 费用规范](https://specs.optimism.io/protocol/isthmus/exec-engine.html)

状态从头部请求开始到完整解码为 346–490 ms，中位 374 ms。两次完整逐腿报价为
312.6 和 308.8 ms。相邻两次状态可用间隔为 631–924 ms，含串行读取、主动间隔及前两次报价，
不能用来估计 sequencer 延迟或领先时间。固定反例的首块和五个后续状态具有相同模型净额。
正候选为零，机会寿命及下一块正例留空。

## 留存与复算

本地忽略目录 `reports/research/base-forward-broadened-20261123/run/` 留有初段原始请求、响应、
原始 SQLite 预算和 STOP。其 `successor/` 子目录有恢复范围、每块时钟、原始块头、同哈希状态,
以及逐腿请求和字节级摘要。两锚各 6/6 已通过原字节与解析状态的离线复核。

```sh
.venv/bin/python -m scripts.run_multihop_ab_experiment \
  --snapshot-manifest reports/research/base-forward-broadened-20261123/run/successor/multihop_replay/manifest.json
```
