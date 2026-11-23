# Ethereum 同一执行器的两周交易分母和失败成本

2026-11-23 UTC。Ethereum 主网 `chainId=1`。对照对象为 [已核对的批次执行器](MULTIHOP_FORWARD_OPPORTUNITY_20261123.md) `0xdadaa485c5a798871802eb89d474a31bca82ab0e`。本轮固定两个完整 UTC 周 `[2026-11-09, 2026-11-16)` 和 `[2026-11-16, 2026-11-23)`。在 Blockscout 直达该合约的顶层交易索引中，仅按 calldata 的前四字节 `0xe279f616` 纳入，保留所有状态。日期于新抓取前写入范围记录，旧页已经可见，因此这是固定窗口的回顾性成本统计。失败样本另做双 RPC 回执核验。[执行器交易索引](https://eth.blockscout.com/address/0xdadaa485c5a798871802eb89d474a31bca82ab0e?tab=txs)

## 同一选择器的已上链调用

新索引页于 `10:10:39.656649Z` 收到，50 个有序交易哈希与先前留存页一致。最早、最晚条目时间分别为 `2026-11-02T03:26:47Z` 和 `2026-11-20T08:03:47Z`，首屏已越过时间窗口的较早边界。两周出现 14 笔直达交易，其中三笔采用其他选择器。11 笔被纳入交易发送方均为 `0xf4f1a9c40c62788e60ea90b8fc246e4ab34077bb`。表中 gas 费用逐行以整数 `gasUsed × effectiveGasPrice` 核对。

| UTC 时间窗 | 纳入 | 成功 | 回滚 | 全部 gas（wei） | 回滚 gas（wei） |
| --- | ---: | ---: | ---: | ---: | ---: |
| 11-09 至 11-16 | 8 | 8 | 0 | 6,875,872,038,166,640 | 0 |
| 11-16 至 11-23 | 3 | 0 | 3 | 445,765,983,387,992 | 445,765,983,387,992 |
| 合计 | 11 | 8 | 3 | 7,321,638,021,554,632 | 445,765,983,387,992 |

观测回滚比例为 **3/11**。gas 总额按 8 笔已上链成功调用平均分摊，覆盖成本所需毛差至少为 **915,204,752,694,329 wei/笔**，其中失败贡献 **55,720,747,923,499 wei/笔**，均向上舍入。全组单笔 gas 费用中位数为 **123,727,234,951,890 wei**。11-10 的一笔成功承担了全组 gas 费用的约 **77.7%**，开销 **5,689,539,495,392,993 wei**。[11 月 10 日交易](https://eth.blockscout.com/tx/0x17e3a88f3c8bea04ea966b728cbba119fbd4f60375a1892fd918aec338f71503)

8 笔成功的状态和 gas 使用该地址索引字段。此前单独选择并做收据根与 trace 对账的 11 月 13 日 UTC 成功交易也落在窗口内。其余成功交易的合计策略收益、直接建块支付和其他成本未获得，整组净额未知。分母覆盖该索引可见的顶层交易，未测内部调用、未纳入区块的提交及索引外缺失，也无法判断第二周活动变化的原因。[此前选样及逐笔对账](MULTIHOP_FORWARD_OPPORTUNITY_20261123.md)

## 三笔回滚的独立回执与收据根

PublicNode 按交易哈希返回三份 EIP-1559 收据，并按高度返回含有序交易哈希的块头字段。Nodely 按三份区块哈希返回完整有序收据，分别 **380、306、244** 份，共 **930** 份。两 RPC 的三笔目标收据在区块哈希、交易索引、类型、状态、`cumulativeGasUsed`、`gasUsed`、`effectiveGasPrice`、交互地址和日志字段上逐项一致，也与地址索引的相应字段一致。离线按交易索引 RLP 作键重算三棵收据 trie，**3/3** 根与 PublicNode 块头 `receiptsRoot` 一致。交易 gasUsed 也等于有序区块收据中相邻 cumulative 差。收据根承诺状态、累计 gas、日志等共识字段。`effectiveGasPrice` 来自两 RPC 一致字段和地址索引。[PublicNode](https://ethereum.publicnode.com/) · [Nodely 免费历史范围](https://nodely.io/docs/ethereum) · [Ethereum 收据 trie](https://ethereum.org/en/developers/docs/data-structures-and-encoding/patricia-merkle-trie/)

| 11-20 UTC | 区块 · 索引 | Gas used | 有效 gas 价（wei） | 实付 gas（wei） | 交易 |
| --- | --- | ---: | ---: | ---: | --- |
| 00:36:11 | 26,452,758 · 253 | 699,015 | 120,232,844 | 84,044,561,448,660 | [`0x494d…e7ec`](https://eth.blockscout.com/tx/0x494d07afdfab8f508e20e84e6834ded6730807846a1c866e64612be03de0e7ec) |
| 01:01:47 | 26,452,886 · 205 | 868,460 | 104,034,361 | 90,349,681,154,060 | [`0xc4c5…0cee`](https://eth.blockscout.com/tx/0xc4c5cdfc7054b221c29b476725d31823e0501010922e5121ad53cc6262130cee) |
| 02:08:35 | 26,453,218 · 35 | 947,448 | 286,423,889 | 271,371,740,785,272 | [`0x5c50…de19`](https://eth.blockscout.com/tx/0x5c50be61d6bc125aba52b014353e097d3900468f59c5e6c3abdc5e41396dde19) |

三笔 `status=0`、提交日志均为零，且各自只支付 0.01 gwei 的优先费。按块头 `baseFeePerGas` 拆分，gas 费用合计 **445,765,983,387,992 wei**，其中 **420,616,753,387,992 wei** 销毁，**25,149,230,000,000 wei** 为优先费。失败交易内部调用发生的状态变更均回滚。三笔的失败原因及可能在其他交易或链下发生的付款未知。[EIP-1559](https://eips.ethereum.org/EIPS/eip-1559)

## 留存与离线复跑

采集窗口为 `2026-11-23T10:10:38Z` 至 `10:12:26Z`。四个匿名 HTTP 请求共 $0，各在原账本预留后发送。Blockscout、PublicNode、Nodely 各成功一次。dRPC 首次请求以 HTTP 400、JSON-RPC `code=31` 拒绝四项免费批次，此请求返回体留存，该来源终止。Nodely 的独立三项整块收据范围另行登记后发送。没有签名或提交链上交易。[dRPC 方法说明](https://drpc.org/docs/ethereum-api/blocksinfo/eth_getBlockReceipts) · [Ethereum 执行 API](https://ethereum.github.io/execution-apis/api/methods/eth_getBlockReceipts/)

`reports/research/eth-batch-selector-cohort-20261123/` 是本地忽略目录。保留范围、请求与响应原文、采集和账本记录、`verify.py` 及 `verified.json`。旧地址页在相邻 `forward-opportunity-20261123/executor-explorer-history/address.res`，用于精确比较有序哈希。以下命令仅用本地文件，生成的 JSON 可逐字节比较。

```sh
evidence=reports/research/eth-batch-selector-cohort-20261123
.venv/bin/python "$evidence/verify.py" | cmp - "$evidence/verified.json"
```

| 响应文件 | 响应原文 SHA-256 |
| --- | --- |
| `blockscout-1.response.json` | `c858bbbb44cc96a637c5b4a56a3ad009617dc6555b54bde2363488a5a810098d` |
| `publicnode.response.json` | `ec3f9ab15c4b06f4d8fd7f96757a00e65a810cb50f0798b46714c64b4443ee63` |
| `nodely.response.json` | `165db1e838b0200b1490bfbfc9825e0d96d5f7c7d1c77e75a25d65b6a24209c5` |
| `drpc.response.json` | `275c5bba5b39aa4576c705feef2519d30d0ea8565bc9ba67b82f35b96d486b64` |
