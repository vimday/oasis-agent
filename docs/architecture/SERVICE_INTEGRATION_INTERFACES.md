# Oasis Agent 服务集成接口文档

Date: 2026-09-08
Scope: 底层数据/证据/影子/门禁/部署清单接口契约，供服务框架与前端实现方对接使用。本文只描述已实现并已验证的接口；未实现的能力（生产签名、广播、自动执行）不在此列。

## 1. 集成边界

底层设计对任何服务框架与前端施加以下不可协商的约束。违反任意一条即视为与底层安全模型冲突：

| 约束 | 强制方式 |
| --- | --- |
| 观察优先 | 观察、shadow 与部署身份清单固定为 `execution_eligible=False`；只有 WP0–WP8 全部有效通过的发布评估才可为 `True` |
| 服务进程不得导入签名/广播能力 | 静态测试 `test_server_package_never_imports_broadcast_capability` 强制 |
| 日志只读 | SQLite 以 `mode=ro` + `PRAGMA query_only` 打开，篡改返回 503 而非部分数据 |
| 令牌只走环境变量 | 配置文件中出现任何凭证/密钥即被拒绝（`configs/server.example.toml` 为严格模式） |
| 默认本机绑定 | 服务器默认 `127.0.0.1`，避免无授权网络暴露 |
| 证据完备才放行 | PASS/确认态必须携带完整、区块钉扎的 `EvidenceRef`（请求/原始响应/解释三重哈希） |

服务框架与前端可以自由决定 UI、路由组织、进程编排，但所有写操作必须走底层已定义的接口（影子日志、执行日志保留、操作审计），不得直接写证据库或影子库。

## 2. 数据层与文件布局

运行时数据目录（示例 `configs/server.example.toml` 指向 `data/`）：

| 文件 | 内容 | 访问规则 |
| --- | --- | --- |
| `data/evidence.db` | 区块钉扎证据（`EvidenceStore`） | 只读；按 `EvidenceRef` 检索 |
| `data/shadow.db` | 影子观测/决策哈希链 + 策略指纹 | 只读（服务端）；由影子入口追加 |
| `data/execution.db` | 账户/nonce 预留、HALT、恢复证明 | 只读（服务端）；由执行日志层追加 |
| `data/server_ops.db` | 操作审计（追加型） | 只读（服务端）；由操作接口追加 |
| `data/readiness-manifest.json` | 就绪门禁评估清单 | 原子替换；按内容哈希校验 |

读路径全部经过哈希链校验：`observations`、`decisions`、`execution events` 三条链在每次读取时复核完整链；任何篡改或追加都触发全量重验并失败关闭。服务框架不应绕过该层直接读原始 SQLite 文件。

## 3. 证据模型

所有链上结论必须携带可审计的出处。核心类型在 `src/oasis_agent/models/common.py`：

```python
@dataclass(frozen=True, slots=True)
class EvidenceRef:
    kind: str                  # 证据类别，如 deployment_identity / canonical_block_range
    content_hash: str          # 负载 SHA-256
    block: BlockRef | None     # chain_id + number + block_hash（+timestamp）
    source: str                # RPC 提供者标识
    retrieved_at: str          # ISO-8601 带时区
    request_hash: str          # 原始 RPC 请求信封哈希
    raw_response_hash: str     # 原始 RPC 响应信封哈希
    interpretation_hash: str   # 解释层哈希
    schema_version: int
```

`EvidenceStore`（`src/oasis_agent/evidence/store.py`）对外只暴露 `put`、`get`、`get_record`、`close`。`get_record` 返回含 `payload` 与三重信封哈希的 `EvidenceRecord`；服务框架读取证据时只应消费 `EvidenceRef` 定位到的记录，不应自己拼接或修改负载。

双提供者一致是放行前提：身份采集（`evidence/deployment_identity.py` 的 `require_deployment_identity_agreement`）与就绪门禁 PASS 都要求至少两个不同 `source` 且解释哈希唯一。

## 4. 影子决策服务

入口模块 `src/oasis_agent/shadow/`：

- `service.py`：`LpShadowService(store, journal, strategy, ...)` 消费 `LpCycle`，返回 `LpShadowDecision`。构造时对 10 个源码文件与策略配置做 SHA-256 指纹并写入 `lp_policy` 表；指纹不一致时拒绝启动（"start a new reviewed journal"）。`consume()` 先复验输入证据绑定，绑定失败写入 `fault` 并保留 HALT。
- `features.py`：实时 LP 特征生产器，只接受同链、无未来数据、双 RPC 一致、区块钉扎的整数输入；`AgreedInteger` 携带 `content_hash`/`interpretation_hash` 绑定，违约拒绝。
- `journal.py`：`ShadowJournal(path, chain_id, max_age_seconds)`，方法 `observe`、`fault`、`record_decision`、`decision_records`；所有记录哈希链接，HALT 后必须人工轮换 journal 才能复位。
- `lp_runner.py`：`LpShadowRunner` 决策运行器，`execution_eligible` 恒为 `False`。

服务框架对接方式：

- 只读消费：通过服务器 API 读取观测/决策；不直接打开 `shadow.db`。
- 触发新影子轮次：经 `POST /api/v1/runs/shadow`（需操作令牌），服务器以隔离子进程运行 `scripts/run_lp_shadow.py`（或 `run_live_shadow.py`），输出尾部脱敏后记入操作日志。
- 实时流：`/api/v1/stream` WebSocket 推送决策流，只读、无命令通道。

## 5. 就绪门禁

`src/oasis_agent/reporting/lp_readiness.py` 定义机器可读、失败关闭的放行评估：

- `LpGate`：`wp0_correctness`、`wp1_pool_access`、`wp2_replay_accounting`、`wp3_research`、`wp4_hook_executor`、`wp5_adversarial`、`wp6_shadow_routing`、`wp7_canary`、`wp8_production`。
- `GateStatus`：`pass` / `fail` / `unresolved`。
- `GateResult`：PASS 必须携带完整出处证据（三哈希齐全）且 `findings` 为空；证据必须来自 ≥2 个不同提供者且解释哈希唯一；WP5/WP7/WP8 的 PASS 还必须有 `independent_reviewer`。
- `LpReleaseAssessment`：聚合九门禁，输出 `stage`（reject/research/shadow/canary/bounded_production）、`blockers`、`execution_eligible`；仅九门禁按顺序全部有效通过时为 `True`。

CLI：`scripts/run_lp_readiness.py` 生成/复核评估清单。服务端 `GET /api/v1/readiness` 返回九门禁状态，`execution_eligible: false` 为固定字段。服务框架不得提供任何"放行/授权"写接口。

## 6. 执行层（策略进程无密钥、无广播）

策略与服务进程不持有密钥、不广播；签名侧车只接受经过独立策略复核的确定性摘要：

- `execution/lp_journal.py`：`LpExecutionJournal` 提供账户/nonce 预留（`reserve`）、`advance`、`recover_halted`；竞争写入者互斥、重启恢复、HALT 全退出有证据门控。服务端只读展示。
- `execution/lp_signer_policy.py`：独立解码有界执行器 calldata 并校验链、账户、nonce、代码、模拟、风险、资金、gas、期限绑定；无密钥、无签名、无广播。
- `execution/lp_signer_sidecar.py`：先在 SQLite 中跨进程预留 nonce，再执行"检查→外部密钥提供方签名→落账"；重启、并发和提供方失败均会锁存未决 nonce。默认 Null provider 拒绝签名，当前仍未接入生产 HSM/远程签名服务，也不广播。

前端如需展示执行状态，读 `GET /api/v1/journal/lp` 与 halt-proofs（`GET /api/v1/p2/halt-proofs`）；所有门禁审批走双人确认流程（`POST /api/v1/p2/gates/{gate}/approval` + `.../confirm`）。

## 7. 部署身份与上线清单

上线前身份核验分为两层：

1. 采集层 `src/oasis_agent/evidence/deployment_identity.py`：单提供者采集（区块开头/结尾双读、`requireCanonical` 哈希钉扎、运行时字节码 keccak/sha256），`require_deployment_identity_agreement` 要求 ≥2 提供者内容哈希一致。
2. 清单层 `src/oasis_agent/deploy/checklist.py`：纯函数分类，`DeployCheck(name, status, evidence, detail)` + `DeploymentChecklist`。十四个规范检查名（`REQUIRED_DEPLOY_CHECKS`）必须精确匹配：`chain_id`、`pool_manager_identity`、`position_manager_identity`、`state_view_identity`、`permit2_identity`、`hook_identity`、`hook_permission_bits`、`hook_bindings_and_state`、`executor_identity`、`executor_bindings_and_state`、`pool_id_and_initialization`、`token0_identity`、`token1_identity`、`provider_diversity`。绑定检查同时核对 PoolManager/PositionManager、控制角色、暂停状态、PoolId、LP 白名单、费用参数、executor nonce 与流动性上限。`identities_verified` 仅在全部 PASS 时为真；`execution_eligible` 恒为 `False`。`EXPECTED_HOOK_PERMISSION_BITS = 0x2880`（beforeInitialize | beforeAddLiquidity | beforeSwap）。

CLI 契约（`scripts/run_deploy_checklist.py`）：

```text
.venv/bin/python scripts/run_deploy_checklist.py \
    --network robinhood --block <pinned> \
    --identity-config configs/identity.robinhood.json \
    --output reports/deploy-check-<date>
```

- 输出目录新建，写入 `evidence.db`（全部区块钉扎证据）、`checklist.json`、无凭据的 `README.md` 与 `MANIFEST.sha256`。
- 退出码：`0` 仅当 `identities_verified` 为真，否则 `1`。
- 身份配置（`IdentityConfig`）严格解析：未知键、非规范地址、非动态费池、越界 `tick_spacing` 均拒绝；期望哈希以角色名（`pool_manager`/`position_manager`/`state_view`/`permit2`/`hook`）映射。额外 RPC 仅可写成 `{"name": ..., "url_env": ...}`，端点从环境变量解析，凭据 URL 不进入输出或证据。

## 8. 服务器 API 契约

服务器已按 P0–P3 分层实现（详见 `docs/verification/2026-09-08-observation-server-delivery.md`）：

| 层级 | 端点 | 说明 |
| --- | --- | --- |
| P0 只读 | `GET /api/v1/health`、`/status`、`/observations`、`/decisions`、`/decisions/{seq}`、`/evidence`、`/evidence/{key}`、`/readiness`、`/journal/lp`、`/chains/{chain_id}/pool`、WS `/api/v1/stream` | 无令牌可读；`/status` 固定含 `execution_eligible: false` |
| P1 操作 | `POST /api/v1/runs/shadow`、`GET /api/v1/runs`、`/runs/{run_id}`、`GET /api/v1/ops` | 需 `OASIS_SERVER_OPS_TOKEN`；令牌比较恒定时间 |
| P2 控制台 | `GET /api/v1/p2/halt-proofs`、`GET /api/v1/p2/gates`、`POST /api/v1/p2/gates/{gate}/approval`、`.../{approval_id}/confirm` | 双人门禁；请求者自我确认返回 409 |
| P3 多链/告警 | `GET /api/v1/chains`、`/chains/{chain_id}/chain`、`/alerts` | 只读；告警只通知不触发任何自动化动作 |

鉴权约定：令牌仅经环境变量注入（`ops_token_env` 指定变量名），`ops_token_required = true` 时缺令牌拒绝启动；所有安全响应头（CSP `default-src 'self'`、`frame-ancestors 'none'`、`nosniff`、`DENY`）由中间件统一输出；仪表盘 `Cache-Control: no-store`。

## 9. 对接约定

- 新增代码只读 `data/*.db` 必须走服务器 API 或 `EvidenceStore`/只读连接，不得绕过哈希链校验。
- 一切"状态变更"（观测、决策、预留、审批、审计）必须落在底层已定义接口上；前端不得自建状态写回通道。
- JSON 载荷遵循 `canonical_json`（排序键、紧凑分隔符）；影子输入禁止二进制浮点，金额以整数基单位或显式字符串 Decimal 表达。
- 服务端与前端进程不得 import `execution` 签名相关模块；静态测试会拦截。
- 多链注册表只读，白名单方法；任何跨链执行命令均不允许。
- 告警评估为请求时触发，非守护进程；当前无外部通知集成，若前端需要通知能力，只允许在前端本地实现展示。
