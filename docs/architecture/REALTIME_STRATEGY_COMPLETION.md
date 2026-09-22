# 实时 LP、Hook、Meme 与 Launch 统一推进设计

日期：2026-09-13。状态：分阶段实施，未完成实盘闭环，无盈利结论。

## 数据与资金流

每条链使用独立配置的 RPC、部署/runtime、最终性、gas 和预算。连续采集器先确认
chain ID 与同一区块 hash，再保留原始响应；事件订阅只负责唤醒采集。LP、meme 与
Launch 共用证据、时效、账户预约和独立签名边界，不共用不兼容的结算模型。
前端读取已校验 journal 的 gate/reason 和证据引用，不重新计算策略或把绿色指标当授权。

现有 LP preparation/simulation、slot0 capture、token identity 与控制台工作区改动应保留。
历史 Base monitor 的本地密钥路径不能作为 Robinhood 扩展入口。

## 分阶段交付和验收

| 工作流 | 设计与实现顺序 | 验收证据 / 尚缺条件 |
| --- | --- | --- |
| 自动 LP | 双源 live headers/slot0/成交窗口 → position/cash reconciliation → 净收益下界与范围决策 → bounded intent | 连续原始数据、路由弹性、全部成本、断流/重组 HALT、可重启影子窗口；收益下界不足时 HOLD |
| v4 hook | 复用现有确定性 amount-band hook，先冻结实验，再运行 R00/R01/R10/R11 | 同资本、同窗口、同范围、被动持币/静态 LP 基线，样本外置信下界为正且风险不过限；新 hook 池需独立证明路由流量 |
| meme | 新增 holders 集中度诊断 → 合约控制面/资金关联 → 双向可执行报价与 fork 卖出 → 费用后信号 | holders 地址数不能证明独立用户；缺失或不完整名单不能按零集中度处理；税/黑名单/升级/流动性撤走须阻断 |
| Launch | 复用同一 holders 门禁，事件去重 → canonical snapshot → 分档报价 → 模拟 → 风险预算 | pending 只发现；RPC 分歧、超时、重组、未知提交必须停止新增敞口；退出能力需每次重新验证 |
| 核心与前后端 | 版本化 typed input → deterministic reasons → hash-linked journal → 现有 API/候选表 | 不产生另一套 UI 策略算法；链切换不能复用另一条链的余额、nonce 或部署 |

顺序优先补共用证据缺口，再接连续生产者和协议适配器，最后实施外部 signer 的交易闭环。
实时运行需要明确目标池/代币与两家独立生产 RPC；不从 symbol 推断地址。

## 本轮实施：holders 诊断门禁

新增显式 `oasis.launch-target.v3`，要求冻结最少非零持币地址数、单地址/前十地址
占比上限以及 HHI 上限（全部整数 bps）。旧 v1/v2 目标不改策略语义。
Cycle 可附两源 holders 观察：链/区块、token/runtime/decimals、总量、地址余额列表、
采集时间、原始证据摘要和完整性声明。两源名单按地址比较，不依赖排序；重复地址、
零地址、超总量、错误类型拒绝解析。缺名单、分歧、跨快照、未来/陈旧数据停止观察决策。

集中度分母固定为该区块总供应量；不按“池/CEX/burn”标签排除地址。余额总和必须
等于总量，完整性声明必须存在；仍不能证明名单或 RPC 为真。HHI = sum(balance²) /
supply²，使用整数交叉乘比较，显示时向上取整，避免边界低估。地址数量仅是账户数。
即使阈值通过，raw holder proof verifier 与 beneficial-owner clustering 仍 unresolved，
`execution_eligible` 保持 false。此实现不新增赚钱信号或进行参数搜索。

输入字段与阈值一起被现有 journal 哈希锁定。新增 gate 通过既有候选 API/前端展示，
无需引入网络请求或签名入口。旧 journal 输入缺此可选字段时保持原序列化形状。
新代码的 restart lock 仍要求新 journal，不能重写历史记录来强行续跑。

## 业界实践的取舍

- [Robinhood 连接文档](https://docs.robinhood.com/chain/connecting/) 与
  [服务约束](https://docs.robinhood.com/chain/terms-of-service/)：公共 RPC 不适合生产高吞吐、
  低延迟用途。采用连接复用、受限并发、每源超时/速率预算；性能先测 p50/p95/p99。
- [Uniswap dynamic fees](https://developers.uniswap.org/docs/protocols/v4/concepts/dynamic-fees)：
  动态手续费是机制，不是收益保证。本项目保留可公开复算和严格有界的费用设计。
- [GoPlus 响应定义](https://docs.gopluslabs.io/reference/response-details)：holders、蜜罐、
  管理权限等风险字段可辅助筛查。缺字段/不支持链不等于安全；第三方标签不能替代
  本地双向交易模拟和原始链证据，本轮没有接入 GoPlus 或宣称其支持 Robinhood。

以上为设计依据，未据此声明任何实时链事实。当前尚无 untouched 样本外盈利结果，
不存在“已实现全部功能”或生产发布批准。

## 调用约定

`scripts/run_launch_shadow.py` 已通过 `parse_target` 使用新版本：将显式 v2 研究目标的
`schema` 改为 `oasis.launch-target.v3`，并提供 `holder_limits` 的四个必填整数。
不提供默认阈值；阈值是操作者冻结的风险约束，不是从现有行情优化出的收益参数。
Cycle JSONL 的 `holder_observations` 是两条观察，每条包含：

```text
snapshot: {chain_id, number, block_hash, timestamp, parent_hash}
source, retrieved_at, raw_evidence_digest
 target_ca, runtime_hash, token_decimals
 total_supply_base_units, complete
 balances: [{address, balance_base_units}, ...]
```

`source` 必须匹配同 cycle 行情观察源，snapshot 必须完全一致。不要把第三方 top-N
列表声明成完整名单。缺 `holder_observations` 时 v3 产生 stand_down；名单内容一致且
阈值未超限时仍产生 reject，因为原始证明和地址共同控制尚未实现。旧 v1/v2 若附带
holders 数据，会报告需要 v3 策略，避免静默忽略输入。

验证后的报告通过 `/api/v1/launch/decisions` 的 `holders` 读取；候选接口同样带
`holders` 和 `HOLDERS` gate，前端展示地址数、单地址/前十/HHI bps 与中文原因。
所有数字都是诊断统计，未代表实盘可执行或独立持有人数量。

## 后续增量：holders 原始 RPC 采集与重放

`launch/holder_capture.py` 已提供显式候选地址的双源读取。每源调用次序为 chain ID、
opening header、token code、decimals、totalSupply、逐地址 balanceOf、closing header。
全部 code/call 使用同一个 `blockHash + requireCanonical`；两源快照、身份、供应量和
每个余额必须一致。候选集 1..512 个唯一非零规范地址，ABI 结果必须恰为 32 字节。
零余额保留在原始请求响应中，但不计入非零持币地址数。供给覆盖由余额和计算，不接受
调用方填入 complete。它依然依赖 ERC-20 的诚实标准语义，不是 token 安全认证。

请求响应逐字节留存，`replay_holder_evidence` 重建请求、区块、runtime、decimals、供给、
余额和覆盖状态，核对来源/时间/摘要绑定。`bind_holder_evidence` 从存储重新验证两份
证据后，才能附入同链、同块、同 token/runtime/decimals、同来源的市场 Cycle，且持币
采集必须早于 Cycle 的 evaluated_at。函数不改写历史时钟或既有 holders 输入；行情
producer 应先取得所有输入再形成决策，不能事后补历史证据。

运行命令（参数须替换为真实冻结目标；此处无内置主网地址或默认风控阈值）：

```sh
.venv/bin/python scripts/capture_launch_holders.py \
  --target /absolute/path/target-v3.json \
  --candidates /absolute/path/candidate-addresses.json \
  --block BLOCK_NUMBER --deadline-seconds 60 \
  --output /absolute/path/new-holder-capture
```

两端点只通过 `OASIS_LAUNCH_RPC_PRIMARY` / `OASIS_LAUNCH_RPC_SECONDARY` 读取，报告使用
primary/secondary 别名。新输出目录包含 evidence.db 和 report.json，成功报告附证据
引用与可供后续 producer 使用的 holder_observations。失败只发出关闭权限的 HALT，
保留已收集的部分 wire evidence；不得覆盖旧目录。每请求 timeout=10s、retries=0；
总预算默认 60s、允许 1..300s，超时在请求前后检测，不声称可以中断正在进行的 HTTP。

现有 token identity replay 也补上原始验证调用；回归测试证实曾存在的缺口：攻击者
改写 decimals 解释并重新计算 payload hash 后，离线重放此前未与原始响应比对。

边界仍是研究采集：未实现 Transfer 全量发现、实时 holder 索引、持续行情 producer、
完整控制面和卖出模拟、实盘执行/核账，也没有样本外盈利结果。captured 或
raw_replay_verified 不会清除现有 Launch 的 release gate。上文旧的“未实现 holders
原始验证器”现仅指 journal 内部的独立资格验证；新增采集/重放 API 已有原始验证。
