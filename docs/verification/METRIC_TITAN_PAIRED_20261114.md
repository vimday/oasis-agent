# Ethereum Metric 与 Uniswap V3 同父状态对照

2026-11-14，基线 `b93ef38`。Codex 实现及自审。使用 Ethereum 历史父状态的提供者
模拟，结果供研究使用。没有链上交易、部署、签名或计费 RPC。

## 固定设计与输入

父块 `26410516`，哈希 `0xecc239466d283c9c4988623463cc33f1c5559297dad025b4b9d02db74025398c`。
目标 `26410517`，哈希 `0xcceb62b4706283b85b7312cabb2ccdff795f2ebec2d5a6026b3f3e4c5a01e379`，
时间 `1794626711`。Alchemy 取回固定块及身份，与原 PublicNode 观察一致。重算两块的
Ethereum 头哈希。两家服务的区块观察一致，底层节点独立性未验收。父块的 Metric 池、
V3 池、WETH、USDC、Metric immutable provider 及 Pyth oracle runtime 与预期一致。
核对 V3 factory、费率、币序和 Metric immutables、币序、scale、两 feed ID 和各 15 秒
有效期。新空账户的 EIP-1186 排除证明通过父块 `stateRoot`。临时注入的唯一代码由
既有始终回滚探针的已审阅构建重新实例化；余额和 nonce 未覆盖。
[头与 MPT 规则](METRIC_STATE_PROOFS_20261114.md)
[探针](METRIC_ATOMIC_PROBE_20261114.md)

两组 `eth_call` 都以父块哈希读状态，应用同样的目标块 number/time/gasLimit/coinbase/
prevRandao/baseFee 上下文，使用同一非零研究 observer、探针 caller 和 recipient、空
extensionData、同样的 calldata 与 gas 上限。每种腿序各测试 2 USDC 与 0.001 WETH。
覆盖组只将第一份合格 Titan 快照中的 ETH/USD 和 USDC/USD 两个映射槽写入指定 Pyth
oracle 的临时 `stateDiff`。其余 15 个 feed 槽、四个其他地址以及 Titan 的 balance、nonce
字段均未应用。环境为父块末状态加有限目标块字段，未执行目标块前缀或系统交易，也未检查
未来生产路由器的权限。对照只检验这两个槽是否足以让该探针在该状态完成。
[Titan 状态格式](https://docs.titanbuilder.xyz/propamms/takers)
[Geth `eth_call`](https://geth.ethereum.org/docs/interacting-with-geth/rpc/ns-eth#eth_call)
[原快照与槽布局](METRIC_BUILDER_FEEDS_20261114.md)

## 结果

四种路径各有两个组，8 次调用均收到 `ExternalFailure(1)`，**0/8 完成两腿还款**。
这是探针的外层标签，可能封装第一笔 swap 或其中的嵌套回调，单凭该标签无法定位失败腿。
没有可用的中间金额、最终输出、整笔 gas 或净利润。另用相同父状态和区块参数对照读取
池的 `getSellAndBuyPrices()`，两组都返回 `PriceProviderFailed(bytes)`，内层确认为
`FeedStalled()`。这个单独调用证实该状态下池的价格读取已失败，原来另一个块
`26410155` 的失败仍有独立的时间和上下文。

同一公开端点只读回读 `price(feed,pool)`，对照回包与父块存储一致，覆盖回包与
两槽中的 Titan 数值逐项一致。该零 origin 接口使用协议的研究读路径，只用于检查
端点是否应用覆盖。两个规范 `priceGuard` 下限和默认上限字段均为零。数值单位为 E8，
spread 单位为 0.01 bps。年龄按模拟目标块的整秒计算。

| Feed | 父块 mid / spread / 年龄 | 覆盖后 mid / spread / 年龄 |
| --- | --- | --- |
| ETH/USD，ID 2 | 190372380999 / 791 / 42 秒 | 190596447284 / 587 / 4 秒 |
| USDC/USD，ID 7 | 99975012 / 67 / 48 秒 | 99976927 / 63 / 4 秒 |

父块原样本的两个 feed 超出 15 秒限制。两槽覆盖解决了观测到的年龄问题，池级
`FeedStalled()` 仍存在。可能涉及 provider 的价格 shaping、其他配置或实际调用路径，
尚无内层执行树。PublicNode 在固定 `debug_traceCall` 的第一笔诊断请求返回 `-32601`，
该诊断在 3/5 停止。未放宽鲜度、替换合约、扩大覆盖或推断收益。

## 可回放记录

各原账本独立记录身份 25/25、空账户证明 5/5、对照 11/11、价格对照 5/5、
价格保护与状态 7/7、覆盖回读 7/7、失败 trace 3/5，共 63 次匿名免费读取。
原先 6/96、2/38、5/21 和 22 次的失败范围保留。原始字节在
`reports/research/metric-paired-20261114/`。对照正式 CLI 的两个最终回放逐字节相同。

| 留存文件 | SHA-256 |
| --- | --- |
| 对照 capture | `e1234ae94bebfdce39e25ad027287c5b8b4d55e16b9225c8f5109b00b4c36b2a` |
| 空账户证明 capture | `d98446d7f8a798930a0494131dffac86e427947cac6cab76bfba38d30c9eb09c` |
| 对照回放 | `63c71d873c898ba2d0292498f7e21da39b747b3b23c2ee97fb3f07a3b20c01f1` |
| 边际价格对照 capture | `d5f62277fbca70aed56cafd54912bf3a7db90a8cccc4b5933c76e9269001b801` |
| 两槽回读 capture | `cf82972b324adc05188f4cbbb55d1bbb2ca30bb021c94ffffbbf9bc5a80fa782` |
| 失败内层 trace capture | `dc807488cd597429c1406147b2535e7baa31f8eb8a81a3fdd82e64343bf9de3e` |

```sh
r=reports/research/metric-paired-20261114
prior=reports/research/metric-probe-20261114/campaign
t=reports/research/titan-state-20261114
feed=reports/research/metric-overlay-20261114
.venv/bin/python -m scripts.research_metric_pair replay \
  --design "$r/pair-design.json" --parent "$r/context/parent" --target "$r/context/target" \
  --peer-parent "$prior/publicnode-parent" --peer-target "$prior/publicnode-target" \
  --proof "$r/proof/empty-account" --titan "$t/campaign/snapshots" \
  --analysis "$t/analysis-plan.json" --feeds "$feed/feed-plan.json" \
  --catalog "$feed/pyth-symbols.response" \
  --artifact reports/research/metric-probe-codec-20261114/probe-artifact.json \
  --root "$r/pair" --output "$r/another-review.json"
```

退出 0 代表八个预定调用与结束块核验完整。协议回滚是合法实验结果，`complete_two_leg_count`
独立报告通过数。范围不完整退出 3 并清空可用组结果。输入和完整性失败退出 2。
未完成项包括解释覆盖后 provider 的分支、真实两腿成功样本，以及含成本和排序的前瞻经济窗口。
[阶段清单](../strategies/NEXT_STAGE_EXECUTION.md)

## 工程核验

本表覆盖本轮 Metric 和 Aave 两项改动。原始测试日志与研究记录保存在同一本地目录。

| 检查 | 结果 |
| --- | --- |
| Metric、Aave 和公共 RPC 相关的回归 | 退出 0，253 项通过 |
| `make check` | 退出 0，6,201 项 Python 测试、138 项子测试、9 项前端模块、82 项 Foundry 测试通过 |
| `make security-check` | 首次退出 2，命中公共探针构建产物的 SHA-256。核对原产物和指纹后登记为非密钥，复跑退出 0 |
| `make contract-security-check` | 退出 0，Slither 分析 85 个合约，0 条发现 |
