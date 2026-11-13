# Metric 公开快照与下一阶段研究验证

日期：2026-11-14（Asia/Shanghai）。实施及自审：Codex；没有独立 reviewer sign-off。
任务起始提交 `09fec54`。最终代码提交可由本文件的 Git 历史定位；本记录不自包含自己的提交哈希。

## 研究结果与证据等级

研究设计与优先级见 [Metric 与事件型套利](../strategies/METRIC_EVENT_RESEARCH.md)。本轮增加
`src/oasis_agent/arbitrage/metric.py`、`scripts/research_metric.py`、冻结示例、合成夹具和回归。
没有更改风险阈值、signer、部署或 Base 现货 shadow 边界。

官方文档通过搜索工具无法读取；直接读取公开 `/llms-full.txt` 成功。原始文档留在本机，
SHA-256 为 `9b79984f6d88f8904270befe5e384bf467487048f269cbc725e71d6fa9b14501`。
源码研究固定的 pool/periphery commit 和接口链接在设计记录中。旧内部名 `metric-core`
的 GitHub 探测返回 404，随后从官方文档找到 `pool-contracts`；这不是合约缺失证据。
初次沙箱 DNS 请求失败，公共文档读取在允许网络的环境重做。未触及付费 RPC。

公开 discovery 为一次 Ethereum 元数据 GET，取 TVL 排序前 4 个；返回的 `total=44` 是
提供者声明，并非本轮检查了 44 个池。数据用于选研究对象，没有用来证明流量真实性、
盈利或链上部署身份。第二个研究池来自官方 API 示例，不能假设示例池仍有可用流动性。

随后固定两个 WETH/USDC 池、两个有向池对、三个 WETH 规模：0.001 / 0.01 / 0.05。
本次 capture 只做定向 metadata GET 和 depth=true batch POST，各一次，均 HTTP 200。
捕获时间为 **2026-11-14 01:59:33–01:59:34 +08:00**；两池 serverTs 都为 1794592774。

| 池 | 提供者声明 | 本轮观察 |
| --- | --- | --- |
| `0xe9c19e5bd10e5096519fe1d2a9d783f79b8211da` | WETH/USDC、v1、pause=0、healthy | 有双边深度；0.001 WETH 第一腿的保守输出为 1,937,028 USDC raw，即 1.937028 USDC。 |
| `0xf85afbadecc7f23dd173ed706f07d9c7e2473e24` | WETH/USDC、v1、pause=0、healthy | totalToken0Available=0，totalToken1Available=1；bid/ask 非哨兵但所有返回深度累计量为 0。 |

六个计划内闭环全部保留，全部因部分或空深度拒绝；没有完整毛收益或净收益候选。
资本、commitments、reserve、gas、builder、融资、失败缓冲及机会成本本来就未核实，继续
保持 null。不能把此次“0 个正估算”解释成全市场无套利，或把第一腿输出当作收益。

这些是**单提供者 API 声明**。未验证独立 provider、块号/哈希、底层报价 age、runtime、
feed/extension 配置、caller 权限或原子执行；没有模拟、签名、广播、收据或真实 PnL。

## 保留与重放

本机证据目录：`reports/research/metric-20261114/`（忽略目录，不提交原始大文件）。

| 内容 | SHA-256 |
| --- | --- |
| 冻结 plan | `1a502b939538c1949f20689260bd836e1f47634197eb02cc76e967cb67b30be4` |
| capture manifest | `9fa96471072de1cc517d378be8c8c737d11ff9f5af261462fc1306fc2f5e071c` |
| 定向 metadata 原始响应 body | `9db63191cc3b910ab5752238e4596a4bd8fce246ea100d2d749b41468c330bb0` |
| batch depth 原始响应 body | `c56b6bfaadb5859b2ea7b8c7c33a126fbba0fd5bb6b0a1d1be63364cd8ebb473` |

请求 spec、计划、响应 body、HTTP status、本地开始/接收时间分别保留。hash 能发现意外修改，
不能证明提供者诚实或阻止持有全部文件者重新制作一份 capture。本地时钟不是独立时间证明。

```sh
.venv/bin/python -m scripts.research_metric replay \
  --capture reports/research/metric-20261114/public-capture \
  --output /tmp/oasis-metric-public-replay.json
```

无需网络即可重放；输出必须是新文件。跨代码版本的 implementation hash 会变化，不能要求
不同实现的报告字节相同。同一实现和相同输入应逐字节相同。

## 检查记录

聚焦回归首轮 39 项通过，补充 bool/float 方向、单位变换、竞争发布及传输失败后 49 项通过。
初次 lint 发现新文件行宽、未使用 import 和 fixture helper 被局部变量遮蔽，已修正。
方向 Literal 接受 bool/float 的自审发现已加严格前置校验和回归。没有放宽检查规则。

安全门第一次以退出码 2 失败：FastAPI 的间接依赖 `opentelemetry-api 1.45.1`
命中 `CVE-2026-126360`。已在 pyproject 声明 `>=1.45.2,<2`，通过 `make lock`
重新生成 uv 及两个导出锁文件，再运行 `make install`；只升级了这一项传递依赖。
[官方修复记录](https://github.com/open-telemetry/opentelemetry-python/blob/main/CHANGELOG.md)
说明 1.45.2 限制 traceparent 提取长度。原失败保留为 `security-check.log`，成功复核另存
`security-check-fixed.log` 与 `security-check-final.log`，未豁免 CVE。

| 检查 | 结果与本机日志 |
| --- | --- |
| 新模块聚焦回归 | 51 项通过；`metric-final.log`。 |
| 新模块及既有 cycle/shadow 回归 | 82 项通过；`focused-reviewed.log`。 |
| 第一轮 `make check` | 退出 0；5,604 项 Python、138 个子测试、前端及 70 项 Foundry；`make-check.log`。 |
| 补丁环境 `make check` | 退出 0；5,606 项 Python、138 个子测试、9 个前端模块、70 项 Foundry；`make-check-final.log`。 |
| 最终 `make security-check` | 退出 0；无已知依赖漏洞、Bandit 无 medium/high、秘密扫描通过；`security-check-final.log`。 |
| `make contract-security-check` | 退出 0；80 合约、102 detectors、0 findings；`contract-security-check.log`。 |
| 文档与入口 | 47 个本地链接目标通过，capture/replay help 和合成/真实回放命令执行成功。 |
| 复核后静态检查 | 全量 Ruff 通过；mypy 288 个 source files 通过。 |
| `git diff --check` / staged diff | 通过；只提交本任务路径，无原始证据或私密材料。 |

最后一次展示复核为每跳 fill 补充 pool、asset_in、asset_out 及 decimals，金额算法不变；
随后再跑 82 项聚焦回归、全量 lint/typecheck，并重新对真实 capture 回放两次。
最终报告 `unit-labelled-report-a.json` 与 `unit-labelled-report-b.json` 逐字节一致。
测试证明解析、条件算术、完整性与拒绝路径，不证明真实合约数学、成交权限或交易收益。

## 尚缺证据及下一安全动作

下一步优先取得 Metric 与独立同链 DEX 的固定区块身份、准确 caller 的整笔两腿模拟，
以及 Ethereum 条件更新前后的报价差异。另为 Elysium 完成精确 prestate 和前瞻对照窗口。
报价采集预算与实际资本授权分别核定，不能沿用本轮公开 HTTP 请求范围作为 RPC 或交易额度。
Aave 事件仍缺用户指明的时间/交易；借贷偿债与 HF 模型尚未实现，不声称已有能力。

## 后续纠正（2026-11-14）

上述末句是本次记录形成时的状态，已由[后续验证](MULTICHAIN_LENDING_20261114.md)更新。
Binance 事件实际发生于 2026-10-29，已有币安与 Aave 的官方说明；上一轮未能找到事件，
将 Bybit 作为相近线索不准确。用户进一步提供 `0x0cdd…661d8` 地址。后续增加了多链目录
入口和合成借贷债务/HF/退出模型，仍未完成该地址的原始借还款收据及净 NAV 回放。
