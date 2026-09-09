# Base Launch Sniper：设计复核与实现边界

日期：2026-09-09。对应 `BASE_LAUNCH_SNIPER_DESIGN.md` v3。

本次实现是 **基础代币侦察 + 留存输入的离线影子诊断 + P0 只读界面**，
不是设计 §10 第一阶段的完整交付。没有开始真实策略观察窗，也没有实现签名、广播、
实盘交易或授权交易。所有决策的 `execution_eligible` 恒为 `False`。
用户原始设计文档保持原样，以下修正单独记录。

## 设计复核

1. **严格 FDV 上限与取整冲突。** 原设计 `ceil(Q/P_max)` 在整除时允许恰好
   $200M FDV。实现使用 `floor(Q*S*10^decimals/F_max)+1`。
   单笔 0.1 ETH、ETH=$3,300、18 位精度时，严格价格约束为至少 `1650*10^18+1` 个 base units。
   滑点约束可以要求更高的最小收款。该函数只计算保护值，不证明它已写入路由 calldata。
2. **分笔买入以原生金额为档位。** 2026-09-09 用户确认买入改为两笔各 0.1 ETH
   （累计 0.2 ETH）。`Quote.spend_usd_micros` 由固定 Literal 档位改为快照 USD 估值，
   报价矩阵按 `Target.buy_tranches_wei`（原生累计 0.1/0.2 ETH）键控；矩阵检查前
   先验证档位已冻结，未冻结一律返回 `tranche_schedule_unfrozen` 阻断。
3. **ETH 与美元不能混用。** `entry_price.minimum_received` 接收 ETH wei 和
   ETH/USD 整数微美元报价，保留有理数乘积直到最终 token 取整。报价矩阵中导入的美元
   支出尚未与链上 ETH 支出、价格证据绑定，因此 G7 始终保留阻断理由。
4. **普通 ERC-20 allowance 没有原生过期语义。** 写入配置的有效期不是链上约束。
   只读授权检查会拒绝将普通 ERC-20 授权标为可过期就绪。Permit2 路径也仍须核实其
   token/spender/owner 绑定、底层 ERC-20 额度和真实路由语义；本次 API 一律显示未验证。
5. **去重不能丢掉来源或交易内不同日志。** 事件 reducer 使用链、工厂、池、block hash、
   tx hash、log index、pending/final 阶段作为键，同时保留不同 provider 的记录。
   创建事件与注入流动性事件独立处理。removed、冲突和缓存溢出锁存停止。
6. **只读与写审计是两个角色。** CLI journal 使用事务单写者和 `synchronous=FULL`；
   服务端用 SQLite `mode=ro`、`PRAGMA query_only=ON` 和读事务获取一致视图。
7. **缺少冻结阈值不能默认通过。** 原设计中待锁定的参数保留 `None` 并显示阻断理由，
   没有将“如 10% / 60 块 / $50K / -5%”自行批准为策略阈值。
8. **供应量口径固定。** FDV 使用 10 亿 reference supply；侦察的 Base 本地供应量
   单独记录，不能因为跨链销毁减少本地供应量而降低参考估值。

## 已实现与未实现

| 组件 | 已实现 | 仍未完成 |
| --- | --- | --- |
| `launch/models.py` | LAPTOP 严格注册配置、8453 链绑定、整数单位、禁止浮点/多余字段/重复 JSON 键 | 版本化工厂、路由与控制面真实基线 |
| `launch/recon.py` | 两源基础身份采集、hash-pinned calls、前后块复核、原始 RPC 留存 | owner/peer/Endpoint delegate/DVN、部署与持有人拓扑、审计源码匹配 |
| `launch/watcher.py` | 有界候选事件 reducer、多源去重、pending/final 分离、停止锁存 | pendingLogs 双 WebSocket、日志 ABI 校验与 N 块补账；reducer 不是网络 watcher |
| `launch/quotes.py` | 原生 0.1/0.2 ETH 累计档位完整双源矩阵检查、snapshot/身份一致性、FDV 与价格影响检查 | 真实 Quoter 适配、原生 ETH/USD 价值证明、批量报价采集 |
| `launch/entry_price.py` | 严格 FDV 与滑点整数保护值 | Router calldata 编解码、部分成交与实际收款证明 |
| `launch/approvals.py` | 无授权/撤销/过期/额度/身份/过期机制的只读检查 | 真实 allowance 读取、批准记录绑定、底层 ERC-20 与 Permit2 联合校验 |
| `shadow/sniper_runner.py` | G1–G11 阻断诊断、内容 hash、幂等导入、代码/配置锁定、重组/时间倒退/停止锁存、每次读取重新计算门禁 | 原始协议证据 verifier；真实观察窗、执行状态机；不复用 LP 的决策 schema |
| `server/routers_launch.py` | 五个 GET 端点、ETag、只读 SQLite、损坏返回 503 | 实时控制面变化时间线、真实池与授权状态 |
| `/decisions?strategy=launch`、`/stream` | 同源决策过滤、`launch_decisions` 事件、损坏时 `launch_error` 并关闭连接 | 独立候选/门禁失败的即时事件推送 |
| Launch 页面 | 目标、G1–G11、报价/FDV 分数、决策与预授权只读表格；读取失败清空旧数据 | 持续 live 数据接入；顶部原有状态条仍为 LP 状态 |
| `pool_verify`、`token_risk`、`simulator` | 未声明实现；对应门禁强制未验证 | 版本化协议证明、完整控制面验证、真实连续状态/压力模拟 |

导入文件只能提供解释结果，不能自证链上事实。因此即使数值检查通过，G1–G11 仍保留
尚未实现的协议证明或执行验证阻断项；输出只有 `reject` 或 `stand_down`，无就绪/买入路径。
两个 provider 别名与相等返回值也不能证明服务商独立，独立来源仍需运营核验。
报价端点把大整数作为十进制字符串返回，避免浏览器二进制浮点截断 token 数量。

## 使用

基础身份侦察需要在运行进程环境中配置 `BASE_RPC_PRIMARY` 和 `BASE_RPC_SECONDARY`。
不要将私钥、助记词或 RPC 凭据写入代码、报告或聊天。命令必须指定一个固定区块：

```sh
.venv/bin/python scripts/run_launch_recon.py \
  --block BLOCK_NUMBER \
  --evidence /path/to/evidence.db \
  --report /path/to/new-recon-report.json
```

两源并发，单次请求超时 10 秒、不自动重试；只调用 chainId、block、code 和 eth_call。
报告独占创建，拒绝覆盖旧报告。RPC 失败不生成身份通过报告；两源返回不一致写出
`stand_down`。它不会更新目标配置，也不会自动开启策略。

影子导入每行一个 `Cycle` JSON，schema 由 `launch/models.py` 定义。
无目标配置时采用未核实、阈值未冻结的默认注册记录。以下空输入是拒绝路径 smoke test，
不是链上样本：

```json
{"observations":[],"evaluated_at":0,"current_block":0}
```

```sh
.venv/bin/python scripts/run_launch_shadow.py \
  --input /path/to/cycles.jsonl --journal /path/to/launch.db
```

可选 `--target /path/to/target.json` 仅绑定诊断配置，不能放开执行。journal 配置/代码
变化会拒绝续写；停止一旦锁存，不会因后续正常输入解除。需要新窗口时保留旧数据库，
在独立评审后使用新路径。精确重复输入返回已有记录。事件 reducer 的缓存不是持久化
意图库，也不代表完成了执行 nonce 去重。

在独立 server 配置的 `[databases]` 添加 `launch = "/absolute/path/to/launch.db"` 后
重启对应观察服务，可展示留存记录。本次没有修改正在使用的 live 配置。
未配置时目标注册仍可见，状态明确为 `unconfigured`，授权为 `unresolved`。

日志哈希链可以检测内部修改和不一致，无法独自证明数据库尾部从未被整体截断；
需要外部可信 digest anchor。当前读接口每次全链验证，未声称满足高频生产吞吐指标。

## 验证记录

本地专项测试 30 项，覆盖整数价格性质、多精度、严格边界、错误输入、完整报价档位、
双源分歧、过期、重组、重复事件、重启锁定、停止锁存、普通 allowance 过期误判、
日志篡改（包括重算哈希后的伪造门禁）、只读 API、ETag 与 WebSocket。
专项覆盖率 94%（438 statements；不是分支覆盖率或生产安全证明）。

完整仓库和安全检查的最终结果在本次交付说明中记录。浏览器实测 Launch 空状态显示正常，
无交易/授权操作入口；JavaScript 通过 `node --check`。

运行环境未配置上述两个 Base RPC。本次没有真实链上侦察结果、持续影子观察窗、
开盘行情结论、真实模拟报告、链上 fork 回放或盈利证据。
