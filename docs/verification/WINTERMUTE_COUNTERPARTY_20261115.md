# Wintermute 对手方观察：归档读取被拒绝

2026-11-15（Asia/Shanghai）；作者/内部自审 Codex，无独立 reviewer sign-off。本记录承接
[公共 RPC 观察入口](../strategies/PUBLIC_RPC_OBSERVATION.md)，只处理只读链上观察，不评估
收益、不构造交易。

## 研究问题与材料边界

二手研究报告称，地址 `0x51c72848c68a965f66fa7a88855f9f7784502a7f`（标签
`Market Maker: 0x51c7...2a7f`）在 Robinhood Chain 与 Base 同址持有金库合约，并在
Robinhood Chain 直接持有 PONS。该报告同时给出约 1.21 亿美元聚合 NAV、约 31 万美元
PONS、约 656 万美元 Robinhood Chain 资产及 `getAllApprovedTokens()` 可枚举白名单等
数字和接口声明。这些均为未经一手核验的二手材料，不能作为策略、LP 经济或执行准入证据。

本轮预先固定的验证目标是：在固定块上读取该地址的 runtime code、原生余额、PONS 余额、
PONS/USDG 元数据，并尝试读取其授权代币集合。任何一项未取得原始字节，都保持未知。

## 事先固定的范围

使用仓库既有公共采集器，冻结来源、链 ID、请求上限、期限与零重试规则：

- 来源：`publicnode_robinhood`（4663）与 `publicnode_base`（8453）。两者属于同一运营商，
  不构成独立双源。
- 原 scope：Robinhood 最多 24 次、Base 最多 12 次，`max_capture_seconds=60`，公开匿名
  endpoint，无认证、无凭据、无 value、无 state override、无广播参数。
- 先观察 head，再以该块号/hash 冻结 `oasis-public-rpc-read-plan-v1`；每次读取使用
  EIP-1898 `requireCanonical`，首尾块头必须一致。失败一次即停止，不自动重试。
- 采集账本与 STOP 保留在本机忽略目录；未复用旧账本、未重置配额。

首次运行在沙箱内发生 TLS 连接失败，`curl` 对三个 PublicNode 主机均返回
`SSL_ERROR_SYSCALL`；同一命令在沙箱外可完成 TLS 握手。该失败保留在
`campaign/rh-head/`，预算计数 1，未把它伪造成 RPC 结果。随后以新的独立 scope
`wintermute_observation_sandbox_tls_retry` 记录环境修复后的运行；原失败账本未删除、
未替换。

## 实际观察

### 成功的 head 观察

| 链 | 来源 | 块号 | 块 hash | 块时间 (UTC) | capture SHA-256 |
| --- | --- | --- | --- | --- | --- |
| Robinhood 4663 | `publicnode_robinhood` | 114863165 | `0x1000d52a28c7fb8ffa97f91873a0a51a34d098487ab5b38ccdb88e78e346b87a` | 2026-11-15T09:30:12Z | `62a14013e4b0f6629767eeef8cda0bd8c6a2c06250ce384bf72f402aebfedab4` |
| Base 8453 | `publicnode_base` | 57723259 | `0x2fc3438c1f708ebb20af811d2f5d76b258bc19c0322d9b78329e7337197d1890` | 2026-11-15T09:30:27Z | `289f087b127280af32595a41c526b50df7a8a4cda0b0a87eccda90a0bd7db6fc` |

两次 head 观察均为 `complete`，链 ID 与块头一致，原始 HTTP 字节与预算计数可离线回放。
它们是单来源、本机时钟观察，不是独立最终性证明。

### 固定块读取在第三个请求停止

Robinhood 固定块计划在块 114863165 / hash `0x1000d52a...` 上依次请求 vault code、
vault 原生余额、PONS `balanceOf`、PONS `decimals`、PONS `symbol`、PONS code、USDG
`balanceOf`、USDG `decimals`。实际只完成前两个请求：

- 请求 1：`eth_chainId`，HTTP 200，`0x1237`（4663）；
- 请求 2：`eth_getBlockByNumber`，HTTP 200，块 hash 与计划一致；
- 请求 3：`eth_getCode(0x51c7..., blockHash)`，HTTP 403，响应体为
  `{"jsonrpc":"2.0","error":{"code":-32602,"message":"Archive requests require a personal token. Get one at: https://www.allnodes.com/publicnode"},"id":3}`。

该失败使原 campaign 进入 `STOP`，未继续请求，也未读取 Base 固定块。原预算计数为
Robinhood 5/24、Base 2/12。失败的 `rh-core/capture.json` SHA-256 为
`69cc7ca8e6d38d88e9e28430488d03fca34095f4630f756d1970a7486f02297b`，403 响应体
SHA-256 为 `a6d3b310fda9ecc7095ed950ab34fb484e2edc41db7579af4e5490bff95a442a`。

另对配置中的官方公共端点 `https://rpc.mainnet.chain.robinhood.com` 做了两次只读诊断。
该诊断未进入上述原预算账本，也不作为留存证据：`eth_chainId` 返回 `0x1237`（4663）；
对同一块 hash 的 `eth_getCode` 返回
`{"code":-32000,"message":"historical state 45cede7d... is not available"}`。因此免费
官方入口同样不能替代归档 provider。

固定块轮次**没有**取得 vault runtime code、原生余额、PONS 余额、PONS/USDG 元数据或
授权代币集合。latest 筛查另见下节；报告中的美元估值和可枚举白名单仍未验证，不能据此
改变 PONS/USDG LP 经济结论或任何资金分配。

### latest 筛查：读取到值，但块头在采集期间推进

为区分“归档不可用”与“地址无状态”，另立 `wintermute_latest_screening` scope，使用
`oasis-public-rpc-latest-read-plan-v1` 在 `latest` 标签读取同一组状态。该计划首尾各读
一次块头，要求完全一致；它不是固定块证据，不能进入 LP 经济或执行准入。

计划共 11 个请求，前 10 个成功，最后的首尾块头比较失败：打开块为 114871811
（`0x75257499...`），关闭块已到 114871821（`0xf7d553e9...`），期间推进 10 块。采集
因此为 `incomplete`，原预算 11/20，campaign 进入 `STOP`。保留的 10 条成功记录显示：

| 读取 | 结果 |
| --- | --- |
| vault runtime code | 20,099 bytes，keccak `0x7cb680c6d2762d069ddf083033e0fab0362046841cafc20fbfeae2395d0b1b83` |
| vault 原生余额 | 0.006229999999909888 ETH |
| PONS `balanceOf(vault)` | 2,189,639.756625049 PONS（18 decimals） |
| PONS `symbol()` | `PONS` |
| PONS runtime code | 5,274 bytes，keccak `0x16c3d3ede897688ddff79262606f13bead398332e65001f192460fbac4e1fb85` |
| USDG `balanceOf(vault)` | 2,057,687.377907 USDG（6 decimals） |
| USDG `decimals()` | 6 |

PONS 的 runtime keccak 与
[Benchmark identity 配置](../../configs/identity.robinhood-benchmark.example.json) 中
token0 的 runtime pin 完全一致，构成一次独立的代码身份交叉核对。vault runtime code 中
存在 `09b0d50a`、`1626ba7e`、`1c02708d` 三个选择器，分别对应报告所称的
`getAllApprovedTokens()`、`isValidSignature(bytes32,bytes)` 和 `killContract()`；这只
证明选择器出现在运行时代码中，不证明其行为、权限或返回内容。

这些值只支持“该地址在 latest 读取时存在 code，且持有 PONS/USDG”这一筛查结论。由于
块头在采集期间推进，各读取可能跨不同块，**不是**同一块快照；也没有读取授权集合。
不能据此计算 $310k 或其他美元估值，不能改变 PONS/USDG LP 结论或执行准入。

## 解释与限制

- 403 表示该匿名 PublicNode 入口要求个人 token 才能读取归档状态，不代表目标地址没有
  code、没有余额或报告为假。失败原因与目标事实必须分开记录。
- 本轮只有单一运营商来源，且时间戳为本机接收时间，不是独立时钟见证。
- 所有输出保持 `finality_independently_verified=false`、
  `independent_providers_verified=false`、`execution_eligible=false`。没有签名、广播、
  部署、授权或生产配置变更。
- 该失败暴露了一个数据源边界：Robinhood Chain 的固定块读取需要归档能力；后续若用公共
  latest 观察替代，必须明确它是筛查而非固定块证据，不能进入 LP 经济或执行准入。

## 复现

网络采集入口与参数含义见 [公共 RPC 观察](../strategies/PUBLIC_RPC_OBSERVATION.md)。
本机材料保存在忽略目录 `reports/research/wintermute-20261115/`，关键命令为：

```sh
.venv/bin/python -m scripts.capture_public_rpc init \
  --scope reports/research/wintermute-20261115/scope-unsandboxed.json \
  --root reports/research/wintermute-20261115/campaign-unsandboxed
.venv/bin/python -m scripts.capture_public_rpc capture \
  --root reports/research/wintermute-20261115/campaign-unsandboxed \
  --plan reports/research/wintermute-20261115/robinhood-head.json --name rh-head
.venv/bin/python -m scripts.capture_public_rpc capture \
  --root reports/research/wintermute-20261115/campaign-unsandboxed \
  --plan reports/research/wintermute-20261115/base-head.json --name base-head
.venv/bin/python -m scripts.capture_public_rpc capture \
  --root reports/research/wintermute-20261115/campaign-unsandboxed \
  --plan reports/research/wintermute-20261115/robinhood-core.json --name rh-core
```

`rh-core` 在第三个请求停止；不要在原 campaign 上重试。复现离线回放使用 `replay`
子命令，输出必须是新文件。

## 材料哈希

| 材料 | SHA-256 |
| --- | --- |
| `scope.json`（首次沙箱运行） | `4326f4fd933d8798b43826cc75dcfdbef085a4c0b87101b0fd21409ddad2f97b` |
| `scope-unsandboxed.json` | `45e4981018d4f9f5a6f2a8db8957020623f77944a18fe488624ed6b87d2cd012` |
| `robinhood-head.json` | `096e4586a42b42a4605d802c912fc945ae0dc777beeb9efcf29ac058884dd1ef` |
| `base-head.json` | `4c073ac1cde60a0fba4e78d713acfddf6a3ffd126a06081339619c790721af02` |
| `robinhood-core.json` | `bcc64b120e00166574bb74dac9e9cc5bce012442eaad998eab797566bad6d6f5` |
| 沙箱失败 capture | `3d6b6fed30dae58c10691cdfaef8855164120eba5001bc439f2a170270e16e08` |
| Robinhood head capture | `62a14013e4b0f6629767eeef8cda0bd8c6a2c06250ce384bf72f402aebfedab4` |
| Base head capture | `289f087b127280af32595a41c526b50df7a8a4cda0b0a87eccda90a0bd7db6fc` |
| `rh-core` 失败 capture | `69cc7ca8e6d38d88e9e28430488d03fca34095f4630f756d1970a7486f02297b` |
| 403 响应体 | `a6d3b310fda9ecc7095ed950ab34fb484e2edc41db7579af4e5490bff95a442a` |
| `scope-latest.json` | `d2bcea384e79807f1a444c102bfc4c32120c9b31baf686af32dd20162bccf18c` |
| `robinhood-latest-core.json` | `3d5bc712434a5b46fd9f9ce402a55efa17cd542d878a459d888b90b7a8342949` |
| latest 失败 capture | `335f7dc9dc22045b2ed0ccb2987cf2004f680d04cec8cd14b7592368831a5785` |
| latest replay | `f3a0b2691317a870e7679526427840db326dbc6a7438398c60155903e576b6b7` |

## 下一步

1. 若需要固定块证据，需在新的独立 scope 下使用具备归档能力的授权 provider；优先争取
   两个独立来源，否则只报告单源观察并保持所有执行标志为 false。
2. `latest` 筛查模式已实现，但 Robinhood Chain 的块头在 11 个请求内推进了 10 块，多读取
   计划很可能跨块。后续只应在单读取计划或明确接受非同一块筛查时使用；它不能替代固定块
   证据，也不能进入 LP 经济或执行准入。
3. 在取得同一固定块的 code hash、PONS/USDG 余额、授权集合和真实 flow 之前，不把
   Wintermute 报告中的持仓数字写入 PONS/USDG 池选择、LP 净收益或竞争假设的结论段。
