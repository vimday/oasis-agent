# 整批双源影子窗口与 Base 取数往返验证

2026-11-27 北京时间，基线 `3f844a1`。研究范围为匿名只读请求、合成测试和保留证据复算。交易资产均为假设投入，实盘资格由独立评审决定。 [上轮工程核验](MULTIHOP_PRELIVE_ENGINEERING_20261127.md) · [四链研究总结](../strategies/MULTICHAIN_MULTIHOP_ARBITRAGE_SUMMARY.md)

## Ethereum 整批双源验证设计

从已有 45 池注册表冻结范围，来源为 PublicNode 与 dRPC，两者用 canonical H 哈希分别读取相同的全部储备。有限本金网格先覆盖登记图，取排名前 15 条的固定金额及其反向路线形成组合候选。独立金额报价为负的路线仍可作为反向候选进入组合搜索。按最多三段、束宽 64、6000 次逐腿预算筛选。首个自然候选按完整费用情景过门槛后选排名第一的组合。若该头没有自然候选，则运行预先固定的两段负控。负控连续两次使用同一条 WETH→USDT→LINK→WETH 路线，每次为 `10^14` WETH wei，用来检验后一段是否使用被前一段更新的共享池。 [候选及原注册表](ETH_AFTERNOON_FORWARD_20261127.md) · [正常入口](../../scripts/run_eth_v2_batch_shadow.py)

探针在 EVM 中一次包装原生本金，每段使用当前储备，并校验工厂配对、代币身份、实际到账与最终逐币余额。构造函数拒绝部署。固定地址的研究运行码只经 RPC 状态覆盖用于模拟。两源提交相同调用字段和池状态绑定，逐字节比较整笔输出。H 另与整数模型逐腿比较。结算锚为 WETH，18 位，非锚资产剩余必须为零。首个双源回报完成后立即查询最新头 K，并另核对 H 的直接子块 C。K 与 H 或 C 相同则复用同状态整笔证据，保留新的头部观测时钟。 [探针代码](../../contracts/src/research/EthV2OrderedBatchProbe.sol) · [归因器](../../src/oasis_agent/fastpath/batch_shadow.py)

V2 池费已反映在每腿整数输出中。原生 ETH gas 与 WETH 以 1:1 计入同一研究锚资产，资金在模拟结束时保持 WETH。以固定 gas 上限和各块 EIP-1559 有效费率给出整笔费用上界。压力净额再计一笔同上限失败准备与 `10^12` wei 机会成本准备。研究假设无外部融资、无显式区块付款、无额外 L1 数据或 operator 费。仅当 dRPC 标记匿名免费时，在 H/K/C 已采集后读取各不同状态的整笔 trace，比对回传与包络，另报基于单个来源 trace 的模拟 gas 和净额。观察时钟来自 HTTP。费用情景和还未观测的实际入块概率分别保留。账本上限为 PublicNode 24 与 dRPC 9，各源 250 ms 最小间隔，75 秒墙钟，首次拒绝即停止。 [EIP-1559](https://eips.ethereum.org/EIPS/eip-1559) · [Geth 整笔模拟](https://geth.ethereum.org/docs/interacting-with-geth/rpc/ns-debug#debugtracecall)

### Ethereum 冻结窗口结果

从块 26,506,728 的直接子块 H 开始。两源在 H 的 45 池储备返回一致，收头后 679.486 ms 交付，1,353.082 ms 完成报价和组合排名。7,976 条路径的 79,760 个整数报价点中，22 个点费前毛差为正，零条路径的全金额毛差上界覆盖 21,000 gas 基础费。按模型毛差排名第一的 WETH→SHIB→WETH 固定网格点毛差为 5,011,540,532,566 wei，该路线全金额上界为 5,781,802,961,559 wei，当块基本 gas 下界为 12,728,774,961,000 wei。30 项组合菜单和 108 个逐腿预算单位评估结束，持有排名第一，净正短名单为空。本窗口因此执行了预注册两段负控，实际自然候选数及正例分母均为零。

固定负控在 H、同哈希 K 和直接子块 C 全部取得双源一致的整笔输出。H 逐腿整数与模型一致。两段均投入 `10^14` wei，回收分别为 97,981,136,903,974 和 97,835,631,430,717 WETH wei。第二段使用前一段改变后的储备。WETH、USDT 和 LINK 账目全部核对，后两者余额变化为零，合计 WETH 毛差 −4,183,231,665,309 wei。H 和 C 的 dRPC 根 trace 均为 466,986 gas，探针函数体 gas 样本为 531,653，二者使用不同计量口径。K 复用 H 的费用和现金证据。

| 时点 | 块高 | 有效费率，wei/gas | 整笔模拟 gas 费，wei | 扣整笔 gas 净额，wei | 再扣失败及机会成本准备后的净额，wei |
| --- | ---: | ---: | ---: | ---: | ---: |
| H | 26,506,729 | 656,132,141 | 306,404,523,997,026 | −310,587,755,662,335 | −1,886,304,894,062,335 |
| K | 26,506,729 | 656,132,141 | 306,404,523,997,026 | −310,587,755,662,335 | −1,886,304,894,062,335 |
| C | 26,506,730 | 656,841,828 | 306,735,937,890,408 | −310,919,169,555,717 | −1,888,339,556,755,717 |

H 首回报与双源齐备分别在收头后 2,365.785 和 2,515.432 ms，K 在 2,951.739 ms 返回，HTTP 首次看到 C 在 10,227.838 ms，C 双源整笔在 11,460.717 ms 齐备。H 的负控回报早于对 C 的本地观测。自然正例仍为零，直接子块的正净额存续率留空。

子块查询是在 H 双源回报之后发起的。2,515.432 ms 和 10,227.838 ms 记录本机先后收到的响应。K 仍返回 H，范围限于该次 PublicNode `latest` 响应。独立连续来源首次看到 C 的时钟缺失。新版正常入口保留三个状态的整笔现金和费用，及时性及自然候选及时存续率保持未知。旧 v1 原件只能在提供预注册哈希时复算已完成的负控，旧时钟字段仍按当时的 HTTP 观测先后复算。

实际使用 PublicNode 15 次与 dRPC 6 次，合计 21 次匿名预留，费用 USD 0，退出 0。原始记录在 `reports/research/eth-v2-ordered-batch-hkc-20261127/`。预注册 SHA-256 为 `9ccc62e45e543190a88020d25af698609c43a8d77399f8b35e18117104dc0be1`，结果为 `291127c80cf5f1b0f75651d2e01bfe5f024aa4efb04062affc7a945cb88046ee`，STOP 为 `53ddc72ddc81e1caba9a5bab91679e8b2b07c9a01d102e8824bdadd45d58baa8`。双源固定包络为 `0469767ace215cd208453f2ab22459f21a70d65af159405945940f2ec559d7dd`。 [PublicNode 公共入口](https://ethereum.publicnode.com/) · [dRPC 公共入口](https://drpc.org/docs/ethereum-api)

```sh
eth=reports/research/eth-v2-ordered-batch-hkc-20261127
.venv/bin/python -m scripts.run_eth_v2_batch_shadow replay "$eth" \
  --scope-sha256 9ccc62e45e543190a88020d25af698609c43a8d77399f8b35e18117104dc0be1
eth=reports/research/multichain-inventory-20261127/fresh-hkc
.venv/bin/python -m scripts.run_eth_v2_batch_shadow replay "$eth" \
  --scope-sha256 35de393e6d2d05ecb2bb17e59e1598a4625230a8690c944628ac371a0bbbceaf
```

## Base 同头官方 HTTP 批处理

正常影子入口新增 `--base-newheads-batch-v4`，将同哈希的 PoolManager 运行码和 V4 状态查询拼在一个 HTTP 请求内。两项仍分别预留并保留原 250 ms 间隔。两源块头和 WebSocket 间的绑定、回包 ID 映射、运行码及完整状态校验均通过后才发布。首次结果错误停止原范围。一次独立匿名能力探针先确认官方端点支持两项同包请求，使用两次逻辑预留。 [入口](../../scripts/run_multicall_100tick_campaign.py) · [取数与回放](../../src/oasis_agent/fastpath/base_mixed_snapshot.py)

同头配对两臂分别使用既有重叠取数与新 HTTP 批处理，执行顺序交替。共见证 26 个原始头，在两个头上获得完整两臂。状态和 4/58/66 池扫描相同，所有池组均为零模型正例。比较的每头官方 HTTP 从 3 降到 2。下表以毫秒计，官方认头至两项 V4 可用含预留等待。第一对 1.296 ms 是收到两条消息的时间差，属于接收突发。

| 匹配头 | 重叠／批处理 V4 传输墙钟 | 官方认头后／V4 完成，重叠／批处理 | 各臂完整状态，重叠／批处理 | 观察到的直接子头间隔 |
| ---: | ---: | ---: | ---: | ---: |
| 62,969,524 | 641.240／386.779 | 647.569／647.612 | 1,240.357／1,025.335 | 1.296 |
| 62,969,537 | 604.803／352.632 | 608.667／613.461 | 949.274／1,229.340 | 214.529 |

请求合并缩短了 V4 的传输跨度。预留等待抵消了这一变化，两臂直接子头及时率均为 0/2。配对使用官方 12 次逻辑预留、实际 10 次 HTTP 传输，另有 PublicNode HTTP 8 次与 WebSocket 3 次预留。加能力探针共 25 次预留，USD 0。配对范围退出 0，报告逐字节复算相同。预注册 SHA-256 `5aa514b71d51bc65c37b0509f8a0800649428018671f229d1cbeb63fb1a66cc8`，报告 `df6803070a46cf5a242dbba0bdd098908bbe5d0e03ff81cf48a95553572ff664`，STOP `da624a6a93c120c830061c8a7ad05935ee69d0c671bc990b637c878c4c80b4bb`。原件位于 `reports/research/base-v4-batch-paired-20261127/`。 [Base 公开端点](https://docs.base.org/get-started/connect-to-base)

```sh
base=reports/research/base-v4-batch-paired-20261127
PYTHONPATH=.:src:docs/verification/fixtures .venv/bin/python "$base/pair.py" replay |
  cmp - "$base/report.json"
```

## 回归与晋级判断

| 校验 | 实际结果 |
| --- | --- |
| `make check` | 退出 0，6,805 项 Python 测试、138 个子测试、9 个前端模块及 132 项合约测试通过，含新探针 5 项合约测试 |
| 单独探针 ABI 回归 | 退出 0，整批入口与后来加入的 14 项探针编解码测试合计 15 项通过 |
| `make rust-check` | 退出 0，30 项 Rust 测试、格式与 Clippy 通过 |
| `make security-check` | 最终退出 0。首次因公开池名单 SHA-256 命中熵扫描退出 2，核对公有留存原件后登记单一公开指纹，重跑通过 |
| `make contract-security-check` | 隔离 Forge 输出目录重跑退出 0，Slither 检查 102 个合约，8 条发现均低于门槛。探针最终构建与冻结 runtime 字节一致 |
| 新窗口复算与文档 | Ethereum 结果逐字节复算一致，Base 配对 `cmp` 退出 0，本地文档引用及 `git diff --check` 通过 |

代码、原始请求、原账本及检查输出保存在工作区与上述两个本机留存目录。检查日志见 Ethereum 目录下 `verification/`，冻结探针 runtime SHA-256 为 `92b5b5432e8bef781879f4ecd7881dc0a77983dc99c5214ee9da1b701121546a`。本轮合计匿名预留 46 次，RPC 支出 USD 0。自然费后正例与直接子块存续证据均缺，后续实盘评估等待这些证据。 [当前实现](../CURRENT_IMPLEMENTATION.md) · [多跳 TODO](../strategies/MULTIHOP_ARBITRAGE_TODO.md)
