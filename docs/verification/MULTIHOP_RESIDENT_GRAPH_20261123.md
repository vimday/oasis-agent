# Rust 常驻池图与多锚扫描

2026-11-23 · 首轮离线影子验收。Rust 池图在会话中复用池状态、拓扑和边索引。已知池的报价状态变化只刷新该池的两个方向边。多锚点在同一固定块扫描，各自使用原始输入区间、费用、最低净额和结果顺序。Base 六个留存块的本地扫描 p99 从 0.722 ms 降到 0.200 ms，三组对照的整数结果全部一致。[架构决策](MULTIHOP_RUST_BOUNDARY_20261123.md) · [Rust 池图](../../crates/oasis-core/src/graph_arb.rs)

## 状态边界

ABI v1 返回版本、构建指纹、容量和能力。Python 会话在返回结果前核对会话 ID、链号、块高、块哈希、父块哈希、池行数、有序锚点和定量未决区间。块内池数据来自已经验过的调用方快照。会话以同链直接子块推进，原子应用成批池变化。调用方负责验证省略的池确实未变，连续父块只证明输入的块关联。未知池、改变池身份或区块谱系断开时，保留旧池数据并关闭扫描，直到调用方从同链完整快照显式替换。空增量也推进块绑定。重新载入和关闭均有确认。原逐锚调用可作为完整对照。新入口范围为只读离线扫描，风险、预算、签名和结算沿用各自的流程。[FFI](../../crates/oasis-core/src/ffi.rs) · [Python 会话](../../src/oasis_agent/fastpath/resident_graph.py)

## 同输入 A/B/C

Mac Studio M1 Max，arm64 macOS，CPython 3.15.2，rustc 1.100.0 release。每组 60 轮，先热身 5 轮，循环调整各臂顺序。逐锚全图、驻留全量替换、驻留增量三个臂都用同一个 release 库、池状态、各锚输入、费用和上限。保留候选与未决区间的行序、每腿金额、每个候选的净额和环路计数，比较时只忽略时钟。定时从已解码的校验状态开始。后续块耗时含 Python 计算增量、应用更新和双锚扫描，首块含会话协商和首次扫描。计时区间内 RPC、Quoter 和整笔 EVM 模拟为 0。重放样本产生描述性延迟分位，块数按实际独立状态计。[实验 CLI](../../scripts/benchmark_resident_pool_graph.py)

| 数据与阶段 | 逐锚全图 p50 / p99 | 驻留全量 p50 / p99 | 驻留增量 p50 / p99 |
| --- | ---: | ---: | ---: |
| Base 58 池，双锚首块，60 样本 | 0.665 / 0.757 ms | 0.382 / 0.426 ms | 0.380 / 0.443 ms |
| Base 58 池，双锚后续五块，300 样本 | 0.644 / 0.722 ms | 0.364 / 0.463 ms | **0.157 / 0.200 ms** |
| Base 66 池含 V4，双锚单块，60 样本 | 15.342 / 16.934 ms | 14.984 / 18.521 ms | 15.170 / 16.863 ms |
| 合成正例，双锚后续三块，180 样本 | 0.563 / 0.687 ms | 0.553 / 0.676 ms | 0.553 / 0.735 ms |

- 58 池使用 Base `61,178,149–61,178,154` 的原始双锚状态。五次相邻块更新为 0、0、1、0、0 个池。驻留增量的后续块 p99 降低 72.3%。逐锚全图每块发 116 行池数据。驻留臂首块发 58 行，后五块合计一行。后续每块仍是两次 FFI，一次推进和一次双锚扫描。首块会话协商、开启、扫描分开计数。六块的正候选和未决区间均为 0。[58 池原始证据](BASE_FORWARD_POOL_COMPARISON_20261123.md)
- 66 池为 Base `61,201,501` 的单个留存状态。三臂的 34 个未决金额区间及 3,716 条环路全等，正候选为 0。单块样本无法度量连续更新。此例 Rust 扫描本身约 14.3–14.5 ms，重算仍占主要成本。[66 池原始证据](BASE_V4_MIXED_GRAPH_20261123.md)
- 四块合成序列包含定价变化、双锚自身费用、16 个正候选的 40 腿和 6 个金额未决区间，全部一致。增量臂在这个定量更重的负载中没有稳定尾延迟优势。回归另覆盖错误 ABI、原子更新失败、错块查询、连续空更新、分叉替换、缺失未决区间和关闭。[Python 回归](../../tests/unit/test_resident_pool_graph.py)
- 额外的 34 池合成差分以每块 1、5、34 个池变化分别运行 13 块，每组插入一次跳块并明确完整替换。39 个状态的双锚逐项一致，覆盖 14,140 条正候选和 15,423 个未决区间。此项只核一致性。忽略目录的 `cache_diff.py` 结果 `cache-diff-sized.json` SHA-256 `a2aa92040cfd4506814ac5627f6946d49ea6cbf2928048d46999a3d6cf604438`。

本轮已验证可复用状态的本地耗时和模型等价。真实经济召回、状态源延迟、整笔结算延迟和后继有利润时长仍待验证。连续前瞻影子应维持同池与同金额条件，把上述本地改进放进完整时钟链；在未证明真实机会以前，继续以离线和只读方式迭代。[多跳 TODO](../strategies/MULTIHOP_ARBITRAGE_TODO.md)

## 复跑与留存

```sh
cargo build --offline --locked --manifest-path crates/Cargo.toml --release
replay=reports/research/base-forward-broadened-20261123/run/successor/multihop_replay/manifest.json
mixed=reports/research/base-v4-mixed-graph-20261123/run/state.json
.venv/bin/python -m scripts.benchmark_resident_pool_graph --snapshot-manifest "$replay" \
  --mixed-state "$mixed" \
  --mixed-state-sha256 3c56cdb11d0fb158b7d515cd4b9d1442784c6d8f6d19685eaf7f2dd83a4dda21 \
  --positive-control --repetitions 60 --warmups 5
```

忽略目录 `reports/research/resident-graph-benchmark-20261123/` 留存报告、时钟、设计和冻结的 release 库。`final-design.json` SHA-256 `17ed220a1a977bf77bcbf165c025ec8437a76b0c1af656a0f2f7ab13469e4f27`，`benchmark-final.json` SHA-256 `50c412b457655a6a5cdc3cb1c52a250d967ed99b768c5e3cee650de9fcc22d94`。动态库 SHA-256 `5d5a19363d67ffa1460834bc513e48eb83e89c92610cd2882fea856ef9186c10`，会话构建指纹 `66197b2afffe971024e9e6d774e11d1ade2f090b52a09e6eb0ca5872b09781c3`。重跑时阶段时钟会变化，整数结果须一致。

`make check`、`make security-check`、`make contract-security-check` 与 `CARGO_NET_OFFLINE=true make rust-check` 均退出 0。通过 6,612 个 Python 测试、138 个子测试、125 个 Foundry 测试和 28 个 Rust 测试。165 个文档相对链接、独立离线 CLI 复跑、原始行情重解码、密钥检查和 `git diff --check` 通过。日志保留 10 条 Python 警告和 6 条既有 Slither 提示，位于 `/private/tmp/oasis-resident-{check,security,contract,rust}-20261123.log`。
