# LP 真实签名端到端测试与本轮交付

日期：2026-09-07。基线仍为 `90e19d26bf0d1f933bfbdb5a5e3ee3792bb6259d` 加工作区修改；没有提交。
用户明确授权独立测试进程使用 `.env` 私钥进行测试。本轮实际发送了 Robinhood **测试网**交易，
因此上一份复核报告中的“未读取私钥/未广播”仅描述上一轮，不是本轮状态。

**confirmed：本地和 Robinhood 测试网 LP 生命周期均完成。尚未完成全部自主 LP 或生产验收。**

## 本轮实现

- `execution/lp_journal.py`：SQLite account/chain/config 绑定、原子 nonce 预留、竞争写入保护、
  digest 乐观并发校验、重启恢复、HALT/不明提交继续阻止新动作。是持久化协调模型，不是证据授权器。
- `shadow/service.py` / `scripts/run_lp_shadow.py`：可运行的证据消费者；校验 header 和完整
  feature/candidate/gate 输入载荷，输入证据关联到决策日志；代码/配置变更禁止沿用旧日志。
  有文件/子进程级 CLI 回归，不虚构上游实时特征或路由数据。
- `contracts/test/OasisRealPermit2Lifecycle.t.sol`：使用依赖锁定的真实 Permit2 runtime，
  与 fork 复用完整生命周期；补充授权过期、撤销、额度不足的原子失败验证。
  `test/shared/OasisLifecycleBase.sol` 保存原场景；fork 保留显式网络入口，没有删除断言或静默跳过。
- `scripts/testnet_lp_e2e.py`：隔离测试账户进程，本地随机账户 / Robinhood 测试网两种固定网络；
  本轮新建合约和测试代币、有界批准、native value 恒零、逐交易 simulation、hash/nonce 关联、
  原始收据、固定区块余额和 runtime 哈希；签名材料与 raw transaction 不输出、不归档。
- 测试钱包跨进程锁和追加式 gas 预算；半条预算记录失败闭合，不因进程重启归零。
- `scripts/verify_lp_e2e.py`：独立离线复放真实收据，验证 nonce、状态、PoolId、手续费、方向、
  liquidity delta、逐币守恒及 native gas。已包含篡改收据并重新哈希后的反例测试。
- Robinhood 测试网 `46630` 纳入显式 Nitro 原生成本策略，有失败后修复回归；留存真实收据
  中存在 `gasUsedForL1`，实际总 gas 按 Nitro 包含 L1 gas 的口径核对。

## 真实执行结果

| 项 | 本地 Anvil | Robinhood 测试网 |
|---|---|---|
| chain ID | 31337 | 46630 |
| 完整成功交易 | 35 | 35 |
| Swap | 双方向 × exact-input/output，4 次 | 同左 |
| LP | mint→collect→executor recenter→撤销白名单→executor remove | 同左 |
| 清理 | ERC-20 / Permit2 / swap 授权清零 | 同左 |
| 逐币守恒 | 8 次通过 | 8 次通过 |
| 实际 native gas | 22,004,840,130,081,568 wei（本地注入资产） | **241,732,800,000,000 wei = 0.0002417328 测试 ETH** |
| 资金边界 | 随机本地账户 | 初始 0.01 ETH；剩余 **0.0097582672 ETH**；预留上限 0.001 ETH |
| 独立收据复放 | 通过 | 通过 |

测试网未发送主网交易，没有使用用户已有 ERC-20 作为 LP 资产。测试中 owner/governor/guardian
是同一测试账户；这是可控测试栈，不能作为生产角色隔离证明。Permit2 从锁定依赖的测试 helper
runtime 部署，不是宣称发现了测试网官方 canonical 地址。两种池币都是本轮生成的测试代币。

测试网关键身份：

- 账户：`0xE653dF20A8c525eDF381942fFC47704B7bDFe97d`。
- PoolManager：`0xf3F7E12dfbF7F8B17ef97Af76b3Db632eD52d4eA`。
- PositionManager：`0xa35631DEa852c2059645fCA076d9F5d7aab3959F`。
- Hook：`0x87C9D6ddc2b6e57F5E58CCF67eE4CE84F135e880`。
- PoolId：`0x5b3b913eb98510b7a96fef70b61736bbe0238fea7450ea45b9aeb6ba20b7313d`。
- 最终 runtime 快照：block `114517638`，hash
  `0x1deff9596c6c49eeb47f9687e257c99d702551c7bb0c3624c2383dc5dd6054f0`。
- REMOVE 交易：[`0xc395…cab4`](https://explorer.testnet.chain.robinhood.com/tx/0xc3952c3b2c663994405858ac7903158ae492ca4a15d6055707da88a3f6b9cab4)。
- RPC source：`https://rpc.testnet.chain.robinhood.com`；逐条 UTC 采集时间见 events.jsonl。
- 最终审计链 digest：`38adb8e34181de23e84c21b584e4c5da30aeaf18d9b5536b1510a557c6399333`。

每个合约的 runtime keccak、每笔 receipt 与 block hash 均在末尾 complete/receipt 记录；
本轮验证为单提供者研究证据，不构成独立 RPC quorum、L1 最终性或长期可用性证明。

## 最终门禁

| 命令 | 结果 |
|---|---|
| `make python-check` | exit 0；ruff/format/mypy（82 文件）通过；**554 passed + 96 subtests**；1 条第三方 websockets 弃用警告 |
| `make contract-check` | exit 0；**36 passed / 0 failed / 0 skipped**，5 套件；含 10,000 fuzz runs / 512 invariant runs 配置 |
| `make contract-security-check` | exit 0；Slither **80 contracts / 102 detectors / 0 results** |
| `make security-check` | exit 0；依赖无已知漏洞、Bandit 门槛通过、secret gate 通过 |
| `coverage run --branch -m pytest` / `coverage report --include='src/*'` | **554 passed + 96 subtests**，覆盖率 **84%** |
| `verify_lp_e2e.py`，本地和测试网日志各一次 | 两次 exit 0，35 receipts / 4 swaps / 8 conservation checks |
| `git diff --check` | exit 0 |

中间出现过普通合约门禁误加载 fork 测试：共享文件残留了 concrete fork 入口，已删去重复入口、
清理重编译并通过普通门禁；没有通过跳过失败测试处理。自动审批也曾因账户额度拦截一次本地复跑，
在用户重申继续及已有本地成功证据后按同一审批路径重新获准；没有绕过审批。

证据：`reports/evidence/lp-e2e-local-2026-09-07/`、
`reports/evidence/lp-e2e-robinhood-testnet-2026-09-07/`、`reports/evidence/lp-delivery-2026-09-07/`。
运行步骤和边界见 `docs/runbooks/LP_TESTNET_E2E.md`。

## 仍未交付的生产/完整自动化能力

1. 实时市场与 reconciled position producer 持续驱动 shadow consumer；独立 RPC/finality、
   稳健特征、route depth 和健康故障演练仍需实际运行。
2. 持久化协调模型与独立策略 signer、真实 simulation/policy/finality verifier 的完整接线；
   测试脚本是确定性场景，不能冒充自主策略执行服务。HALT 后自动恢复/安全退出仍缺独立核对路径。
3. 完整恶意代币、回调、重入、并发与 reorg 战役、生产角色/权限隔离及独立发布审计。
4. R00/R01/R10/R11 的真实样本外经济回放、routing/elasticity 和全成本收益证明；测试代币交易
   不提供这些证据。预先声明的长期 shadow 和受限主网 canary 也未完成。

因此，本轮可以验收上述 LP **技术生命周期及新增组件**；不能验收“所有 LP 功能 / 自动盈利 /
生产就绪”。本报告没有把测试网成功升级为主网部署或资本扩张授权。
