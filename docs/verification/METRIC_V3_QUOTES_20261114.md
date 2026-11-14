# Metric / Uniswap V3 报价与首次公开观察

日期：2026-11-14（Asia/Shanghai）；基线 `862e9e8`。作者和内部自审：Codex；没有独立
reviewer sign-off。本文件的 Git 历史定位实现提交。[实验锁定与命令](../strategies/METRIC_V3_QUOTE_EXPERIMENT.md)。

## 实现与证据等级

新增 `oasis_agent.arbitrage.metric_v3` 和离线入口 `scripts.research_metric_v3`，复用
严格 RPC export、EIP-1898、资产/V3 身份与报价读取器。两个起始资产 × 两种 venue 顺序
× 预先声明规模；每跳输出成为下一跳输入，最终资产必须回到起点。chain ID 是参数，
四链合成测试证明隔离规则，**不证明四链都有该部署或流动性**。

Metric 使用带 pool、sender、recipient、方向、amountIn、priceLimitX64、extensionData 的
Live 重载（selector `0x67a00105`）；priceLimitX64=0 由 canonical quoter 归一化为开放边界，
不是 minimum output 承诺。检查实际 consumed input 等于请求。其 token 顺序
不假定地址排序。V3 使用 QuoterV2 tuple（`0xc6a5026a`），四项返回中没有实际 input；
仅在 canonical V3 的开放价格边界未触及时推断 exact-input 完成，边界结果拒绝。
两者 calldata 均与独立 `cast calldata` 比较一致。

每条路线的五项增量成本与资金占用显式声明；未知成本或未承诺资本未知时不计算净值。
拒绝部分输入、身份/精度/来源/区块冲突、非规范 ABI、超预算和不可能的检索时间。
RPC error 只保留有界 bytes4 诊断，不传播提供者 message 或 revert 参数；未使用的过期
错误不作为本轮回滚原因。CLI 不覆盖已有报告，结果可确定性重放。

这是**同一块末的独立单跳 quote 组合**，不是整笔原子交易。首跳状态影响、builder
更新/排序、callback 付款、余额和结算均未证明。runtime pins 仅校验观察一致性，不等于
canonical source 编译一致或 proxy implementation 已验证；输出明确这些限制，始终
`execution_eligible=false`、`realized_pnl_raw=null`。

源码语义 pin：

- Metric pool `ddf8ae0cdb2ad0041c3ff3aebc200cf488d5b0d3`：[IMetricOmmPool](https://github.com/Metricxyz/pool-contracts/blob/ddf8ae0cdb2ad0041c3ff3aebc200cf488d5b0d3/contracts/interfaces/IMetricOmmPool/IMetricOmmPool.sol)，24-word getImmutables、token 与 scale 语义。
- Metric periphery `5ceaec32a4a8ceb5fe1411ec1bc5d5f9aef32647`：[quoter](https://github.com/Metricxyz/periphery-contracts/blob/5ceaec32a4a8ceb5fe1411ec1bc5d5f9aef32647/contracts/lens/MetricOmmSwapQuoter.sol)，显式 caller 上下文及实际 input/output。
- Uniswap v3-periphery `24ccf014fe7f81d74a14c1689d6fdf123f280e45`：[QuoterV2](https://github.com/Uniswap/v3-periphery/blob/24ccf014fe7f81d74a14c1689d6fdf123f280e45/contracts/lens/QuoterV2.sol)，exact-input 与边界；地址参照[官方 Ethereum 部署表](https://developers.uniswap.org/docs/protocols/v3/deployments/v3-ethereum-deployments)。
- Metric oracle `7525bbd6b536bcd0c283de18af256fe54d629456`：[PriceProvider](https://github.com/Metricxyz/oracle-contracts/blob/7525bbd6b536bcd0c283de18af256fe54d629456/contracts/PriceProvider.sol) 和 [AnchoredPriceProvider](https://github.com/Metricxyz/oracle-contracts/blob/7525bbd6b536bcd0c283de18af256fe54d629456/contracts/AnchoredPriceProvider.sol)，用于离线回滚诊断，未证明本池当时的实际分支。
- 区块绑定：[EIP-1898](https://eips.ethereum.org/EIPS/eip-1898)。

## 真实单源观察：第一次报价回滚

先记录范围，再向 [PublicNode 官方免费 Ethereum 端点](https://ethereum.publicnode.com/)
发起匿名、只读的一次性观察。上限 40 次逻辑请求、120 秒、每响应 2 MiB；原始 SQLite
预算在 I/O 前预占，失败照计。22 次实际请求，均 HTTP 200，末次为 JSON-RPC error。
没有认证、付费 RPC、重试、签名、广播、部署或生产服务变更。

固定 head−16 的块（这不是独立最终性证明）：Ethereum `1`，块 `26410155`，哈希
`0xb4d09499b94a431dc10366c08f658c79b910d7e9a3d39cef1d1ae1ce7678717d`，时间戳
`1794622367`。捕获为 2026-11-14 02:16:02.674763–02:16:06.857199 UTC；计划检索截止
02:18:05.381163 UTC。EIP-1898 合约读取均要求 canonical。

| 上下文 | 地址或值 |
| --- | --- |
| WETH / USDC | `0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2`（18 位） / `0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48`（6 位） |
| Metric pool | `0xe9c19e5bd10e5096519fe1d2a9d783f79b8211da` |
| Metric quoter | `0x3b9ea79f482094323d06064e637a7f4528560fcb` |
| Metric sender / recipient | SimpleRouter `0x02201e6f13c41506c7bb635d7543f98826229ab7`；extensionData=`0x` |
| V3 pool / fee | `0x8ad599c3a0ff1de082011efddc58f1908eb6e6d8` / 3000，由 factory getPool 返回 |
| V3 QuoterV2 | `0x61ffe014ba17989e743c5f6cb21bf9697530b21e` |
| 预定起始规模 | 0.001 WETH 与 2 USDC，各两种顺序；成本、资金均未知 |

chain/block、九项 runtime、Metric immutables、两资产 decimals、V3 token/factory/fee 及
quoter factory 的读取与离线一致性校验完成。runtime 和 immutables hash 是本次观察后
固定的，不能声称采集前已独立确认部署源码。

第 22 请求是 0.001 WETH 的 Metric Live quote，返回 code `3`、data `0x9a0423af`。
采集立即停止，未请求其余报价或结束块头。selector 与 canonical `FeedStalled()` 匹配，
但它只是一条诊断线索：没有实际 provider 配置、feed 时钟和源码等价证明，不能区分
过期、无效报价或其他 guard，也不能把四字节匹配当成已确认根因。旧 API depth 捕获
不是同时点数据，不能据此声称“API 健康而链上不可用”。

离线报告为 `incomplete_evidence`，0/4 完整、0 个可计算净额。24 种离线查询包含缺失
记录的查找，不是额外网络请求；原预算仍是 22。报告是 parsed retained RPC，独立
提供者和原始 HTTP 自动验证均 false；原始 HTTP 字节另外保留，尚无双源/重组闭环。
独立于报告生成器的本机核对确认 44 个请求/响应摘要、22 条 parsed record 与原始字节
相符，原预算有 22 行；记录为 `local-capture-audit.json`。这不提升提供者独立性或外部
时钟可信度，也不修改报告的自动验证标记。
没有当前可执行价差、收益、历史事件 PnL 或策略晋级结论。

## 原始记录、失败和复现

本机忽略目录 `reports/research/metric-v3-20261114/` 保存来源源码、采集脚本、原预算、
每次原始请求/响应、时钟、capture 和测试日志；不将这些本机产物加入 Git。
首次本地运行因缺少 `PYTHONPATH=.:src` 导致 import 失败，发生在目录/预算/网络创建前，
日志 `public-observation.log`，退出 1。补上 import 路径后的采集无重试，日志
`public-observation-run.log`；临时脚本捕获异常后退出 0，**不代表采集成功**，其
`capture.json` 明确 incomplete。可发布的离线 CLI 同样允许输出完整的失败研究报告，
调用者须读 decision。原报告与失败记录保留，新版本另写文件。

| 本机文件 | SHA-256 |
| --- | --- |
| public-ethereum/scope.json | `310235d14efcc6ed0ba73f739a0bd66d82fd38785ac7e6bda202f6912fc1469a` |
| public-ethereum/capture.json | `dfc031264536357efc3643755f5df349396fbe501a1a52deb504f8943f3265f3` |
| public-ethereum/plan.json | `bf6e4acd97fa5d92d116159f2b8f527438b72f63262c54f7197693bacafce225` |
| public-ethereum/exports.json | `91b3e46f1195842fab11c203d49cec903f2d2bedd62a895006ed6ad5daf21c6d` |
| public-ethereum/21.request.json | `5416c56f7a1a5427b5564be3030fef70355dab74a91d4aa8351acade92d2080d` |
| public-ethereum/21.response.json | `244c62cbf03e16a5798abf98c176fdfc61d0e65a400e1770147df540e8f57ddd` |
| public-final-a.json（与 b 逐字节一致） | `9efe7bbb13aa6174cedf68b1ab8577b096482268094eb6d0e4567b3f081c9dc4` |
| cast-abi-vectors.json | `2bb7aa9c15d9a094232926febd14c2ea28072685facd20269bf520d2e1353b43` |

```sh
.venv/bin/python -m scripts.research_metric_v3 \
  --plan reports/research/metric-v3-20261114/public-ethereum/plan.json \
  --export publicnode=reports/research/metric-v3-20261114/public-ethereum/exports.json \
  --output /tmp/metric-v3-public-replay-new.json
```

该命令依赖本机保留数据，不访问网络；可移植的合成例子在实验文档。合成八个候选中
四个条件净额为正，仅验证算术及方向，无 untouched evaluation、成交或 net NAV。

## 检查与下一步

第一轮 `make check` 退出 0（5,710 Python、138 子测试、9 前端模块、70 Foundry），
早于新增检索时间回归，保留 `make-check.log`，最终门禁另写文件。初次静态检查发现
未使用 import 和 import 排序，已修正；补跑聚焦命令一度引用不存在的测试文件，退出 4，
保留 `focused-release.log`，正确命令另写 `focused-release-corrected.log`。

| 检查 | 结果 / 本机日志 |
| --- | --- |
| 相关五模块回归 | 135 通过（新增 48），退出 0；`focused-release-corrected.log` |
| `make check` 最终版本 | 退出 0；5,711 Python、138 子测试、9 前端模块、70 Foundry 全部通过；`make-check-final.log` |
| `make security-check` 最终版本 | 退出 0；依赖无已知漏洞，Bandit 无 medium/high，秘密扫描通过；`security-check-final.log` |
| `make contract-security-check` | 退出 0；80 合约、102 detectors、0 findings；`contract-security-check.log`。在最终检查的 Python 阶段完成，未与 Foundry 构建重叠 |
| 文档、入口与合成例子 | 53 个本地链接目标和 help 通过；两次输出一致，退出 0；`docs-examples-check.json` |
| 真实失败捕获离线重放 | 两次退出 0、逐字节一致，均明确 incomplete；`public-final-a.json` / `public-final-b.json` |
| `git diff --check` 与内部审阅 | 通过；核对 ABI、方向、币种精度、部分输入、失败保留、源码/观察身份边界及秘密；没有独立 reviewer 或新增资本权限 |

合成发布报告 SHA-256：`7a4cc94280fb942569708331ac1d175bc37fea8f055652d879952ce698d621ed`。

下一项是补齐具体 pool 的 provider/feed 状态、更新时序和 runtime/proxy 对应关系，
再在新的明确只读范围与原预算规则下做独立来源复核；不能通过忽略 staleness 或重试
本次失败来制造可报价结果。随后验证精确 caller 的整笔无签名模拟和前瞻事件覆盖。
用户地址借还款全账本、Elysium 精确 prestate 以及其他链部署档案继续按
[多链路线](../strategies/MULTICHAIN_LENDING_EXPERIMENT.md)推进。
