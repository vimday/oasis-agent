# 八链样本归集、公开交易复核及 Ethereum 新影子窗口

2026-11-27，北京时间。以工作区基线 `3f844a1` 和当时已完成的整批双源及 Base 配对研究为起点。本记录新增八链交易索引、四链论文样本的原始收据、两个匿名 Ethereum 新窗口及子块及时性修正。[研究总览和各链代表交易](../strategies/MULTICHAIN_MULTIHOP_SAMPLE_ATLAS.md) · [起始验证记录](ETH_BATCH_HKC_AND_BASE_RPC_20261127.md)

## 交易身份和证据强度

1. 核对既有交易报告、队列、收据或 trace 原件、早期调研附录和 Elysium 档案。原有五链共 637 笔个案，同一交易多次分析保留全部来源。Ethereum 211 笔分别有 90 笔收据根与调用树、6 笔调用树、24 笔收据根、16 笔收据级核查、75 笔仅入队。BSC 97 笔中 20 笔有调用树或收据级资金证据。不同链、不同证据等级的分母独立。[五链总表及入口](../strategies/MULTICHAIN_MULTIHOP_SAMPLE_ATLAS.md#各链到底找到了多少)
2. 重建四链事件筛选，共 83,165 条来源行，对应 82,894 个去重交易。Ethereum 覆盖以 UTC 为单位的 11 月 13、23、24、25 日四个完整日；11 月 26 日下载超时，全天分母缺失。BSC 一个截断响应仅纳入 4,078 个完整且父子连贯的块，截断尾部及相邻范围之间的 18 块缺口留档。Base 最早 500 块和 Robinhood 最早 20,000 块保留汇总及命名样本，缺全体逐笔哈希。仅有状态的影子路线、同窗无交易的头及未覆盖的高度单独记范围。[原件、选择规则和同高交叉核验](MULTIHOP_SCREENED_SOURCES_20261127.json)
3. 读取公开论文作者的四个固定 Git blob，逐行验证身份、组别和不同日志索引，选择每链两个极值。分片包含 17,500 笔标签。第一次目录列表超过下载限制，只用于定位；四个入选文件分别完整下载并核对 Git 对象 ID 和 SHA-256。新取八笔全部 263 个兑换事件的金额和日志索引逐项复算，另追踪中间人、共享 vault、实际收款方和 gas 付款方。原始收据和块头查询首次空结果停该来源，Ethereum、Arbitrum、zkSync 各有一项。独立冻结的两份 Optimism 区块取价补齐旧收据的 gasPrice 与 L1 分项。历史核验两个范围共 27 + 6 次免费 RPC 预留。[作者原始数据](https://zenodo.org/records/12786367) · [原始响应指纹](PUBLISHED_MULTIHOP_RECEIPTS_20261127.json) · [逐笔结算](PUBLISHED_MULTIHOP_SETTLEMENT_20261127.md)

最终 **100,572** 个 `(chain_id, tx_hash)`，覆盖八条链。案例表 **655** 笔中显式分类为 17 笔串行、36 笔分组、8 笔混合、47 笔对照、547 笔结构未定。分类描述交易结构。单看总表的分类无法推出闭合、归属、费用完整度或盈利。全表有 177 笔能导出至少一个网络费分项；空字段保留未知。

| 可移植产物 | 字节级 SHA-256 |
| --- | --- |
| [全部可定位交易，gzip TSV](MULTICHAIN_MULTIHOP_ALL_HASHES_20261127.tsv.gz) | `bf5a3cafbae4b84c6949f3dc66e209a23d053b6b8c76de83f0df9adb3ea593bd` |
| [其中 655 笔案例，普通 TSV](MULTICHAIN_MULTIHOP_CASES_20261127.tsv) | `d1e615ad3448415f143534299613ff775ddd55045744a8f92e26c8b25b5c5e1c` |
| [宽筛源清单](MULTIHOP_SCREENED_SOURCES_20261127.json) | `5fd6c9c60e42a70331acca67b7a9ff7b093d0a261b1f275c25af25facc673ce4` |
| [八笔收据机器证据](PUBLISHED_MULTIHOP_RECEIPTS_20261127.json) | `cf3fd6372815fdf06a6f0b7889677904723799e86d440a635bb24f88b787ff56` |

两表字段相同。`review` 记录核查方式，`shape` 记录结构，`screen_events_max` 是原筛选报道的最大事件数。`funded_market_swaps_lb` 是留存材料能导出的连续池交易下界；跨协议闭合动作数和其中的池交易总数分别见 `closed_hybrid_actions`、`market_swaps_in_closed_route`。只对已有相关机器字段的案例填数。`published_*` 保存作者行号、索引数、组数和按币种、整数金额相接的最长闭合组。`observed_network_cost_native_raw` 用链原生资产最小单位，按 `cost_basis` 区分收据 gas、两层费用部分和仅执行费。`sources` 可以追到每条来源，各币种收付、未知成本、原交易索引和受益归属见分链清单和结算报告。

## 固定本金的两个 Ethereum 新窗口

采用相同 45 池、7,976 条路线、每条十档本金。先计确定币种和方向的整数毛差，再和基础 gas 下界比较，随后做有序整批选择。每个新头独立冻结范围、包络和原免费账本。若自然净正短名单为空，则执行固定的两段共享池负控。H 为发现头，K 为 H 双源整笔返回后读到的最新头，C 为 H 的直接子块。三个窗口均 K=H，因此 H、K 对应同一链状态。下表为整笔 dRPC trace 的模拟 gas 口径，单位 ETH wei；双源整笔输出及逐腿金额另行验证。

| H 高度 | 正毛差点 / 79,760 | 过基础 gas | H/K 扣 gas 净额 | C 扣 gas 净额 | H/K 再扣准备 | C 再扣准备 |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 26,506,729，上轮 | 22 | 0 | −310,587,755,662,335 | −310,919,169,555,717 | −1,886,304,894,062,335 | −1,888,339,556,755,717 |
| 26,506,873，新增 v1 | 36 | 0 | −220,975,961,166,521 | −235,666,709,828,435 | −1,333,929,671,566,521 | −1,424,121,177,828,435 |
| 26,506,941，新增 v2 | 30 | 0 | −211,887,461,422,509 | −230,778,723,003,207 | −1,279,399,243,022,509 | −1,395,379,127,803,207 |

每个 H/C 的 trace gas 均为 466,986。第二个新窗在收头后 2.440 秒取得 H 双源，H 后续的 PublicNode latest 仍返回同一头。直接子块查询由整笔返回触发，无法反推 C 最早可见时刻。旧格式曾用这一串行观测顺序给及时性赋真；新入口将缺独立 C 时钟的及时性留空，旧范围凭原预注册哈希复算以保留原件。全表三头计 239,280 个报价点、88 个正毛差点，没有自然费后正例。自然候选的直接子块存续率分母为零。[双源实验与上轮 Base 配对](ETH_BATCH_HKC_AND_BASE_RPC_20261127.md) · [正常影子入口](../../scripts/run_eth_v2_batch_shadow.py)

两个新增范围分别使用 PublicNode/dRPC 14/6 和 15/6 次免费预留。v1 范围、结果 SHA-256 分别为 `35de393e6d2d05ecb2bb17e59e1598a4625230a8690c944628ac371a0bbbceaf`、`a8e45837ab1fe4372a3bc4030642c1eaaea4e1a3484af1f7444851d362031610`。v2 为 `f7c3ff9090038956a42a2306be4658a3436c0e74370d10569369ff8e08a2ea82`、`c5a1c15736a496eed20772f968b002202044b4ce2394f9837d45afe69e49b64a`。两个新窗及公开收据共 74 次免费预留，支出 USD 0。Base 上轮两组同头配对的状态及 4/58/66 池扫描一致，HTTP 减少一次，两臂各 0/2 赶上直接子头。这里按原配对的两个观测计数。

## 同期的 BSC、Robinhood 和 Base 新窗口

| 链与观测范围 | 候选与净额 | 交付时钟与证据边界 |
| --- | --- | --- |
| BSC，H 136,050,291，115／138 池 | 9,582 个整数报价点无正毛差，另有 1,032 个深度或舍入残差。固定两跳负控按普通气价，仅扣 gas 后 H、K、C 区间上端依次为 −10,425,977,166,165、−10,427,815,860,672、−10,425,413,814,331 BNB wei。 | H 整笔在收头后 949.838 ms 返回，K=H+3。单一官方源。H 的 139 份收据重建根通过，10 笔各有至少两个兑换形状，计入逐笔表及宽筛，各有实付网络费，3 笔命中同一新增池。资金归属仍未解。[窗口](BSC_NEXT_WINDOW_20261127.md) · [十笔哈希与原始日志索引](BSC_NEXT_WINDOW_SCREENED_20261127.json) |
| Robinhood，H 124,298,173，主、延迟 feed | 24 个路线与状态组合，21 个返回报价，3 个 24 跳回滚。零个过基础 gas。两跳负控按整笔估费，H、K、C 分别为 −2,847,995,460,695、−2,833,389,906,070、−2,837,725,092,695 ETH wei。 | 两路各 144 帧，130 条消息同哈希配对。K=H+58。三态整笔调用和估费均已双源完成，C 的补充现金探针在第二来源触发归档权限拒绝并停止，H/K 的补充现金读缺测。[双 feed 和逐状态观测](ROBINHOOD_NEXT_WINDOW_20261127.md) |
| Base，H 62,987,604，保留 4／58／66 池 | 三组扫描在两臂一致，均无模型正例。无自然包络需要送三态复核。 | 直接子头间隔 214.609 ms。重叠与批处理从同一收帧到完整状态各为 1,041.752、1,871.026 ms；两臂顺序执行，第二臂含等候第一臂的时间。单个同头不足以确定默认方案。累计三个配对头，两臂各 0/3。[新配对](ROBINHOOD_NEXT_WINDOW_20261127.md) · [前两个配对](ETH_BATCH_HKC_AND_BASE_RPC_20261127.md) |

本轮新增请求的各自原账本计 Ethereum 和外部收据 74、BSC 16、Robinhood 46 个 HTTP 及两条 feed 连接、Base 13 个含 WS 的逻辑预留。这些单位和实际 HTTP 往返分列。全部范围使用免费数据，支出 USD 0。新 BSC 块的 10 笔都与原 82,894 个宽筛哈希不同，合并后宽筛覆盖 82,904 笔。

## 离线复算

提交的表可以直接读。重建需要四个作者第一分片和各次采集本机保留的原始请求、响应、原账本及 STOP；全表不包含认证信息、交易输入或可广播的原始交易。以下命令从仓库根目录运行，生成物写入既有研究目录。

```sh
run=reports/research/multichain-inventory-20261127
f=docs/verification/fixtures
.venv/bin/python "$f/build_screened_multihop_hashes.py" --output "$run/screened.tsv" --manifest "$run/rebuilt-screened-manifest.json"
.venv/bin/python "$f/replay_published_multihop_shards.py" "$run" > "$run/rebuilt-selection.json"
.venv/bin/python "$f/replay_published_multihop_receipts.py" "$run" > "$run/rebuilt-receipts.json"
.venv/bin/python "$f/build_multichain_sample_catalog.py" "$run" "$run/rebuilt.tsv.gz" --case-output "$run/rebuilt-cases.tsv"
cmp "$run/rebuilt.tsv.gz" docs/verification/MULTICHAIN_MULTIHOP_ALL_HASHES_20261127.tsv.gz
cmp "$run/rebuilt-cases.tsv" docs/verification/MULTICHAIN_MULTIHOP_CASES_20261127.tsv
.venv/bin/python -m docs.verification.fixtures.replay_bsc_next_window_screen | cmp - docs/verification/BSC_NEXT_WINDOW_SCREENED_20261127.json
.venv/bin/python -m scripts.run_eth_v2_batch_shadow replay "$run/fresh-hkc-v2" --scope-sha256 f7c3ff9090038956a42a2306be4658a3436c0e74370d10569369ff8e08a2ea82
```

正常入口及时性修正的 18 项聚焦回归通过。`make check` 退出 0，6,821 项 Python 测试、138 个子测试、9 个前端模块、132 项合约测试通过。`make rust-check` 退出 0，30 项 Rust 测试通过。`make security-check`、`make contract-security-check` 均退出 0；Slither 检查 102 个合约，原有 8 条低于阻断门槛的提示保留。日志在上述目录的 `full-final.log`、`rust-final.log`、`security-final.log` 和 `contracts-final.log`。
