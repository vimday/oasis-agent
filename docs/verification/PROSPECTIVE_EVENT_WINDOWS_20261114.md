# Base 与 Elysium 前瞻日志窗口

2026-11-14。基线 `393b45d`。Codex 实现与自审。两次实际观察使用匿名 PublicNode，
归类为单一提供者的前瞻选窗、窗口结束后拉取及离线复核。

## 选窗与验收

原 Base 实验 14/32 在锚点时间检查停止。复核原六项文件摘要和原账本用量一致。
[原记录](EVENT_COVERAGE_20261114.md)

Base Denim 每 200ms 产生完整块，标准区块时间仍以整秒表达。Elysium 也有亚秒块。
短间隔导致相邻块可以共享时间戳，因此需要同时用高度、父链关系、块哈希和收包时间。
[Base Denim](https://www.base.org/blog/denim)
[Elysium RPC 规格](https://www.alchemy.com/docs/reference/elysium-api-faq)

新实验先冻结链、过滤器、顺序、区块偏移、窗口长度和等待时间。`H` 为 `eth_blockNumber`
报告的高度。选取 `H + offset` 作父块，观察其后 N 块。父块及后续块都按编号读取，日志
按范围和精确 topic 取回，再核对末块。第一组连续块哈希约束其余过滤器，分支变化会在
发下一笔日志请求前停止。时间规则继续用水位响应本机时间的整秒下界，区块时间晚于
该下界且不晚于各响应接收时刻。复核检查固定等待下界、组件先后、原账本和截止时间。

本轮两个实验各用一个原始额度账本。固定读取预算包含 2 次水位、2 次父块和每过滤器
N+3 次；失败请求仍预占。没有计费 RPC、签名、转账或交易。跨链使用同样的读取、校验
和回放语义，偏移和等待是这次各链预定的参数。
[Base 匿名端点](https://base.publicnode.com/)
[PublicNode Elysium 服务目录](https://publicnode.com/)

## 实际结果

| 项目 | Base | Elysium |
| --- | --- | --- |
| 本机收到水位，UTC | 09:29:11.405 | 09:30:24.706 |
| 水位 / 固定 offset / 父块 | 57,290,880 / 32 / 57,290,912 | 6,355,230 / 48 / 6,355,278 |
| 预定等待 | 13 秒 | 19 秒 |
| 区块范围 | 57,290,913–57,290,924 | 6,355,279–6,355,286 |
| 过滤器覆盖 | 3/3 × 12 块 | 6/6 × 8 块 |
| 日志 | WETH/USDC 500 pips 池，1 Swap、0 Mint、0 Burn | 三池路线的六个过滤器共享完整区块窗，均为空 |
| 最后一个过滤器复核完成，UTC | 09:29:30.822 | 09:30:54.669 |
| 最后完成减末块标称秒级时间 | 10,822 ms | 12,669 ms |
| 原账本 / capture 及 replay | 49/49，均退出 0 | 70/70，均退出 0 |

Base 的 Swap 来自 57,290,924，交易索引 7、日志索引 66。按 V3 ABI 解码，池的
token0 WETH 变化量为 `-844403139997736`（18 位），token1 USDC 变化量为
`1607000`（6 位），tick `-200813`。该事件的日志响应时刻是 09:29:26.665 UTC，
其过滤器在 09:29:26.783 核对结束。用早先 Base registry/getter 观察的池身份；本次
未刷新 bytecode，不归因路由者动机。三组日志均通过 `scripts.import_public_range` 入库。
[Uniswap V3 事件 ABI](https://github.com/Uniswap/v3-core/blob/v1.0.0/contracts/interfaces/pool/IUniswapV3PoolEvents.sol)
[交易](https://basescan.org/tx/0x0d4b46537e828b3cfe6706663a680c1e3c322c1d3e6ed1a53f3cc9b74f9c6680)

Elysium 沿用已留存路线的池、资产与规模网格。六组原范围经 `scripts.replay_cycle_shadow`
进入持久队列，`show` 报 observed、零任务、无 quote。报告中的轮询网格共 32 个
潜在报价点，这是回放后的计数，实测等预算轮询尚未执行。队列的 deadline 是为离线
导入从最后收包时间派生的参数，采集设计和源预算已在请求前冻结。没有恢复实时
quote cutoff 或验证新窗口内的部署 runtime。两链均缺独立时钟、独立日志/最终性及
完整覆盖证明。上表延时包含预设等待与各过滤器串行拉取，只能代表这两个观测窗口。

## 留存和复现

原始请求、响应、接收时间、配置、hash 与账本保留在本机忽略目录
`reports/research/prospective-window-20261114/`。完整回放均独立执行两次并逐字节相同。

| 留存对象 | SHA-256 |
| --- | --- |
| Base 冻结设计 | `54c9d4a38c4843f1a31351aafe5ab9c57e413c1560381142591b791c4ace2ccf` |
| Elysium 冻结设计 | `44aa4b5a9beb77faf4dcec28631b11e5bb5d92ccd3acbee60e8ad2278353e493` |
| Base 窗口回放 | `d41a474d418000df62db160be84140fd4f590842801a4bdac5ed543900538a7f` |
| Elysium 窗口回放 | `c0475f1f263279a1e1536e1e9538ebc7dbdbe85130235d835bd22eb04cd948ea` |
| Elysium shadow | `cef22ca264d01e14290f4bf5d67d9a0221a730cb723bd6bc5e00af3aead8b91e` |
| Elysium 持久队列只读结果 | `1328ff751c00d7f549ee38f71d855f6c055fe27837ae6f16550942ce5c35a85d` |

```sh
run=reports/research/prospective-window-20261114
.venv/bin/python -m scripts.observe_public_window replay \
  --run "$run/base" --output "$run/base-replay-reviewed.json"
.venv/bin/python -m scripts.observe_public_window replay \
  --run "$run/elysium" --output "$run/elysium-replay-reviewed.json"
```

聚焦回归覆盖正常入口、同秒多块、锁定前块、空父块、跨过滤器分支变化、预算预占、
报告清空和重复目录拒绝，共 132 项通过。首个测试命令引用了错误的队列测试文件名，
修正后完成原计划范围。原 Base 失败回放的解析结果相同，早期存档与当前 CLI 输出的
JSON 排版不同。

| 检查 | 结果 |
| --- | --- |
| `make check` | 退出 0，6195 个 Python 测试、138 个子测试、9 个前端模块、82 个 Foundry 测试通过。Python 报告 11 个警告 |
| `make security-check` | 退出 0，依赖审计和密钥检查通过，Bandit 中高严重度 0 |
| `make contract-security-check` | 退出 0，Slither 检查 85 个合约和 102 个检测器，报告 0 项 |

下一步补实时区块/日志或 maker 流的第一次接收留存、断线后重叠范围补齐、同一路线上
冻结同预算轮询及完整报价对照。Ethereum 与 Robinhood 继续分链验收。
[持续执行清单](../strategies/NEXT_STAGE_EXECUTION.md)
