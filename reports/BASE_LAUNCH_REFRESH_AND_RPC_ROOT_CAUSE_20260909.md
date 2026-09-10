# Launch 刷新与 RPC 根因复核

页面问题已修复：原先忽略 launch_decisions WebSocket 消息，常规定时刷新为 30 秒，页面回到前台后也要等下个周期。现在 Launch 使用 3 秒轮询、事件触发刷新、防重入、10 秒请求超时、禁用请求缓存、每秒更新的浏览器时钟，以及超过 15 秒未成功读取的页面陈旧提示。每个池展示的是该池最后一次尝试时间，不能改成当前时间伪装成最新状态。报价表只渲染最近 100 条，原始记录仍保留。

真实浏览器无手动刷新验证：日志序号从 6162 / 最近日志 22:45:13，自动前进到 6180 / 22:45:34（UTC+8）；页面时钟同时前进，页面读取距今 1–2 秒。

## 当前首池失败的具体原因

链 8453；区块 51,087,820；区块哈希 0xb54d73c5f0d0b6e01d0bf586a735e40bde9cc383cb1ece76ebff9923d1d8fc6f。
池 0x7ed7bfbcc7167fcb87f43ca730548dd52547e774；双 RPC 一致的 liquidity=0、sqrtPriceX96=4295128740、tick=-887272。池代码 keccak256 为 0x7b8c8ff147b8a9c78200f9ff4e387e19d9110e39fec54d1f616976cb8a846beb。

主 RPC 固定区块 debug_traceCall 证明 Quoter 和池都回退 SPL，gasUsed=0x8bb3。WETH→LAPTOP 是 zeroForOne，标准 Quoter 默认下界为 MIN_SQRT_RATIO+1，恰好等于当前 sqrtPriceX96；池要求限制价格严格小于当前价格，因此在交换循环之前拒绝。此状态下，降低买入金额也不会消除 SPL。不能据此推断所有池或未来状态都不可买。

备用 RPC 对 debug_traceCall 返回 HTTP 429：这是限流，不是相同的合约根因，也不证明普通 eth_call 永远不可用。新的错误格式区分 primary/secondary、方法、错误分类、节点码、HTTP 状态；仅保留白名单合约原因（例如 SPL），不输出任意节点原文、凭据或交易数据。新日志代码尚需运行进程重新加载才生效；本复核没有重启交易进程。

证据：evidence/launch-liquidity-current-20260909.json，采集时间 Unix 1788964986，SHA256 ea7fbc94fc031062e2b8aa91127140a75044ad8597e0f63bc5bb3e4726513461。双源状态，单源成功追踪。
源码对照：https://raw.githubusercontent.com/Uniswap/v3-core/main/contracts/UniswapV3Pool.sol 和 https://raw.githubusercontent.com/Uniswap/v3-periphery/main/contracts/lens/QuoterV2.sol 。

## 实测扫描瓶颈仍需修复

只读检查 live journal：轮次 5 有 248 条失败池记录，首尾相隔 456 秒；轮次 4 有 246 条，首尾相隔 521 秒。代码逐池串行处理大量候选。因此“全局日志持续有事件”不代表每个池及时重查。页面已增加当前扫描轮次记录跨度及超过 120 秒的延迟提示。随后加入 PoolSchedule：每批最多 8 个池，最多半批优先处理已验证流动性事件提示，其余按 FIFO 轮转，防止事件洪泛饿死其他池；每批重新固定双 RPC 区块快照。它改变候选检查顺序和批次边界，不改变买入预算、报价/FDV 条件或 nonce 串行约束。代码尚未加载到现有实盘进程，不能宣称实盘延迟已降低。


调度验证：第 300 个池出现事件后进入下一批首位；持续事件洪泛时 40 个池在 10 批内全部被检查；删除池与大小写重复不进入错误批次；Monitor 的观察模式集成测试确认 300 个候选只处理 8 个，且使用发现后重新获取的区块快照。离线基准 300 候选 × 1000 次选择共 74.51 ms，仅为调度 CPU 耗时，不是网络或成交延迟。

修正轮次统计：只计算末尾连续相同轮次，避免进程重启后从 1 重新计数时合并之前的同号轮次。新增回归测试证明历史 cycle=6 不会污染重启后 cycle=6 的时长。

补充双源 eth_call：同一固定区块两路均返回 execution_reverted code=3 revert=SPL，主 366 ms、备用 500 ms；备用 RPC 429 限流只在上述 trace 请求得到证实。证据 evidence/launch-spl-dual-replay-20260909.json，SHA256 02a32201c1454a7ca8224d8673e8a566bdeb3e1e0af76a48d6f68ee187698b89。

最终验证：make python-check 全通过（Ruff lint/format、mypy、933 tests、108 subtests，1 条既有 websockets 弃用警告）；JavaScript node --check 通过；Bandit 中高危 0，低危 41；secret gate 通过。没有修改合约，未重跑合约测试。只读 UI 已更新；没有签名、广播、重启实盘进程。
