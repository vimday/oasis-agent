# Base 开盘狙击（Launch Sniper）功能设计

**状态：** 目标设计 v4，尚未实现；观察与影子模式优先，`execution_eligible` 保持 False
**首战目标：** LAPTOP，官方 Base CA `0xB095274743941e953c746F9C228DA9c18Bb6ec29`，2026-09-09 开盘
**已锁定参数：** 可执行 FDV 严格低于 $200M 才抢；买入分两笔各 0.1 ETH（累计 0.2 ETH，USD 独立上限 $1000，ETH≤$5000 的保守上界）；卖出侧预授权全量 LAPTOP（ERC-20 approve max uint256；Permit2 层 max uint160 + 过期），不受持仓上限约束；预授权策略采用可撤销 + Permit2 过期，门禁批准后执行；前端仅观测（P0 只读）
**参数修正记录：** ① 2026-09-09 用户确认买入从单笔 $500 改为分两笔各 0.1 ETH；报价档位由 $100/$250/$500 改为原生累计档位 0.1/0.2 ETH；对应代码 `Target.buy_tranches_wei=(10**17, 2*10**17)`，预授权追加文件 `reports/evidence/2026-09-09-launch-preapproval/amendment-0.1eth-split.json`。② 同日用户确认无最大持仓限制，预授权改为全量 LAPTOP，取代有界额度策略；Permit2 的 allowance 类型为 uint160，`amount=2^256-1` 会 revert，故 tx2 使用 max uint160；见 `reports/evidence/2026-09-09-launch-preapproval/amendment-max-allowance.json`。威胁记录：Router 若被攻破可移走钱包全部 LAPTOP（含未来买入），缓解为 Permit2 过期 + 可撤销（`approve(spender,0)`）+ 路由地址 pinning
**关联：** `docs/PROJECT_CHARTER.md` Phase 4、`docs/architecture/AUTONOMOUS_TRADING_SYSTEM.md`、`docs/templates/STRATEGY_EXPERIMENT.md`、AGENTS.md 第 7 条

## 1. 目标与边界

功能目标：对注册在案的 Base 目标代币，在观察到可执行流动性后，经分层验证门禁，以 $500 ETH 本金、成交价严格低于 $200M FDV 的条件完成早期建仓。设计覆盖 观察 → 验证 → 影子决策 →（独立评审后的）有界执行 全链路；当前仓库边界只允许观察与影子部分，前端只读观测。

边界与非目标：

- 不承诺第一笔成交，也不承诺下一个 Flashblock；只争取仍符合价格上限的早期成交。
- 不做同块抢先、不插入他人 pending 交易、不做三明治、不制造虚假成交量、不参与 pump-and-dump 协同。
- 不预设 CEX 同步开盘或存在可成交的对冲市场；白皮书为"寻求上币"口径。
- 前端不暴露任何操作入口（无触发/授权/签名控件）；研究代码与服务进程不接触私钥、签名材料与广播能力；执行路径仅设计态。
- 0.2 ETH（两笔各 0.1 ETH）是累计买入上限；压力测试损失不代表最大可能损失（流动性完全消失时止损可能无法执行）。

## 2. 已核实事实

### 2.1 官方信息（2026-09-08/09 核实）

| 事实 | 来源 | 状态 |
| --- | --- | --- |
| 官方 Base CA `0xB095274743941e953c746F9C228DA9c18Bb6ec29` | 官网首页 | confirmed |
| 总量 1,000,000,000；官网称 TGE 解锁 35%、36 个月全解锁 | 官网 | confirmed（文案），链上待核 |
| 创始人 30%（报道口径锁 6 个月、2 年 vest）；空投 20% 两批；20% 覆盖做市/交易所/成本；最多 30% 由 30 个预测事件触发销毁或捐赠 | WSJ 转述（BeInCrypto、金色财经） | inferred（合约不强制） |
| 托管安排（Coinbase Custody、创始人分发使用 Token Manager）；GSR/G20 代币借出合计 2,050 万枚（2.05%） | 官方披露（白皮书本次未能直连复核） | unresolved，列入侦察 |
| 10% 流动性份额覆盖做市借币、交易所需求与后续流动性，不能按 1 亿枚全入池推算开盘深度 | 官方披露（同上） | unresolved，侦察核实 |

### 2.2 执行环境事实（Base 官方文档，今日核实）

- 排序机制：Base 每 200 ms 产生一个原生区块，按优先费降序执行；同一槽位内排序在区块构建后锁定，后续更高优先费交易不能改写已构建区块。"发现 LP → 下一区块买入"是目标，不是保证的执行结果。[$TRAE_REF](https://docs.base.org/specifications/transactions/transaction-ordering)
- 事件提示：使用标准 `eth_subscribe("logs")` 做亚块级提示，断线后用 HTTP `eth_getLogs` 回补；不再依赖已退役的 Flashblocks `pendingLogs`。事件只触发重新报价，不直接触发买入。
- 区块头时间：EVM `block.timestamp` 仍按整秒递增；精确毫秒读取区块头 `timestampMs`，合约侧读取 BaseTime `0x4200000000000000000000000000000000000030` 的 `timestampMs()`。禁止用 `block.number` 推算秒数。
- `eth_simulateV1`：Base 提供交易序列模拟、状态覆盖与 nonce/余额验证；是否可用于 pending 状态必须按当前节点能力实测，不能假设。[$TRAE_REF](https://docs.base.org/base-chain/api-reference/flashblocks-api/eth_simulateV1)
- Denim 已于 2026-11-05 激活；本仓库 2026-11-15 观察到 Base 头部携带 `timestampMs`，见[验证记录](../verification/BASE_DENIM_HEADER_20261115.md)。旧 Flashblocks 专用接口不作为设计前提。

### 2.3 合约与协议结构

- LaptopOFT 结构结论沿用：无税、无黑/白名单、无 maxTx/maxWallet、无 cooldown、无 `enableTrading`、owner 不能暂停、非代理、`preCrime` 为 LayerZero 组件、无本地 mint；`setPeer()` 可启用跨链 mint，是真实信任边界（Kelp DAO 单验证器事件为先例）。
- 边界修正：Hacken 报告的 Single DVN 问题针对 Base Sepolia–Solana 测试网模板，不能据此断言主网已双 DVN；除 owner 外必须检查 Endpoint delegate，独立 delegate 可修改消息库与安全配置。[$TRAE_REF](https://docs.layerzero.network/v2/tools/integration-checklist)
- Aerodrome 非单一工厂：官方仓库列出多个部署（旧工厂 `0x5e7B…809A`、Gauge Caps 工厂、Gauges V3 工厂及对应 Router/Quoter），执行器必须版本匹配。[$TRAE_REF](https://github.com/aerodrome-finance/slipstream)
- 工厂 `createPool()` 不要求调用者持有或存入代币：空池可提前创建，创建事件不能证明项目参与或可交易（引用工厂源码，[$TRAE_REF](https://github.com/aerodrome-finance/contracts) 实施时逐行核对）。

### 2.4 链上状态与 RPC 通道（2026-09-09 核实）

`.env` 已配置两个 Base RPC 端点，变量名与厂商解耦：`BASE_RPC_PRIMARY`（默认 `mainnet.base.org`）与 `BASE_RPC_SECONDARY`（默认 `mainnet-preconf.base.org`），均为官方公开免费端点，需携带浏览器 UA。两者块高差 ≤ 1，用 `min(head)` 对齐后用钉定块做双源哈希校验，preconf 领先 1 块不影响一致性。付费 Alchemy 端点以注释保留在 `.env`，需要时切回 `BASE_RPC_PRIMARY` 即可。经双源确认官方 CA 在 Base 主网存在部署代码（12,802 字节）。本机网络路径是延迟主瓶颈（约 0.42s 基线），对"观察后验证入场"模式可接受，对同块竞速不适用。totalSupply/owner/peer/DVN 配置/持有人拓扑/实际池与路由尚未核实，列为实施第一步，必须双源验证。

## 3. 威胁模型（按风险排序）

1. 真 CA 假池与假价格：任何人可用官方 CA 建池并自定义价格；报价完成到执行之间低价流动性可能已被买走。对策：LP 来源追溯、成交价上限写进交易（amountOutMinimum）、FDV < $200M 门禁、报价矩阵。
2. 开盘信号漏报：官方向已存在的空池注入流动性，或先微量后大额；只监听 `PoolCreated` 会漏掉真正开盘，"最早 LP 不是官方"也不能永久否决。对策：池创建与流动性到位分离跟踪，以可交易状态为触发。
3. 控制面突变：`setPeer`/owner/delegate/DVN/PreCrime 变更可导致跨链增发或治理捕获；危险动作与抛售可在同一交易/预确认批内完成，收到日志时损失可能已发生。对策：事件监控用于异常后减少继续暴露，不承诺事前拦截。
4. 执行时序误判：pendingLogs 是预确认状态而非"未执行交易预告"；原子加池+首买交易无法插入其内部调用；广播超时 ≠ 未发送。对策：状态机约束（§5.9）。
5. 数据层：RPC 不一致、pending 跨节点/时刻状态不同、reorg、日志缺失。对策：双源一致、区块固定、过期即弃。
6. 自身资金风险：自买冲击造成账面浮盈不可兑现；外部卖压使退出深度消失；集中流动性边界出现深度断层。对策：压力退出约束 + 累计买入上限 0.2 ETH（两笔各 0.1 ETH）。

## 4. 总体架构与数据流

新增包 `src/oasis_agent/launch/`（观察与影子）+ `src/oasis_agent/server/routers_launch.py`（P0 只读）+ 前端 launch 标签页。原则"广泛发现、有限执行"：候选池覆盖多协议，只有已核验、适配并连续模拟过的路径才允许成交，临时池型记录并跳过。

```text
目标注册表（CA / 链 / 版本化工厂与 Router/Quoter / quote 资产 / 标签 / FDV 与限额 / 基线哈希）
   │
   ├─ 控制面 watcher（token + Endpoint delegate）
   │     PeerSet / OwnershipTransferred / DelegateSet / DVN / PreCrime / 代码哈希
   │        │ 任一变更 → STAND DOWN（独立 kill switch）
   │
   └─ 池状态 watcher（创建事件 ∪ 已有池流动性变化，pendingLogs 双源 + 对账窗口）
          │
          ▼
   候选池验证：资产身份 / 池真实性 / 可交易状态 / 流动性来源 / 撤流动性控制权
          │
          ▼
   报价矩阵 + 连续状态模拟（eth_simulateV1，双源、区块固定）
          │
          ▼
   FDV < $200M 门禁 + 成交价上限（amountOutMinimum 写进 calldata）
          │
          ▼
   影子决策日志（hash 链，execution_eligible = False）
          │
          ▼
   P0 只读 API + 前端 launch 标签页（观测）
          │
          ▼
   （独立评审 + 双人门禁后）有界执行：预授权就绪 + 状态机 + journal + signer policy
```

## 5. 组件设计

### 5.1 目标注册表

每目标一条记录：`target_ca`、链、版本化工厂/路由/Quoter 清单、quote 资产（ETH 原生）、已知钱包标签、`max_entry_fdv = 200M`、`buy_tranches_wei = [0.1, 0.2] ETH`（累计报价档位）、`max_entry_age_blocks`、`max_slippage`、累计买入上限 0.2 ETH（USD 独立上限 $1000）、击杀价、部署时 pinned 代码哈希与控制面基线。LAPTOP 为首个注册目标。

### 5.2 launch/watcher.py

池创建事件与已有池流动性变化并行监听；与目标代币相关的转移、加池、撤池、Mint/Burn、Position 变化进入候选流。使用标准 `eth_subscribe("logs")` WebSocket 提示 + 最近 N 块 `eth_getLogs` 对账窗口；按 `(factory, pool, block, tx_hash)` 去重；事件与块头固定存档；pending 与 final 分离，决策输入只来自单一固定区块快照。

### 5.3 launch/pool_verify.py（G3–G6）

| 确认对象 | 正确证据 |
| --- | --- |
| 资产身份 | 池实际 token0/token1 地址，非名字、标题或展示 CA |
| 池真实性 | 版本化工厂登记结果与实际池地址一致 |
| 可交易状态 | 储备、价格区间与实际报价，而非创建事件 |
| 流动性来源 | 加池交易的最终代币付款方与资金路径（穿透 Router/Position Manager/Safe/执行合约，不把 tx.from 或 Mint 事件调用方直接认定为 LP 出资者） |
| 撤流动性控制权 | LP token / 头寸 NFT 的控制者与权限 |
| 是否值得买 | 本单成交价、退出深度与风险预算 |

"最早 LP 不是官方"不构成永久否决；判定只看当前可交易状态与资金路径证据。vAMM 读 reserves；Slipstream 按实际 tick 与区间流动性、收费机制计算。

### 5.4 launch/token_risk.py（G2）

token 控制面：`OwnershipTransferred`、OApp `setPeer`、`setDelegate`、DVN 配置变更、`setPreCrime`、`setEnforcedOptions`；另查 Endpoint delegate、生效接收消息库、DVN 组合与阈值、默认配置继承。异常铸币与代币向交易池转移、LP 撤出与头寸控制权变化。运行时代码哈希与基线比对；`paused()` revert 不构成"无暂停逻辑"证据；"无税/无开关/不可升级"的判断以实际运行代码与完整继承路径为准。能力边界：事件监控用于异常后减少继续暴露，不承诺拦截已发生损失。

### 5.5 launch/quotes.py + launch/fdv_gate.py（G7）

成交口径固定：`P_entry` = 实付报价资产美元价值 ÷ 实收 LAPTOP；`FDV_entry = P_entry × 1e9`，S_reference 固定 10 亿；标准 OFT 跨链导致 Base 本地 totalSupply 下降时不得据此认定估值变便宜。自买冲击造成的账面浮盈不可兑现；外部卖压单独评估。FDV 上限表达"最多接受这个价格"，不证明"比市场便宜"或"买完有人接"，三者分别验证。

### 5.6 launch/entry_price.py（G8 成交保护）

价格上限写进交易。Q 为本单支付数量，F_max = $200M，S = 1e9，B_quote 为报价可买量，δ 为允许滑点：

```text
P_max = F_max / S = 0.20 USD
B_min = max(ceil(Q / P_max), floor(B_quote * (1 - δ)))
```

以 `amountOutMinimum` 写入 calldata，实际收款低于该值即回滚。以单笔 Q = 0.1 ETH、ETH=$3,300、FDV 上限 $200M 为例，B_min 至少为 1,650 LAPTOP（按代币精度向上取整），同时不低于报价可买量的 (1−δ)。实现要求：decimals 精确处理；整数运算，金额不使用二进制浮点；gas 与优先费独立预算；Router 部分成交语义；实际收款地址。该约束限制买贵，不保证买入后不跌，也不防流动性被撤。

### 5.7 launch/simulator.py（连续状态验证）

两次独立 `eth_call` 不构成往返验证。使用 Base `eth_simulateV1` 做序列模拟：实际钱包身份与路径；连续状态中执行买入；卖出所需授权（按真实授权状态，不覆盖关键失败条件）；卖出刚买入的实际数量；核对最终余额与费用。正式仓位多大就模拟多大。压力场景：买入后立即全卖、他人买单先成交、外部地址卖入一批、可撤 LP 被撤走一部分、价格跨集中流动性边界、买入成功但下一笔授权或卖出失败。压力场景用真实合约操作复现，不随意改储备槽位。

### 5.8 shadow/sniper_runner.py

复用 `shadow/service.py`：hash 链接决策日志、配置与代码锁定、restart 恢复；`execution_eligible` 恒 False。每个候选记录：触发证据哈希、门禁逐项结果、报价矩阵、FDV、连续模拟与压力场景结果、通过/拒绝原因。日志只追加，SQLite 只读打开（`PRAGMA query_only`）。

### 5.9 执行状态机与热路径（仅设计态）

| 状态 | 允许动作 | 转入下一状态的条件 |
| --- | --- | --- |
| 观察 | 发现池、补齐历史、识别资金来源 | 资产与交易路径核验通过 |
| 待命 | 维护报价、余额、授权、nonce | 价格、仓位、模拟均通过 |
| 已提交 | 查询同一交易，必要时处理替换 | 明确获得成功或失败结果 |
| 持仓 | 更新实际余额、卖出报价与风险敞口 | 达到退出条件 |
| 退出中 | 跟踪卖出结果、处理剩余仓位 | 确认实际余额与回款 |
| 停止 | 停止增加敞口 | 重新核验触发原因 |

事故细节约束：多 RPC 重复事件只生成一次意图；广播超时 ≠ 未发送（不得换 nonce 重买）；同一已签交易发多节点 ≠ 多个 nonce 买单；预确认后跟踪封块与重组；pending 跨节点/时刻状态不同；块时间戳式 deadline 无法表达"只在下一个 200ms 窗口成交"。热路径只保留已缓存身份核验、报价、模拟、签名与发送；钱包归属研究、协议版本识别、历史回溯提前完成。

### 5.10 仓位规模

```text
Q_max = min(Q_资金预算, Q_买入冲击约束, Q_压力退出约束) = 0.2 ETH（分两笔各 0.1 ETH）
```

压力测试损失不是最大可能损失（流动性完全消失时止损可能无法执行），累计买入上限 0.2 ETH 独立受限；第二笔仅在第一笔成交且 FDV 上限仍满足时执行。2026-09-09 用户追加要求优先取得低 FDV 筹码：单个池暂时超出 FDV 上限，只跳过该池当前报价，继续检查其他池与后续快照；不因此永久放弃第二笔。明确停止、退出后放弃、预算耗尽或未决交易仍阻止新增买入。

### 5.11 执行路径（仅设计态）

复用 `execution/lp_journal.py` 的 nonce 预留、竞写保护、hash 链、HALT 全退闭环与 `lp_signer_policy.py` 的链/账户/nonce/代码/模拟/限额/gas/deadline 独立校验，另加开盘专用绑定：版本化工厂与路由 allowlist、FDV < $200M/滑点/`amountOutMinimum` 约束、累计买入上限 0.2 ETH（两笔各 0.1 ETH，第二笔条件执行）。本阶段不实现、不广播、不接入密钥材料。

### 5.12 前端观测集成（仅观测，P0 只读）

后端新增 `src/oasis_agent/server/routers_launch.py`（`/api/v1`，tags=["launch"]，只读，与 `routers_observe.py` 同边界）：

- `GET /api/v1/launch/targets`：目标注册表 + 控制面 watcher 状态（peer/owner/delegate/DVN 变更时间线、代码哈希基线比对结果、stand-down 状态）
- `GET /api/v1/launch/candidates`：候选池列表（池地址、工厂版本、token0/1、可交易状态、LP 来源标签、G3–G6 逐项结果）
- `GET /api/v1/launch/quotes`：每目标最新报价矩阵与各档位 FDV（P_entry 口径）
- `GET /api/v1/launch/decisions`：影子决策流（hash 链，与 `/decisions` 同源可过滤）
- `GET /api/v1/launch/approvals`：预授权准备状态（就绪/等待门禁/已过期/已撤销；不含密钥与签名材料）
- WebSocket `/api/v1/stream` 追加 launch 事件（候选池出现、门禁失败、影子决策）

前端单文件 `server/static/index.html` 新增 launch 标签页：目标状态卡（控制面 watch 与 stand-down）、候选池表格（门禁矩阵 G1–G10）、报价/FDV 表、影子决策时间线、预授权状态面板；全部只读渲染，复用现有通用表格渲染、轮询与 ETag 机制；无任何操作按钮，与 P0 安全边界一致；告警（P3）仅通知不自动处置。

### 5.13 后端高性能设计

分层：冷路径（钱包归属研究、协议版本识别、历史回溯、标签库）离线提前完成；热路径只保留已缓存身份核验、报价、模拟、签名与发送。

- watcher：pendingLogs 双源 WebSocket 订阅（亚块级事件驱动，不忙轮询）+ 最近 N 块 `eth_getLogs` 对账；多 RPC 重复事件去重为单意图。
- 状态：候选池与报价保持内存态（块固定快照、LRU），异步追加写 SQLite 审计（单写者、`synchronous=FULL`）；pending 与 final 分离。
- 报价与模拟：并发批量 `eth_call`（复用现有并发模式）、预计算 selector/工厂/路由 ABI；`eth_simulateV1` 连续模拟在真实节点执行。
- 交易准备（仅设计态）：交易模板（router、路径、收款地址、deadline、`amountOutMinimum` 推导式）预计算，决策时只填数量；签名与广播由外部 signer 服务按策略执行，研究/服务进程不接触密钥。
- 延迟预算：pending → 候选验证（并行 RPC）→ 报价+模拟（批量）→ 决策日志 →（门禁通过后）签名+广播；每步有超时与失败语义，超时按 §5.9 处理，绝不追价。

### 5.14 预授权（Approval Prep）设计

目标：开盘后免去授权等待，让交换交易可直接提交。用 ETH 作为报价资产时买入路径无需授权（原生 ETH），只需为卖出路径预先授权 LAPTOP（Router 对 ETH/WETH 包裹解包的处理实施时核验）。

约束（对齐 AGENTS.md 最小权限）：有界精确额度（≤ $500 预算对应上限 + 缓冲）、带过期时间（如 24–48h）、可撤销；仅在 CA 核验、代码哈希固定、控制面 watcher 干净、工厂/路由版本核验、独立评审完成、双人门禁批准后，才允许广播授权交易。授权是链上状态变更：不自动广播、不随策略进程触发；撤销路径与应急 HALT 联动；授权状态（就绪/等待门禁/已过期/已撤销）在 §5.12 前端只读展示。

## 6. 判定门禁（fail-closed 全通过）

| 门禁 | 检查 | 失败动作 |
| --- | --- | --- |
| G1 | CA 命中注册表；运行时代码哈希匹配基线 | 拒绝 |
| G2 | 控制面无变更（peer/owner/delegate/DVN/preCrime/enforced options/Endpoint delegate） | STAND DOWN |
| G3 | 工厂/路由/Quoter 属已核验版本 | 拒绝 |
| G4 | token0/token1 与 CA 精确相等（顺序无关） | 拒绝 |
| G5 | 池处于可交易状态（储备/区间/报价），而非仅创建事件 | 拒绝 |
| G6 | 流动性来源资金路径可追溯；撤流动性控制权明确 | 拒绝或降级 shadow-only |
| G7 | 全档位（0.1/0.2 ETH 累计）报价双源一致；可执行 FDV 严格 < $200M；影响斜率有界 | 拒绝 |
| G8 | 连续状态模拟（含压力场景）通过；成交价上限已写进 calldata | 拒绝 |
| G9 | 单笔 ≤ 0.1 ETH、累计 ≤ 0.2 ETH；入场价 ≤ 击杀价 | 拒绝 |
| G10 | 状态机允许（观察 → 待命 → 已提交）；无重复意图 | 拒绝 |
| G11 | 预授权已就绪且未过期（卖出路径）；买入路径确认无授权依赖 | 拒绝 |

任一门禁失败即整体拒绝；缺失、过期、不一致的数据一律 fail closed。

## 7. 执行模型（设计态）

- 时序：观察到可执行流动性 → 验证 → 只争取仍符合价格上限的早期成交；不承诺第一笔，不承诺下一个区块。原子加池+首买交易无法插入其内部调用。
- 提前广播条件买单：增加抢先加池回滚、gas、nonce 管理与失效交易处理成本；未经实测的排序优势不视为免费先手，默认不启用。
- 提交方式：私有 order flow 为可选配置并显式声明假设；公开 fallback 单独风险评分。Denim 已激活，使用标准 `newHeads`/`logs` 与 `timestampMs`，不依赖 Flashblocks 专用接口。
- 持仓后：止损、时间止损、库存上限与连续模拟卖单的自动退出；对账复用 `farming` 账本；异常 → HALT + 证据门禁全退闭环。

## 8. 机会定义与退出流动性

"合约容易买卖"不能证明"抢开盘有正收益"；必须回答凭什么比其他买家拿得更便宜，以及买到以后谁提供退出流动性。

| 机会 | 成立条件 | 覆盖 |
| --- | --- | --- |
| 首次流动性明显低价，随后出现买盘 | 有独立价格判断，承担需求未出现的风险 | 本设计主要押注，受三约束（§5.10） |
| 不同市场出现可执行价差 | 两边真实深度 + 库存或原子执行能力 | 不在本阶段范围 |
| 初期卖压造成短暂价格失衡 | 能观察实际卖盘、承接与退出深度 | 纳入观察，不预设必然反弹 |

若 CEX 开盘，比较的应是对应仓位可成交的买侧深度而非最新成交价；DEX 买入后充值 CEX 存在到账与开放充值的不确定性，不按无风险套利计算。

## 9. 侦察阶段（开盘前/开盘初期最高优先级）

1. 双源抓取部署块与初始 mint 接收方；
2. 1B 转账图：确认 650M 真实所在地与托管形态（链上强制锁仓 / 托管机构约束 / 项目承诺三态），核查 Coinbase Custody 地址、Token Manager 权限与 delegate；仅凭地址余额无法回答"谁能提前调拨、谁能改分发计划"；
3. 标签：Foundation / 做市商（GSR、G20 借币 2,050 万枚对应地址）/ CEX 存款 / 空投分发器；
4. approve spender、peer/EID、Endpoint delegate、DVN 组合与阈值扫描；
5. 版本化工厂/路由/Quoter 清单、池创建事件主题哈希、Router 对原生 ETH 的处理（wrap/unwrap）双源确认；
6. 输出 pinned 拓扑报告与标签库到 `reports/evidence/`。

## 10. 实施阶段（对齐推广门禁）

1. 开盘前/初：recon + watcher + pool_verify + FDV gate + 连续模拟 + shadow runner + 预授权状态机，全部观察/影子、无签名、不广播；
2. 后端性能实现：pendingLogs 双源订阅、内存态候选流、并发批量报价与 `eth_simulateV1`、异步审计写入；
3. 前端观测集成：`routers_launch.py` P0 只读端点 + launch 标签页 + WebSocket 事件扩展；
4. 开盘当日：影子模式覆盖全程，记录决策与门禁结果；事后 pinned fork 重放检验检测器；
5. 之后：fork replay → 独立评审 → 双人门禁 → 预授权就绪 → 有界 canary（0.2 ETH，两笔各 0.1 ETH）→ 有界执行；`execution_eligible` 维持 False，WP0–WP8 维持未授权。

实盘开关前必须补齐：可复核部署代码与 owner/delegate/peer/安全配置；实际可交易池及版本化 Router/Quoter；LP 来源、控制权与双向深度；真实节点上的连续模拟、广播与失败恢复验证；预授权广播的独立评审记录。

## 11. 冻结设计与预承诺阈值（观察窗前锁定）

| 参数 | 值 | 状态 |
| --- | --- | --- |
| `max_entry_fdv`（F_max） | $200M（严格低于） | 已锁定 |
| 单笔 / 累计买入上限 | 0.1 ETH / 0.2 ETH（两笔，第二笔条件执行）；USD 独立上限 $1000 | 已锁定 |
| 报价档位 | 原生累计 0.1 / 0.2 ETH（USD 估值随快照 ETH 价格） | 已锁定 |
| 预授权 | 卖出路径 LAPTOP 全量授权（ERC-20 max uint256 / Permit2 max uint160 + 过期）+ 可撤销，门禁后执行 | 策略已锁定 |
| 最低真实流动性 | 如 $50K | 待锁定 |
| 最大入场延迟 | 池可交易后如 60 块 | 待锁定 |
| 最大滑点 δ | 如 10%（按档位） | 待锁定 |
| 往返/压力模拟净下限 | 如 ≥ −5% | 待锁定 |
| stand-down | 控制面变更 / RPC 不一致 / 可交易状态消失 / CEX 先行上市 | 待锁定 |

成交均价按 §5.5 公式；`amountOutMinimum` 按 §5.6 推导并在 calldata 强制。按 `STRATEGY_EXPERIMENT.md`：数据切分、总试验次数、阈值与 kill 条件在打开观察窗前锁定；失败输出拒绝报告而非放宽阈值。

## 12. 已撤销假设与未解决事项

已撤销：必在 Aerodrome 首发；保证 N+1 成交；看见异常就能跑；无链上锁仓即口头承诺。

未解决：runtime bytecode 与审计 commit 逐字节匹配；主网 DVN 组合与 Endpoint delegate 实际配置；实际可交易池与版本化 Router/Quoter；Router 对原生 ETH 的处理方式；LP 来源、控制权与双向深度；白皮书托管/借币安排复核；CEX 上市节奏。
