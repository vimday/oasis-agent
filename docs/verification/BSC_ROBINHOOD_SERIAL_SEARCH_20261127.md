# BSC 与 Robinhood 整块收据长串行搜索

北京时间 2026-11-27。扫描两链留存完整区块的 **1,242 份收据**，不按池地址预筛。
新发现的 BSC 目标有 **27 条交换 ABI 形状、20 个逻辑池**，验证了 **六条同账户 USDT
闭环**和一条 **四跳连续资金链**。直接原生币支付与全成本净额未知。Robinhood 的高事件量
交易均为已研究样本。此次没有新证据证明任一链有 20–200 跳同一资金连续闭环。

## 范围与协议漏筛

先冻结已留存的 12 个 BSC 区块与 6 个 Robinhood 区块，纳入每块的成功及失败收据。
这些块原先出自不同采样，部分因高事件交易而入选。统计适用于这些完整收据，无法估计
全链发生率。BSC 的 10 块能用块头与完整收据复算一致的区块哈希和收据根，其中 5 块是
独立来源配对，另 5 块来自同一 BNB Chain 官方 RPC。其余 2 块有 190 份完整收据，能
复算排序和收据根，本次冻结输入没有对应块头。冻结 `screen.json` 中的 `independent_header`
字段表示有可对照的块头，不表示提供者独立。Robinhood 6 块全部重算 Nitro 区块哈希与收据根。
收据根绑定日志、状态及累计 gas，交易哈希顺序用块头接口的有序交易列表交叉核对。

地址池按 emitter 去重，单例 manager 用 `(emitter, PoolId)`。检测先用仓库 V2/V3/V4、
Aero、Pancake V3、Infinity CL 和 Bancor 签名，补充稳定池四种 `TokenExchange` 形式、
Balancer V2、DODO V2、Infinity Bin。双向 ABI 校验每条命中的 topic 数与数据字节。
所有计数都是 ABI 形状数量，单次交换的不同协议事件可能重叠，工厂身份与资金归因需要另证。
[Curve StableSwap ABI](https://github.com/curvefi/stableswap-ng/blob/main/contracts/main/CurveStableSwapMetaNG.vy) ·
[Balancer Vault ABI](https://github.com/balancer/balancer-v2-monorepo/blob/master/pkg/interfaces/contracts/vault/IVault.sol) ·
[DODO V2 ABI](https://github.com/DODOEX/contractV2/blob/main/contracts/DODOPrivatePool/impl/DPPTrader.sol) ·
[Pancake Infinity Bin ABI](https://github.com/pancakeswap/infinity-core/blob/main/src/pool-bin/interfaces/IBinPoolManager.sol)

| 链 | 区块 / 收据 / 失败收据 | ABI 形状数 | 每笔至少 5 / 10 / 20 / 50 / 100 / 200 条 | 排除已研究样本后对应数量 |
| --- | ---: | ---: | ---: | ---: |
| BSC | 12 / 1,167 / 52 | 909 | 14 / 9 / 8 / 6 / 0 / 0 | 6 / 2 / 1 / 0 / 0 / 0 |
| Robinhood | 6 / 75 / 29 | 99 | 4 / 4 / 3 / 0 / 0 / 0 | 0 / 0 / 0 / 0 / 0 / 0 |

表中含事后补充的 Algebra ABI。初次排序由交换形状数降序、池数降序、块与交易索引升序决定，
补充 ABI 后两条链的各阈值计数及 BSC 首选未变。BSC 共新增 **24 条无符号 `TokenExchange`、
3 条 Infinity Bin、4 条 Algebra Integral** 形状，Robinhood 未新增匹配。Algebra 的签名来自
协议自己的事件接口。该步骤对样本内所有收据统一重扫。Robinhood 三笔 20 条以上均与此前
21/22 条样本相同。另一个 19 条样本已有 15 份根绑定收据，先前 trace 超时，最长资金链和
整笔净额仍未知。[Algebra 事件接口](https://github.com/cryptoalgebra/Algebra/blob/master/src/core/contracts/interfaces/pool/IAlgebraPoolEvents.sol#L532) ·
[Robinhood 已研究样本](MULTICHAIN_LONG_ROUTE_SAMPLES_20261123.md) ·
[19 条样本及超时](ROBINHOOD_512_BLOCK_TAIL_20261124.md)

## BSC 新候选的可见资金

[交易 `0xa578…9792`](https://bscscan.com/tx/0xa578d37f34bf73830b3464884ad72c07a4d22e806f55a0dd9156b2b24c5c9792)
位于 **135,377,965 / 14**，区块时间 2026-11-24 **09:51:05**（北京时间）。
区块哈希 `0x4d5a78ccab70016295404213a7995a57229d6c530d8c207a16991ddb9fd46292`，
父块哈希 `0x82cec87a3d869f1cb9899f7dcb4109ebe714bf237ddf1856c19ce29542f0f05a`。
PublicNode 的 **173 份有序收据**重算收据根
`0x7aeedd834593191da2337e4bc90eac64706813226ef1d323cff2e5c5a71f8771`，与留存 Triport
块头一致。初筛命中 25 条交换形状。目标的日志 231、245 发自两个不同地址，其前两整数
及发送、接收地址与 Algebra Integral `Swap` ABI 和实际 ERC-20 转账一致，扩到 **27 条、
20 个逻辑池**。尚未独立核实这两个地址的工厂与代码身份。完整收据有 **66 条日志**，
其中 **33 条 ERC-20 Transfer**。

21 条地址池交换形状各能匹配唯一且等额的入款和出款。两腿仅在同一条 Transfer 同时是
唯一出款和唯一入款时相连，共匹配 **13 条资金接边**。五条 manager 交换形状和一条地址池
交换形状的结算未唯一归属，最长 **4 跳**是已证下界。四跳见 Swap 日志
`237 → 238 → 239 → 240`，中间实际转账日志 `235 → 234 → 233`。首腿从执行器支出
**8,361,908,875,793,517,960 USDT 最小单位**（日志 236），末腿向同一执行器返还
**8,371,377,176,490,757,983**（日志 232）。回调使部分转账与 Swap 日志序相反，证据采用
同一转账的资金方向。资产按地址和整数详列在 `verified.json`。

| 同币同执行器路径（按资金顺序列 Swap 日志） | 毛差，USDT 最小单位 |
| --- | ---: |
| `237 → 238 → 239 → 240` | +9,468,300,697,240,023 |
| `250 → 251 → 253` | +5,109,798,895,007,014 |
| `257 → 260 → 261` | +5,089,657,702,572,807 |
| `274 → 275 → 277` | +4,988,797,304,513,536 |
| `282 → 283 → 284` | +4,932,919,486,294,297 |
| `244 → 245` | +6,282,266,046,695,551 |
| 合计 | **+35,871,740,132,323,228** |

上述合计恰等于发送者与执行器合并的 USDT `Transfer` 差额。WBNB 普通转账增加
**109,359,371,317,456,991 wei**，`Withdrawal` / `Deposit` 分别减少
**125,823,681,082,426,060**、增加 **16,473,397,495,181,892**，WBNB 余额可见增量为
**9,087,730,212,823 wei**。包装与解包还隐含原生币分录净流入
**109,350,283,587,244,168 wei**，不能把这一分录当最终原生币留存。实际交易 gas
**1,721,363 × 50,000,000 = 86,068,150,000,000 BNB wei**。收据未显示的原生币流
（包括直接支付或返还）和 USDT 在该状态的可兑现换算缺少独立证据，完整净额未决。

## 新读取停止记录

BSC 单次冻结 trace 与 Robinhood 新固定位点序列是两个独立 USD 0 范围。BSC 首次匿名
Triport `debug_traceTransaction`，Robinhood 首次匿名官方 `finalized` 块头，都在本机
DNS 失败，curl **6**、HTTP **000**，未收到提供者 HTTP 响应。各原账本保留一次预留，
两范围均未重试。Robinhood 新窗口的全块收据覆盖为 **0**，高度随 first finalized anchor
冻结，原计划的 8 个相对高度未绑定。BSC 新 trace 覆盖 **0 / 1**，原有收据工作仍可复验。
[Triport BSC 匿名接入](https://triport.io/bsc-rpc) ·
[Robinhood 官方接入](https://docs.robinhood.com/chain/connecting/)

另在独立 USD 0 范围通过 Aleph 做两次 AWS 匿名 S3 目录读取，未读链上 RPC 或 Parquet
数据。`v1.1/bnb/` 的完整一级目录只列 `blocks/` 与 `transactions/`。后者确认按
`date=YYYY-MM-DD` 分区，本次按 15 个前缀上限截断，从 2020-08-29 起返回，未证明近期
覆盖或交易记录内的日志列。AWS 所列 Lighter on Robinhood 为 Lighter 应用数据，不能用作
Robinhood 全链交换事件分母。请求、两份响应和 SHA-256 在 `bnb-open-data/`。
[AWS 开放数据目录](https://registry.opendata.aws/aws-public-blockchain/)

## 离线复算

忽略目录 `reports/research/bsc-robinhood-serial-search-20261127/` 内含冻结选择、输入文件 SHA-256、
收据排序、逐笔和逐腿金额、原始请求、失败元数据和预算账本。原区块与收据完整字节保留在
各自原采集目录，`offline-scope.json` 确定输入路径。复算无网络访问。

```sh
set -o pipefail
evidence=reports/research/bsc-robinhood-serial-search-20261127
.venv/bin/python "$evidence/replay.py" | cmp - "$evidence/screen.json"
.venv/bin/python "$evidence/replay_findings.py" | cmp - "$evidence/verified.json"
```

| 文件 | SHA-256 |
| --- | --- |
| `offline-scope.json` | `5a2cf4c78ff7587d897623714b86a6cf03e7acbc5e9d48e6058f2342b5027edb` |
| `screen.json` | `5b6b91f26002dea81b562044003d3bf033193fe821adb0d9a1a4c1b2b670e10a` |
| `verified.json` | `df2f45222d4f2215017dafa39e17f5c3540dd18bc872957fbd3c85341c1083f3` |
| `bsc/STOP.json` | `fa780d68b1abb9164734416e913e83662d4627420a08e019902467047cc89628` |
| `robinhood/STOP.json` | `178fabd61e77d6dd263847841bd045582eb3931425aa1bb8fecd333fc88cb214` |
| `bnb-open-data/list-1.response.xml` | `775411711ee337ef393ef475080c8cc6e951433ef2866b797ab60b9e76b5faf2` |
| `bnb-open-data/list-2.response.xml` | `6059d5e53435d6ecb6caba695a56d85b529bc8f5125521426a8c7f39093c9a63` |
