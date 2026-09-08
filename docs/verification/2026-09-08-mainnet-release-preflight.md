# 2026-09-08 LP 主网上线预检

## 结论

生产部署、配置、首次 canary 建仓、撤权和受限再平衡/退出路径已经形成可执行工具链，但 Robinhood
主网资金广播尚未放行。当前阻塞来自缺失的生产输入和链上经济事实，不来自第三方审计要求。

## 真实链上结果

- Robinhood mainnet chain ID `4663`，最新双提供者固定区块 `57,634,560`，hash
  `0x221c4e2d5c9ee8664fe54ad9c6e116fce900abb245a3194b61cbb6fa80004bb2`。
- Robinhood 官方公共 RPC 与 Blockmachine 公共 RPC 对 PoolManager、PositionManager、StateView、
  Permit2、基准 hook、PONS、USDG 的 runtime keccak，以及完整 PoolId/slot0 状态取得一致结果。
  核心与基准池身份检查通过；项目自己的 hook/executor 尚未部署，相关权限位、角色、绑定和状态
  仍为 `UNKNOWN`，所以完整 `identities_verified` 仍为 false。
- PONS/USDG 基准 PoolId 为
  `0x374531f25cfb1b92f6d03b946598d53dd934fba6e138bcece153a52113aa73a5`。
- StateView 返回 `sqrtPriceX96 = 1461446703485210103287273052203988822378723970341`、
  `tick = 887271`、`protocolFee = 0`、`lpFee = 0`。价格处于 v4 上界附近，不能据此证明双边深度、
  可路由成交或生产经济可行性。
- 在固定区块 `57,634,560` 对官方 V4 Quoter 通过两个提供者分别运行 12 个真实只读报价：
  exact-input/output、两个方向、每组 3 个金额在两端全部同样失败。`zeroForOne`
  返回 `NotEnoughLiquidity(poolId)`；`oneForZero` 返回 `PriceLimitAlreadyExceeded`，其中当前价与价格
  上限均为 `1461446703485210103287273052203988822378723970341`。这不是 ABI 失配，而是该池当前不可
  双向报价的直接拒绝证据。
- 身份证据包：`reports/evidence/deploy-check-robinhood-benchmark-57634560-v10-2026-09-08/`；
  `MANIFEST.sha256` 全部通过。核心、token、PoolId/slot0 与 provider diversity 项通过；未部署项目
  hook/executor 使最终结果保持 `identities_verified: false`、`execution_eligible: false`。
- 路由证据包：`reports/evidence/route-probe-robinhood-benchmark-57634560-v5-2026-09-08/`；
  `MANIFEST.sha256` 全部通过，两个提供者的三个 runtime hash、Quoter 绑定和全部报价解释一致，
  因而 `identities_verified: true`。报价网格本身失败，`route_probe_verified` 与
  `execution_eligible` 均为 false，并直接排除当前 PONS/USDG 基准池。

## 已完成工程项

- `run_deploy_checklist.py`：14 项双提供者、固定区块身份与控制状态检查；完整 slot0；配置和证据不
  落 RPC URL/凭据；区块号调用由前后相同 header 约束，发现 reorg 即废弃整包。
- 双提供者实测修复了 slot0 结构值不能直接放入 Python `set` 的缺陷；现以规范 JSON 比较完整
  slot0，并新增相同/不一致/缺提供者回归测试。
- `DeployOasisContracts.s.sol`：外部签名器兼容的 factory/hook/executor 部署和不可变量验证。
- `ConfigureOasisPool.s.sol`：治理角色配置费用、白名单和唯一池初始化。
- `MintOasisCanary.s.sol`：有界首次 ERC-20/ERC-20 建仓、两层短时授权及立即清零、单 NFT executor
  授权和完整后置条件。
- `RevokeOasisCanaryAuthority.s.sol`：部分失败或人工停机时独立撤销两层 token 与 NFT 权限。
- `run_v4_route_probe.py`：固定区块、双提供者、双方向、exact-input/exact-output 分档 Quoter 探针，
  同时固定 Quoter/Universal Router/PoolManager runtime 与 Quoter 绑定；结果不会授权执行。

## 尚缺生产事实

1. 至少两个带生产 SLA 的独立 Robinhood 主网 RPC；双公共 RPC 已完成预检，但公共端点不适合作为
   生产执行与监控的唯一来源。
2. 可生产的目标币对/新池方案以及可执行路由、双边深度、退出深度和 token 风险证据。
3. 相互独立的 deployer、governor、guardian、LP owner 地址和外部签名器；当前测试账户不能同时
   承担这些角色。
4. 预注册的 canary 资本、每日损失、gas、滑点、库存、回撤上限和观察窗口。
5. 持续 shadow、最小 canary 的真实收据与逐币对账；这些时间序列证据不能由单元测试代替。

商业第三方审计已按项目所有者决定从当前项目自有最小 canary 阻塞项中移除。独立交叉复核仍应
以可追溯 reviewer 标识和证据哈希进入 WP5/WP7/WP8。

Robinhood 官方连接文档明确将公共端点标为限流且不建议生产使用，并列出 Alchemy 等生产提供商；
Uniswap 官方部署表确认了 chain `4663` 的 Quoter `0x8dc178efb8111bb0973dd9d722ebeff267c98f94`
和 Universal Router `0x8876789976decbfcbbbe364623c63652db8c0904`。这些官方身份已进入工具
和运行手册；双提供者直接报价已经取得，结果明确失败，标准路由选择与真实 settlement 因而
仍未进入 canary 阶段。
