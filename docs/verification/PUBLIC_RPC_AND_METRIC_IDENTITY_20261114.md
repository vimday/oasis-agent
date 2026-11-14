# 公共 RPC、多链部署与 Metric provider 诊断

2026-11-14（Asia/Shanghai），基线 `237c98b`；作者及内部自审 Codex，无独立 reviewer
sign-off。本记录对应[持续执行清单](../strategies/NEXT_STAGE_EXECUTION.md)，入口与格式见
[公共链观察](../strategies/PUBLIC_RPC_OBSERVATION.md)。原始公开网页和通信在本机忽略目录
`reports/research/next-stage-20261114/`，不将第三方源代码副本或运行材料提交。

## 原块 provider 身份

沿[上次失败报价](METRIC_V3_QUOTES_20261114.md)的 getImmutables 找到 immutable provider，
固定 Ethereum 块 26410155，hash
`0xb4d09499b94a431dc10366c08f658c79b910d7e9a3d39cef1d1ae1ce7678717d`，timestamp 1794622367。
原报价 22/40 请求账本未更改、未重试。

| 对象 | 观察 |
| --- | --- |
| pool | `0xe9c19e5bd10e5096519fe1d2a9d783f79b8211da` |
| immutable provider | `0xf17ad25ce854d9eb044e5896e3e8aecb5e9e43da`，AnchoredPriceProvider |
| provider runtime keccak | `0xc88d5b49c64cf3b94f6e76cd115e29643ecf1cdf3c7dfa1204802386bfabaf7e` |
| offchainOracle | `0x30a88bf894d1b806125a23ebf23d715496fd7102`，kind pyth-lazer，version 1.1.0 |
| oracle runtime keccak | `0xd7169b763899b0e9a19b2b26ed2b566375440905f62964bd08817f9f1cb37ca2` |
| reference 配置 | MAX_REF_STALENESS、baseMaxStaleness、quoteMaxStaleness 均为 15 秒；feed ID 分别为 bytes32(2)、bytes32(7) |
| 其他配置 | token0 WETH、token1 USDC；MUTABLE_PARAMS false；source `0xad75dfdc62d65ad8416e510b6764bd165903bbad` |

[Provider 浏览器页面](https://etherscan.io/address/0xf17ad25ce854d9eb044e5896e3e8aecb5e9e43da#code)
及 [oracle 页面](https://etherscan.io/address/0x30a88bf894d1b806125a23ebf23d715496fd7102#code)
发布的 runtime 与上述链上字节匹配。页面声称源码 Exact Match；未在本机以对应编译器
独立重编译，因此源码对应关系仍含浏览器证明假设。provider 主源码 SHA-256 为
`98171315bbff156d91f0038b768ef562115348f9b82cff76ffb272cc748ff4d0`，oracle 主源码为
`ed9727934ab071641fdfcba64ceee065e51cf470e71546b69ff171db262cd9ea`。

独立配置诊断 scope 限 64 次免费读取。provider-config 20 次完整；oracle-state 第 6 次
读取 getOracleData 回滚，选择器 `0x987d5c54` 与 ReadDisabled() 一致，累计 26/64 后 STOP。
源码的继承 raw getter 无条件拒绝读取，不能因为 ABI 列出它就假定可调用。
失败请求/响应 SHA-256 分别为
`74672f6181844dbcc49c672c7faa73faf0db1f0685c484487f31d7e010eb0428`、
`ec9b5a9a8e7ca0eb28b0c3ef136846797e2c7cce24afe959fae2a959e0e013d2`。

新离线诊断使用 price 与 priceGuard 语义，但没有联网重试或移除 STOP。
真实报告明确为 incomplete_evidence：两个 feed 的 price/time/guard 均缺失。15 秒配置已
观察，**原 FeedStalled 是否由年龄、spread 或其他 guard 导致仍未证实**。零 origin
可跳过部分模拟读取门禁，不代表真实合约调用权限；custom source shaping 未核验。

Metric 官方[鲜度说明](https://docs.metric.xyz/how-pools-work/freshness-and-ordering)及
[深度接口说明](https://docs.metric.xyz/reference/api/retrieve-current-bid-and-ask)将 Ethereum
条件更新与一般 latest 状态区分。本轮进一步找到
[Titan taker 文档](https://docs.titanbuilder.xyz/propamms/takers)：它公开列出该 Metric oracle，
有无需认证的 pAMM 状态接口；这成为下一项独立研究，不把构块者状态声明混为已落链状态。

## 四链身份观察

四个新范围各上限 32 次，分别只研究对应链的块头与五个官方部署地址。使用官方公开的
[Ethereum](https://ethereum.publicnode.com/)、[Base](https://base.publicnode.com/)、
[Robinhood](https://robinhood.publicnode.com/) 和 [Elysium](https://elysium.publicnode.com/)
免费匿名端点。它们属于同一运营商。无付费 RPC、认证、签名、广播或生产状态修改。

| 链 | 范围结果 | 原账本累计 |
| --- | --- | --- |
| Ethereum 1 | 块 26410382 头与五合约 getCode 完整 | 10/32 |
| Base 8453 | 块 57173599 头与五合约 getCode 完整 | 10/32 |
| Robinhood 4663 | 头在请求发出后、响应收到前生成；旧采集器错误地按请求开始时间拒绝 | 2/32，STOP 保留 |
| Elysium 99901 | 块 6246555 头完整；首个 EIP-1898 getCode 返回 HTTP 错误及 RPC −32602 | 5/32，STOP 保留 |

Robinhood 时间校验已改为响应接收边界，新增回归；原失败不改写、不重试。
Elysium 不自动降级参数，也不据单次拒绝认定整个节点或链永久不支持历史读取。
本轮 RPC 共 53 次（provider 26，加四链 27），均原账本预占，失败计数保留。

Ethereum 头 hash `0x82eee2146b1ac29bc5e8fe0ef9f9193e1cc0dbabfa2ac385a18723a7df0094d0`；
Base 头 hash `0xad5a8df59aff7c172c7527dcd150960ec4f97037c89b9bd5d1d896453d6dbd2a`；
Elysium 头 hash `0xb18fdb292276964f5294086f3ff0f7de59e13302c8d86350192cb47f67359df9`。

使用[官方部署表](https://docs.metric.xyz/reference/supported-chains-and-deployments)的地址，
Ethereum/Base 的 factory、quoter、compressed oracle runtime 分别相同：

| 对象 | runtime keccak |
| --- | --- |
| factory | `0xe34ec675aad2fc67175956d951b49d182c1289638daa4438fbf80d2ade12291e` |
| quoter | `0x3b74842f3e4266663f706ee356c2ad16911cb978240f9bbc76c495d1c5b18d34` |
| compressed oracle | `0x546b1ee252053f41cd7672c50a59d16dde0d7aed8cb753c413e0ce5eddf8f340` |
| Ethereum router | `0xa85888d310ce7dbed8e419f70d76931771bbbc9aaa0fc1949da9e7b5afdc4a5f` |
| Base router | `0x42cef52002fbc056a2c8adbc620262388ec5b0d53dd855201355bc59c622c2fc` |
| Ethereum liquidity adder | `0xd27c6ec1e8dc018344417ce6859f3451001ffe33223c5fe88d3fe6303b541fef` |
| Base liquidity adder | `0xf999c2121c4251f59a7b4fdf660638cfe3882ff724ec87203a212d3a7591f4f3` |

同地址路由器和 adder 代码不同，符合逐链 constructor/wrapped native 配置需要单独固定
的研究边界；没有证明具体差异原因、完整源码等价、池访问、流量、深度或收益。

## 留存与验证

| 文件（相对本机研究目录） | SHA-256 |
| --- | --- |
| provider-campaign/provider-config/capture.json | `9a6d2e2c0a3db6d5dcfbce929a76c0dbc8d3025ed87654c1598d3edc9b91b6ce` |
| provider-campaign/oracle-state/capture.json | `11c859c8bd05c9b665d10bf676730ec9942abb1ff624c17f8febcd9242723d12` |
| multichain/1-campaign/deployment-profile/capture.json | `929f6e41d902a47fa17866e1885ee552ee4b5769c4c86ac24c1bf19df0d411ef` |
| multichain/8453-campaign/deployment-profile/capture.json | `64e94e3d81a005a8d0960c48dd5c49dada0061168ccb0e28d8b1845b28e665ce` |
| multichain/4663-campaign/head/capture.json | `9a21f1a20302bca58c9cfdfbbfb5eb6fe99205387f0f1952d7bb8490c52cb64f` |
| multichain/99901-campaign/deployment-profile/capture.json | `168e6431b4f51de2f7be05f736abf9157108901788270f43f157ace9ae2a16f6` |
| oracle-diagnosis.json | `d2df01fcccd9d6249c93a7e638d9ce08e09706dcbfe59221d26dce1a06766d4e` |

新增采集器验证原始字节、上下文、原预算、期限、并发与崩溃行为；新增 oracle 诊断只读
留存证据并保持未知。初次静态检查的类型变量复用和 import 顺序问题已修正。45 项采集器、
23 项 oracle 合成测试通过；与原 Metric/V3、RPC budget 合计 168 项通过，日志
`focused-combined.log`。首次安全门禁把采集名称的字母数字白名单字符串识别为高熵候选；
人工确认它是验证字符集，改用等价正则，未添加秘密扫描豁免。原失败日志保留，受影响
45 项回归、lint/format 和完整安全门禁重跑通过。

| 检查 | 结果 / 本机日志 |
| --- | --- |
| 聚焦组合 | 168 通过，退出 0；`focused-combined.log` |
| 名称校验修正回归 | 45 通过，退出 0；`focused-name-guard.log` |
| make check | 退出 0；5,779 Python、138 子测试、前端 9 模块、70 Foundry 通过；`make-check.log` |
| make security-check | 退出 0；依赖无已知漏洞、Bandit 无 medium/high、秘密门禁通过；`security-check-final.log` |
| make contract-security-check | 退出 0；80 合约、102 detectors、0 findings；`contract-security-check.log` |
| 文档与入口 | 52 个本地链接目标、两入口 help 通过；真实诊断重复回放逐字节一致，仍为 incomplete_evidence |
| diff 与提交范围 | diff --check 通过；仅任务的 11 个源码、测试及文档文件；原始采集、源码副本和日志未入 Git |

当前下一步：构块者状态声明的独立有界采集与严格离线解释、事件游标和前瞻覆盖；缺失的
原块 feed、独立源、整笔模拟、历史债务及精确 prestate 仍在持续清单中。已停止的 campaign
不得通过新账本、复制账本或自动重试恢复。本次源码变更没有产生资本或发布权限。
