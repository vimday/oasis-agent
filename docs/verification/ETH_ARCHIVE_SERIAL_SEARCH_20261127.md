# 两个 Ethereum 全天的多跳普查与逐笔对账

北京时间 2026-11-27。在 Aleph WSL 的 CPU 上扫描两个完整 UTC 日的公开事件归档，找到一笔可追踪 **54 次连续资产变换后回到 USDC** 的交易。它通过原八类事件筛选时只显示 20 腿等额相接，通用调用树解码器自动证明了其中 4 腿。展开资产、资金经手方、回调与结算后，才能看出全貌。两日事件最多的另一笔有 180 个命中，通用解码能确认的最长资金链也是 4 腿。两笔的成交证据和选取依据均可回查。[逐腿核验](ETH_TWENTY_FLOW_20261127.md) · [机器可读分母、成本与证据哈希](ETH_ARCHIVE_SERIAL_SEARCH_20261127.json)

## 范围与选择

预先选择行业帖所在的 11 月 13 日，以及近期对照日 11 月 25 日。上限 3.5 GB，使用匿名 S3，从完整目录每日至多取 64 个归档对象，预设备选日期未启用。两日各有一个 Parquet，共 2,473,001,915 字节。报告给出覆盖日志的高度范围、总行数与交易数；没有发出日志的交易不在分母中。AWS 是此步骤的数据提供者。下文另对两笔交易重建收据根。[AWS 公开归档](https://aws.amazon.com/blogs/web3/access-bitcoin-and-ethereum-open-datasets-for-cross-chain-analytics/) · [Ethereum 表结构](https://github.com/aws-solutions-library-samples/guidance-for-digital-assets-on-aws/blob/main/analytics/consumer/schema/eth.md)

| UTC 日 | 有日志的区块 | 有日志的交易 | 日志 | 命中八类事件的交易 | 命中事件数 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 11-13 | 7,183 | 1,341,195 | 6,328,493 | 236,933 | 382,186 |
| 11-25 | 7,174 | 1,301,626 | 6,584,421 | 220,350 | 370,486 |

| 每笔最低事件数 | 5 | 10 | 20 | 50 | 100 | 200 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 11-13 | 8,271 | 2,464 | 794 | 119 | 7 | 0 |
| 11-25 | 8,314 | 2,844 | 1,125 | 184 | 9 | 0 |

筛查包括 V2、V3、Pancake V3、Uniswap v4、Balancer V2、两种 Curve 和 Bancor Conversion 的 topic0，不限池地址。单例池以 emitter 和 PoolId 配对。按交易分组后，对至少有五次命中的 16,585 笔分别解析币量和方向；一个原始整数恰好出现一次作为输出且一次作为输入，就建立一条待查边。回调可使事件顺序与资金方向相反，寻找时允许逆日志序，同一逻辑池只可访问一次。资产相异却同额会误连，分摊、零核心 delta、RFQ、未识别协议和相同金额重复出现都会漏连。排序用深度、不同池数、命中数、交易哈希依次决胜。另外取事件最多且不同于第一名的一笔作对照。该选择用来寻找反例，两笔样本不能估计成功率。[离线 SQL](ETH_ARCHIVE_SERIAL_SEARCH_20261127.sql) · [候选排序命令](../../scripts/rank_archive_swaps.py)

16,585 笔中，等额候选深度的最大值为 20，另有各 1 笔为 13、12、11。第一个候选的手工核对深度已超过筛查值。这直接表明，仅搜索“自动算出 ≥50 腿”的交易会错过长链。行业帖同时提供了另一条检索途径。11 月 13 日恰有 272 个 ERC20 标准形状 Transfer 的交易有 4 笔，其中仅 1 笔命中兑换事件。其 473 条日志、四批 WETH 资金流、109 计数约定、原帖时间关系与作者身份归属边界见[独立核验](ETH_TRANSFER_FINGERPRINT_20261127.md)。

## 收据、调用树与费用

冻结两笔的链、位置及哈希后，用 PublicNode 读 Ethereum 区块头、有序交易哈希，Nodely 读全块收据，dRPC 读包含日志的 Geth callTracer。每源 2 次、共 6 次匿名读取，RPC 支出 USD 0。按 Prague 头部字段重算块哈希，按交易索引、收据类型和累计 gas 重建收据树；通过后将收据绑定到块头所列位置，逐条比对调用树中已提交的日志。收据根承诺累计 gas、状态和日志，effectiveGasPrice、交易列表与 trace 仍有对应提供者边界。没有从签名交易重建交易树或证明账户前后状态。

| 指标 | 等额链最长 | 命中事件最多 |
| --- | ---: | ---: |
| Ethereum 高度 / 交易位置 | 26,489,188 / 266 | 26,493,761 / 18 |
| 完整区块收据 / 本笔日志 / 调用帧 | 327 / 190 / 556 | 268 / 663 / 1,697 |
| 归档匹配事件 / 不同逻辑池 | 48 / 48 | 180 / 127 |
| 等额候选长度 | 20 | 4 |
| 通用解码器识别 / 已解出双方币量的腿 | 45 / 15 | 166 / 165 |
| 通用解码器成立的资金连接 / 最长路径 | 7 / 4 | 64 / 4 |
| gasUsed × effectiveGasPrice，wei | 4,349,452 × 56,698,735 | 13,154,427 × 350,803,198 |
| 实际收据 gas 费，ETH wei | 246,608,426,343,220 | 4,614,615,059,457,546 |
| 执行器直接向块 fee recipient 付款，ETH wei | 179,497,026,312 | 16,586,266,029,080,583 |
| 合并发送者与目标合约，可见 ETH + WETH 变化，扣 gas 后，wei | +36,221,727,789,478 | +71,744,317,490,579 |
| 其余非零 ERC20 变化 | USDC −2 最小单位 | 33 种余额变化，其中 sDAI −706,374,242,475,133,895,631 最小单位 |

ETH 只计算成功祖先下真正跨过两个地址边界的 CALL，单列了构块者付款。WETH 纳入标准转账和包装／解包。每个整数金额保留原币种。后一笔还有来自多个协议的代币余额，直接把 ETH 余量当作总收益会遗漏库存的价值。前一笔从微小 USDC 输入开始，54 次资产变换后闭合，再用一次转换结为 ETH；能追溯本次可见入账。外部归属、跨交易支付、其他尝试的成本以及独立账户余额证明仍缺。54 腿样本在块末按原包络重跑，首腿实际输入从 2 降为 0 并回滚；直接子块因原 fee cap 低于 base fee 在 EVM 前被拒。[同包络寿命](ETH_SERIAL_LIFETIME_20261127.md)[54 次变换的原交易](https://etherscan.io/tx/0xe8fe407861f22fcfc39e2914de3ead6e0a8c28e87c7ce26236ce3f7b5e369b6e) · [180 事件对照](https://etherscan.io/tx/0xf6be79db72f8dda6fa6bac6bcd9fa0d2ec6b56a04bd461d448e206a367b54683)

## 离线重现

机器可读报告提供原始两个公开对象 URL、字节数、SHA-256 和六组 RPC 请求/回复哈希。将校验过的日分区分别命名为 `2026-11-13_0.parquet` 与 `2026-11-25_0.parquet`，在存放它们的独立目录运行。DuckDB 2.0.0 使用六个 CPU 线程和 4 GB 内存限额。重新执行已提交 SQL，生成的 `candidates.csv` SHA-256 为 `629285306b7e86f0f1fe168c4c22022deda620fc24e444de8b095445d4e6e7c9`。仓库排序器与 Aleph 首轮独立实现就全部深度分桶、两个排行榜、选定路径和输入摘要完全一致。完整收据和 trace 的离线重放也逐字节再现了两个结果。

```sh
duckdb -init /dev/null -batch -bail < "$repo/docs/verification/ETH_ARCHIVE_SERIAL_SEARCH_20261127.sql"
PYTHONPATH="$repo" "$repo/.venv/bin/python" -m scripts.rank_archive_swaps candidates.csv > ranking.json
```

本地 `reports/research/eth-open-archive-20261127/` 保留候选、预算账本、计划、六组原始往返、派生结果及复核脚本。两个日志日分区和 11 月 13 日交易分区持久保存在 Aleph 的 `~/.local/share/oasis-agent/research/eth-archive-20261127/`。三份 Parquet 在保存后均再次读取并校验 SHA-256。归档筛选与这两笔整块复核全部采用公开免费数据。全天数据在日终后到达，历史普查没有测得发现当时、模拟完成时和下一块的实时净额。

## 工程验证

本轮代码以 `c9699f9` 为起点。覆盖免费归档排序、BSC 多见证搜索、Base 单池整数跨 tick 重放；Ethereum 专项验证器只读取留存数据。首次 `make check` 退出码 2，出现一个仓库禁止的 future import，以及工作树在测试期间加入排序脚本导致的一次代码摘要变动。移除 import，冻结测试输入，两个失败用例均在聚焦回归中通过，随后用最终源码完整重跑。初次失败日志和最终日志都保留在 `reports/research/multihop-archive-gates-20261127/`。

| 检查 | 最终退出码 | 结果 |
| --- | ---: | --- |
| `make check` | 0 | Ruff、mypy、前端检查通过；6,668 项 Python 测试和 138 个子测试，127 项 Foundry 测试通过 |
| `make security-check` | 0 | 锁定依赖、Bandit 和秘密材料检查通过 |
| `make contract-security-check` | 0 | Slither 检查 98 个合约，6 项既有提示 |
| `CARGO_NET_OFFLINE=true make rust-check` | 0 | 格式、Clippy、30 项 Rust 测试通过 |

四条链的离线复核、逐腿 JSON 与两次 Base capture/grid 复算通过。54 腿交易的直接子块免费模拟以费用拒绝退出码 3 落盘，离线验证完整重现拒绝。[后继状态对照](ETH_SERIAL_LIFETIME_20261127.md)
