# CCS 2024 四链第二分片与两笔资金结算

在作者同一提交的四个编号 `2` 分片找到 **17,500 个新哈希**，与首分片、上一版 100,572 条总册均无交集。128 笔有至少 20 个不同兑换事件索引，28 笔至少 50 个，4 笔至少 100 个，200 个为零。组内相邻腿资金同币同额且回到起始币的最大深度为 **7**。独立核查发现，Optimism 的 109 事件样本做了 37 个可证短环，Arbitrum 则有一笔实际七跳闭环。[作者固定提交](https://github.com/christoftorres/Rolling-in-the-Shadows/tree/79d31e0a07b702b5eabb849ac5d513863cad8bb3/data/mev/arbitrage) · [论文](https://arxiv.org/abs/2405.00138)

## 覆盖与分组

| 链 | 第二分片唯一哈希 | ≥20 / ≥50 / ≥100 / ≥200 事件 | 相同整数资金闭环组 | 最长单组 |
| --- | ---: | ---: | ---: | ---: |
| Ethereum | 5,000 | 0 / 0 / 0 / 0 | 3,992 | 6 |
| Arbitrum One | 5,000 | 6 / 0 / 0 / 0 | 3,190 | 7 |
| Optimism | 2,500 | 122 / 28 / 4 / 0 | 3,807 | 6 |
| zkSync Era | 5,000 | 0 / 0 / 0 / 0 | 2,020 | 5 |
| **合计** | **17,500** | **128 / 28 / 4 / 0** | **13,009** | **7** |

每笔按作者的 `Swap.index` 去重。逐组检验起末资产一致、每个相邻输入输出币种相同、整数金额完全相等，记录组号、实际原子单位、尚未扣费的同币差额及每腿顺序。全分片有 442 笔多组记录，组间交换日志没有重复。该筛法保留独立短环和单组串行的区别，会漏掉经转换、拆分合流或共享托管内部结算的路径。研究只用四个编号 2 分片。未对数据集中其他编号分片或全链发生率作估计。[与上一轮首分片对照](PUBLISHED_MULTIHOP_SETTLEMENT_20261127.md)

| 链 | 原始 Git blob | 字节 |
| --- | --- | ---: |
| Ethereum | `9e791a6e7bc664b2103a69080502c9ed47b1b653` | 20,080,094 |
| Arbitrum One | `a0791bf703ddcf28ee5f1bae099660d0ba6898b7` | 17,057,571 |
| Optimism | `a8f3ff564e1840cf3a24f32fd1ee368408ca6554` | 15,214,019 |
| zkSync Era | `dbff28e408620f8c893c4c14a0c09555259d4467` | 18,885,223 |

原 Git tree 响应尾部截断，四个目标对象记录均完整。冻结后做四次匿名 GET，取得 **71,236,907 字节**，逐文件按 Git `blob <字节数>\0` 前缀重算对象哈希，再核对 SHA-256。总册对照冻结了原压缩表指纹 `bf5a3cafbae4b84c6949f3dc66e209a23d053b6b8c76de83f0df9adb3ea593bd`。新增表的 `published_*` 字段保留作者投影证据级别。逐笔独立复验仅覆盖下述两笔。[17,500 哈希压缩表](MULTIHOP_SECOND_SHARDS_HASHES_20261127.tsv.gz) · [来源、交集及分组](MULTIHOP_SECOND_SHARDS_SOURCES_20261127.json)

## 两笔预选交易的原始回执与资金

冻结规则先取新哈希中事件最多的一笔，再取其余样本中精确闭环组最长的一笔，平分按完整哈希排序。两笔实际交易均重算区块哈希、交易根与收据根，并核对发送者签名恢复地址。没有调用树，链下付款、原生币内部转移及完整经济净额未验证。[逐笔根、原始请求和逐组资金边](MULTIHOP_SECOND_SHARDS_RECEIPTS_20261127.json)

**Optimism `0xb85f0867de699f8e71ff611aa40eefe79cdbf68f1bfe9015ce0f518fbb5733e5`。** 区块 11,169,933，索引 0，2022-06-10 16:28:21 UTC。该块只有一笔交易，两家的完整单笔收据及块头足以重算整块收据根。109 条兑换事件全部复核整数输入输出，共 **35 个三跳组和 2 个两跳组**。所有 72 条中间资金边都有转账，2 条为池间直连，70 条通过唯一的等额中转。35 组三跳每次使用 `25,000,000,000,000,000` WETH wei，另两组分别使用 `24,999,999,524,615,988` 和 `24,999,999,830,172,804`。合计投入 `924,999,999,354,788,792` wei 是交易周转。37 组内末池向执行器的 WETH 转账在该组执行器向首池支出前已经出现，首尾各组为 `0→2` 和 `553→555`；具体资金授权与调用上下文仍缺调用树。[交易记录](https://optimistic.etherscan.io/tx/0xb85f0867de699f8e71ff611aa40eefe79cdbf68f1bfe9015ce0f518fbb5733e5)

第一组毛差 `285,725,073,472,475`，最后一组 `61,193,560,335,988` WETH wei。执行器 WETH 转账净额为零，日志 568 把全部组差额 `5,654,794,866,077,543` wei 付给第三方 `0xb5833db3379abba68c591645e5df1423d56460b4`。原发送者的已知网络费为 `11,377,986 × 1,000,000 + 225,208,929,127,520 = 236,586,915,127,520` ETH wei，包含原收据 L1 费。以 WETH/ETH 等值合并两地址，扣该网络费后的条件差额为 **+5,418,207,950,950,023 wei**。两地址共同控制未验证，逐组增量 gas 也未知。该样本与首分片的 71 个两跳环同为复用容量的短环批次，但结算资产、资金流与收款归属不同。[首分片对照](PUBLISHED_MULTIHOP_SETTLEMENT_20261127.md)

**Arbitrum `0x1ff0c2dd4f302a5d278261be2924131f3c0f18cb3ea6d8da95becfa0252bddb1`。** 区块 54,354,197，索引 4，2023-01-20 20:07:16 UTC。Nitro 官方源留存整块 5 份收据及交易。发送方在日志 21 向新建执行器付 `28,125,000` USDC.e 原子单位。七腿通过 USDC.e → Sperax USD → handleUSD → handleFOREX → WETH → USDT → WETH → USDC.e，兑换日志 `28→32→34→42→47→53→58`。六条实际中间资金边均可追踪，逐腿金额、池和原始资金日志索引在机器记录。日志 59 将 `29,758,774` 原子单位全部付回原发送者，发送者可见差额 **+1,633,774 USDC.e raw**，执行器净余额为零。网络费 `6,175,292 × 100,000,000 = 617,529,200,000,000` ETH wei。缺同块可兑现换算与完整原生币流水，跨币净额留空。[交易记录](https://arbiscan.io/tx/0x1ff0c2dd4f302a5d278261be2924131f3c0f18cb3ea6d8da95becfa0252bddb1)

## 免费预算、缺测与重放

四个独立预注册 RPC 来源范围实际使用 **13/14** 次只读请求，费用 USD 0。Optimism 官方来源取得头和目标收据后，其第 4 次 `eth_getBlockReceipts` 返回 HTTP 403、RPC −32601，方法未列入允许列表。dRPC 另有独立头和目标收据，单笔区块的收据根仍可完整复算。Arbitrum dRPC 第 2 次查询返回 `null`，停止该来源，放弃预留范围内的头读取；Arbitrum 的根和费用来自官方归档原件。没有重试、替换源、签名或广播。

本机忽略目录 `reports/research/multihop-second-shards-20261127/` 保留 4 个原始分片、原总册副本、选样与预算、RPC 字节和停止记录。`closed-groups.tsv.gz` 保留 13,009 个闭合分组的所有资产、原子金额和日志索引，SHA-256 `920079ba1e7e1ca66af07c70940088ebf4327c5cdb37fd89f2ab841d01ab6515`。新哈希表 SHA-256 `d3d352111af5e046f7cb7b6b22845dbd77842963190d4a6ca6de128abbe44fef`。离线复算需要原收据与原分片。[全量构建器](fixtures/replay_multihop_second_shards.py) · [收据复算](fixtures/replay_multihop_second_shard_receipts.py)

```sh
set -eo pipefail
root=reports/research/multihop-second-shards-20261127
tmp=$(mktemp -d)
.venv/bin/python docs/verification/fixtures/replay_multihop_second_shards.py "$root" \
  --prior-catalog "$root/prior-catalog.tsv.gz" --hashes "$tmp/hashes.tsv.gz" \
  --groups "$tmp/groups.tsv.gz" | cmp - docs/verification/MULTIHOP_SECOND_SHARDS_SOURCES_20261127.json
cmp "$tmp/hashes.tsv.gz" docs/verification/MULTIHOP_SECOND_SHARDS_HASHES_20261127.tsv.gz
cmp "$tmp/groups.tsv.gz" "$root/closed-groups.tsv.gz"
.venv/bin/python docs/verification/fixtures/replay_multihop_second_shard_receipts.py "$root" |
  cmp - docs/verification/MULTIHOP_SECOND_SHARDS_RECEIPTS_20261127.json
```
