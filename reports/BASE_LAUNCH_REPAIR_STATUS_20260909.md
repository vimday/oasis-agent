# Base 开盘执行器修复与重启状态

本轮执行了用户要求的停止和修复，但**没有恢复真实自动交易**。旧非 dry-run 进程
PID 31686 已退出；`data/launch-execution.stop` 保留。新 PID 9616 运行
`run_launch_monitor.py --dry-run`，只观察，不加载签名账户，不广播。
旧执行数据库和旧日志未改写；新记录写入 `data/launch-observe-fixed.db` 和
`data/launch-observe-fixed.log`。

## 修复

- feed 的 8 位 answer 转 6 位 USD micros，改为向上取整除以 100；拒绝负值与不完整轮次。
- 修正 Quoter 静态 tuple 编码、四字段 returndata 解码；地址 ABI 采用规范小写 hex。
- 原生 ETH 买入添加 WRAP_ETH 0x0b；卖出使用 UNWRAP_WETH 0x0c。
- eth_call 保留真实 from/value，并以同一 block hash 读取；不再从 Router 空返回伪造 amountOut。
- 修复非空 journal 的 policy_digest 初始化顺序；新观察进程采用稳定 policy，保留旧日志不迁移。
- 退出报价改为实际仓位数量；买第二 tranche 不再成为评估已有仓位退出的前提。
- 重复错误设置停止买入状态；恢复时未完成广播事件保持 unresolved。此处没有声称完成
  生产 UNKNOWN 交易对账或隔离 signer：旧进程内签名代码已禁用，真实广播入口明确拒绝。
- CLI 强制 `--dry-run`，只提取 RPC 配置并从旧 journal 读取公开钱包地址，不创建 Account。

## 真实 RPC 复核

固定 Base 区块 51,064,869，hash
`0xb23a38e3103c8f8a906b2e83d573c98d83c00dbb124562d1ddc4dcde0390c206`：

- 两源 Quoter factory 与 WETH 绑定返回一致。
- 完整钱包/gas 参数的 eth_simulateV1 基础调用，两源均返回 status 0x1。
  最初不完整的探测 gas 参数曾导致 RPC 错误；不是接口不支持。
- 500 fee 候选池对 0.001 ETH、0.1 ETH 的规范报价，在两源均返回 `Unexpected error`。
- 在不覆盖钱包余额、不改池状态、不广播的条件下，修正后的 0.1 ETH 实际钱包买入
  模拟在两源均返回 status 0x0。尚未证明其根因为价格、流动性或其他执行条件。
- 因买入本身失败，未完成连续买入—卖出验证；不得开启自动交易。

诊断记录：`reports/evidence/launch-repair-buy-simulation-20260909.json`，SHA-256
`ef1780ad1371bdf763c9fb196d97efb873cc10b1e3cc87ee5b842a218384040f`。
该记录是解析后的诊断快照，不是完整 raw RPC 证据包、运行时代码固定证明或 release approval。

## 验证结果

- 五个独立回归测试在修复前全部失败，复现价格/ABI/模拟/Router/journal 问题。
- 修复后 `pytest tests/unit/test_sniping.py tests/unit/test_sniping_regressions.py`：34 passed。
- `make check`：通过；当次 Python 857 passed，Solidity 70 passed、0 failed、0 skipped。
- 新增四项隔离与恢复回归测试后再跑 `make python-check`：861 passed，108 subtests passed，
  1 个既有 websockets deprecation warning；lint、format、mypy 通过。
- `bandit -r src scripts -ll`：0 medium、0 high；秘密扫描通过；`git diff --check` 通过。
- sniping 专项语句覆盖率 52%（649 statements）。Calldata 95%、journal 93%，
  monitor 32%、RPC 43%；不能作为完整执行安全证明。未执行真实资金交易。

当前仍缺少连续真实买卖验证、完整控制面/LP 来源证明与隔离 signer，因此只读重启
不等于原请求中的自动下单已经恢复。不得把数值单测通过或进程存活当作实盘放行。

## 速度优先调整

用户明确接受限额内少量回滚与 gas 损耗后，已将两个 provider 的请求并发发送，
包括双源块高读取；仍以同一 block hash 校验，任一来源失败不替代为单源通过。
新增 Barrier 并发回归测试，不用不稳定的时间阈值伪造性能结论。
专项 35 passed；最终 `make python-check` 为 862 passed、108 subtests passed，
lint/format/mypy 通过；秘密扫描通过。

已重启加载并发版本，观察进程 PID 26915（替代 9616）。资金和 FDV 参数未修改；
仍为 observation-only，未恢复自动买单。一次单源 latest debug_traceCall 显示
0.1 ETH amountSpecified 正确传入 500 fee 池，回调 amount0Delta=0、amount1Delta=0，
随后 Quoter 返回 Unexpected error。该跟踪是诊断，非双源 block-pinned release 证据。
接受少量回滚不能使本次零输出调用变成可成交报价。
