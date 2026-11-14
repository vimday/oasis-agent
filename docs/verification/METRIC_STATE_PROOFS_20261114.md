# Metric 历史状态证明

2026-11-14。基线 `08e5d80`。Codex 作者自审，独立审阅待完成。原始资料、冻结计划、
回放报告和日志保存在 `reports/research/metric-state-proof-20261114/`。

## 历史块锚与证明校验

从[原父子块采集](METRIC_ATOMIC_PROBE_20261114.md)重放原始 HTTP 字节。按 Prague
执行层头的字段顺序组装 RLP，包含 `requestsHash`，取 Keccak-256 并与采集前固定的
块哈希比较。父子关系复核一致。此处的根承诺取决于原块锚的可信度，独立最终性仍待验证。
字段依据为 [Geth Header 定义](https://github.com/ethereum/go-ethereum/blob/master/core/types/block.go)
和 [EIP-7685](https://eips.ethereum.org/EIPS/eip-7685)。

| Ethereum 块 | 重算块哈希 | 绑定的 `stateRoot` |
| --- | --- | --- |
| 26410516 | `0xecc239466d283c9c4988623463cc33f1c5559297dad025b4b9d02db74025398c` | `0x3fc7586b3efae76fdc8828e03b8df8e496f2ae56625481c922e9572c6603d9ba` |
| 26410517 | `0xcceb62b4706283b85b7312cabb2ccdff795f2ebec2d5a6026b3f3e4c5a01e379` | `0xbc6ae0a2f13d566399be741ca2edc56fee441f50475a08a61616645e90203f23` |

既有 `scripts.capture_public_rpc` 新增 `oasis-public-state-proof-plan-v1`。计划固定
Ethereum、块号与哈希，以及 1–16 个账户和每账户最多 32 个完整槽键。成本为 `2N+3`
次请求。链号和开头块头之后交替读取 `eth_getProof` 与 `eth_getCode`，两者均要求
EIP-1898 canonical block hash，末尾重新校验块头。复用原账本预占、一次尝试、
首错 STOP、原始字节及独占输出目录。此计划的块头编码支持 Ethereum Prague。

校验按 [EIP-1186](https://eips.ethereum.org/EIPS/eip-1186) 和
[Geth trie 证明](https://github.com/ethereum/go-ethereum/blob/master/trie/proof.go)
沿 Keccak 后的键路径检查哈希引用、嵌入节点、分支与紧凑路径。从账户叶读取 nonce、
原生币余额（wei）、storage root 和 code hash，并把代码字节与 code hash 核对。槽值
从已认证的 storage root 验证，包括有路径证据的零值。截断路径、错误块根、缺槽、
超出请求的证明节点、字段矛盾或 runtime 不匹配均拒绝。全部读取与闭合块头通过后，
回放才输出账户和槽的证明结果。部分回包继续保留在原文件，失败报告的 `block` 为 null。

```sh
research=reports/research/metric-state-proof-20261114
.venv/bin/python -m scripts.capture_public_rpc replay \
  --capture "$research/campaign/parent-proof" --output "$research/another-parent-replay.json"
```

成功结果只覆盖计划点名的账户与槽位。完整执行仍需要补齐实际读集、区块环境、
合约代理与配置证据，并在访问未知账户或槽时停止执行。父块末状态和目标块末状态
不能直接定位块内交易之前的状态。原构块者 overlay 的身份、状态转换和适用时点仍待验证。

## 两次匿名公开读取

父子块实验预先锁定 WETH、USDC、Metric 池、V3 池、provider、custom source、
pyth-lazer oracle 和历史源码指向的 PythLazer 地址。oracle 首查从已复核布局得到的
ETH 与 USDC 两个键，其余只查账户与代码。原方案为父块 19 次，父块完整通过后才允许
目标块 19 次。`design-lock.json` 在 I/O 前固定两个计划的哈希，scope SHA-256 为
`54b67fbadc83d2629e07ecf78309635ef3d93744bdbce8a7e9929ec4429d9df5`。
[Automata](https://www.1rpc.io/ecosystem/ethereum) 公布该匿名 Ethereum 端点为免费。
链号请求 HTTP 200，父块请求 HTTP 429，RPC code `-32005`，响应提示 Nodies 公共限流。
原证明范围累计 **2/38 STOP**，退出 3。没有发出 `eth_getProof`，目标块计划保持未执行。
父块失败 capture SHA-256 为 `478e068ee49fe0983c4c2a5d58f4699b6e5db0c129ea71d671e1a709091ddaff`。
此前 context 的 **6/96 STOP** 记录照旧保留。

新近块实验针对首个新观察的 `safe` 块，事前固定同一账户清单、最小高度和最多 21 次
读取，使用 [PublicNode 免费公共端点](https://ethereum.publicnode.com/)。初始两次
读取完成后固定块 26411647，哈希
`0x4e17e468234779dc927131e2b3ec21e62d594b487585eb21b92beb33a5bfc148`，
时间戳 `1794640343`。头部 RLP 与该哈希一致，绑定的根为
`0x387bb1d822c24b66801214570099a59ea2e93dc7b981f3a4149899f365d7fc56`。
证明采集的链号和块头通过，首个 oracle 证明返回 HTTP 200 / RPC `4444`，提示目标距离
超出证明窗口。累计 **5/21 STOP**，退出 3。未取得任何可用证明或代码回包。capture
SHA-256 为 `109e6bbb51d06cda014768b07b7baca719f718c5d31bb463d579537c623b9032`。
两个范围均以 USD 0 费用上限执行，失败请求已预占，未重试。各范围的字节和计数离线复核通过。

归档平面状态与历史 trie 保留是不同能力。后续来源的验收应覆盖具体高度和方法。
[Geth 归档配置](https://geth.ethereum.org/docs/fundamentals/archive)

## 本地客户端互操作

零账户 Anvil Prague 第一轮从合成 genesis 返回的块头根，与账户证明首节点的哈希
不同，校验按预期拒绝，子进程退出 0。保留 `anvil-proof-initial.log`。另一个本地对照
从 `anvil_dumpState` 发现本机 Anvil 加入了四个系统账户。将四个账户及状态显式加入
合成 genesis 后，块头和证明根一致。三个存在的槽值为 257、514、771，一个缺槽证明
为 0，另有一个账户不存在的证明。合约 runtime 与账户叶 code hash 匹配。两次对照
共 14 次实际本地 IPC，两子进程均退出 0。所有 IPC 原始帧都保留。辅助脚本第一次从
报告目录启动缺少模块路径，在进程启动前失败，补上仓库模块路径后完成对照。

最终验证使用 Anvil 1.8.4 Homebrew，合成块哈希
`0x54086daf35234f04ad9b8f5a5ac9a3d2e498a99dc863bd9cbbface36f9d83ef4`，
根 `0x9c917342d6dfe7a99976cbfe44df56137c7070a182e48417bbb03c4e38b9dc52`。
详见 `anvil-proof-summary.json` 与 `anvil-proof-system-run.log`。真实市场块仍缺执行
读集证明、两腿模拟与净成本核对。[持续执行清单](../strategies/NEXT_STAGE_EXECUTION.md)

## Aave 线索复核

Bitquery 的事件调查对用户地址估算 26 笔交易及 USD 3.46M 净额，采用其交易与费用
口径，无法据此复算 Aave 债务、完整计息和还款。当前最高价值补证是用固定 Pool、事件
签名和 `onBehalfOf` 索引发现真实借款交易，再与块、收据及代币转移匹配。
[Bitquery 调查](https://www.bitquery.io/investigations/binance-hack-who-profited)

本轮另外冻结了 26300600–26302600 的索引发现计划，包含五种 Aave 事件过滤器，使用
Routescan 免费无密钥接口。辅助脚本调用 `RpcBudget.reserve` 漏传 `now_ms`，在第一笔
请求预占前退出 3，账本计数 0，HTTP 请求 0。限额和停止记录保留在
`aave-routescan/one-shot/`，原 PublicNode 收据范围保持 5/24 STOP。该失败来自本地脚本，
对 Routescan 的可用性和窗口内的借贷活动没有给出结果。
[Routescan 日志接口](https://docs.routescan.io/api/etherscan/logs)

## 检查与提交范围

| 本机检查 | 退出码和结果 |
| --- | --- |
| 聚焦测试 | 0，101 项，`focused-final.log` |
| `make check` | 0，6,132 Python 项及 138 子测试、9 前端模块、82 Foundry 项，`make-check.log` |
| `make security-check` | 0，锁定依赖审计、Bandit、秘密扫描，`security-check.log` |
| `make contract-security-check` | 0，85 个合约、102 个 detector、零 findings，`contract-security-check.log` |
| 文档和入口 | 0，4 份文档、54 个本地链接、5 段 shell、1 个 CLI help，`docs-check.log` |
| `git diff --check` | 0 |

额外运行 `mypy src scripts/capture_public_rpc.py` 退出 1，基线和当前版均为相同 18 项
脚本诊断，留存 `mypy-combined-*.log`。仓库规定的 `mypy src` 通过，覆盖 304 个
源码文件。全量 Python 留有 11 项既有警告。本机为 Python 3.12.4、Node 23.11.0、
Foundry 1.8.4 Homebrew。原始证据、运行账本和辅助脚本均留在本机报告目录。
