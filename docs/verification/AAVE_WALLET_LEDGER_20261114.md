# Aave 缩放头寸回放与钱包对账

2026-11-14，基线 `c4e076f`。Codex 作者自审。范围为 Ethereum 账户
`0x0cdd9cbb825212c6cc5b0b36239e94218ee661d8` 的既有事件窗口。原始结果在
`reports/research/aave-ledger-20261114/`。此前已匹配 60 笔供借还撤事件与底层转账。
[历史借还款](AAVE_HISTORICAL_FLOWS_20261114.md)

## 缩放头寸

`scripts.research_aave_receipts --scaled` 使用原 62 笔收据和两个已固定的头寸快照。
起始缩放余额必须全为零。按同一交易内的储备更新和头寸代币日志核对 RAY 指数，
从底层整数数量推导缩放变化，并逐笔核对名义铸销、应计增量、归属账户及匹配的
Transfer。非零地址间的转移检查 `BalanceTransfer`、名义 Transfer 和应计日志。
缺失、多义、未归因的头寸转移均拒绝。若期末缩放量不同，保留报告并以代码 3 退出。

| 头寸 | 计算期末缩放整数 | 固定块读数 | 残差 |
| --- | ---: | ---: | ---: |
| aUSDC | 420040 | 420040 | 0 |
| aWETH | 0 | 0 | 0 |
| variableDebtWETH | 0 | 0 | 0 |

62 步均通过。可变债务事件中的应计增量合计 0.033423428300863292 WETH。
此前观察的还款超过借款 0.033423428300863309 WETH，两数相差 17 wei。公式回放
验证每步的指数、铸销和应计变化。若要进一步将差额逐项归因为合约取整，仍需证明部署
与参考源码等价。该报告保持选定样本和公式条件下的范围。
[Aave TokenMath](https://github.com/aave-dao/aave-v3-origin/blob/8305565ae342f1773c42cd2e4593f175fe5968a0/src/contracts/protocol/libraries/helpers/TokenMath.sol)
[Aave ScaledBalanceTokenBase](https://github.com/aave-dao/aave-v3-origin/blob/8305565ae342f1773c42cd2e4593f175fe5968a0/src/contracts/protocol/tokenization/base/ScaledBalanceTokenBase.sol)

## 钱包证据及残差

26300878–26301547 固定划为 11 段，每段最多 64 块。每段查询 USDC、WETH、顶层交易
和内部调用索引。全部 44 次匿名请求完成，每页最多实际返回 75 条，均低于 200 条的
上限。共 225 条 USDC、1 条 WETH、116 条顶层交易和 122 条内部调用，涉及 204
笔交易。原整窗满页记录保留。分区索引为数据商声明，成功响应只证明该请求有结果。
[Routescan 账户索引](https://docs.routescan.io/api/etherscan/accounts)。
Powered by Routescan.io APIs。

准备补齐新增的 142 笔收据。我的预检初稿少计五次请求，冻结计划的准确计数为 438 次，
超过原账本 433 次上限。首次预检失败发生在该批次 I/O 前，留存
`wallet-rpc/receipt-batch.log`。随后在原账本上固定可覆盖的最长顺序前缀，预计
累计不超过 430 次，尾部两笔继续列入缺口。该批次在块 26300955 停止，账本累计
53 次，含两端余额和指数预读及另一个块锚。原始 HTTP 200 成功收据含两条零 topic
日志，旧解析器错误拒绝。正常解码已修正并追加回归，原失败响应离线可解码，原
capture 仍记为不完整，原账本保持 STOP。此轮另有 9 笔完整收据可用于对账。

钱包离线入口逐页核对查询、文件哈希、原账本计数和分区交集，绑定原生余额至相同的
两个块锚。它用收据逐资产核对完整转移多重集及 gas，保留相同数量的多笔转移。顶层
交易失败时，发送方仍计 gas，交易 value 不入账。内部索引的正负数量保留为声明。

| 资产 | 起点余额 | 终点余额 | 索引计入的净变动 | 余额变动减索引 |
| --- | ---: | ---: | ---: | ---: |
| USDC | 2,099,000 | 1,596,791.218642 | -502,208.781358 | 0 |
| WETH | 0 | 0 | 43.478260869565217392 | -43.478260869565217392 |
| ETH（扣索引 gas） | 0.308023469315130707 | 3.562519829833880634 | 512.457921226137683715 | -509.203424865618933788 |

现有 71 笔收据覆盖索引候选的 34 条 USDC 转移，69 笔发送方 gas 与索引一致，
133 笔索引候选仍缺完整收据。USDC 的零残差是余额与索引的算术一致，剩余 191
条待逐笔确认。WETH 索引在 26301494 块列出一笔转入，在 26301516 块列出一笔从
WETH 合约转入的同额 ETH 内部调用。第二笔在同一索引里的外部调用选择器为
`0x2e1a7d4d`。索引线索符合先收 WETH 再解包的顺序。两笔的完整收据均待采集，
钱包报告保留原残差。原生 ETH 差额的具体来源仍待追踪。
[转入线索](https://etherscan.io/tx/0x1fd2ea4dcc5c4c1b8cf7ff98dd288b24ff3ce38b483d6287a5316b0650be3f6d)
[解包线索](https://etherscan.io/tx/0x4a2a8638b79ac61a7468d49ad302480dc3c55da8b84268d00e00877d0bba16fc)

另做 66 次匿名免费读取，取得所有 11 个分区末的块头与原生余额。前六段及末段残差为零，
非零部分如下，可直接聚焦后续逐块及交易级复核。

| 块区间 | 实际 ETH 变动减索引 ETH 变动 |
| --- | ---: |
| 26301262–26301325 | -69.415491887716412283 |
| 26301326–26301389 | -0.010023454782608695 |
| 26301390–26301453 | -56.179229350655021834 |
| 26301454–26301517 | -383.598680172464890976 |

本次累计 163 次匿名免费读取，费用上限 USD 0。RPC 余额使用 `eth_getBalance` 和
EIP-1898 固定哈希，首次异常停止。公开采集范围的请求字段上限扩大到 512，用于容纳
超过 256 次的历史窗口。旧范围的 cap 和账本不随代码变化增长。
[Alchemy 公共入口](https://www.alchemy.com/rpc/ethereum)

## 复现与余项

正式 CLI 各回放两次，输出逐字节一致。详细日志 `replay-reviewed.log` 留存于原始结果目录。

| 报告 | SHA-256 |
| --- | --- |
| `scaled-reviewed-1.json` | `fb7c73c4c52f5606c2c669f754dfc2d65b7cd0139d35803e21b98c202c2713df` |
| `wallet-reviewed-1.json` | `fc240ecc8482ce42dd9cf72312484a10c99c264e76f42b4089473f53866cee9c` |

`scripts.research_aave_receipts` 可复用[原回放命令](AAVE_HISTORICAL_FLOWS_20261114.md#回放入口)，
加上 `--scaled` 和新的输出路径。

`scripts.research_aave_wallet` 读取原始索引目录、62 笔 `--position`、固定的头寸
和原生余额边界，以及补查的 `--wallet-receipt`。不完整收据捕获只作为缺口留在报告。

```sh
prior=reports/research/aave-index-20261114
research=reports/research/aave-ledger-20261114
balances="$prior/boundaries/campaign"
native="$research/wallet-rpc/campaign"
set --
for receipt in "$prior"/alchemy-campaign/tx-*; do
  set -- "$@" --position "$receipt"
done
for receipt in "$native"/block-*; do
  set -- "$@" --wallet-receipt "$receipt"
done
.venv/bin/python -m scripts.research_aave_wallet \
  --plan reports/research/aave-history-20261114/analysis-plan.json \
  --cashflows "$prior/cashflow-boundaries-plan.json" \
  --position-boundaries "$balances/start" "$balances/end" \
  --native-boundaries "$native/native-start" "$native/native-end" \
  --wallet-index "$research/wallet-index" "$@" --output "$research/wallet-repeat.json"
```

先核对两笔 WETH 线索和差额最大的原生区间，再补交易级 trace 或状态差、其余收据、
关联账户归属和同期 oracle。卖出、买回及费用的完整归因和净收益仍待验证，继续保留在
[阶段清单](../strategies/NEXT_STAGE_EXECUTION.md)。

## 本机检查

| 检查 | 退出码及结果 |
| --- | --- |
| 聚焦回归 | 0，164 项，`focused-reviewed.log` |
| `make check` | 0，6,168 Python、138 子测试、9 前端模块、82 Foundry，`make-check-reviewed.log` |
| `make security-check` | 0，锁定依赖审计、Bandit、秘密扫描，`security-check-final.log` |
| `make contract-security-check` | 0，85 合约、102 detectors、0 findings，`contract-security-check.log` |
| 文档及 `git diff --check` | 0，4 份文档、58 个本地链接、5 段 shell、1 个 JSON 示例、3 个 CLI help，`docs-check-reviewed.log` |
| Elysium 留存案例兼容回放 | 0，原块末调用和收据仍可复核，精确交易前状态未解决，`elysium-compatible.log` |

全量保留既有 11 项警告。初次安全检查将重复写入的公开上游 Git 提交号列为未审候选，
报错日志留在 `security-check.log`。代码改为复用已记录的协议来源标识，未改扫描策略；
最终冻结代码的完整检查用上表日志。原始采集、报告和失败记录保存在忽略目录。
