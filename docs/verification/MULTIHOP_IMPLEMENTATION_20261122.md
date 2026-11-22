# 多跳套利第一批实现与验证 · 2026-11-22

开发基线 `14260a4`。工作树改动覆盖离线搜索、影子采集、费用对账和实验。运行范围包括 Aleph WSL 合成基准及本机回环 Anvil。链上历史数字来自留存数据，本轮新增公共链 RPC 调用数为 0。[研究问题与原始反例](MULTIHOP_RESEARCH_20261122.md) · [后续 TODO](../strategies/MULTIHOP_ARBITRAGE_TODO.md)

## 金额搜索与可复核的范围

V2 两池、逐腿整除的最小反例，在最低净额 135 时返回投入 204、输出 339，在 136 时无候选。独立逐金额计算得到 204 和 207 同为最优。每条进入定量的路线，在最多 256 个整数投入的区间逐额复算。宽区间先取已知最好解；纯 V2 可表示时，用连续 CPMM 复合曲线和精确整数不等式证明子区间无法改进，或在预算内继续细分。宽区间 CL、混合池和无法证明的 V2 路线会返回剩余投入区间及保守净额上界。报告中的空数组表示已评估路线没有未决区间；旧库缺少该字段表示覆盖未知。结果仍受上游池集、跳数和路径筛选限制。[Rust 搜索和独立对照](../../crates/oasis-core/src/graph_arb.rs) · [Python FFI](../../src/oasis_agent/fastpath/pool_graph.py)

另取 40 个合成 34 池快照，交替执行修改前后的 release 库。40 个块的候选一致，观察到 2,757 个路线/块的未证明区间。FFI 调用 p50 从 47.458 变为 46.696 ms，p95 从 61.274 变为 63.622 ms。此样本用于量化报告未决区间的开销。

## 影子采集与离线复跑

在 Multicall 入口开启 `--record-multihop-replay` 会按块哈希发起 canonical Multicall，保存原始请求/响应、摘要、时间、父哈希、state root、池状态及各锚定币的成本和金额约束。离线 CLI 重走原解码器，对照清单验证图和费用；完整性错误或已知分叉会被拒绝，跨缺块重建。此格式标明单一来源、未核验池字节码及 CL 的合成 tick 区间。[采集](../../scripts/run_multicall_100tick_campaign.py) · [留存格式与离线校验](../../src/oasis_agent/fastpath/multihop_replay.py) · [A/B 入口](../../scripts/run_multihop_ab_experiment.py)

四个带人为价差的 fixture ticks 产生 24 条正模型候选，每块 3 个定向拓扑、各有 WETH 和 USDC 两个经济起点；12 条逐腿通过，12 条因 Top-3 跳过。采集分别扫描两个锚定币；通用多起点 API 的 `deduplicate_cycles=True` 仍返回拓扑代表，跨币经济比较须逐锚输入资金与费用。每个锚定币单独断网回放时，全扫和增量在 4/4 块候选相同，各自累计 12 个未证明定量区间，未决区间集合也相同。四个高度之间合计漏采 9 块。此次清单摘要为 `396eebc8d4db619a1a690ccc141ec440ce8daaffd85038719fdb03b55764d735`。记录包含接收时钟，复跑以候选、数额和缺口为对照。

另外复跑了三个真实 Base 的旧样本。四池状态来自已校验摘要链的历史归因，每块 1 个 V2 储备对和 3 个 CL 活跃状态；沿用当时的 ±200 tick 合成边界。每块重建 12 条待检查路线，10 条固定金额探测失败、2 条完成、0 条正候选。三次固定探测的最佳净额分别为 −144,316,868,169,769、−144,316,867,942,385、−144,317,417,772,244 wei，与留存结果一致。块号 58,990,391、58,990,410、58,990,433 间存在间隔，增量臂三次均走全扫；候选逐项 3/3 一致，正例召回不可估。该旧格式缺原始 RPC 字节、父哈希、state root 与初始化 tick，能复现当时的模型，无法支持新清单级别的原始状态证明或经济性结论。[旧 Base 样本](PRELIVE_PROFIT_BRIDGE_20261118.md) · [旧归因专用复跑脚本](../../scripts/replay_legacy_multihop_shadow.py)

旧归因可用 `.venv/bin/python -m scripts.replay_legacy_multihop_shadow reports/research/prelive-20261118/base-shadow` 独立复跑。

以下命令生成新夹具并以两种起始资产复跑。输出位置应使用新目录。A/B 的计时从内存中的池图输入到扫描返回，两臂逐块交替先后顺序，分别报告首块和后续块。对照保留每个资产的漏额与小数位，正例为零时召回率为空。

```sh
out=$(mktemp -d)
.venv/bin/python - "$out" <<'PYTHON'
from pathlib import Path
import sys
from scripts.run_multicall_100tick_campaign import run_multicall_campaign
run_multicall_campaign(out_dir=Path(sys.argv[1]), mode="fixture", ticks=4,
    interval_ms=0, max_rpc_calls=64, fixture_scenario="dislocated_quoter_verified",
    record_multihop_replay=True)
PYTHON
.venv/bin/python -m scripts.run_multihop_ab_experiment --snapshot-manifest "$out/multihop_replay/manifest.json"
.venv/bin/python -m scripts.run_multihop_ab_experiment --snapshot-manifest "$out/multihop_replay/manifest.json" --base-token 0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913
```

## 整笔交易与成本

- 本地 Anvil 的 V3→V2 夹具在固定父块状态，以同一 caller、目标、calldata、value 和 gas 上限执行整笔 `eth_call`。随后本地交易为子块首笔，模拟、`CycleExecuted` 事件和实际代币余额差均为 9,550,169,617,820,656,117 个最小单位，消耗 191,538 gas。将最低收益提高到 9,550,169,617,820,656,118 后，复核到准确的 `InsufficientNetProfit` 错误选择器和三个参数。本机四项 Anvil 演练全部通过。这是代币余额的毛增量，交易执行的原生币成本另列。[本机整笔演练](../../scripts/verify_atomic_vault_anvil.py)
- 复用 Base 费用回放。留存的 Base Sepolia 结算交易花费 L2 20,085,342,000,000 wei、L1 7,643,809,493 wei、operator 0，合计 20,092,985,809,493 wei。双币价格折为 49,843 个 USDC 最小单位。另一个含非零 operator fee 的夹具中，毛额 +2,500 变成净额 −531，漏扣 operator 则得到 +277。缺失、错误交易状态的费用或资产价格应保留未知。[费用](../../src/oasis_agent/farming/transaction_costs.py) · [计价](../../src/oasis_agent/farming/receipt_cost_valuation.py) · [翻转对照](../../tests/unit/test_price_evidence.py)
- Elysium 历史案例用已留存双来源回执和块末调用复跑，得到 `receipt_token_flows_reconciled`，真实赢家交易的精确前状态仍为 `unresolved`。块末调用、本地夹具、Base 影子逐腿结果按不同证据层记录。要验证新 Base 候选，仍须结合真实执行器和目标池，在相同交易前状态模拟整笔，并按实际编码核算 L1、付款和融资。[历史案例](ELYSIUM_CYCLE_RESEARCH_20261111.md)

## 长交易、分流与硬件

Robinhood 留存的 `rh20k_long2.json`（原字节 SHA-256 `1c2d9cde8f2d9171102ad98a0dccc4d7453dd861ba28e5c75222b005c14eaeb1`）在原研究 20,000 块窗口中有 25,829 个唯一匹配交易哈希，其中 4 笔含至少 20 个匹配 Swap 事件。每行只含数量、池数及哈希。除已列的 22/4、21/6、20/4 外，还核到第二个 22 Swap 样本，涉及 3 个池，哈希为 `0xa96d1eda66c013d77e2d89bcb623be3275fb38bfbba748ccac535ae6cb4b6a88`。Base 六池对照只留有计数、发送者、目标与 gasUsed。均缺交易前状态、调用帧和逐资产余额，无法确定最长串行深度与最终受益人。[窗口与已列哈希](../strategies/MULTIHOP_ARBITRAGE_RESEARCH.md)

小图上增加到六跳，最优单笔从持有的 0 变为 +134。另一个固定 600 单位投入的夹具中，最佳单路 +42，穷举得到拆分合流 +116；两支路依序触碰同一出口为 +112。错误地给两支路重复使用初始出口储备会高估 15。费率、单位、余额守恒与全体分配见[可复现的六跳和分流对照](MULTIHOP_TOY_CONTROLS_20261122.md)。

Aleph WSL 上 RTX 4070 Ti 与 i7-13700 完成六组合成基准。50,000 条给定路线 × 16 档金额、8 线程时，GPU 含传输和 CPU 精算的 p99 为 2.427 ms，最快 CPU 比较臂为 2.680 ms；p50 由 1.474 升至 1.701 ms。六组与全金额网格一致，512 和 5,000 条路线时最快 CPU 的 p99 更低。单轮实测未达 30% p99 门槛；缺真实机会密度、单位电耗和按时完成整笔复核的分母。常用扫描保留 CPU。[CUDA 方法、命令与结果](MULTIHOP_CUDA_20261122.md)

## 验证与下一步

| 检查 | 最终结果 |
| --- | --- |
| `make check` | 退出 0。Python 6,543 项及 138 个子测试通过，既有进程恢复测试有 10 条多线程 fork 弃用警告。仪表盘检查通过；Foundry 17 组共 125 项通过。最终文件另跑 `make lint format typecheck`，886 个文件格式通过，361 个源文件通过 mypy。 |
| `make rust-check`、release 构建 | 均退出 0。Rust fmt、Clippy、23 项测试通过；release 使用 `cargo build --locked --offline --release -p oasis-core --manifest-path crates/Cargo.toml`。 |
| `make security-check`、`make contract-security-check` | 均退出 0。锁定依赖审计无已知漏洞；Bandit 当前严重级别门槛和密钥扫描通过。Slither 在 98 个合约报告 6 个提示，低级调用、汇编、命名及复杂度等已有提示保留，未修改 Solidity 合约。 |
| 策略聚焦与文档检查 | 9 个相关 pytest 文件共 151 项通过；WETH 与 USDC 分别由清单离线复跑；文档相对链接及 `git diff --check` 通过。 |

初次在沙箱中运行 `make contract-install` 和 `make security-check` 因包注册表访问受限退出 2；按锁定依赖联网重跑均退出 0。首次本地 Anvil 夹具受回环权限限制退出 2。沙箱内 `make check` 的 socket/Anvil 测试共有 28 项失败（退出 2）；整理导入排序后在允许本机 socket 的环境全量复跑通过。实际终端日志在本机 `/private/tmp/oasis-nov22-{npm,security,check2,final-check,final-check2,security-final,contract-sec,rust}.log`，业务结论只使用已复核结果。

下一项采样应预先固定 Base 池集、窗口、两个来源及各自的累计请求预算，报告真实可用状态到整笔复核的 p99、同等时限下的正净候选数和明确的未复核分母。精确 CL initialized tick、运行时代码身份、补齐历史交易前状态与独立复核是最先要补的数据。六跳与分流的合成夹具可以持续作回归对照。


**下一窗口的判定方式。** 先冻结 discovery 后的池身份、允许的锚定币与名义金额区间，再按连续区块划分调试段与留出段。每个唯一块分别报原始候选、按预算截断、逐腿通过、整笔通过与过期数。两臂使用相同候选复核配额，并配对比较 200 ms、1 s、5 s 内通过的候选，以及延后 1/2/5 块重估的剩余净额。同块共享池的多个候选先按兼容性处理；无法定价的付款、融资或竞争损耗单列未知。若缺真实正例只发布运行时与覆盖率，不能用零分母通过经济门槛。GPU 是否投入后续工程取决于完整路径中可并行计算所占比例。
