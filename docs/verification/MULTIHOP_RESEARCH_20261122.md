# 多跳套利离线复核 · 2026-11-22

基线 `14260a4`，工作树含研究稿的编辑。以下为本地合成对照与既有 Base 影子归因复核。本轮新增链上请求、签名和广播均为 0。[实施 TODO](../strategies/MULTIHOP_ARBITRAGE_TODO.md)

## 两座 V2 池即可观察到金额搜索漏解

两个池的 A、B 储备分别为 (615, 1497)、(1730, 1521)，两币都是 6 位小数。两腿各收 3000 pips，固定成本为零。所有数字以最小单位表示。枚举 1 到 250 个 A，经第一池换 B 再经第二池回 A。逐跳取整后，投入 204 或 207 的净额都是 135。正常 Rust 扫描入口在门槛为 1 时选 195，输出 329、净额 134；门槛升为 135 时返回空集。这是可证伪的**定量搜索**问题，两条腿的报价可以分别核对。[Rust 池数学](../../crates/oasis-core/src/pool_math.rs) · [金额搜索](../../crates/oasis-core/src/graph_arb.rs)

从仓库根目录用锁定 release 库可复核。命令中的 `assert` 记录本版本行为；修复后最后两项预期应随之改变。

```sh
bench_target=$(mktemp -d)
cargo build --manifest-path crates/Cargo.toml --locked --offline --release -p oasis-core --target-dir "$bench_target"
# 以下文件名适用于本次 macOS arm64；Linux release 库为 liboasis_core.so。
export OASIS_CORE_LIB_PATH="$bench_target/release/liboasis_core.dylib"
.venv/bin/python - <<'PY'
from oasis_agent.fastpath.pool_graph import PoolKind, PoolNode, quote_pool, scan_cycles

a, b, p, q = [f"0x{i:040x}" for i in (1, 2, 11, 12)]
pools = [
    PoolNode(8453, p, PoolKind.V2, a, b, 6, 6, 3000, reserve0=615, reserve1=1497),
    PoolNode(8453, q, PoolKind.V2, a, b, 6, 6, 3000, reserve0=1730, reserve1=1521),
]
def swap(x, reserve_in, reserve_out):
    return x * 997 * reserve_out // (1000 * reserve_in + 997 * x)

profits = {}
for x in range(1, 251):
    y = swap(x, 615, 1497)
    z = swap(y, 1521, 1730)
    assert quote_pool(pools[0], a, x).amount_out == y
    assert quote_pool(pools[1], b, y).amount_out == z
    profits[x] = z - x
assert max(profits.values()) == 135
assert [x for x in profits if profits[x] == 135] == [204, 207]
def scan(threshold):
    return scan_cycles(pools, [a], max_hops=2, min_amount_in=1,
                       max_amount_in=250, min_net_profit=threshold).candidates
assert [(c.optimal_amount_in, c.amount_out, c.net_profit) for c in scan(1)] == [(195, 329, 134)]
assert len(scan(135)) == 0
PY
```

## 同种子 A/B 复跑

同一 release 库下运行 `.venv/bin/python -m scripts.run_multihop_ab_experiment --require-pass`，退出 0。默认种子 20261122，34 池、120 个合成块、每块改变 1 池、最多 4 跳、全扫间隔 200。A 为全扫，B 为变池过滤加缓存。

| 指标 | A | B |
| --- | ---: | ---: |
| 候选条数（含跨块重复） | 2,693 | 2,693 |
| 进入环路评估的次数 | 224,160 | 26,392 |
| p50 / p95 / p99（ms） | 59.83 / 81.10 / 85.17 | 5.22 / 24.89 / 39.43 |

120/120 个块的候选与金额完全相等。本次 p99 降低 53.70%，原记录为 52.99%。B 第一次全扫，后续 119 次增量。计时包含 Python 调用与本地计算，A 总在 B 之前运行，输入已在内存中；实际事件到达、远端报价与整笔模拟未进入计时。两臂共用同一金额搜索，上述最小反例可同时被两臂漏掉。另运行 `.venv/bin/python -m pytest -q tests/unit/test_incremental_cycle_scanner.py`，三项通过，退出 0。[实验实现](../../src/oasis_agent/fastpath/incremental_scanner.py) · [原实验](../strategies/MULTIHOP_ARBITRAGE_RESEARCH.md)

## Base 历史归因

复核本机 `reports/research/` 下各轮的 `shadow_summary.json` 与 `shadow_attribution.jsonl`。Wave 26 和两次 Wave 30 的摘要与逐条归因哈希链相符。下表按区块哈希计数，保留早停轮。来源为历史采样和当时的成本模型，报告完整性与底层链状态的独立确认应分别记录。[历史运行记录](WAVE23_REVIEW_FIXES_AND_ANTITOXICITY_20261121.md)

| 留存目录（相对 reports/research） | tick / 唯一块 | 有正模型净额的唯一块 | 通过逐腿复核的候选条数 |
| --- | ---: | ---: | ---: |
| `wave25-20261121/shadow-bybit-600-r2` | 600 / 600 | 1 | 3 |
| `wave26-20261121/shadow-bybit-900` | 900 / 887 | 14 | 33 |
| `wave30-20261121/shadow-bybit-1200`（早停） | 214 / 214 | 1 | 2 |
| `wave30-20261121/shadow-bybit-1200-r2` | 1,200 / 1,199 | 0 | 0 |

Wave 26 的 160 条原始候选中，33 条通过逐腿复核，另 127 条在该轮每 tick 最多复核三条的上限之外。33 条中三跳 28 条、两跳 5 条。逐腿复核包含相应协议的 Quoter 或本地池数学。相同状态上整笔可执行的候选数、纳入区块的概率及实际净结算仍未知。Wave 30 完整轮报告的 86 笔成交属于跨场所做市影子策略。各轮资产集合、采样间隔与市场状态存在差别，不能合并估算套利胜率。[筛选入口](../../scripts/run_multicall_100tick_campaign.py) · [逐腿核价实现](../../src/oasis_agent/fastpath/multicall_universe.py)
