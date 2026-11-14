# Elysium 同链闭环套利验证

记录日期为 2026-11-11（Asia/Shanghai）。当前入口以留存证据验证固定块末报价、历史
收据、块末无签名整笔调用和事件触发的报价。真实前瞻采集与实际执行竞争仍待验证。

## 历史收据与状态边界

两个公开端点返回相同的 `3628114–3628116` 三个块头和两笔收据，底层节点独立性未知。
主源完整块确认误操作位于 `3628115` 最后一笔（索引 26），赢家位于 `3628116` 索引 5。
三块时间戳均为 `1794272403`，无法用其测量观察者实际得到的时间。
[误操作交易](https://elysiumexplore.com/tx/0xbdfce4e2f04b27b266d61a77077d2e857a0c69b66524742396e27c30df233a59)
[赢家交易](https://elysiumexplore.com/tx/0x4c88853a130363551076e2c66ae13bcd2fcb049b43494d615fc4aeb76d478ef7)

- 误操作花费 1,097.108842913299341565 WHYPE，实际收到 4.896343 USDC，最低输出
  为 4.884102 USDC。两笔输入资产费用转账与池的 `Fees` 事件一致。
- 赢家投入 0.064364289869769402 HYPE，依次得到 11.265597392481973137 KNTQ、
  4.955656 USDC 和 1,093.818002962823959343 WHYPE。收据显示最后一笔 WHYPE
  全额销毁。按 1:1 赎回假设，扣本金和 0.006007979929439563 HYPE 的收据 gas 乘积后，
  条件净额为 1,093.747630693024750378 HYPE。原生到账、额外费用和窗口总收益未知。
- 误操作的 Sync 记录 291 USDC 最小单位和 1,093.882426081641751799 WHYPE。应用赢家
  的净转账后，与第二个 Sync 的 4,941,081 USDC 最小单位和 0.064423118817792456 WHYPE
  一致。三个历史块的独立储备读取、部署身份和各规模报价仍缺。

| 执行状态 | 复核结果 |
| --- | --- |
| `3628115` 块末 | 单源固定哈希 `eth_call` 使用 `requireCanonical: true`，绑定原调用者、执行器、calldata、value、gas、两项费率和 access list。返回 `1093753638672954189941`，等于收据中 WHYPE 输出减本金。执行器原生预存余额覆盖本金。调用的实际 gas 和原生余额差额未知。 |
| `3628116` 赢家索引 5 前 | 精确状态未重建。须按 Nitro 语义从父块末重放索引 0–4，包括第一笔 `0x6a` 系统交易，或取得具有同一语义的历史重执行 trace。 |

首轮公开探测未取得目标块，档案节点返回历史区块拒绝，官方接入探测在链 ID 处传输失败。
后续取得上述头和收据，完整 trace 探测仍在传输层停止。再次申请历史元数据时，自动审查
无法验证账本与原授权范围的绑定，命令未执行。原始证据、诊断与累计账本在本机留存。
[EIP-1898](https://eips.ethereum.org/EIPS/eip-1898)
[Nitro 执行语义](https://docs.arbitrum.io/how-arbitrum-works/inside-arbitrum-nitro)

### 2026-11-14 赢家前序收据

使用免费 [PublicNode Elysium](https://elysium.publicnode.com/) 的新收据查询范围，
固定原留存完整块 3628116 的哈希和索引 0–5 的六笔交易。九次预算请求全部成功，
原 trace 尝试和原账本仍保留。赢家收据经同一解码器与留存官方收据逐字段一致。
索引 0 的系统交易类型为 `0x6a`，无日志。索引 1 有七条日志，2–4 无日志。
索引 0–4 均未见赢家三池地址发出的日志。精确 prestate 仍需核对系统交易和这五笔的
实际读写、区块内执行上下文及独立来源。零日志的交易仍可能修改影响执行的状态。

原始证据保存在 `reports/research/aave-sale-20261114/elysium-prefix/`，capture SHA-256
`e652cf12deedffd063915fd860edf3fd5fe4cddd4466f8b27bbc7129e1cd32fa`。
对照报告是同级目录下的 `elysium-prefix-comparison.json`。底层节点独立性未确认。

后续取得两块全部收据并核对交易根、收据根和 ArbOS 参数。见[Nitro 核验](ELYSIUM_NITRO_COMMITMENTS_20261114.md)。

## 报价与发现

- 计划限定 native → ERC-20 → ERC-20 → wrapped native，再做已审核的 1:1 unwrap。
  每个规模独立使用同一块末，逐跳保留身份、原始报价和失败。计划校验池、Quoter、资产代码
  及精度。未知 hook、动态费、无匹配代码的部署保持不可报价。净额单列 gas、builder、
  协议和融资成本，资金约束包括库存、已有承诺与 gas 预留，未知输入继续标为未知。
- 封存的 shadow 窗口绑定计划、来源、父块哈希、范围、截止时间和请求上限。原始范围证据
  通过开闭块头及日志校验。V4 `Initialize` 发现限定在已知 PoolManager 和允许资产，
  核对 PoolKey、PoolId 与初始价格；LP 权限、可用深度和部署身份需额外证据。新池须纳入
  后续冻结计划才能报价。
- 同块 Swap、流动性变化和 Sync 合并为一次刷新，按受影响池筛选计划内路径。报价必须与
  范围证据的块号、哈希、父哈希和时间戳一致。缺已知池报价过滤时清空候选；单独缺
  `Initialize` 时已知池仍可复核。可选的封窗全扫覆盖静默或未监听的变化。

两块、两条路径、三个输入规模的合成窗口使用八个过滤和 40 条留存通信。每块全扫有
12 个计划报价点、74 次本地请求查找，事件筛选有 3 个点、36 次查找。两种模式共同
可报价部分的最佳条件净额均为 64,648 native raw。查找含身份检查及缺失数据，范围
采集另计；实验网络请求为零。测试覆盖分叉、无效 PoolId、缺过滤、过期、超预算和静默窗口。

下一阶段用 V3 `PoolCreated` 与 `getPool`、已验证的 Solidly 工厂事件补全池索引。
V4 用 StateView 核对活跃流动性和 ticks，用路由成交与准入证据校验 LP 价值。实时订阅
唤醒后，以重叠的 HTTP 范围补齐断线或重组；缓存按块哈希、合约身份、方向、规模和
caller/hookData 隔离。冻结共同窗口后，对比轮询和事件筛选的覆盖率、首次事件至报价及
模拟延迟、每个完整候选的请求数，并保留失败和空窗。真实抢单能力还需测量排序、提交、
实际净结算与失败成本。
[Uniswap StateView](https://developers.uniswap.org/docs/protocols/v4/guides/state-view)
[Geth 订阅语义](https://geth.ethereum.org/docs/interacting-with-geth/rpc/pubsub)
[Uniswap Smart Order Router](https://github.com/Uniswap/smart-order-router)

## 离线复现

从仓库根目录执行。输出必须是新文件。合成夹具可直接生成；历史复核需要本机已留存的
公开 RPC 证据，原始通信文件留在仓库之外。

```sh
out=$(mktemp -d /tmp/oasis-cycle.XXXXXX)
.venv/bin/python -m tests.fixtures.cycle_research "$out/synthetic"
.venv/bin/python -m scripts.run_cycle_research \
  --plan "$out/synthetic/plan.json" \
  --export "synthetic=$out/synthetic/synthetic.json" --output "$out/quotes.json"

.venv/bin/python -m pytest tests/unit/test_cycle_shadow.py --basetemp "$out/fixtures"
fixture="$out/fixtures/test_v4_discovery_and_affected0"
.venv/bin/python -m scripts.replay_cycle_shadow \
  --cycle-plan "$fixture/cycle.json" --window "$fixture/window.json" \
  --store "$fixture/evidence.db" --refs "$fixture/refs.json" \
  --export "synthetic=$fixture/quotes.json" --output "$out/shadow.json"

evidence=reports/runs/elysium-evidence-20261110
.venv/bin/python -m scripts.replay_cycle_case \
  --plan configs/cycle-research.elysium.example.json \
  --primary "$evidence/official" --secondary "$evidence/boxa-secondary" \
  --block-end "$evidence/official-sim-1" --output "$out/case.json"
```

`run_cycle_research` 可用重复的 `--export SOURCE=PATH` 读取结构化 JSON-RPC 对，单条
记录包含 `endpoint`、`fetched_utc`、`request` 和 `response`，也可用
`oasis-parsed-rpc-bundle-v1` 封装为 `records`。报告只输出端点摘要。截止时间依赖
导出声明，真实前瞻验证仍需独立的时间锚。缺证据与无机会是报告状态，CLI 退出 2 表示拒绝
输入或无法发布。

首次全量检查因沙箱限制无法绑定本地 Unix socket，依赖审计首次也因网络限制失败。
允许本地 socket 和审计网络后，两项均通过。收据、发现和合成报价的各阶段报告及失败
日志留在本机；最后一次发现验证对照结果可逐字节复现。

清理旧原型后的聚焦回归通过 43 项测试，三个文档入口均已离线执行，真实案例报告与
留存版本逐字节一致。`make check` 通过 5,555 项 Python 测试、138 个子测试、前端检查
和 70 项 Foundry 测试。`make security-check` 通过，Slither 扫描 80 个合约，报告 0 项发现。
