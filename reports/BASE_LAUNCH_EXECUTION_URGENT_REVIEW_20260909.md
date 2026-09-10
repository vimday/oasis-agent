# Base 开盘执行器紧急复核

结论：**不通过。应暂停新增买入，不能把进程运行或现有单测通过视为开抢就绪。**

复核时间：2026-09-09 01:37–01:40 UTC（北京时间 09:37–09:40）。
代码版本：`b659f0ddad0b69f6dd2a3805556dbc9ec3c7afac`。复核开始时工作树干净。
本次只读复核，没有修改执行代码、配置、停止文件，没有读出私钥、签名或广播。
以下链外本地证据不替代链上余额、nonce、收据和授权复核。

## 运行现场（confirmed：本机观测）

- PID 31686 的 `scripts/run_launch_monitor.py` 进程存在，未带 `--dry-run`。
- `data/launch-execution-2026-09-09.db` 本次读取时：2417 条 `buy_failed`、
  860 条 `monitor_error`；没有 `buy_broadcast`、`buy_confirmed`、`sell_broadcast`
  或 `sell_confirmed` 事件。最新若干买入错误包含 revert。
- `data/launch-execution.stop` 不存在。
- 8666 端口 Launch 目标/授权接口返回 HTTP 错误；没有据此确认运行 UI 与执行器对齐。
- 执行库没有买入事件不等于链上账户必然无仓位；其他进程/此前交易/授权必须另行核对。

## 阻断发现

### 1. [P1] ETH/USD 换算放大 10,000 倍（已离线复现）

`src/oasis_agent/sniping/monitor.py:121` 将 8 位小数 feed answer 转换为 6 位
USD micros 时执行 `answer * 100`；正确单位转换应除以 100，涉及风险上限时还须明确
保守取整策略。用 $3000 feed 输入，预期 `3_000_000_000` micros，实际
`30_000_000_000_000`。FDV、最低接收量和美元成本均失真。

### 2. [P1] 买卖 Quoter 参数与返回值均错误（已离线复现）

`monitor.py:152–158` 和 `172–178`：静态五字段 tuple 前多插入一个 32 字节
“长度”字；规范 ABI 应为 selector + 160 bytes，实际为 selector + 192 bytes。
真实 Quoter 收到错位参数，会 revert。返回值应解码为
`(uint256 amountOut,uint160 sqrtPriceX96After,uint32 initializedTicksCrossed,uint256 gasEstimate)`，
当前却把全部 128 bytes 解析为一个整数，不能当作 token 收到量。

离线 ABI 比对：actual calldata 196 bytes、canonical 164 bytes；模拟正常四字段
returndata 的 amountOut 为 `1000*10^18`，当前解析出 838 位整数。
规范参考：[IQuoterV2](https://raw.githubusercontent.com/Uniswap/v3-periphery/main/contracts/interfaces/IQuoterV2.sol)。

### 3. [P1] Router 买卖命令错误（官方源码对照）

`src/oasis_agent/sniping/calldata.py:20` 把 `UNWRAP_WETH` 写为 `0x06`；
Uniswap Universal Router v1.2.2 和当前源码中该值是 `0x0c`，`0x06` 是
`PAY_PORTION`，参数结构也不同。

`calldata.py:80–88` 的原生 ETH 买入只生成 V3 swap，`payer_is_user=False` 要求
Router 用自己的 WETH 付款，却没有前置 `WRAP_ETH (0x0b)`。发送 msg.value 不会
自动替代 wrap 命令。正常空 Router 余额下不能完成该原生 ETH 付款路径。

参考：[v1.2.2 Commands](https://raw.githubusercontent.com/Uniswap/universal-router/v1.2.2/contracts/libraries/Commands.sol)、
[Dispatcher](https://raw.githubusercontent.com/Uniswap/universal-router/v1.2.2/contracts/base/Dispatcher.sol)。
本次没有对部署地址 runtime 做 block-pinned 源码匹配，因此不把官方 tag 对照冒充部署验证。

### 4. [P1] “模拟 exact payload”实际上丢弃 from/value；且误读 Router 返回值

`src/oasis_agent/sniping/rpc.py:189–195` 创建包含钱包和 value 的 payload 后，
调用 `eth_call(to,data,...)`，实际 RPC 只有 `to/data`。离线捕获已确认不含
`from` 和 `value`。钱包余额、Permit2 权限、原生 ETH 路径都没有按实际交易模拟。

`monitor.py:276–280`、卖出相同逻辑要求返回值含 uint256 amountOut；标准
Universal Router `execute` 不返回 amountOut，成功 eth_call 是空返回。
应使用真实连续状态模拟及余额差验证，不能从 execute 的最后 32 bytes 推断收入。
当前实现也没有 `eth_simulateV1` 连续买入/授权/全量卖出压力验证。

参考：[v1.2.2 UniversalRouter.execute](https://raw.githubusercontent.com/Uniswap/universal-router/v1.2.2/contracts/UniversalRouter.sol)。

### 5. [P0] 广播结果未知后可能再次买入；没有 durable UNKNOWN 阻断

`monitor.py:225–248` 在发出交易后若 receipt 超时，会重复发同一交易并最终抛错；
`_buy_cycle` 捕获异常后继续尝试池或下一轮。只有全部确认/余额检查成功才更新
`tranches_done`。若旧交易已成功但回执或余额读取失败，下一轮按新的 latest nonce
再次买同一 tranche，可能突破累计预算。journal 中没有持久化未知状态来阻止新意图；
链上 tx hash 在确认成功前也没有与意图形成可恢复绑定。

`_next_nonce` 只读 latest nonce，没有跨进程账户 nonce 预留，不能保证单意图。

### 6. [P1] journal 重启失败，恢复路径不可依赖（已离线复现）

`src/oasis_agent/sniping/journal.py:41` 在 `policy_digest` 初始化之前调用 `_verify`。
数据库只要已有一条记录，正常重新打开就抛
`AttributeError: 'ExecutionJournal' object has no attribute 'policy_digest'`。

此外 `scripts/run_launch_monitor.py:82` 将每次启动的新时间写入“不可变 policy”，
正常重启还会触发 policy mismatch。代码有 `_recover_position`，但上述初始化错误会
阻止恢复；未确认广播事件也未被恢复为未知状态。

### 7. [P0] “halting buys”只是日志，未关闭买入状态

`monitor.py:532–534` 五次错误后只打印停止买入并清零错误计数，没有设置停止状态。
后续 `_cycle_once` 仍可以调用 `_buy_cycle`。多数 buy 失败在 `_buy_cycle` 内部被吞掉，
甚至不会增加这个连续错误计数。日志不能作为资金安全控制。

### 8. [P1] 退出估值使用一枚 token 的价格外推全部仓位

`monitor.py:385–387`、`401–403` 只询价一枚 LAPTOP，乘仓位量后当作全仓可兑现
价值/卖出 minimum。低流动性或跨 tick 时没有线性可扩展性，容易高估退出价值、
不触发止损或设出无法成交的 amountOutMinimum。应按实际全仓/半仓数量报价并模拟。

`monitor.py:552–559` 在第二 tranche 尚待处理时不一定调用 `_sell_cycle`；
第二 tranche 持续报错可以使第一 tranche 仓位长时间不接受退出评估。

### 9. [P1] 双源标签不能弥补未固定区块和未实现控制面门禁

报价、价格和余额用 `latest` 分别读取，没有共同 block hash；`max_age_blocks`
和 `max_age_seconds` 未在入场执行路径校验。token code 首次读取即接受为基线，owner
仅打印，没有比较；没有 Endpoint delegate/peer/DVN 基线校验、路由 runtime pin、
LP 付款来源/撤出权限证明。研究 shadow 的 G1–G11 拒绝状态没有接入新 `sniping`
执行器，因此 shadow `execution_eligible=False` 不会保护这个独立签名广播进程。

## 参数与权限需要单独确认

当前累计买入配置是 **0.1 ETH + 0.1 ETH = 0.2 ETH**；不是原设计的固定 $500 上限。
执行器没有独立的单笔/单日/单币美元预算和最大 gas 美元预算。用户可能在另一个任务
明确修改过参数，本次不能确认该授权上下文，不将差异直接认定为越权。

脚本实际读取 `.env` 并在策略进程 `Account.from_key`，不是隔离外部 signer。
本次没有读取该密钥或对其所属账户做资金归属判断。

## 验证与处置边界

- 离线最小复现脚本：`/tmp/review_launch_readonly.py`；不包含密钥、不联网、不签名。
- `pytest tests/unit/test_sniping.py`：25 passed。这套测试没有覆盖上述 RPC、
  feed、真实 ABI 返回、正常非空 journal 重启和未知广播恢复；部分 calldata 测试
  用被测模块自己的错误常量作预期，无法证明协议正确性。
- 本次没有提交代码或触碰运行配置。没有尝试通过“先修报价、忽略模拟错误”来赶开盘。
- 建议先由操作方停止新增买入并核对链上持仓/nonce/授权；修复后必须完成实际钱包
  身份、真实路径、正确原生 ETH 处理、全量买卖连续模拟与 UNKNOWN 恢复验证。
  只修复单个 ABI 错误不足以恢复自动交易。
