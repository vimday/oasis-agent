# Robinhood RPC 使用与付费限制

用户于 2026-09-13 指定使用 `.env` 中的 `ROBINHOOD_RPC_URL` 或
`ROBINHOOD_ALCHEMY_RPC_URL`，并明确后者收费、不得频繁调用。

执行约定：

- 优先使用 ROBINHOOD_RPC_URL；Alchemy 仅做必要的低频交叉核验。
- 每次研究采样预先确定方法、请求数和最小间隔，采用硬请求预算；失败消耗预算，
  不自动重试、不自动切换到无限付费轮询、不并发打满付费端点。
- 双源证明需要真实双源结果；公共端点失败时停止或降为单源观察，不用缓存/推断
  伪装成第二家实时结果。
- 端点只在进程中解析，不输出完整 URL、密钥或其他 .env 内容；不得 source/执行
  未验证的 .env shell 内容。原始 RPC 请求响应留存不含传输 URL。
- 调用次数不等于费用；不同方法的计费权重、套餐和本期剩余预算未核实，不能给出
  未经验证的金额承诺。复杂 trace、大范围日志/历史扫描须先估算调用规模。

本次 ETH/STOCKHOOKS 单快照采样采用公共最多 9 次、Alchemy 最多 7 次，分别至少
间隔 1 秒和 3 秒；无重试、无后台定时器、无签名广播。该数字是本次采样上限，
不是长期全天候任务的授权或费用预算。后续持续任务应优先使用公共端点并为收费
端点配置跨周期预算，不能每轮重新创建预算来绕过限额。

## 有界采集命令与持久化请求预算

`scripts/run_pool_state_observer.py` 将单次研究采样变成可测试的命令：

```sh
.venv/bin/python scripts/run_pool_state_observer.py \
  --config configs/observer.robinhood-stockhooks.example.json \
  --budget /absolute/path/campaign/budget.db \
  --output /absolute/path/new-output \
  --samples 2 --interval-seconds 5
```

默认只加载 `.env` 的 ROBINHOOD_RPC_URL，不读取或调用付费变量。显式传入
`--verify-secondary` 才加载 ROBINHOOD_ALCHEMY_RPC_URL；这不改变付费请求的硬上限。
配置示例整个 campaign 最多 18 次公共调用、7 次付费调用，最小间隔分别 1s/3s。
一次完整采样需公共 9 次（选 head 2 次 + 精确 slot0 capture 7 次），启用交叉验证额外
付费 7 次。不足以完成下一次采样时预检拒绝；竞争进程也可能在预检后用掉剩余额度，
最终每请求预约仍会拒绝超支。

`rpc/budget.py` 的 SQLite ledger 在网络请求前持久化预约；失败、异常和崩溃不退还。
每次预约使用 BEGIN IMMEDIATE，共享同一 ledger 的进程不会同时取得最后一个配额。
等待间隔不算预约，醒来后重新争取；clock 回退、策略变化、哈希链篡改均拒绝。
不同来源 alias 的配额独立；上限计数不是账单金额，也没有验证方法 CU 权重。

所有重启和后续采样必须复用同一 `--budget`；输出目录必须为新目录，不覆盖既有证据。
禁止自动更换 ledger/alias、删除 usage 记录或调高 cap 来解除耗尽状态。配置源 alias
不证明基础设施独立；操作员对预算文件删除/替换的权限仍在代码边界之外。此 ledger
只约束使用 BudgetedPoster 的调用，不约束机器上别的进程/CLI/直接访问同一 RPC key。

采样输出包括：hash-pinned raw RPC、原始重放、block/pool/runtime 身份、slot0、单源或
双源信任标记、累计预算和缺失 LP 决策输入。`sample_requests_spent` 是采样期间该
campaign 账本的差值；多个采样进程同时共享时可包含其他进程的预约。
单源结果不能沿用上次双源采样的信任等级。`lp_action=unavailable` 表示参考价格、
持仓核账、成熟 markout、可执行深度、全部成本与样本外经济证据仍未齐备，不伪造 HOLD
或盈利信号。离散 latest 快照不等于连续完整事件流，也不构成最终性或路由证明。

该命令每次只运行 1..10 个样本，样本间等待 5..60 秒，不创建后台服务或自动任务。
对所有 evidence/wire 的采集失败会保留关闭资格的报告；预算预检失败在采集前退出。


## 2026-09-14：显式市场窗口模式

配置可额外包含 `market` 对象，字段为 `pool`（完整 PoolKey）、`blocks`（2..10）、
`horizon_seconds` 与 `max_lateness_seconds`（各 1..300）。PoolId 必须与 PoolKey
一致，且此模式必须提供两源；公共单源模式仍只采集研究状态，不能伪装 quorum。
市场窗口每源需要 `7 * blocks + 4` 次请求，公共源额外 2 次选 head。例如 3 个区块
公共 27 次、付费 25 次。预算预检使用整个窗口成本；原有 18/7 示例额度不会被自动
提高，并且不足以运行此模式。调用次数不是 Alchemy CU 或金额估计。

固定的成熟期截止规则不需要额外调用。完整市场特征保存在 report 的 `market_window`，
仍缺独立参考价格、真实持仓、路由与成本/样本外验收。所有身份读取仍逐块验证；当前
尚未优化为订阅加增量缓存。收费源最小调用间隔可能导致头过期或样本不重叠，届时
停止采集序列。该模式经过模拟 RPC 集成测试，本轮没有实际调用 Alchemy。


## 市场检查点与重启

market 模式默认使用 `--budget` 同目录的 `<budget文件名>.market.db`，也可显式传入
`--checkpoint /absolute/path/campaign/market.db`。所有后续启动复用原检查点和预算，
输出使用新目录。状态文件保存配置绑定、窗口、证据路径以及哈希链接，最多 1000 个
窗口；达到容量时停止，不自动清空或滚动丢弃。

恢复和每次追加在 SQLite 事务内读取历史链，并从每个原始 evidence.db 重新构建窗口；
不是把序列化特征当作事实。相同窗口幂等，不前进或证据不一致的替代窗口拒绝。
每次推进验证重叠状态及 deferred 覆盖。预算、检查点、原始证据应一并保留；其中任一
缺失不得新建空历史来声称恢复成功。默认路径是操作约定，进程无法阻止操作员主动改
路径或删除整套数据。游标不授予交易权限，也不能证明 RPC 独立性或链上最终性。

实现会重新验证全部历史窗口，CPU/磁盘工作随窗口数增加，尚未做增量验证优化。
原始窗口重建可能幂等追加派生价格证据，不发起 RPC。检查点只在数据通过新鲜度检查
后推进；报告写入失败时，下一次恢复仍依据已保留的原始证据而非报告文件。


启动时还检查两个持久化文件的配对：已经消耗额度的预算若丢失检查点，或含有效历史的
检查点丢失预算，均在发起 RPC 之前退出。全新空任务允许创建两者。此规则防止意外
删除单个文件导致额度重置或游标丢失；不声称抵抗操作员同时重建所有文件的行为。

## Base 公共 RPC（2026-09-17）

观察器现在支持 `--primary-env` 与 `--secondary-env`，默认仍为原 Robinhood 变量。
通过显式变量名换链，不需要将 Base 地址写入 Robinhood 的配置项：

```sh
.venv/bin/python scripts/run_pool_state_observer.py \
  --config configs/observer.base-eth-usdc.example.json \
  --env-file configs/rpc.base-public.env.example \
  --primary-env BASE_RPC_DRPC --secondary-env BASE_RPC_ALCHEMY_PUBLIC \
  --verify-secondary --budget /absolute/path/base-campaign/budget.db \
  --output /absolute/path/new-base-output --samples 1
```

示例使用 dRPC 和 Alchemy 的无密钥公共 Base 端点。`paid` 仍是兼容旧格式的第二源
内部别名，不代表该示例使用付费密钥。此配置每 campaign 上限为 9/7 次，两源均至少
间隔 3 秒；换源不得自动重置同一 campaign 的预算。切换后链 ID、区块和 runtime 仍须
重新验证，历史 quorum 不可直接继承。变量名必须明确包含 RPC，不能选择 KEY 变量。

两源已在 Base 区块 51410447 完整匹配六个 runtime 与三个 ETH/USDC 池状态，详见
[固定区块证据](../../reports/evidence/base-v4-public-replacement-20260917/report.json)。
这些 runtime 是观察固定值，不是源代码可复现构建认证；池已初始化也不证明有盈利能力。

来源：[dRPC Base](https://drpc.org/docs/base-api)、
[Alchemy 公共 Base RPC](https://www.alchemy.com/rpc/base)。公共端点没有持续可用性保证。
