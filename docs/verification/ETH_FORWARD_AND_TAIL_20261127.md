# Ethereum 前瞻三时点与新历史资金流复核 · 2026-11-27 UTC

新历史样本中复核到 **9 个不同池、2 个包装动作**组成的同一条 WETH 闭环。另一笔独立交易内复核到 **3 组** Uniswap v4 WETH 本金借还。实时窗口覆盖八个完整状态和 638,080 个路径金额组合，发现零个正毛额候选。所有采集只读，匿名 USD 0。

## 前瞻整笔模拟

45 个 Uni/Sushi V2 池使用已有工厂、代码与币对登记证明。每个新头按区块哈希批量读取全部储备并复核同高哈希。每状态对 7,976 条 2–6 池 WETH 闭环计算 10 档整数本金，范围 0.0001–10 WETH。各池在同一路径中只走一次，连续报价上界的筛选只扣当时 21,000 gas 的基础费下界。保守报价门槛使用另行冻结的 900,000 gas、下一块基础费涨幅和固定优先费假设。窗口、撮合协议和起始币限制均写入原计划。

| UTC 窗口 | 区块、请求数 | 检查结果 |
| --- | --- | --- |
| 01:36:38.790–01:37:50.300 | 26,503,298–303，6 个连续区块，22/28 HTTP、29/48 逻辑 RPC | 478,560 个路径金额组合，0 毛额为正，0 个连续全金额上界越过基础费下界。接头至状态 468–498 ms，至排名 1,130–1,164 ms |
| 01:39:00.620–01:39:14.321 | 26,503,310 完整扫描，26,503,311 只检验直系子块，9/15 HTTP、12/25 逻辑 RPC | 79,760 个路径金额组合，三个正值计数均为 0。接头至状态 481 ms，至排名 1,139 ms |

两个范围均冻结第一条可定价路线为负对照。为 `WETH→USDT→LINK→WETH` 输入 **100,000,000,000,000 wei**，整笔 `eth_call` 在仅覆盖研究调用者余额与探针代码的条件下读取真实池状态。五次执行均返回 **99,801,718,876,155 wei**、毛差 **−198,281,123,845 wei**，与整数模型一致。探针内部计量 327,119 gas，不包含真实交易的全执行开销，也没有从调用者扣费。正机会寿命的分母为 0。第一范围的三个状态为 H/H+1/H+2，第二范围另行实测首次返回后看到的最新头。

| 第二窗口阶段 | 实际区块和接收到的证据 | 同一包络下的保守净额情景（wei） |
| --- | --- | ---: |
| 发现块 H | 26,503,310，首次整笔响应 01:39:02.473 | −88,346,460,923,845 |
| 首次整笔返回后的首次最新头 | 下一次网络读取于 01:39:02.684 发出，02.942 接收，仍是同高同哈希 H，复用 H 的整笔证据 | −88,346,460,923,845 |
| 直系子块 H+1 | 26,503,311，单独核验 parent hash 等于 H；整笔响应 01:39:14.321 | −84,229,750,223,845 |

第二窗口步骤二包含发送前 211 ms 的研究请求节流，接收时距离首次整笔响应 469 ms。重用同块证据时不会把同一结果当成第二次独立执行。两窗口调用包络 SHA-256 均为 `4a9b887d40bc32995a2a412ed9c980d0a3df81375748867c4ba06636f4dbfe1a`，冻结 `from/to/value/gas/data` 和静态覆盖。交易类型、chain ID、nonce、access list 与两个 EIP-1559 费率均未随这两个窗口的调用封包，表内费用基于文中政策参数情景。该实验只观察公开已出块状态，V3/V4、未登记池、挂钩、包排序和即将入块的机会均超出此次前瞻覆盖。

### 完整 type-2 参数补测

独立短窗于 **01:53:42.038–01:53:52.211 UTC** 扫同一 45 池。发现块的池储备、发送者 nonce 和状态回读同一批取得，区块哈希固定。共 79,760 个路径金额组合，毛额为正、政策净额为正、连续全金额毛额上界过基础费下界的计数各为 0。接头至状态 460 ms，至排名 1,128 ms。以第一可定价的 `WETH→USDT→LINK→WETH` 和本金 100,000,000,000,000 wei 做负对照。

发送给 PublicNode 的同一无签名调用对象包含 `from/to/value/gas/data`、`type=0x2`、`chainId=0x1`、`nonce=0x0`、`accessList=[]`、`maxFeePerGas=102,572,801 wei/gas`、`maxPriorityFeePerGas=50,000,000 wei/gas`。gas limit 900,000，静态研究覆盖内容相同，覆盖未改 nonce。包络 SHA-256 `29a83571ee1171b0f3cf57b872ef4b9f96cdc65ddf46f56e57faed363fe20573`，EIP-1559 无签名摘要 `0xcae72cda2187962910381cee059b8117d649968a4cb54308192d1b3f8872c1e9`。两个不同状态分别按 canonical hash 查询真实研究发送者 nonce，均为 0。此为 RPC 调用与独立 nonce 核查，未验证签名或入块排序。[EIP-1559 交易与费用规则](https://eips.ethereum.org/EIPS/eip-1559)

| 阶段 | 观测 | 固定限额及该块有效费率下的净额下界（wei） |
| --- | --- | ---: |
| 发现块 H | 26,503,383，首次整笔响应 01:53:43.895 | −87,285,658,787,836 |
| 首次整笔响应后的最新头 | 01:53:44.107 发出下一次读取，44.356 收到，距首次响应 461 ms，仍为 H 且哈希相同，复用首个结果 | −87,285,658,787,836 |
| 直系子块 H+1 | 26,503,384，独立校验 parent 等于 H 的 hash，整笔响应 01:53:52.211 | −82,919,164,787,836 |

两次整笔返回均为 **99,772,581,412,164 wei**，符合模型，毛差 **−227,418,587,836 wei**。按相同输出与包络费用上限，保守净下界 `gross − 900000 × maxFeePerGas` 为 −92,542,939,487,836 wei。表内用更窄的每块 `min(baseFeePerGas + maxPriorityFeePerGas, maxFeePerGas)`。原 `eth_call` 返回值中的 327,119 是探针函数内计量。

同账本第一段在 2 次 HTTP、5 次逻辑 RPC 后停止，两个传输均成功。我把本地批次解包中的 nonce 与回读头位置交换，未能进入首个整笔模拟。修正前先离线重解原始字节，确认同块 45 池与 nonce 0。随后冻结新发现头范围并在同一账本预留，修正段使用 8 次 HTTP、13 次逻辑 RPC。总计 **10/15 HTTP、18/25 逻辑 RPC**。失败前缀保留为独立停止记录，`typed/result.json` SHA-256 为 `c45f88f76c8aa2c2248da0257ad63f1a5b6c6db6e254e18f06dcc152dd844dd5`。

### 同一负对照的整笔 gas 审计

02:04:02.608–02:04:03.593 UTC，另行预留的两次 dRPC 匿名 `debug_traceCall` 均返回 HTTP 200，响应头标记 `free`。沿用发现块及直系子块的 canonical block hash、原 type-2 调用对象与静态覆盖。两个顶层 trace 的调用者、目标、calldata、value、gas limit 与原封包一致，返回数据同原 `eth_call` 逐字节一致。两个状态的顶层 `gasUsed` 均为 **283,936**。首次响应后的最新头仍是 H，可沿用 H 的费用情景。

Geth v1.16.5 的原生 `callTracer` 用模拟交易的 receipt gas 覆盖顶层 `gasUsed`。交易状态转换先扣 intrinsic gas，再计入退款和 calldata 费用下限。这个空 access list 的 type-2 封包按字节计数得到 intrinsic **24,340 gas** 和 calldata floor **29,350 gas**。dRPC 响应未提供后端客户端版本或独立退款计数，因此下表采用兼容 Geth 的整笔计费语义作为模拟情景。实际链上扣费和入块结果无观测值。[Geth 顶层 tracer](https://github.com/ethereum/go-ethereum/blob/v1.16.5/eth/tracers/native/call.go#L196-L216) · [收据 gas](https://github.com/ethereum/go-ethereum/blob/v1.16.5/core/state_processor.go#L160-L175) · [intrinsic、退款及 calldata 下限](https://github.com/ethereum/go-ethereum/blob/v1.16.5/core/state_transition.go#L410-L535)

| 状态 | 原包络有效价（wei/gas） | 整笔模拟费用（wei） | 净额情景（wei） |
| --- | ---: | ---: | ---: |
| H · 26,503,383 | 96,731,378 | 27,465,520,543,808 | −27,692,939,131,644 |
| H+1 · 26,503,384 | 91,879,718 | 26,087,959,610,048 | −26,315,378,197,884 |

## 历史归因与结算

2026-11-23 UTC 日分区覆盖 6,878,661 条日志，1,194,475 笔发出日志的交易，7,175 个发出日志的块，高度 26,474,106–26,481,281。扫描八种兑换日志外形，7,989 笔至少五条；整数唯一同额假设的深度分布为 `1:3740,2:2237,3:1092,4:488,5:339,6:66,7:18,8:7,9:2`。此筛选会遗漏拆合、金额含整型余量、临时记账、部分协议和未发对应日志的兑换，因此深度 9 是该方法的观察最大值。另有 968 笔符合相同主体、唯一等额 WETH 入出假设，1/2/3 对分别有 910/49/9 笔，需以调用作用域证明本金。对早先已报告哈希做固定去重后，分别选择高金额匹配深度与多本金对两个候选。结果仍未建立 20 个不同池的串行样本。AWS 预留 1 次 LIST 和 1 个对象，匿名取回 1,287,833,130 字节，SHA-256 `a860202b464447b0411b4c9dac34ac7058c1410828f43e5fdd289349778cbc72`。[AWS 日分区与源](https://registry.opendata.aws/aws-public-blockchain/)

冻结每笔各读一次 PublicNode 头、Nodely 完整区块收据、dRPC callTracer 日志，共 6/6 请求成功。复算区块头和完整收据 trie，核对交易位置及累计 gas，再用收据顺序验证 committed call tree 和 AWS 全部 612 个原始日志。头和收据共识承诺、RPC 附加元数据、调用树提供者分别记录。实际有效 gas 单价为收据字段，两笔均与各自区块 base fee 一致。树内 direct ETH 付款按接收方等于头部 fee recipient 归类，收款方链外身份未知。

### 9 池闭合路径

区块 26,478,290 索引 5，出块于 13:59:59 UTC，27 份区块收据复算根，目标 401 条日志。宽筛有 85 个兑换日志外形和 65 个池身份。通用 trace 解码覆盖 75 个支持外形中的 71 个，证实 37 条精确连接，保守最长 6 池，剩余 4 个事件保留理由。带选择器、成功调用、每步本金传递和完整相邻凭据的独立 witness 给出 9 个不同池且闭合的更长下界。[交易哈希](https://etherscan.io/tx/0x17886d908ef8eb9f864cd6776991952c15ac79a5a364037318d0ef53efe19c89)

同一个 Morpho 闪借回调内，路径凭据的事件顺序是 `839,842,845,848,851,854,857,860,863,867,870`。其中 863 为 `wrap(uint256)`，870 为 `unwrap(uint256)`，均核查锁入与铸造、销毁与返还的等额转账。845 复核 `exchange(uint256,uint256,uint256,uint256,address)` 及方向、金额、收款地址与返回值。相邻动作具有同币同额，资金接收与下一动作支出之间没有触碰执行器的同资产转账，USDC 在不同阶段经由不同池再次出现。Morpho WETH 转出/归还日志 836/1235 各 9 ETH。该闭合子路线的实际输入 **1,229,423,667,727,232,768 wei**、输出 **1,235,576,405,779,495,093 wei**，局部毛差 **+6,152,738,052,262,325 wei**。整笔交易含更多动作，局部路径没有独立 gas 分摊。[Morpho 闪借实现](https://github.com/morpho-org/morpho-blue/blob/main/src/Morpho.sol)

整笔执行器向区块费接收者直接转出 **22,397,725,459,947,090 wei**，向顶层发送者转出 **22,407,589,625,322,357 wei**。顶层发送者付 `9,182,960 × 1,832,443,568` = **16,827,255,987,201,280 wei**，其可见净余额变化为 **+5,580,333,638,121,077 wei**。费接收者直接付款已经从执行器现金流扣除一次。执行器可识别 ETH 与 ERC-20 净额均为零。

### 三组 WETH 本金借还

区块 26,476,009 索引 51，出块于 06:22:23 UTC，208 份区块收据复算根，目标 211 条日志、19 个宽筛兑换外形。9 次 canonical PoolManager unlock 中，4 次回滚，2 次提交但缺少初始 WETH take 签名证据，3 次以同一 unlock 内可唯一定位的 `take → sync → 转账 → settle` 证明。通用兑换证明仍有 6 个协议或方向残差，保守最长 1 池。三组本金借还说明执行结构，不分配池交换深度和局部利润。[交易哈希](https://etherscan.io/tx/0x5d7da8c6f67fb7872f450b86913473ae8cb2e761437d00c21e7f31e70ee6d419) · [Uniswap v4 PoolManager](https://github.com/Uniswap/v4-core/blob/main/src/PoolManager.sol)

| WETH take/repay 日志 | 本金（wei） |
| --- | ---: |
| 510/548 | 2,497,966,436,279,213,568 |
| 553/583 | 228,948,587,723,994,112 |
| 609/638 | 153,261,329,591,852,704 |

整笔接收执行器持有的可识别 WETH 增量 **7,400,781,467,448,979 wei**，向同一块头 fee recipient 直接付款 **5,946,274,233,958,486 wei**。顶层发送者实际 gas `9,346,848 × 94,525,308` = **883,513,686,029,184 wei**。若将执行器及费用支付者作为同一经济账户，并按 WETH/ETH 等值结算，则可见余额净变动 **+570,993,547,461,309 wei**。账户控制关系、可能的链外成本及不可识别资产仍未知。

## 复算

未跟踪原始字节、预留账本、失败停止条件、样本计划、历史 SQL、候选清单保存在 `reports/research/eth-forward-and-tail-20261127/`。采集时原始历史 Parquet 位于 Aleph 的 `/tmp/oasis-eth-unseen-20261127/2026-11-23_0.parquet`，源对象地址和字节哈希见 `historical/aws-ledger.jsonl`。离线入口支持提供一组原始证明和可选的显式序列 `--leg event,input_log,output_log`，输出机器可读的复算结果。9 池的显式序列只复核登记的区块、交易位置、原始调用树哈希与转账序列。其他显式序列返回未解。ABI、日志与返回值用于复核该样本，通用深度字段仍以支持的日志和调用形态为依据，入口未为外部池核验运行时代码或币对身份。日志和 callTracer 推导的公开余额可见范围与完整账户资产负债范围不同。

```sh
e=reports/research/eth-forward-and-tail-20261127
PYTHONPATH=. PYTHONDONTWRITEBYTECODE=1 .venv/bin/python "$e/forward.py" replay
PYTHONPATH=. PYTHONDONTWRITEBYTECODE=1 .venv/bin/python "$e/immediate/run.py" replay
PYTHONPATH=. PYTHONDONTWRITEBYTECODE=1 .venv/bin/python "$e/typed/fixed/run.py" replay
PYTHONPATH=. PYTHONDONTWRITEBYTECODE=1 .venv/bin/python "$e/typed/gas-audit/run.py" replay
sh "$e/historical/replay.sh" serial | cmp - "$e/historical/serial-verified.json"
sh "$e/historical/replay.sh" batch | cmp - "$e/historical/batch-verified.json"
```

| 结果 | SHA-256 |
| --- | --- |
| `forward-result.json` | `9a1b1bb9d3a55cd8a4997cd682347c5f860abefb2dd7d540117e44b0765c4ea3` |
| `immediate/result.json` | `c2f8c2b53ddd3db1ce399b325e66932f3f2e4b7a7cd9e79f61d1090832f5565c` |
| `typed/fixed/result.json` | `c98c5eee4d7123c0d81e6b0aa4f3edaf0341094f789ae3585bc34e8cff810d39` |
| `typed/gas-audit/result.json` | `63c1a6c519573e66456250292360fada3cea7d5c4d3048a5782e4cd4f35eb397` |
| `historical/serial-verified.json` | `cdba912ad750698ec8af204deccc9005f44d37c08850e8946df64438768c26d6` |
| `historical/batch-verified.json` | `241a6653d03dcd8d2c5ca45885c739df3de5db1f3355fc33777821474564aed8` |
| `historical/aws-logs.json` | `771751d88bc1b86f39ba3bc4a04c0c2860405c03816c7f3f8da0c72b9be5e2c0` |
