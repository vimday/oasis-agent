# Metric 目标部署与本地状态执行

2026-11-14，基线 `67078d6`，Codex 作者/内部自审，无独立 reviewer sign-off。
承接[调用包与回滚解析](METRIC_PROBE_CODEC_20261114.md)。本轮有两种严格分开的证据：
目标块五个部署 runtime 的真实单源读取，以及合成 genesis 上实际 Anvil IPC 调用。
原字节和日志在 `reports/research/metric-local-state-20261114/`；没有资本操作或生产变更。

## 原范围内的真实部署观察

复核原[四链部署范围](PUBLIC_RPC_AND_METRIC_IDENTITY_20261114.md)：Ethereum 的
`deployment_profile_1_20261114` 未 STOP、有效期至 2026-11-14T08:58:08.632103Z，
只研究对应链块头与原五个官方部署地址。没有把剩余额度用于其他池、provider、报价
或已失败的上下文补读。旧 provider、Metric quote、Titan 及 context 范围均未重启。

在 I/O 前保存 `target-deployment-lock.json` 和固定计划。沿用原 scope、原 ledger
identity、原 32 请求上限：既有 10 次，本次最多 8 次，最坏累计 18/32。来源仍是已核实
免费匿名的 PublicNode Ethereum，单次与合计费用 USD 0；第一异常即停止，无 fallback。
scope SHA-256：`b45a8dd1f53267fbf3b3e77ae17d37dc65b3f6c91223b7823ea3eda0306f402d`。

实际八次全部完成（chainId、首块头、五 getCode、尾块头），原账本累计 **18/32**。
固定目标块 26410517，hash
`0xcceb62b4706283b85b7312cabb2ccdff795f2ebec2d5a6026b3f3e4c5a01e379`。
首尾块头一致，原始字节回放的 retained_bytes / original_http_bytes 均通过。

| 地址 | 对象 | runtime bytes | 与原块 26410382 |
| --- | --- | --- | --- |
| `0x2a53833cc95548cf52c7b159110e22d3a9018f32` | factory | 21,451 | 全字节相同 |
| `0x3b9ea79f482094323d06064e637a7f4528560fcb` | quoter | 8,276 | 全字节相同 |
| `0x02201e6f13c41506c7bb635d7543f98826229ab7` | router | 15,487 | 全字节相同 |
| `0xffc65b3bef0544b2dbfa40deea02c61f60397958` | liquidity adder | 9,872 | 全字节相同 |
| `0xe844701934d9c6085b292e428f41b911cc0b3872` | compressed oracle | 8,662 | 全字节相同 |

各 runtime keccak 与原验证表一致。这里的 compressed oracle 不是待研究池的
`0x30a88b…` pyth-lazer oracle；未由此验证目标池、custom source、代理/配置、权限、
价格或可成交性，也没有获得第二个独立来源。

新 capture 路径仍在原 campaign 下：
`reports/research/next-stage-20261114/multichain/1-campaign/target-26410517-deployments/`。
capture.json SHA-256：`5df60b6e0ddab8a04cfd4b94d0eee9e6f50eec16756cff3bd288dde2f20c00cb`。
真实分析保存于 `target-deployment-analysis.json`，未复制或替换原预算账本。

## 正常本地执行入口

新增 `scripts.run_metric_local_probe run|replay`，复用原 probe 的 ABI、完整 immutable
runtime 重建和严格结果解释。`run` 只接受 `synthetic_fixture` 状态 image 与
`synthetic_local_evm` 模板；历史声明会被拒绝，不能给缺失历史槽补零后宣传成交。

image 显式列出最多 64 个账户、code/nonce/native balance 与每账户最多 256 个非零
storage word。缺省槽按合成 genesis 的零槽处理；四个目标角色 code hash 必须匹配，
probe 与 observer 均不得已在 genesis 中。所有资产、数量、storage、地址及 schema
遵循严格类型与大小限制。本地 genesis 有自己的块 hash：仅在链号/虚拟块号/时间核对
后生成新的 local_plan，保留原模板，不把本地 hash 写回真实历史证据。

启动参数固定：零解锁账户、Cancun、禁止自动出块、loopback 随机 HTTP 端口与私有
临时 IPC、无远程 fork、无默认 create2 deployer。只继承 PATH/TMPDIR/LANG，不载入
wallet `.env` 或 RPC 凭据；子进程 stdio 不输出钱包材料。入口不接收 URL、通用 RPC
方法、任意 override、签名或广播命令。记录 Anvil binary hash、实现 hash 与 child PID /
IPC 身份，正常和异常路径都 terminate/wait，必要时 kill/wait；不操作别的进程。

父进程被强制 SIGKILL 时不能保证 Python finally 执行。`child.json` 与未完成 manifest
用于后续独立核查；不按旧 PID 自动杀进程、不自动恢复或覆盖失败目录。本轮实际节点
均已取得退出状态，不能把这一结果推广成任意硬崩溃都能清理。

固定最多 60 秒、11 次本地请求：client version、零账户、链号、块头、完整 state dump、
空账户观察、空 code、一次带受限 override 的 eth_call、完整 state dump、空账户复核、
末块头。先比较 genesis 的完整账户集/余额/code/storage 与节点 dump；隐含账户、缺项
或任何值不同均在 probe 调用前拒绝。空账户检查同时要求 nonce、native balance、code
和 storage root 为空；返回 proof 保留，但不宣称已独立验证 MPT 证明。

唯一 override 是已证明在本地 image 中不存在的 probe 地址：固定 runtime 和空 state。
不覆盖其 balance/nonce，不改 pool、oracle、token、虚拟块参数，不调用持久 setCode。
eth_call 的 from/to/value/gas/calldata/pin 全部派生自固定计划。只有完整 ProbeResult
通过参数和逐资产数量核验、完整压缩 state dump 前后逐字节相同、probe 仍为空且首尾
header 相同，才返回 `local_probe_observed`。结果始终无资本或部署授权。

## 实际合成 EVM 验证

新增 Foundry `testExportLocalStateImage` 从全新合成 token/pool 构造器状态导出 code、
nonce、余额及非零槽，断言 mapping 余额槽与 pool 比率槽对应实际值。沿用四方向 probe
字节向量，不复制真实 Metric/V3 数学或历史流动性来伪造成功。

本机 Anvil 在空 alloc 的 Cancun genesis 中会加入
`0x000f3df6d732807ef1319fb7b8bb8522d0beac02` 系统账户。首次观察保存在
`anvil-experiment-*`；正式合成 image 显式包含该节点观察到的系统账户。去掉它的负例
在第 5 次 dump 检查被拒，尚未执行 probe。此现象也说明不能仅凭本地 header hash
就宣称账户集等于历史状态。`genesis_root_independently_verified` 保持 false。

四种两池顺序/资产方向均通过正常 CLI 实际 IPC 执行及离线回放；每次 11 次本地请求、
child exit 0、完整状态不变、probe 调用前后为空。合成毛差额均为 100 起点资产基础单位，
由 fixture 比率决定，不是市场发现或美元利润。缺系统账户负例退出 3、child exit 0，
同目录重跑退出 2，原结果 hash 不变；重复离线回放逐字节相同。

最终有效实例前缀为 `framed-local-0` 至 `framed-local-3`，负例为
`framed-missing-system-run`，汇总 `framed-local-run-summary.json`。原本地实验与早期验证
均保留，不能混入真实链样本数。Anvil 运行、状态内容和 RPC 回包是真实本地执行；
genesis、池数学、库存和虚拟时间是合成输入。真实历史状态、独立来源、完整交易 gas、
净收益及实际结算仍未验证；这些不是 deploy/live 准入证据。

```sh
research=reports/research/metric-local-state-20261114
.venv/bin/python -m scripts.run_metric_local_probe run \
  --plan "$research/local-0-plan.json" --image "$research/synthetic-image.json" \
  --artifact "$research/probe-artifact.json" --out "$research/another-local-run"
.venv/bin/python -m scripts.run_metric_local_probe replay \
  --capture "$research/framed-local-0" --output "$research/another-local-replay.json"
```

## 失败与修正记录

初次脚本类型检查发现三个 object 类型边界，已用明确类型和严格整数/digest 验证修正。
首次本地 run 成功，但请求日志未包含实际发送的换行，不将该旧目录当成完整 IPC 帧证据。
随后单独的分包回归证明：若 JSON 与末尾换行分两次 recv，旧读取逻辑提前结束；
`split-frame-regression-initial.log` 保留退出 1。修复后必须收到完整单行帧，请求逐字节
等于固定编码，响应包络/ID、时序、原字节 hash、输入/构建/子进程身份及最终解释均重新
验证。坏返回、时间倒退、hash 修改或未完成 capture 不沿用旧成功结果。

聚焦 Python 118 项通过，含 35 项新增 state/IPC/生命周期用例，覆盖隐含账户、历史
输入拒绝、非空 probe、状态变化、frame 分包、部分响应留存、压缩膨胀上界、启动失败、
kill/wait、环境过滤、目录排他、请求重写、时间/类型冲突和结果篡改。unit FakeRPC 明确
不执行 EVM；上面的实际 Anvil 路径补上其不能证明的运行证据。

第一轮 `make check` 退出 2：6,114 项通过，LP 恢复测试一项拒绝。检查运行期间本轮
修改了 `scripts/run_metric_local_probe.py`；恢复入口按既定规则固定所有 src/scripts，
因此正确报出 `run code/config/input changed`。`full-gate-code-drift.json` 与原 fixture
记录确认唯一漂移为该新脚本。保留 `make-check-final.log`，没有放宽恢复保护；冻结源码后，
原失败用例独立重跑通过（`lp-resume-focused.log`，退出 0）。

冻结最终源码后，检查及实际退出状态如下。日志均在本轮忽略目录，未以首轮失败记录
替代最终门禁；`reviewed-code-final.json` 中四个代码/测试 hash 在最终检查后保持不变。

| 检查 | 退出 | 结果 / 日志 |
| --- | --- | --- |
| 聚焦 Python | 0 | 118 passed，含 35 项新增；`python-focused-framing-final.log` |
| Foundry 合成状态/向量导出 | 0 | 2 passed；`forge-state-vectors.log` |
| 实际本地执行与回放 | 0 | 四方向各 11 IPC、负例 5 IPC，五个 child 均退出 0；`framed-local-run-summary.json`；负例按预期退出 3，同目录再运行退出 2 |
| CLI 额外类型检查 | 0 | `script-types-final.log` |
| `make check` | 0 | 6,116 Python tests + 138 subtests、9 个前端模块、82 Foundry tests（CI profile），lint/format/typecheck 通过；`make-check-reviewed.log` |
| `make security-check` | 0 | 依赖审计、Bandit、secret gate；`security-check-reviewed.log` |
| `make contract-security-check` | 0 | 85 合约 / 102 detectors，零未抑制 findings；`contract-security-reviewed.log` |
| 文档链接/命令 | 0 | 4 份文档、52 个本地链接、5 个 shell 语法块、1 个 CLI help；`docs-check-reviewed.log` |

全量 Python 保留 11 项既有弃用警告。聚焦 Foundry 曾提示本机 signature cache 目录不可写，
该运行仍退出 0，原日志保留。本机 Python 3.12.4、Node 23.11.0、Foundry/Anvil 1.8.4
Homebrew；没有将本机环境描述为 CI 固定的 Node 22.14.0 / Foundry 1.8.1。
最终本地证据复核见 `reviewed-local-evidence.json`；作者自审不代替独立 reviewer sign-off。

下一依赖仍是独立的真实 canonical context、目标 pool/provider/config 和完整历史状态。
当前 loader 有意拒绝历史 image 声明，真正历史 fork/证明装载仍未完成；其余 Aave、
Elysium、前瞻 shadow 和净经济验收继续见[持续清单](../strategies/NEXT_STAGE_EXECUTION.md)。
