# BSC 以成交路径扩池后的同头盲测 · 2026-11-27 UTC

两笔历史交易的 31 次兑换涉及 23 个不同逻辑池。将这些池加入旧 115 池后，新窗口的八个评分头中，增量池覆盖 **50 次实际兑换、43 笔交易**，旧组覆盖 **2 次实际兑换、2 笔交易**。普通气价情景下，两组均没有通过 21,000 gas 门槛的报价点。[逐池、逐头和逐笔复算结果](BSC_HISTORICAL_POOL_EXPANSION_20261127.json)

## 先取证，再冻结池及路径

旧样本 21/10 次兑换共六组 WBNB 闭环，腿数分别为 5/6/5/5 与 5/5。复用已核收据和调用树的逐腿整数到账，按单池地址或 PoolId 去重。于 **135,998,971** 固定哈希 aa7ac991…01f9 复核 23 池和 16 种资产。机器结果保留每腿事件位置、方向、整数到账量、运行码 SHA-256/Keccak、币地址和 decimals。池身份及流动性在新状态读取，历史成交金额来自此前取证的原交易。[原始两笔成交样本](BSC_FROZEN_WINDOW_20261127.md)

| 新增池 | 取证范围 |
| --- | --- |
| 11 Pancake V2 | 币对、双边储备、官方 factory 反查；全部与旧登记 Pancake V2 基线 runtime 完全相同；池费 25 bps |
| 8 Pancake V3 | 官方 factory 反查到同地址、币对和费档；fee 为 100、500 或 2500 pips；保留各池运行码、间距、现价和有效流动性 |
| 2 Pancake Infinity CL | 原 calldata 重算 PoolId，核对 manager 反查、币种及零 hook，取 slot0 和有效流动性 |
| 2 Uniswap v4 | 原 calldata 重算 PoolId，核对官方 manager 与 StateView，hook 为零，取 slot0 和有效流动性 |

四个 singleton 池用当时 slot0 的方向协议费和 LP 费按官方公式复合。历史 swap 事件实际费比率分别为 Infinity 22、5 pips 与 Uniswap v4 2、31 pips。池身份不能证明长尾 ERC-20 的转账行为，已知收费的币单独标记，新币诊断仍需整笔到账验证。[Pancake V2 费率](https://docs.pancakeswap.finance/trade/pancakeswap-exchange/trade) · [Pancake V3 部署](https://developer.pancakeswap.finance/contracts/v3/addresses) · [Infinity CL ABI](https://github.com/pancakeswap/infinity-core/blob/main/src/pool-cl/interfaces/ICLPoolManager.sol) · [Infinity 方向费用](https://github.com/pancakeswap/infinity-core/blob/main/src/libraries/ProtocolFeeLibrary.sol) · [Uniswap v4 部署](https://developers.uniswap.org/docs/protocols/v4/deployments) · [Uniswap v4 方向费用](https://github.com/Uniswap/v4-core/blob/main/src/libraries/ProtocolFeeLibrary.sol)

## 同头前瞻

07:35:30.097 UTC 冻结两组、监测法和分母。首头于 07:35:30.849 收到，末次收据于 07:35:41.285 收到。评分头为 **135,999,569–135,999,591** 范围内八个去重头，有高度间隔。每个样本一次按哈希 Multicall 读两组状态，回读链号、块高并复核该头全部收据根。两组采用同一 2–4 跳 WBNB 规则与六档本金，对正边际路径逐腿整数复价。V2 使用常数乘积曲线，CL 只确认未达到下一可能初始化间隔的报价。遇到边界、深度缺失或舍入为零时保留残差。

| 指标 | 旧 115 池 | 仅新增路线 | 扩展合计 |
| --- | ---: | ---: | ---: |
| 每头路线数 | 8,536 | 1,166 | 9,702 |
| 路线 × 状态 | 68,288 | 9,328 | 77,616 |
| 正边际路线 × 状态 | 14,887 | 1,804 | 16,691 |
| 可定价本金点 | 81,786 | 9,744 | 91,530 |
| 深度或舍入残差点 | 7,536 | 1,080 | 8,616 |
| 正毛额点 | 8 | 8 | 16 |
| 最大毛差（WBNB wei） | 188,103,070,660 | 224,612,054,228 | 224,612,054,228 |
| 过普通气价 × 21k / 60k | 0 / 0 | 0 / 0 | 0 / 0 |

若只新增 11 个 Pancake V2 池，范围中没有新增 2–4 跳 WBNB 环。1,166 条增量路线均需要 CL 报价。接头至状态可用 129.39–205.82 ms，旧组排序额外 55.57–63.78 ms，增量路径额外 11.15–11.59 ms。两组共享同一状态 RPC，这些耗时适用于联合观测。

六条历史闭环按原本金分别复价，得到 48 个路径状态。两条在每头都给出完整负毛额，范围分别为 **−911,264,048,326 至 −734,538,213,321** 与 **−1,424,661,181,602 至 −1,387,707,217,446** wei。三条留下 CL 深度缺口，一条因已知转账扣减挂起。此处为每条路径独立复价，未建立六条共享状态顺序执行的结果。

普通费率用观测 **50,000,000 wei/gas**，21,000 gas 费用下界 **1,050,000,000,000 wei**。零气价场景中，新组最大毛差仅提供 **224,612,054,228 wei** 的对外支付额度上限。纳入条件、额外付费与竞争成本未见证，零气价未纳入可执行机会分母。

## 完整收据与新 3% 留出样本

八头共 **850** 笔交易复算完整收据根。十种兑换日志外形产生五笔至少五条形状的交易，至少 20/50/100 条的交易均为零。零气价、正气价两组各选择触及新增池最多的一笔，再取得匿名调用树。

- **17 次兑换、15 个逻辑池，6 次触及新增池。** 四组 WBNB 闭环为 **6、4、3、4 腿**，实际 gas 1,572,140，收据有效气价为零。总毛差 **5,356,491,881,422 wei**，和执行器出账 **2,273,848,750,000 wei** 原生 BNB 与 **3,082,643,131,422 wei** WBNB 相抵。两个收款地址均不同于头部费用接收者，可见执行角色净余额变动为零，收款人控制关系未知。[交易 0x040c…094f](https://bscscan.com/tx/0x040cd60bf6604c7248b6276a5be83a11bdc420cda16b77bb077abdd5da6d094f)
- **18 次兑换、10 个逻辑池，2 次触及新增池。** 已证执行费用 **1,393,691 × 50,000,000 = 69,684,550,000,000 wei**。六条资金链由另一币转入 USDT，可见 USDT 增加 **1,892,413,938,200,670,976,182** 原单位，另一币减少 **1,000,000,000,000** 原单位。缺少该资产估值与同锚闭合证据，经济净额仍未知。[交易 0xf4b7…54fa](https://bscscan.com/tx/0xf4b7a19e4f40575e2b227bff1615f341c19cefaec778a15262091893c5d254fa)

第一笔第 4 条路径按资金顺序为日志 794、783、788、791。本金 **147,044,717,500,000** WBNB wei，回款 **147,338,812,591,318**，毛差 **294,095,091,318**。等额规则只连接 2+2 两段。断点处 Pancake V3 调用的接收者等于执行器，方向 token1→token0，有符号返回 **(−57,300,053,414,015,550,613, 76,456,556,391,508,298,778)**，与池支出日志 781 和输入一致。代币 0x19f46fddbac1ab00d203cc80515a44e320260419 一次成功 transfer 内的日志 784/785 记录向代币合约留存 **1,719,001,602,420,466,518**、向 V2 池实际支付 **55,581,051,811,595,084,095**。执行器的三次余额为 0、完整收入、0，V2 交换内部的代币余额和 Sync 日志 787 较此前储备增加同样的实际到账额。

独立固定头核对这两个池的运行码、币对和 factory 反查。源为 Pancake V3、费 10,000 pips，目标为 Pancake V2、运行码与已登记基线一致。通用扣减规程要求两侧的 V2 储备证明，该案源池为 V3，通用入口仍不给这条边。本次用已登记成交的调用参数、返回值、独立资产身份和转账复核 **3%**。其他方向、金额和时间的费率仍未解。

## 整笔新增池负对照

独立冻结 WBNB→USDT→WBNB。首腿为新增 Pancake V3 100 pips 池，第二腿为旧 Pancake V2。研究探针以临时代码覆盖验证工厂、V3 回调来源及付款与到账的实际余额。固定本金 **100,000,000,000,000 wei**、H 上查询的 nonce 和费用参数，包络 SHA-256 为 8772185014073c035b20b2cb07a6c1934a5a6e8362ff0d987be92ee08209ddc4。前瞻最高四跳仍涉及独立 v4 结算和长尾资产，整笔证明尚未覆盖。

| 阶段 | 区块 | WBNB 回款（wei） | 毛差（wei） | 普通费用净额区间（wei） |
| --- | ---: | ---: | ---: | ---: |
| 发现 H | 136,000,666 | 99,511,560,944,117 | −488,439,055,883 | [−75,488,439,055,883, −10,631,239,055,883] |
| 首次整笔响应后的首次最新头 | 136,000,669 | 99,513,256,415,269 | −486,743,584,731 | [−75,486,743,584,731, −10,629,543,584,731] |
| 直系 H+1 | 136,000,667 | 99,512,510,411,634 | −487,489,588,366 | [−75,487,489,588,366, −10,210,939,588,366] |

首次整笔返回于 **07:43:45.740356 UTC**。下一次网络读取 2.677 ms 后发出，105.361 ms 后返回，实际已见 H+3。另按高度查 H+1 并验证 parent hash。H 与 H+1 整笔输出都与整数模型一致。区间使用固定 1,500,000 gas 上限和函数体加 intrinsic 后允许五分之一退款的下界。三次均为负毛对照，实际 gas 扣费与纳入仍无观测值。[只读探针](fixtures/BscExpansionShadow.sol) · [BNB 公共 RPC](https://docs.bnbchain.org/bnb-smart-chain/developers/json_rpc/json-rpc-endpoint/) · [BSC 状态转换](https://github.com/bnb-chain/bsc/blob/master/core/state_transition.go)

## 留档与验证

四个匿名范围分别使用 **43/45**（身份）、**26/36**（新盲窗）、**2/2**（留出成交调用树）、**17/24**（整笔探针及额外身份），共 **88 HTTP、USD 0**。原始传输、时钟、原账本、冻结计划均在本地忽略目录 reports/research/bsc-pool-expansion-20261127/。初次离线盲窗复算因整数直方图键与 JSON 字符串键排序差异报告不一致。归一键后复算等值，原始冻结代码与记录均留存。Solidity 探针用 solc 0.8.26、optimizer 200、Paris 重编，运行码与留存逐字节一致。

离线入口登记并先核对所有九份本轮和传递依赖脚本的固定 SHA-256，再从已校验原始源码字节求值。缺失、篡改、符号链接转移和未登记的动态脚本均拒绝，缓存字节码不会参与。这九份包括本轮 identity/forward/whole 的 run.py 与 landed/replay.py，前期 bsc-wide-forward 的 forward.py、bsc-recent-arbitrage 的 collect.py、bsc-eight-address-holdout 的 capture.py/replay.py，以及收据树复算 eth-cohort-cost-outlier 的 verify.py。精确相对路径及每份哈希由机器结果的 verified_evidence_scripts_sha256 记录。[完整性回归](../../tests/unit/test_bsc_expansion_fixture_integrity.py)

```sh
set -o pipefail
PYTHONPATH=. .venv/bin/python docs/verification/fixtures/replay_bsc_pool_expansion.py reports/research |
  cmp - docs/verification/BSC_HISTORICAL_POOL_EXPANSION_20261127.json
```
