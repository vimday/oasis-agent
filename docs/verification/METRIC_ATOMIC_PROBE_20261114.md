# Metric 父块观察与整笔回滚探针

2026-11-14，基线 `5672a6a`。作者/内部自审 Codex；无独立 reviewer sign-off。
承接 [feed 槽位解释](METRIC_BUILDER_FEEDS_20261114.md)。原始资料和日志保存在忽略目录
`reports/research/metric-probe-20261114/`。本轮没有签名、广播、bundle 提交、生产部署或资金操作。

## 验收范围

两个依赖分别验收：一是将原 Titan 目标 26410517 与父块 26410516 的读取关联；二是在
本地 EVM 完整调用两池 swap、执行两次付款并逐资产核对余额。后者使用明确标注的合成
池及代币，没有把真实协议的报价、bin/tick 数学、历史流动性、oracle 或权限替换成已验证。

事先固定反证：父子哈希/时间不一致、来源不同、缺任一必需来源、构块目标变化；probe
错误调用者、错误链/块/时间/代码、未认证或重复回调、部分成交、付款短缺、输出与余额
不一致、预存本金、支付重入、外部合约伪造成功结果。没有经济阈值调参、untouched 收益
窗口或实盘晋级。本机 design-lock 在网络 I/O 前冻结来源、块号、预算和停止条件。

## 真实块头与停止记录

新增范围 `retained_titan_26410517_canonical_context` 只研究原快照的 canonical 上下文。
两个免费匿名来源各最多 48 次、总最多 96 次、最坏费用 USD 0；原计数从初始化起累计。
这不是重启旧块 26410155 的报价/诊断或 Titan 3/3 范围；没有再读 Titan，也不允许状态
override、报价重试、认证或提交。新范围第一处异常立即 STOP，无来源回退。

来源依据：[PublicNode 公共入口](https://ethereum.publicnode.com/)、
[MEV Blocker 免费说明](https://docs.mevblocker.io/)及
[固定 base URL](https://docs.mevblocker.io/reference/api/transaction-endpoints)。采集器新增
`mevblocker_ethereum` 固定只读别名，复用方法白名单、预算预占和原始字节验证；网站的
交易提交功能不在采集器能力内。服务名称不同不证明底层节点独立。

PublicNode 两次完整 head capture 共 4 请求（每次 chainId + 固定块）：

| 字段 | parent | target |
| --- | --- | --- |
| number | 26410516 | 26410517 |
| hash | `0xecc239466d283c9c4988623463cc33f1c5559297dad025b4b9d02db74025398c` | `0xcceb62b4706283b85b7312cabb2ccdff795f2ebec2d5a6026b3f3e4c5a01e379` |
| timestamp | 1794626699 | 1794626711 |

target 的 parentHash 与上表 parent hash 一致，target timestamp 与原 Titan slot 声明
一致。target extraData 解码为 `Titan (titanbuilder.xyz)`；这是可由提供者/构块者填写的
标签，不是构块者身份签名、overlay 已落地或状态完整性的证明。

MEV Blocker 的 chainId 请求 HTTP 200，下一次固定父块请求发生
`transport_context_or_resource_failure`，无 HTTP 状态、未收到完整响应体。失败请求已
预占计数；原 campaign 总计 **6/96 STOP**：PublicNode 4/48、MEV Blocker 2/48。未查询
该来源 target，也未进行目标 runtime/config 读取、状态覆盖或模拟。异常后没有重试。
其原始空响应 SHA-256 为 `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`。

原字节离线回放：两个完整 capture 的 retained_bytes / original_http_bytes 均验证通过；
失败 capture 仅 retained_bytes 通过，original_http_bytes 为 false。原账本保持 STOP。

## 上下文正常入口

`arbitrage/metric_context.py` 和 `scripts.research_metric_context` 将原始 Titan capture、
分析计划及固定 header capture 联结。计划固定 Ethereum、目标号/时间、必需来源，以及
Titan capture 与分析计划的字节 hash。所有网络材料先经过原采集器的完整回放。

```sh
research=reports/research/metric-probe-20261114
titan=reports/research/titan-state-20261114
.venv/bin/python -m scripts.research_metric_context \
  --plan "$research/context-plan.json" --titan-plan "$titan/analysis-plan.json" \
  --titan-capture "$titan/campaign/snapshots" \
  --header-capture "$research/campaign/publicnode-parent" \
  --header-capture "$research/campaign/publicnode-target" \
  --header-capture "$research/campaign/mevblocker-parent" \
  --output "$research/another-context-report.json"
```

本轮正常退出 **3**，结果 incomplete_or_rejected，明确缺第二来源两份 header。可以
查看单源 observed_context，但不能当成最终性、来源独立或执行准入。输入/输出冲突退出
2，所有计划要求的观测一致才退出 0；即便退出 0，所有执行及独立性标志仍为 false。
任一哈希、父子关系、时间或构块声明矛盾均清空 observed_context，保留逐源失败证据；
不沿用上次成功值，不把未取得的响应补成空块，也不重新请求网络。

## 整笔探针的真实代码与合成验证

新增 [MetricV3RevertingProbe](../../contracts/src/research/MetricV3RevertingProbe.sol)，
正常 `probe(Request)` 总是回滚，包括成功完成两池付款的情况。它没有 approve、
transferFrom、任意目标调用或成功结算入口。public immutable getter 只读。

配置固定 observer、两池、两资产、四个 runtime hash 和 EVM 链/块/时间。资产 A/B
按地址排序；Metric 的 token0 可与 V3 相反，配置必须单独验证。请求固定两腿顺序、
起点、输入、最低**毛**收益、Metric Q64 价格界、V3 Q96 sqrt 价格界和 extension bytes。
pool/recipient 恒为固定池/探针本身，实际池调用者是探针，不能用它证明另一生产 router
或 executor 具有相同权限。此合约尚无真实部署地址或已验证的目标 runtime。

流程：第一池先发送输出，在第一回调中将它作为第二池的完整 exact-input；第二池发送
起点资产，支付中间资产后返回；然后偿还第一池本金。拒绝预存 ERC20/native 本金，
两次回调须来自确切池、正确协议回调函数及固定 request hash/阶段。第二回调开始支付
前将状态锁为 3，两个 callback 和 probe 都拒绝再次进入；所有返回 delta、实际输入、
逐笔转账前后余额及最终残余逐一核对。任一部分成交、费用扣转、缺输出或不偿还即拒绝。

成功结果只在 `ProbeResult` revert 中给出 request hash、原币本金/中间量/最终输出/毛
收益及内部 gas 差值；实际状态、余额、日志全部回滚。内部 gas 差值不是可落地交易的
完整 gas 或净收益，不能直接套成收益率；intrinsic、probe 与未来 executor 差异、builder
支付、失败成本和机会成本仍须单独处理。外部 pool/token 抛出的同名错误会被包装为
ExternalFailure，不能伪造成本探针已经完成的结果。

语义来源均为原始协议源码，保存原文件和 hash：

- Metric pool-contracts `ddf8ae0cdb2ad0041c3ff3aebc200cf488d5b0d3` 的
  [swap 接口](https://github.com/Metricxyz/pool-contracts/blob/ddf8ae0cdb2ad0041c3ff3aebc200cf488d5b0d3/contracts/interfaces/IMetricOmmPool/IMetricOmmPoolActions.sol)、
  [callback](https://github.com/Metricxyz/pool-contracts/blob/ddf8ae0cdb2ad0041c3ff3aebc200cf488d5b0d3/contracts/interfaces/callbacks/IMetricOmmSwapCallback.sol)
  和 [池实现](https://github.com/Metricxyz/pool-contracts/blob/ddf8ae0cdb2ad0041c3ff3aebc200cf488d5b0d3/contracts/MetricOmmPool.sol)。swap 输入/返回是 int128，callback 却是 int256；输出先转账再 callback。正数 exact-input，实际可部分成交。
- Uniswap v3-core `5ea68a77cb541c6b3812df783be71ba8c081e25f` 的
  [swap 接口](https://github.com/Uniswap/v3-core/blob/5ea68a77cb541c6b3812df783be71ba8c081e25f/contracts/interfaces/pool/IUniswapV3PoolActions.sol)：输入和 delta 为 int256、价格界为 uint160。先解析官方 ref，再按 commit 取回并逐字节比较，内容一致。

仅写兼容 ABI，未升级仓库 Solidity 0.8.26，未复制真实池实现作为合成成功证据。
后续调用必须审阅目标代码、proxy/config、oracle 注册和 blacklist、extension、各价格
界和 EVM 实际块上下文。特别是 Metric 原池反向买入时 priceLimit=0 会阻止成交，不能
照抄会做 normalizePriceLimit 的 quoter 输入。

```sh
cd contracts
FOUNDRY_PROFILE=ci forge test --match-contract MetricV3RevertingProbeTest
```

本地 10 项测试通过，其中 fuzz 10,000 次；覆盖两种 venue 顺序、两资产方向、相反
token 排序、不同基础单位、完整还款和强制回滚，以及部分成交、缺/重复/错 callback、
返回值错、极值/错符号、代币扣费、伪造错误、池/观察者/支付期间重入、已有库存、身份
与时间变化。这些是合成 EVM 测试，**没有真实 pinned fork 或远程整笔模拟成功**。
最终 fuzz 每组连续执行两次不同方向/规模，验证同一实例回滚后的阶段和余额恢复；共
10,000 组、20,000 次 probe 调用。基础单位测试读取合成代币实际的 6/18 decimals，
不依赖部署地址排序来猜测哪个资产使用哪种 decimals。

## 失败审阅与门禁

初次 build 被 10 项 lint 提示阻止，保留日志；改用 SafeCast、明确初始化、原生余额
非零检查和有理由的单行历史时间 pin 注释。第二次因未显式初始化布尔局部变量被阻止，
已修正。初次 shell 包装在打印 tail 后返回 0，不能将其当作底层 build 通过；以
原日志的 post-build lint failure 为准。第二次直接 build 退出 1；后续命令保留直接退出状态。

Slither 首轮 make 退出 2（内部工具 255），共七项提示。三个 reentrancy-no-eth 提示
通过简化阶段状态、付款前关闭所有 callback 消除。其余两个 reentrancy-balance 提示
来自刻意的付款前后余额核对，events-maths 来自必定回滚的临时请求参数，timestamp
来自固定历史上下文相等比较；在精确函数处分别记录理由并局部抑制，未改变全局检测器。
支付锁及 authenticated pool/observer/token 重入均有独立合成用例；不依赖“总会回滚”
而容许伪造 ProbeResult。复核 85 合约、102 detectors 后零未抑制 findings。

| 材料 | SHA-256 |
| --- | --- |
| PublicNode parent capture.json | `ecf81862a18fd9f3d162e1292407434dc9bccbf60f0a3a7e28d7d7c3adab7c45` |
| PublicNode target capture.json | `d31ec093f9db4ba550275f493da04a97da9157d25821bd1d31cf1cb5ceb25570` |
| MEV Blocker 失败 capture.json | `d21cb10e0cd8887df7ad93e8e4adf7d87eed0c7202f51cd95f5ad611fcc06245` |
| context-plan.json | `dbf636c9813f8ea324c0c9d6e422b633f81174a12804339c76f50d55831efd54` |
| context-analysis-final.json | `e4c755a09b3ecd844d3a4164a43cbff59236958b27ce544d5e3be12fb3c2b846` |
| Metric callback 源码 | `a4fda25eff91e2f205aad03b3d0c68825386ac6d5fb58f5021fdfc53b79295fe` |
| V3 swap 接口源码 | `8d78f2ebaf97e3c0189aedcdd1a88689f2a0de4fde9be0cf9c46cb0d12e35dce` |

聚焦 Python 共 155 项通过，包含新增 16 项上下文测试，以及新固定来源的公共采集
参数化回归。真实 context 命令重复运行退出 3，报告逐字节一致；缺源不是命令故障或
可以自动重试的信号。4 份文档的 45 个本地链接、6 个 shell 示例语法及两个 CLI help
通过。实际命令日志保留在本轮忽略目录，原始证据、下载源码、构建文件均不入 Git。

最终门禁针对同一份已审阅代码，退出状态均直接取得：

| 检查 | 退出 | 结果 / 原日志 |
| --- | --- | --- |
| 聚焦 Python | 0 | 155 passed；`python-focused-final.log` |
| 聚焦 Foundry CI profile | 0 | 10 passed，10,000 组连续回滚序列；`forge-focused-final-v3.log` |
| `make check` | 0 | lint/format/typecheck、6,014 Python tests + 138 subtests、9 个前端模块语法、80 Foundry tests、合约 lint；`make-check-final.log` |
| `make security-check` | 0 | 依赖审计、Bandit 中/高严重度均 0、secret gate；`security-check-final.log` |
| `make contract-security-check` | 0 | 85 合约 / 102 detectors，零未抑制 findings；`contract-security-final.log` |

Python 全量运行保留 11 项弃用警告：一项 websockets legacy、十项既有多线程进程中的
fork recovery 测试提示；未把警告写成失败或隐藏原日志。

本机 Python 3.12.4、Node 23.11.0、Foundry 1.8.4 Homebrew；不宣称这是 CI 的
Node 22.14.0 / Foundry 1.8.1 环境。研究合约不进入生产部署脚本或现有 LP/spot 授权。

下一项仍是独立的 canonical context、目标 runtime/config 与代理、受约束的 probe
调用/回滚证据采集以及真实双 venue pinned replay。新原 campaign 已停止，不得重建
账本补读。准备好调用证据不授予广播权，合成毛收益不替代净 NAV、独立成本或 untouched
经济窗口。其余 Aave、Elysium、前瞻 shadow 继续见[持续清单](../strategies/NEXT_STAGE_EXECUTION.md)。
