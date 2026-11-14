# Metric 构块者 feed 的槽位与时间解释

2026-11-14，基线 `2691ce8`。作者/内部自审 Codex，无独立 reviewer sign-off。
本记录承接 [Titan 首次观察](TITAN_STATE_20261114.md)，只解释原来保留的三份快照。
研究材料保存在本机忽略目录 `reports/research/metric-overlay-20261114/`，没有新 RPC、
重试原报价/诊断、修改原账本、应用 override 或资本操作。

## 研究问题和结论

假设：原 Titan 的 17 个 storage diff 是已观察 PythOracle 的 feed 数据，而非改变访问
控制的配置槽。反证包括编译布局不同、未知槽、替换代码/完整状态、其他账户的 storage
修改、单位错误、缺目标 feed、时间退序或同时间不同值。此项为历史诊断，不是 untouched
经济窗口；不预设套利收益或因解码成功而改变策略阶段。

17 个槽全部映射到该源码的 `oracleData`；官方 Pyth 元数据标明 feed 2 为 ETH/USD，
feed 7 为 USDC/USD。两者均不是直接的可成交报价。目标池 token0 为 WETH、token1 为
USDC 的历史配置来自[前次身份研究](PUBLIC_RPC_AND_METRIC_IDENTITY_20261114.md)；WETH
使用 ETH/USD feed 是配置关系，不能把所有 WETH、ETH、USDC 的风险或兑换性视为相同。

| 原快照 | ETH mid E8 | ETH spread（0.01 bps） | USDC mid E8 | USDC spread（0.01 bps） | 两个 feed 的 reference 毫秒 |
| --- | --- | --- | --- | --- | --- |
| 0 | 190596447284 | 587 | 99976927 | 63 | 1794626707200 |
| 1 | 190599971404 | 599 | 99976257 | 56 | 1794626707400 |
| 2 | 190601540798 | 628 | 99976325 | 104 | 1794626707800 |

原声明的目标为 Ethereum 26410517、slot 15650224、timestamp 1794626711。源码将
reference 毫秒向下取整为秒，因此这些记录在目标时刻均为 4 秒旧；相对于原块观察到的
15 秒阈值通过年龄检查。没有重读目标块配置，也没有验证签名更新 payload；这是声明
内部的一致性诊断，不能称为独立验证的 feed 鲜度，更不能解释旧块 26410155 的回滚。
三份快照仍不能证明连续 feed 更新或完整订单流。

## 源码、布局和单位

原始来源为 [Etherscan 的 oracle 源码](https://etherscan.io/address/0x30a88bf894d1b806125a23ebf23d715496fd7102#code)。
先前保留的 standard input 文件 SHA-256 为
`a1fd6c7afda75402ec2f15063bb41df683ae1a8d7740dbb98c808c5dec09aba8`。
源码为 PythOracle 1.1.0 / pyth-lazer，编译器版本 0.8.33+commit.64118f21。

从 [Solidity 官方版本清单](https://binaries.soliditylang.org/macosx-amd64/list.json)选择
该版本，并按清单核对下载 binary 的 SHA-256：
`8324280591ce398d7e2722846bc10ecf1779b13a328ef97b687c92cd9c70801a`。
本机离线重编译保留的完整依赖、原 remappings / optimizer 200 / viaIR / Prague 参数。
编译成功、无 error；输出 storageLayout：

| 字段 | mapping base / 字节偏移 | 位宽 | 解释 |
| --- | --- | --- | --- |
| oracleData | base slot 1 | bytes32 key | `keccak256(abi.encode(feedId, uint256(1)))` |
| price | 0 | uint64 | 统一 E8，不能再次套用目录 exponent |
| spread0 | 8 | uint16 | 0.01 bps，provider 对 >=65534 拒绝 |
| spread1 | 10 | uint16 | LazerConsumer 写入 65535，正常未使用字段 |
| timestampMs | 12 | uint56 | 毫秒；`price()` 返回整除 1000 后的秒 |

该 struct 共 152 bit，单槽；解释器对高 104 bit 非零的声明保守拒绝。源码的
`priceGuard` 在 base slot 2，blacklist 在 10，registeredPool 在 11；不能把它们当 feed。

实际 runtime 和重编译 runtime 都为 9,829 字节。排除编译器列出的五个 immutable
区间和尾部 53 字节 CBOR 后，执行代码逐字节一致；完整 runtime **不一致**，剩余差异
为 IPFS metadata hash 的 32 字节。保留初始不匹配记录，再以 v2 记录说明匹配范围；
不称为完整可复现构建或目标块代码证明。历史 observed runtime Keccak：
`0xd7169b763899b0e9a19b2b26ed2b566375440905f62964bd08817f9f1cb37ca2`。
编译器 immutable 位置对应的历史 PythLazer 地址为
`0xacea761c27a909d4d3895128ebe6370fde2df481`，与覆盖中的一个账户相同；其余额/nonce
变化的意义及目标时点身份没有因此得到证明。

## 官方 feed 元数据与覆盖范围

[Pyth feed ID 文档](https://docs.pyth.network/pro/price-feed-ids)指向公开
[symbols 元数据接口](https://pyth.dourolabs.app/v1/symbols)。本轮单次匿名 GET HTTP 200，
保留 5,417,247 字节、3,991 条目录记录，接收于 2026-11-14 04:59:28.225 UTC；这是后于快照的目录，不是当时已知的
元数据或历史 feed 名称证明。所有字段原样保留并整体哈希绑定；分析器只解释明确支持字段。

匹配的 17 个编号为 1、2、7、8、19、25、29、56、85、105、163、172、174、201、397、631、2921，
在本次目录中分别是 BTC、ETH、USDC、USDT、LINK、UNI、AAVE、QNT、ENA、ZRO、PAXG、XAUT、
MORPHO、ONDO、CBBTC、WETH、LIT 对 USD 的 feed。全部目录 exponent 为 -8；源码仍以
自己的归一化 E8 语义为准。只有计划的 ETH、USDC 两腿带有 15 秒年龄阈值，其余不是已
完成 provider 配置核验的交易对。

五个覆盖账户中只有 oracle 有 stateDiff；其他四个只有 balance/nonce。报告逐账户
保留字段名和内容哈希，不生成可调用 override。未识别的账户角色继续未知；缺少的 diff
只表示声明未携带该值，不能推断 canonical storage 为零或不存在 feed。

## 正常入口与失败处理

`arbitrage/metric_builder.py` 接入原 `scripts.research_titan_state`，可选参数必须成对提供：
`--feed-plan` 和 `--feed-catalog`。计划固定 schema、布局、Ethereum、oracle、pool、目录
SHA-256 及两条 feed/token/symbol/年龄绑定。目录原始字节 hash 或必需 symbol 不符即拒绝。

```sh
research=reports/research/metric-overlay-20261114
titan=reports/research/titan-state-20261114
.venv/bin/python -m scripts.research_titan_state \
  --plan "$titan/analysis-plan.json" --capture "$titan/campaign/snapshots" \
  --feed-plan "$research/feed-plan.json" --feed-catalog "$research/pyth-symbols.response" \
  --output "$research/another-offline-report.json"
```

只重放已保留的原 HTTP 字节，不调用网络；输出必须新建。未知槽、code/full-state 覆盖、
非 oracle 存储修改、缺目标 feed 或 padding 错误均不能输出可用的部分价格。时间与价格
异常保留诊断并标记拒绝；相邻快照即使 pair sequence 增加，也检查 feed reference 退序
和同时间不同值。零/未来时间、零价和 spread0 饱和均可见。没有签名、提交、重试或授权路径。

首次本地解析因目录中三个合法 symbol 含空格、星号、加号而拒绝。已保留该失败，改为
有长度上限的可打印 ASCII 名称，并继续要求完整目录 hash 和目标 symbol 精确一致。
格式检查后的类型检查也曾指出变量名复用造成类型冲突，已修正；没有放宽时间或价格条件。

## 验证和下一依赖

44 项新增合成测试，连同 Titan、oracle、跨 venue 回归共 164 项通过。覆盖原始 capture
到正常 CLI、目录篡改/重复键、布局、整数边界、年龄向下取整、缺失/未知覆盖、feed 冲突、
输出不覆盖、确定性回放。真实回放输出 3×17 条 feed 声明；执行准入仍为 false，净收益为 null。
所有日志及原始材料均留在忽略的研究目录。

| 材料 | SHA-256 |
| --- | --- |
| 原 Titan capture.json | `8e6de6973c54107f1d29eac2bdec89f8e41eedd35f2b8e2c9921549d1dcbfcaf` |
| pyth-symbols.response | `80d95f05a48848db111bf0e9dadc6ab73b9519c4284810fe053d7b393c62b7d3` |
| feed-plan.json | `23061a5bc649cf6dc0421bb1115e021039439ce70719fd50ef46ae2660420f38` |
| feed-analysis-final.json | `f74b805ebbc9b52925b6ec272108875dbfca3c2dc1b893fc1785d33953317413` |
| oracle-build-verification-v2.json | `055ba36a3ff65df978e20f147a94cb3d3dd3bb415dfbd44d864ffad0cff4eb98` |

| 检查 | 结果 / 本机日志 |
| --- | --- |
| 聚焦回归 | 164 通过，退出 0；`focused-final.log` |
| make check | 退出 0；5,997 Python、138 子测试、9 前端模块、70 Foundry 通过；`make-check.log` |
| make security-check | 首轮退出 2，仅秘密扫描拒绝新增的公开源码 hash；审阅固定指纹后最终退出 0；`security-check.log` / `security-check-final.log` |
| make contract-security-check | 退出 0；80 合约、102 detectors、0 findings；`contract-security-check.log` |
| 真实离线回放 | 正常入口退出 0；重复报告逐字节一致，未改变原 3/3 已耗尽采集范围 |
| 文档/接口 | 4 份文档、42 个本地链接、5 个 shell 示例语法及 CLI help 通过；`docs-check.log` |
| 提交前审阅 | diff --check 通过；仅审阅过的源码、测试、非秘密指纹及文档，原始证据和编译器不入 Git |

秘密扫描只增补已重算的源码 SHA-256 非秘密指纹，没有扩大排除路径。首次失败与修正
分别保留。环境为 Python 3.12.4、Node 23.11.0、Foundry 1.8.4 Homebrew；这是本机门禁，
不宣称运行了 CI 所固定的 Node 22.14.0 / Foundry 1.8.1。0.8.33 仅用于忽略目录的研究
编译；仓库合约版本和依赖未更改。

精确模拟还必须固定 canonical parent **26410516 的 hash**，证明目标区块 runtime/config、
独立来源、签名更新与逐账户变化、区块内排序、source shaping、pool 注册和 blacklist、
真实 origin/caller/recipient/extension、付款/回调/偿还及完整两腿余额变化。源码 `price()`
对非零 origin 检查 `pool.inSwap()==msg.sender`、池未列黑名单且 feed 已注册；零 origin
模拟不验证这些条件。不能用 metadata、零地址报价或局部余额覆盖代替完整交易模拟。

再次检查先前固定版本的 [Metric quoter](https://github.com/Metricxyz/periphery-contracts/blob/5ceaec32a4a8ceb5fe1411ec1bc5d5f9aef32647/contracts/lens/MetricOmmSwapQuoter.sol)：
`_resolveLivePricesX64` 直接调用 provider.getQuote，再将返回价格交给 pool 的
simulateSwapAndRevert。因而 quote 的 sender 参数并不等于执行了真实付款、provider
访问路径和完整 swap；此处是固定源码的语义，不是另一次目标 runtime 身份验证。

下一步完成上述 canonical base 与受约束模拟证据；原停止/耗尽范围不重启。多链档案、
Aave 完整债务、Elysium 精确 prestate、真实 shadow 和独立净经济窗口继续按
[持续清单](../strategies/NEXT_STAGE_EXECUTION.md)推进。
