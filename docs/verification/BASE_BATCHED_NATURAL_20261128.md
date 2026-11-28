# Base 合并报价与自然候选新窗

在同一规范状态、同一路线和本金的四组对照中，两段 Quoter 与费用从三次请求合并成一次。到账和费用逐项一致，含同源限速的读取时间由 **631–687 ms** 降至 **128–153 ms**。随后冻结六池的新窗口。68 个采样头中有 **58 个在直接子头前**完成报价和费用核对。1,104 个有效金额点均无正毛差，三阶段自然候选分母为零。[配对账本](BASE_BATCHED_QUOTE_AB_20261128.json) · [新窗逐头证据](BASE_BATCHED_NATURAL_20261128.json)

## 同状态配对

以追回的历史微利和三个已留存状态校准。两组先走串行，两组先走合并，均用同一家匿名 PublicNode、同一进程和按源 250 ms 的最短请求间隔。新读取只在 `eth_call` 覆盖一个临时地址的代码。第一段实际回报价作为第二段输入。同一调用中还读取执行信封大小对应的 L1 费用上界、operator fee 上界、池工厂注册和池运行码摘要。块号和 BaseTime 毫秒时间与已核头一致。两个清算币的金额用各自整数单位。[报价夹具](fixtures/BaseBatchedQuoteProbe.sol) · [Uniswap QuoterV2](https://github.com/Uniswap/v3-periphery/blob/main/contracts/lens/QuoterV2.sol) · [Slipstream 部署](https://github.com/aerodrome-finance/slipstream#deployments) · [Base 网络费用](https://docs.base.org/specifications/transactions/network-fees)

| 父块 | 串行读取，ms | 合并读取，ms | 节省，ms |
| ---: | ---: | ---: | ---: |
| 63,043,738 | 630.501 | 128.114 | 502.387 |
| 63,043,739 | 687.233 | 153.294 | 533.940 |
| 63,043,740 | 631.704 | 152.566 | 479.138 |
| 63,325,056 | 634.584 | 150.679 | 483.904 |

扩大到同时计算三档本金的首个预检在 1,800 万外层 gas 上限耗尽。单腿 14 万 gas 的第二版在历史同状态对照遗漏 Slipstream 第二腿，串行 Quoter 在该腿返回 189,065 gas 的估计。正式范围将每头限制为一档本金、每个腿最多 40 万 gas，最多六次首腿和三十次后续腿。单腿失败留作缺测。历史正控在原本金和池对上仍得到首腿 112,289,127 USDC 最小单位、最终 67,284,190,259,874,398 WETH wei，按本轮发现费用估计余 **1,560,488,492,919 wei**。用新整笔解析器离线验收上轮两家独立公共整笔原件，合计网络费后的已知历史净额仍为 **1,682,277,530,217 wei**。[历史整笔正控](BASE_SIMULATOR_LATENCY_20261128.md)

## 独立自然候选窗

四段各见证 64 个连续公共 WebSocket 头，高度分别为 63,345,200–63,345,263、63,345,271–63,345,334、63,345,342–63,345,405、63,345,413–63,345,476。每段按预注册位置选 17 个头。六池均为 WETH/USDC，搜索每个不同池的有向组合。三个本金按采样位置轮换，触发候选时本金、池序、calldata、gas 上限及运行码固定下来。预注册门槛是在 310,551 gas 的费用估计下至少余 1 wei，另记 293,025 gas 的灵敏度对照。若触发，使用 Base 官方及 dRPC 整笔核验 H，并在同机收到的 K 和严格直接子块 C 上按相同包络复算。

| WETH 本金 | 采样头 | 计划点数 | 完整报价 | 正毛差与费后正例 |
| ---: | ---: | ---: | ---: | ---: |
| 0.067280448940895129 | 24 | 720 | 480 | 0／0 |
| 0.25 | 24 | 720 | 384 | 0／0 |
| 1 | 20 | 600 | 240 | 0／0 |
| **合计** | **68** | **2,040** | **1,104** | **0／0** |

936 个计划点未获完整两腿报价，仍计入覆盖分母。逐腿复查发现它们都涉及两个 tick spacing 1 的 Slipstream 池 `0x4e392fbf…` 或 `0xdbc69982…`。单独用 150 万 gas 在首个已知缺测的固定头复查前一个池，公共 Quoter 返回 `Unexpected error` 后按规则停止。这一例增大 gas 后仍缺结果。头到报价及费用验收用时 **125.9–442.7 ms**，中位数 **142.1 ms**，58/68 次赶在直接子头前。全窗最佳发现净额为 **−12,860,216,879,066 WETH wei**。两种 gas 口径都零正例，因此未派发自然整笔调用，K、C 的自然净额保持空值。历史正控不进入实时分母。

## 预算及复算

正式新窗用了 90 次免费预留，含 78 次 HTTP 与 12 次 WebSocket 控制。正式四组配对另用 18 次。前两步设计共保留 34 次预留，含首版四组串行配对 18 次、扩大预检停止前的 10 次与逐腿低上限停止前的 6 次。独立缺报价诊断另用 2 次，原件留在 `reports/research/base-quote-ts1-diagnostic-20261128/`。Base 公共数据累计 **144 次**免费预留，支出 USD 0。所用 Solidity 为本机 0.8.26，采用 Cancun EVM、IR 优化及无字节码元数据哈希设置。原字节、编译配置、冻结脚本、停止记录和原账本保留在采集机。两份正式报告离线逐字节复算一致。

报价夹具的专项 Slither 严格检查退出 255，列出默认零初值、未使用的 Quoter 附加返回值、有界循环内调用及复杂度。下标在 Solidity 中按类型初始化为零，只有实际出币进入定价，循环的目标、总腿数和每腿 gas 都固定。专项高危阈值复核退出 0，输出均保留在 `reports/research/base-quote-reth-integration-20261128/`。夹具仅用于只读调用覆盖。

```sh
set -o pipefail
export PYTHONPATH=.
.venv/bin/python docs/verification/fixtures/measure_base_batched_quote.py replay \
  reports/research/base-quote-lot-20261128 |
  cmp - docs/verification/BASE_BATCHED_QUOTE_AB_20261128.json
.venv/bin/python docs/verification/fixtures/run_base_batched_natural.py replay \
  reports/research/base-combined-lot-natural-20261128 |
  cmp - docs/verification/BASE_BATCHED_NATURAL_20261128.json
```
