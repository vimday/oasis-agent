# LP 主网上线运行手册

**适用范围：** Robinhood Chain（chain ID `4663`）上的项目自有资金、最小额度 canary。
**当前状态：** 发布工具链、双公共 RPC 预检、可配置 RPC/角色模板和 ETH/STOCKHOOKS 双向报价
基准已具备；项目 hook 池的部署身份、标准路由、独立角色地址和持续经济证据未齐，禁止广播。

本手册把部署、配置、首次建仓、自动再平衡、撤权和退出拆成可核验步骤。每一步先在固定区块
fork 上运行同一脚本，审阅交易计划和 gas，再由外部硬件/远程签名器广播。生产脚本不读取
`.env` 私钥，也不接收 `--private-key`。测试网 E2E 对测试账户私钥的授权不扩展到生产托管。

## 1. 放行输入

开始广播前必须锁定：

- 两个不同运营方的 Robinhood 主网 RPC；配置只保存 `url_env` 名称，不保存 URL/凭据；
- native/ERC-20 或 ERC-20/ERC-20 目标币对、token0/token1 顺序、初始价格、tick spacing、
  路由和退出深度证据；
- 相互独立的 deployer、governor、guardian、LP owner 地址；LP owner 同时是 bounded executor owner；
- 每笔、每仓位、每日损失、gas、滑点、库存和回撤上限；canary 观察窗口及终止条件；
- PoolManager、PositionManager、StateView、Permit2、hook、executor 和两个 token 的预期 runtime keccak；
- 可暂停、可完整退出、无 Treasury 拉款权限的签名/告警值班安排。

商业第三方审计不是项目自有最小 canary 的单独硬门。WP5/WP7/WP8 仍需在发布清单中记录可追溯
的独立复核标识；多 AI 交叉复核可以作为复核证据，但不能代替双 RPC、经济样本、canary 收据、
逐币对账或角色隔离。

## 2. Robinhood 核心身份

当前仓库使用以下主网地址；每次发布仍必须在新固定区块重新核验：

```text
PoolManager     0x8366a39cc670b4001a1121b8f6a443a643e40951
PositionManager 0x58daec3116aae6d93017baaea7749052e8a04fa7
StateView       0xf3334192d15450cdd385c8b70e03f9a6bd9e673b
Permit2         0x000000000022d473030f116ddee9f6b43ac78ba3
Quoter          0x8dc178efb8111bb0973dd9d722ebeff267c98f94
UniversalRouter 0x8876789976decbfcbbbe364623c63652db8c0904
```

前四项进入 14 项部署身份清单；Quoter 和 Universal Router 进入独立路由证据。地址来源于
[Uniswap v4 官方部署表](https://developers.uniswap.org/docs/protocols/v4/deployments)，仍需在固定
区块验证 runtime keccak。Uniswap 官方已说明 v2/v3/v4、应用、API 和 UniswapX 在 chain `4663`
可用；这只说明协议支持，不能代替目标池被实际选路的证据。参考：
[UniswapX on Robinhood Chain](https://developers.uniswap.org/docs/changelog/completed-notifications/uniswapx-live-on-robinhood-chain)。

公共 RPC 可用于预检，端点始终由环境变量解析，前端或部署环境只需替换变量值即可切换 Alchemy。
主配置、服务配置、身份清单和路由探针分别使用 `rpc_url_env`、`rpc_primary.url_env` 或
`providers[].url_env`，不把 URL 或凭据写进 Git。以
`configs/identity.robinhood-stockhooks-eth.example.json` 为正式角色模板：

```json
"address_env": {
  "hook": "OASIS_HOOK",
  "executor": "OASIS_EXECUTOR",
  "deployer": "OASIS_DEPLOYER",
  "initializer": "OASIS_GOVERNOR",
  "governor": "OASIS_GOVERNOR",
  "guardian": "OASIS_GUARDIAN",
  "lp_owner": "OASIS_LP_OWNER"
},
"rpc_primary": {"name": "provider-a", "url_env": "ROBINHOOD_RPC_A"},
"rpc_extra": [{"name": "provider-b", "url_env": "ROBINHOOD_RPC_B"}]
```

加载器要求 deployer、governor、guardian、LP owner 四个地址互不相同，initializer 必须等于
Phase-1 governor；缺少变量、重复角色、零地址或同一字段同时直写和引用环境变量都会失败关闭。
模板中的 `max_liquidity`、hook/executor 地址与 hash 必须在演练和部署后替换，模板本身不会放行。

部署后运行：

```bash
PYTHONPATH=src .venv/bin/python scripts/run_deploy_checklist.py \
  --network robinhood --block <PINNED_BLOCK> \
  --identity-config <RELEASE_IDENTITY_JSON> \
  --output reports/evidence/<NEW_IDENTITY_PACKAGE>
```

清单严格包含 14 项：chain、四个核心 runtime、hook/executor runtime、hook 权限、两份绑定和控制
状态、两个 token、PoolId/完整 slot0、provider diversity。只有两个提供者对全部事实给出同一解释
且全部匹配配置时，`identities_verified` 才为真。该工具始终输出 `execution_eligible: false`，因为
身份正确只是发布输入，不是资金授权。

Robinhood 官方把公共 RPC 标为限流且不建议生产使用，并推荐 Alchemy；官方同时列出 QuickNode、
Blockdaemon、dRPC 和 Validation Cloud。生产的两个端点必须来自不同运营方，其中至少一个能覆盖
发布所需历史区块；公共 RPC 只能作第三路观察。参考：
[Robinhood Chain 连接文档](https://docs.robinhood.com/chain/connecting/) 和
[自建全节点文档](https://docs.robinhood.com/chain/run-a-full-node/)。

## 3. 构建与固定区块演练

```bash
make python-check
make contract-check
make security-check
make contract-security-check

cd contracts
forge script script/DeployOasisContracts.s.sol:DeployOasisContracts \
  --rpc-url "$ROBINHOOD_RPC_URL" --fork-block-number <PINNED_BLOCK> \
  --sender "$OASIS_DEPLOYER"
```

`DeployOasisContracts` 强制 chain ID、核心 codehash、PositionManager→PoolManager 绑定、非零流动性
上限以及 deployer/governor/guardian/LP owner 的角色分离。它部署 CREATE2 factory/hook 和 bounded
executor，并验证 hook 权限位及所有不可变量。演练输出中的地址和 gas 只是模拟结果。

广播时沿用经过审阅的环境和固定构建产物，添加运营方选定的 Foundry 硬件或远程签名器参数以及
`--broadcast`。不允许把原始私钥放入命令行、日志或生产 agent。部署收据最终确认后，把实际地址和
runtime keccak 写入发布 identity 配置，再运行第 2 节清单。

## 4. 配置和初始化池

`ConfigureOasisPool.s.sol` 由 governor 签名，验证 chain、核心/hook codehash 和 hook 控制绑定后，
设置确定性费用参数、仅允许 LP owner 增加流动性，并初始化唯一 PoolKey。先做固定区块演练：

```bash
cd contracts
forge script script/ConfigureOasisPool.s.sol:ConfigureOasisPool \
  --rpc-url "$ROBINHOOD_RPC_URL" --fork-block-number <PINNED_BLOCK> \
  --sender "$OASIS_GOVERNOR"
```

广播包含多个顺序交易。若中途失败，立刻停机并逐笔核对收据；不要用新 nonce 盲目重跑。配置完成
后再次运行身份清单，确认 hook owner/pending owner/guardian、暂停状态、PoolId、LP owner 白名单、
费用规则和完整 slot0。

## 5. 最小 canary 建仓

`MintOasisCanary.s.sol` 支持 native/ERC-20 与 ERC-20/ERC-20 池。它要求当前 tick 位于预设范围内，
检查全部 runtime/codehash/角色/绑定/暂停状态，拒绝任何已有 ERC-20 或 Permit2 allowance，验证
余额、原生 gas reserve 和 executor 流动性硬上限。成功路径：

1. 每个 ERC-20→Permit2 授权精确到对应 `amountMax`；原生 ETH 不创建 token allowance；
2. Permit2→PositionManager 授权同样有界且短时失效；
3. mint 指定 liquidity/range 的一个 NFT，原生池把多余 `msg.value` sweep 回 LP owner；
4. 立即把两层 token allowance 清零；
5. 给受限 executor 可撤销的 PositionManager operator 权限，使 RECENTER 后的新 NFT 能继续自动管理；
6. 验证 owner、liquidity、operator authority、零 token allowance 和 PositionManager 未新增 ETH 余额。

```bash
cd contracts
forge script script/MintOasisCanary.s.sol:MintOasisCanary \
  --rpc-url "$ROBINHOOD_RPC_URL" --fork-block-number <PINNED_BLOCK> \
  --sender "$OASIS_LP_OWNER"
```

原生池必须设置非零 `OASIS_NATIVE_GAS_RESERVE`，且 `OASIS_TOKEN0_HASH` 为零；ERC-20/ERC-20 池
的 reserve 必须为零。任何部分失败都先运行 `RevokeOasisCanaryAuthority.s.sol`，该脚本会撤销
所有适用的 token/Permit2 allowance 和 executor operator 权限；填写真实 `OASIS_TOKEN_ID` 时还会
清理遗留的单 NFT 授权。撤权后再调查失败原因。

## 6. 双向报价和标准路由验证

部署并完成最小流动性配置后，先用 `run_v4_route_probe.py` 取得直接 Quoter 证据。复制
`configs/route-probe.robinhood-stockhooks-eth.example.json` 已固化已确认基准 PoolKey、金额网格和
三个 runtime hash。项目 hook 池部署后复制它并替换 PoolKey；每个 exact-input/exact-output × 两个
方向组合至少三个金额：

```bash
PYTHONPATH=src .venv/bin/python scripts/run_v4_route_probe.py \
  --config <ROUTE_PROBE_JSON> --block <PINNED_BLOCK> \
  --output reports/evidence/<NEW_ROUTE_PACKAGE>
```

工具验证双提供者区块头、PoolManager/Quoter/Universal Router runtime、Quoter→PoolManager 绑定、
所有报价一致性，并计算相对最小档的整数 ppm 价格影响；RPC URL 仅从 `url_env` 读取。它始终输出
`execution_eligible: false`。`route_probe_verified: true` 只证明该固定区块的直接 Quoter 网格。

已确认的 hookless ETH/STOCKHOOKS 基准在区块 `57,747,350` 通过全部 12 个双向直接报价，证据见
`reports/evidence/route-probe-robinhood-stockhooks-eth-57747350-v3-2026-09-08/`。该结果不能授权
Oasis hook 池，因为 hook 地址改变 PoolKey/PoolId；新池必须重新取得相同门禁和标准路由选择证据。

随后必须通过 Universal Router、Uniswap API 或应用实际取得目标池被选中的双向报价，记录所选
route/split、calldata、deadline、gas、价格影响和失败原因。canary 再比较 quote 与 receipt 的实际
settlement。任何方向不可达、Quoter/Router 结果不一致或 route 不选择目标池，都阻止建仓。

## 7. 自动再平衡、暂停和退出

首次 canary 的 NFT 及 RECENTER 产生的后续 NFT 由 `OasisPositionExecutor` 通过可撤销 operator
权限管理。每次 RECENTER/REMOVE 都必须通过无密钥
`lp_signer_policy.py` 的 chain、合约哈希、PoolKey、nonce、deadline、模拟、风险、gas 和资本上限
检查，再由外部 sidecar/HSM 签名。executor 只允许 owner 调用；guardian 只能暂停；RECENTER
不能投入超过刚移除仓位的资产；REMOVE 在 hook 暂停、LP 白名单撤销和 token allowance 为零时仍
可把资产直接返还 LP owner。

退出完成后必须核对 NFT liquidity、owner 两个 token 余额、PoolManager 余额变化、receipt/log、
实际 gas 和会计残差。随后撤销 executor operator、遗留单 NFT 及 Permit2/ERC-20 权限。任一数据过期、RPC 不一致、codehash/
角色变化、模拟与收据不一致、非零不可解释残差或风险限额突破，都触发 HALT。

## 8. 发布证据包

每次阶段晋级单独创建新目录并生成 `MANIFEST.sha256`，至少保留构建/测试日志、固定区块及 hash、
身份清单数据库、交易计划、签名者角色清单、广播 tx/receipt、逐币与 gas 对账、shadow/canary 指标、
暂停和完整退出演练。禁止修改历史证据包来追认新结论。

阶段顺序保持：`fork -> shadow -> minimal canary -> bounded production`。部署合约不等于生产放行；
只有 WP0–WP8 的新鲜证据全部通过时，release assessment 才能输出 `execution_eligible: true`。
