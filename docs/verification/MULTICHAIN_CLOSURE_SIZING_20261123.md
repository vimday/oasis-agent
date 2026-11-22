# 中小池闭环增量、整数金额认证与 Robinhood 连续性

北京时间 2026-11-23。沿用已留存的固定块和公开原始数据，比较扩大池集后能增加多少闭环，以及边际正值在有限整数输入和费用下还能留下多少。此次复算无新增 RPC。所有输出属于研究，未产生签名、广播或生产执行权限。[固定块与采集范围](MULTICHAIN_TAIL_DEPTH_20261123.md)

## 用闭环增量评估新池

同一块上固定已有池为基线。新池两个端点若已在同一连通分量，则单池可以闭环；若两池连通同一对分量，则同时补两池可形成闭环。基线中的返回路径取自按同块储备代理排序的最大生成森林，其中有未知深度则保留未知。每条路径保留连续代币、实体池身份和新增池集合。两池组合的总可能数与实际检查数分开，排名预算耗尽时保留进度。当前研究比较一到两条新增边及一条具体的基线路径。[扩池分析入口](../../scripts/replay_v2_pool_expansion.py) · [算法](../../src/oasis_agent/fastpath/pool_closures.py)

| 固定块 | 基线池 | 补充池 | 单池闭环 | 两池组合闭环 | 在一或两条新增边的口径下没闭环的补充池 |
| --- | ---: | ---: | ---: | ---: | ---: |
| Ethereum 26,471,850 | 45 | 753 | 0 | 4 | 745 |
| BSC 135,110,916 | 115 | 453 | 0 | 5 | 443 |

Ethereum 基线用原始两厂报告中实际出现的池身份。抽样范围中有一池与基线重叠，使用发现分层标签会把它错算为新增池和新增两跳环。当前代码验证基线文件的指纹及其在原采集范围中的绑定。BSC 的 115 池基线取同一固定块的具名资产池。两条链的九组闭环各向复算时，四组 Ethereum、五组 BSC 都没有覆盖参考价下 21,000 gas 的最低情景成本。这个结果支持按可新增的闭环连接发现池，样本来自工厂分层抽样和一个块末状态。

## 在 2–400 个不同池上做整数报价和最优认证

V2 每步用明确的费用分子、分母、储备及方向报价，输出代币最小单位向下取整。不同池的未取整曲线复合后仍形如 `Ax/(B+Cx)`，可同时给全金额上界和给定资金区间的上界。用实际整数输出验证单调平台的最小输入，优先处理仍有潜力的金额区间。报价与搜索分别限次，预算耗尽时逐区间给出未决上界。从同一锚定资产单位扣调用者提供的总成本。资产重访允许，池重访使用独立的有状态实验。[定价实现](../../src/oasis_agent/fastpath/v2_sizing.py) · [固定种子穷举、预算截断和 400 跳回归](../../tests/unit/test_v2_sizing.py)

- **Ethereum 四跳。** 70 次逐跳报价在 1 wei 至 10 WETH 区间认证了精确最优，比此前枚举 174,500 个中间输出状态得出同一最优报价。投入 49,460,114,820,510 wei，池费与滑点后毛差 2,860,521,360,232 wei。按 21,000 gas 情景会显示 +10,335,651,232 wei；按所选交易封装至少 25,076 gas 核算为 **−542,871,822,572 wei**，尚未计剩余执行 gas。40 跳报价模型确定最优毛差也为负，49 跳证实无正整数到账。
- **BSC 四跳。** 21,000 gas 参考价情景模型净额约 +200,012,214,061 wei，128 次报价后剩十个未决区间。未决上界离已找到的最佳值只有 1 wei。按 60,000 gas 情景，全金额上界转为 **−1,749,987,785,938 wei**。这两个成本都属于固定情景，完整 EVM gas 没有测量。
- **BSC 14–123 跳。** 14 跳仅需一次整数报价即认证整个 1 wei 至 1 WBNB 区间，最好毛差 **−1,015,636 wei**。15 和 16 跳用整条路径的需求反解，证实任何 `uint256` 正投入均不能产生非零结算资产。对已留存的 100、101 和 123 跳见证，两个方向的全金额模型上界也都未覆盖仅 21,000 gas 的参考成本。

同一台开发机、同一路线与金额区间交替运行两种求解器各九次。旧中间输出枚举中位约 980.6 ms，新求解中位约 0.267 ms，金额和毛收益都相同。新臂用已构造的路线，计时只覆盖整数寻优；采集、拓扑、路线构造及 EVM 未计时。仓库测试与实验同时运行。结果只适用于这条储备浅、整数平台多的四跳样本，原始计时保存在本地 `eth-solver-timing.json`。

21,000 gas 情景用于提前排除明显无容量的路线。通过这个下界的样本仍须承担 calldata、EVM、其他付款、真实转账行为及排序相关的适用成本。单链完整费用通过前的模型正值不进入已验证净额分母。[固定成本对 CFMM 路由的影响](https://arxiv.org/abs/2204.05238)

## Robinhood 的可恢复连续游标

SQLite 回放入口接收留存的 Nitro 解码及与其指纹匹配的官方 RPC 锚定。只发布连续前缀内的块提示，断号后缓冲后续消息并撤销当前最新提示，补齐缺口后继续。相同序号重复导入可去重；同序新哈希、已见区块哈希复用或 RPC 与 feed 冲突会撤销当前提示并持久化冻结。feed 声称的哈希和官方 RPC 同运营方印证有独立字段，交易位置只来自对应锚定。[适配器](../../src/oasis_agent/evidence/robinhood_nitro.py) · [回放入口](../../scripts/replay_robinhood_nitro_hints.py)

用 144 个原始主网帧复核到 136 个连续块提示、371 个交易哈希。其中 2 个块、5 个交易位置与官方 RPC 留档一致，其余 134 个块仍为 feed 声称。重启重导生成 0 个新增、识别 136 个重复。移除真实序号 120,740,553 后缓冲 90 条后续消息，补回后可恢复完整连续前缀。同序异哈希对照撤销提示且重开进程仍冻结。未验证 feed 签名或独立最终性。[原始帧取样](ROBINHOOD_NITRO_FORWARD_20261123.md)

Ethereum 公共读取器也增加只读方法严格校验、原 SQLite 预占时钟核对和 RPC 畸形响应拒绝。Router02 对照逐跳比较返回。三组已留存采集及两组离线诊断按相同输入复核，成功输出逐字节匹配。

## 重放与保留证据

研究入口 `scripts.replay_v2_pool_expansion` 接收以前留存的 Ethereum `graph.json` 或 BSC `result.json`。Ethereum 同时传入对应 `scope.json` 与原两厂基线。可选路线文件以 `snapshot_sha256` 绑定快照，每条记录有 `id` 和按方向排列的 `pools`。完整本地复算、路线文件和固定成本比较保存在忽略目录 `reports/research/multichain-closure-solver-20261123/`。本报告不包含原始 feed 数据、链状态导出、SQLite 或钱包材料。


无网络复算示例，其中路线文件为可选。

```sh
root=reports/research/multichain-depth-20261123
prior=reports/research/multichain-forward-20261122
.venv/bin/python -m scripts.replay_v2_pool_expansion "$root/ethereum/graph.json" \
  --chain 1 --eth-scope "$root/ethereum/scope.json" \
  --eth-baseline "$prior/ethereum-wide/result.json"
.venv/bin/python -m scripts.replay_v2_pool_expansion "$root/bsc/result.json" --chain 56
feed="$prior/robinhood/bounded-validation"
.venv/bin/python -m scripts.replay_robinhood_nitro_hints \
  --checkpoint reports/research/robinhood-hints.sqlite3 --rpc-capture "$feed/rpc" "$feed/decode.json"
```


| 本地复算结果 | SHA-256 |
| --- | --- |
| `ethereum-routes.json` | `fb081e1b519bb290fdb36395b005cd15c964f846f6a426f4052c8b9aef21ae21` |
| `eth-intrinsic.json` | `d1eb838b68a52b0dd510c7c161101156905edc318e0479e3ce5185cf49d39b38` |
| `eth-enclosure.json` | `34fff21f80572d678f0babdb80e112d220801636cfadffd60739ba2abfbdf45e` |
| `eth-solver-timing.json` | `e5bc1608170f54c7238a632a2d41b170fa704428c302a96199ab5731f28baeca` |
| `bsc-routes.json` | `c091444a57c631bb80c740533dcfd64d81171b6d0fc183d4f20ec84ccbf3aaf3` |
| `bsc-intrinsic.json` | `fe10e46d1636248d8209063a06e6b0761430ed828226a2eb9fa1f40cd98a6e72` |
| `bsc-gas-sensitivity.json` | `d3ee45ceb21a1bc56d52d585a4256baf428889dd934d5f139cd5bd67686e5491` |
| `robinhood-first.json` | `a3ee2c315b5d1f5e7e3ee1d1bd1bb4ce302ae6a6ad90540b8ab1d51cec49e3f4` |
| `robinhood-restart.json` | `f45c1397a0403f05de376e0f21bdcb502015db587c2ec70459e273e7b5e61972` |

## 提交前验证

`make check`、`make security-check`、`make contract-security-check`、`make rust-check` 最终均退出 0。Python 6,593 项和 138 个子测试通过，仍有 10 条既有警告；Dashboard 检查通过，Foundry 125 项、Rust 23 项通过。依赖审计及密钥扫描通过，Slither 在 98 个合约报告 6 条既有提示。32 项相关代码回归、保留证据复算、JSON 解析、244 个相对链接和 `git diff --check` 通过。运行机日志为 `/private/tmp/oasis-closure-{check-final,security-final,contract,rust}-20261123.log`。
