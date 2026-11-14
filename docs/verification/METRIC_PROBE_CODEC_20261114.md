# Metric 整笔探针调用包与回滚解析

2026-11-14，基线 `3983de1`，Codex 作者/内部自审，无独立 reviewer sign-off。
承接[两池探针](METRIC_ATOMIC_PROBE_20261114.md)。本轮只运行离线 Python 与本地合成
EVM；没有新 RPC 请求、付费访问、远程 override、部署、签名或广播。所有已停止的原
campaign 和累计预算保持原样。原日志在 `reports/research/metric-probe-codec-20261114/`。

## 完成的连接

原探针只能在 Solidity 测试里解释结果，现在正常命令
`scripts.research_metric_probe prepare|replay` 接上固定计划、构建 artifact、调用包与
原始请求/响应字节。它没有网络 client；prepare 只写文件，不发送任何请求。

计划严格固定：链 ID、块号/hash/时间、来源与 endpoint digest、截止时间、probe 和
observer、两池与两资产及 decimals/runtime hash、Metric token 顺序、构建文件 hash、
完整 probe runtime hash、gas 上限及 Request。数量只用整数基础单位，拒绝 bool/float
代替整数、重复/零地址、非 canonical 资产排序、超限输入及 extension。

`metric_probe_build.materialize` 使用明确 hash 固定的 Foundry artifact：验证 Solidity
0.8.26、Cancun、optimizer 200、无 metadata trailer/外部 link、已审阅 probe/SafeCast
源码 hash；按 AST 的名称和类型绑定全部 13 个 immutable 的所有偏移。拒绝缺项、重复、
越界、重叠和非零 placeholder。重建后对完整 runtime 做 keccak，再与保留 getCode 的
完整字节相等比较，没有忽略 immutable 或 metadata 差异来匹配。

artifact hash 是待审阅构建材料的身份，metadata 仍是编译器输出的声明；这不是独立
重编译或编译器诚实性的证明。任意文件附上同样 source metadata 不获得代码信任。
prepare/replay 结果也不证明真实池/代理/config、token decimals、权限或状态来源。

调用包恰为四个按顺序的请求：chainId、固定块头、固定块哈希 getCode、同一块哈希
eth_call。最后一项固定 from/to/value=0/gas/calldata，采用 EIP-1898 requireCanonical；
不接受 latest、额外参数、state/block override 或更改方法/目标。现在也不提供历史地址
注入能力；真实链上没有这个 probe 时不能靠文件把它当成已部署。

## 结果解释边界

保留格式包含四次请求与响应的完整 hex 字节和逐条 received time。每次离线解析重新
拒绝重复 JSON key、ID/type 冲突、结果和错误并存、缺覆盖、来源或时间不一致。严格
比较原请求与生成的请求，并记录原字节 hash；文件回放不能证明真实 HTTP 传输发生。

只接受明确支持的 JSON-RPC `error.code=3` 和直接字符串 `error.data`。其他 provider
错误格式保留为拒绝，不扫描 message 或 nested data 猜测成功。不把普通 result 中的
同名 selector 当作成功。`ProbeResult` 须恰为 selector 加六个 ABI word，request hash
与 Solidity 的 `abi.encode(probe, observer, chain, block, timestamp, Request)` 一致；
核对本金、正数 int128 内的中间/输出量、output-principal=profit、minimumProfit 和
内部 gas 的正数上界。任一错误清空 observed_result，保留失败理由与字节身份。

成功状态名为 `bound_revert_payload`，只说明所给材料中的身份/请求/返回关系相符。
保留双方资产单位；innerGasUsed 不是完整交易 gas，gross 不是净收益。来源独立、原始
transport、历史 state/overlay、目标 config、token metadata、完整模拟 gas 预算、实际
结算和执行准入标志始终 false，净收益和实际 PnL 均为 null。调用包不能授予现有公共
采集器的新方法或重启 STOP 范围。

## 可复现的交叉验证

新增 Foundry `testExportProbeCodecVectors`，在同一实例连续执行四种两池顺序/资产方向，
每次完成真实的本地 callback/transfer/revert 并检查池及探针余额恢复。输出实际 runtime、
calldata、Solidity request hash 和 revert bytes，供 Python 正常入口比较。

这里的池与代币仍是上轮的合成模型；头部、来源和 RPC 包装由本地脚本显式合成。
包装里的 gas 上限用于解析边界测试，Foundry helper 的原调用不是一次完整 RPC 交易
gas 测量。四次毛差额均为固定合成费率产生的 100 起点资产基础单位，不能外推市场。

```sh
cd contracts
forge test --ast --match-test testExportProbeCodecVectors -vv
```

普通 artifact 不含 AST：首次交叉解析退出 1，报 `AST must be a JSON object`。原
`forge-vectors.log`、`probe-artifact-initial-without-ast.json` 和 `vector-verification.log`
保留；补上显式 `--ast` 后重新编译、导出并解析，不修改原失败文件。

四种路线均通过：完整 runtime、calldata、request hash 逐字节一致，实际 revert bytes
解码及本金/毛差额校验正确；八次正常 prepare/replay CLI 全部退出 0。没有把 Python
自行编码后再解码当成唯一 ABI 依据。

```sh
research=reports/research/metric-probe-codec-20261114
.venv/bin/python -m scripts.research_metric_probe prepare \
  --plan "$research/vector-0-plan.json" --artifact "$research/probe-artifact.json" \
  --output "$research/new-packet.json"
.venv/bin/python -m scripts.research_metric_probe replay \
  --plan "$research/vector-0-plan.json" --artifact "$research/probe-artifact.json" \
  --capture "$research/vector-0-capture.json" --output "$research/new-report.json"
```

输出排他发布，不覆写已有成功或失败报告。prepare / 绑定成功退出 0，合法材料里的
拒绝或不完整结果退出 3，坏计划/构建/包络/输出冲突退出 2。所有例子只读本机忽略目录。

| 交叉材料 | SHA-256 |
| --- | --- |
| `probe-artifact.json` | `e9717bbaf9ef385ba6e09b7f58d86ee94041c7b85f205c441a6ba090a1616def` |
| `forge-vectors-with-ast.log` | `52ccacc25e980b9f723bc630a215a38ebbe96ba222c9d7b1a2625791456ef584` |

聚焦 Python 131 项通过，包括 67 项新增包络、构建及数值拒绝用例。测试覆盖 hash 复用、
伪造成功返回、额外 override、错误链/块/code、时间倒退、缺证据、恶意错误文本、build
偏移/类型/源码/编译设置冲突，以及输出冲突不覆盖。首次 lint 的行宽问题已修正。

额外 CLI 类型检查首次因 import 路径设置退出 1，不构成源码类型结论；用
`MYPYPATH=src .venv/bin/python -m mypy --explicit-package-bases scripts/research_metric_probe.py`
定位两处类型问题（module.__file__ 可空、报告字段类型推断），修正后退出 0。保留三个
`script-mypy-*.log`。随后增加 evaluator 自身的 runtime hash 边界，防止绕过 CLI 传入
另一份 runtime；最终代码 hash 保存在 `reviewed-code-final.json`。

同时尝试读取用户线索对应的 Etherscan 交易页，web 工具报不可访问，未取得收据；
`aave-public-page-attempt.json` 保留失败，没有增加 RPC 请求或更改原 Aave 账本状态。

最终审阅代码冻结后，门禁及实际退出状态如下（日志均在本轮忽略目录）：

| 检查 | 退出 | 结果 / 日志 |
| --- | --- | --- |
| 聚焦 Python | 0 | 131 passed；`python-focused-reviewed.log` |
| Solidity 与正常 CLI 字节交叉 | 0 | 四方向、八次 prepare/replay；`reviewed-cli-results.json` |
| CLI 额外类型检查 | 0 | `script-mypy-corrected.log` |
| `make check` | 0 | 6,081 Python tests + 138 subtests、9 个前端模块语法、81 Foundry tests（CI profile），lint/format/typecheck 全通过；`make-check-reviewed.log` |
| `make security-check` | 0 | 依赖审计、Bandit、secret gate；`security-check-reviewed.log` |
| `make contract-security-check` | 0 | 85 合约 / 102 detectors，零未抑制 findings；`contract-security-final.log` |
| 文档链接/命令 | 0 | 4 份文档、47 个本地链接、6 个 shell 语法块、1 个 CLI help；`docs-check-reviewed.log` |

全量 Python 保留 11 项既有弃用警告，未隐藏。前一轮全量也通过，但发生在 CLI/边界的
最后修正前，保留为 `make-check-final.log`，不拿它替代最终回归。本机 Python 3.12.4、
Node 23.11.0、Foundry 1.8.4 Homebrew，不宣称使用 CI 的 Node/Foundry 固定版本。

下一依赖仍是真实目标身份/config、独立 canonical 上下文，以及有证据的本地历史状态
装载或受约束模拟采集。当前 CLI 接口未解除这些缺口；没有 pinned fork、前瞻市场窗口
或经济验收结果。[持续清单](../strategies/NEXT_STAGE_EXECUTION.md)中的其余策略继续保留。
