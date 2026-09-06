# 多链配置与逐仓位核心结算证据适配器

日期：2026-09-06。结论：已交付可复现的核心 delta 证据适配器，并用真实 Base Sepolia
样本完成离线重建；**完整逐仓位所有人结算、全成本收益和上线验收仍未完成**。

## 改动与安全边界

- 配置不再隐式选择 Robinhood `4663`。必须显式提供 `chain_id` 或 `OASIS_CHAIN_ID`；
  缺失时失败。通用配置与结算用例覆盖 Ethereum、Base、Robinhood、BNB 链 ID 和测试网 ID。
  这证明链身份隔离与接口可复用，不证明每条链已具备 v4 实盘能力。
- 章程、工程说明和 LP 文档把 Robinhood 定位为初始研究基准；其他 EVM 链按独立部署、
  字节码、成本、finality、fork 和 shadow 证据晋级。用户简称 `bas` 尚未确认为具体链。
- `position_settlement.py` 绑定原始 chain/header/receipt/trace/code/header 请求，校验
  交易、区块、日志位置、PoolKey、NFT salt、范围、caller 和精确 ABI 返回值。
  原始数据限制 5 MB、10,000 个调用节点和 128 层调用深度。
- 解析 CALL/STATICCALL、DELEGATECALL、CREATE/CREATE2 的执行地址；代理的实现地址不能
  冒充子调用发送者。PoolManager 修改仍必须是直接 CALL。其他类型、回滚子树、重复目标动作、
  截断返回、错误请求和无法唯一归因的事件被拒绝。
- `position_pipeline.py` 接受可选 `settlements`，只为完全匹配的事件保留核心结算引用。
  `replay_position_settlement_evidence` 从存储中的原始字节重新计算，并拒绝被篡改的解释。
- `callerDelta` 属于 PositionManager，可包含本金、费用和 hook delta；不是 NFT owner
  实际到账。所有结果保留 `owner_amount0/1=null`、`settlement_complete=false`、
  `execution_eligible=false`、`net_pnl=null`。
- `gasUsed * effectiveGasPrice` 仅作为整笔交易的执行 gas 原生币最小单位观测；没有把它当作
  所有链的完整成本，也没有把同一交易的总费用分摊给某个 NFT。

## 真实测试网证据

端点为 [Base 官方公共 RPC](https://docs.base.org/base-chain/api-reference/rpc-overview)，
合约地址来源于 [Uniswap 官方部署表](https://developers.uniswap.org/docs/protocols/v4/deployments)。
采集器不持有密钥、不签名、不广播，也未部署本项目 hook。

首次固定区块 `46446912` 的 2,000-block lookback 未发现变更；随后公共 RPC 出现超时和
HTTP/2 错误。加入逐响应持久化，并使用系统 curl 的 HTTP/1.1、TLS 校验后，扩大至
50,000-block lookback，在结束区块 `46447837` 收到了 30 条修改日志。
这是单来源发现窗口，不是经过双来源和完整区块谱系验证的历史回放。

最终保存并成功重建的样本：

| 项目 | 值 |
|---|---|
| Chain ID | `84532`（Base Sepolia） |
| 区块 | `46440623` |
| 区块 hash | `0x02dc6183137f45c7ab7df7b2b6f91801953bf7110af154983f4f396bc3b7d903` |
| 交易 | `0x6cdbe27248149e2f159030a53fa088960ed22f18f60c0870c1efb013944d602d` |
| 交易 index / log index | `6 / 44` |
| PositionManager / NFT | `0x4b2c77d209d3405f41a037ec6c77f7f5b8e2ca80 / 27369` |
| PoolManager | `0x05e73354cfdd6745c338b50bcfdfa3aa6fa03408` |
| StateView | `0x571291b572ed32ce6751a2cb2486ebee8defb9b4` |
| PoolId | `0xca703923401208feb8abe27f7feb14656c7ea0a286c14dd815d6a8a436a44bfe` |
| token0 / token1 | `0x036cbd53842c5426634e7929541ec2318f3dcf7e / 0xdb2b389860f036d11d3c08eaefec242b20ca03ee` |
| caller delta，分别为各自 token base units | `0 / -11641294639626340598824` |
| accrued fee delta，分别为各自 token base units | `0 / 0` |
| 整笔交易执行 gas，原生币 base units | `20085342000000` |
| 核心证据最后响应时间 | `2026-09-06T03:09:52.626156+00:00` |
| 核心解释 SHA-256 content hash | `58c69cd3077db93cff0bbd8a96975fd5b4627f2c39f1dfadb063f20d6149879a` |
| 原始响应 envelope SHA-256 | `28a47a7bf15bd94f6629befdf15c52e97a56750f15c6435450975496c4067d3c` |

同笔交易包含 NFT salt `27345, 27346, 27364, 27365, 27368, 27369` 六个仓位修改。
适配器只提取目标 NFT 的返回值，不把整笔交易的净转账当成该 NFT 的结算。
该公开池的 fee 字段为静态 `3000`，不是本项目动态费 hook 的测试。

观测到的 runtime keccak：

- PoolManager：`0x03c45db6d09b14da7c1f7239a5a49697f976d395277e6d2acb6fbed3f9e0249f`（24,009 bytes）。
- PositionManager：`0xe8329b35b8b34290b6cf03affc0836f7b23205229cc96ffdc66544b93112c076`（23,877 bytes）。
- StateView：`0x0e54f1b36938b15a1d4207f9cfe5e13eee61c5393c7a498e859d5bdc86999c39`（3,531 bytes；发现窗口结束区块观测）。

PoolManager/PositionManager 还在目标交易区块用 EIP-1898 重新取码，哈希一致。
这些哈希不是部署源码等价、安全或最终性的认证。

原始 JSONL、快照与核心证据数据库、总结位于：

- `reports/evidence/base-sepolia-2026-09-06-recorded/`
- `reports/evidence/base-sepolia-2026-09-06-recorded/resumed-http1/summary.json`
- `reports/evidence/base-sepolia-2026-09-06-recorded/resumed-http1/evidence.db`

首次失败会话也保留；成功快照不会因为后续 trace 解析失败被丢弃。
原始字节经过现有 snapshot 重建器校验，核心证据经新适配器离线重建。
`test_recorded_testnet_settlement.py` 将这份真实公开数据作为离线回归资料，不在测试时请求网络，
并验证即使保留合法原始响应，伪造解释中的金额仍会被拒绝。

## 可重复检查与结果

```bash
python3 -m pytest tests/unit/test_position_settlement.py tests/unit/test_recorded_testnet_settlement.py
make check
git diff --check
PYTHONPATH=src python3 scripts/probe_v4_testnet.py --lookback 50000 --output <fresh-directory>
```

- 聚焦适配器及真实样本回归：**57 passed**。
- 最终 `make check`：**348 passed，67 subtests passed**；Ruff/lint/format 通过，mypy 66 个源文件通过。
- 合约：18 项测试/组通过，0 failed，0 skipped；已有 fuzz/invariant campaign 随全仓库 gate 运行。
- 覆盖率工具在 `/tmp/oasis-settlement-qa` 隔离环境安装，未修改系统或项目依赖。
  该环境继承系统包，安全工具引入的依赖版本与原始运行环境不完全相同；原始环境的 `make check`
  独立执行。最终覆盖率数据见保留的 `coverage.txt`，不作为上线覆盖率门槛已满足的声明。
- 现有 Foundry advisory lint 和本地 signature-cache 权限警告仍存在。

安全检查没有被描述为“全部通过”：

| 检查 | 实际结果 |
|---|---|
| Bandit 1.9.4，`src scripts` | exit 1：20 LOW、1 MEDIUM。新适配器无报告项；探测脚本包含两条 subprocess LOW 提示，使用固定 `/usr/bin/curl`、固定公共端点、参数列表及 `shell=False` 默认值，无命令插值 |
| Slither 0.11.6 | 分析 22 contracts、102 detectors；6 条 Informational，未报告 High/Medium/Low；CLI exit 255，不能写成全绿 |
| pip-audit 2.10.1 | exit 1：原始环境项目运行时依赖闭包 43 包中，4 包有 52 条已知 advisory 记录 |
| 独立审计、专用密钥扫描 | 未完成；人工审阅不是替代品 |

依赖记录分别为 `aiohttp 3.9.1`（41）、`idna 3.8`（2）、`requests 2.31.0`（3）、
`urllib3 2.2.2`（6）。这些是数据库报告数量，不等于已证明本项目具有 52 条可利用路径。
依赖清单来自原始 Python 的项目依赖闭包，而不是安装安全工具后的隔离环境。
本轮没有升级共享环境的包；依赖修复和可复现锁定需要单独验证，当前仍是安全阻塞项。
扫描报告及审计输入清单位于 `reports/evidence/quality-2026-09-06-settlement/`。

## 未验收事项和下一步

1. 衔接 PositionManager actions 与 SETTLE/TAKE、Permit2、ERC-20/native transfer、退款和
   recipient override；证明动作时点的所有权、付款人、收款人以及逐币种余额守恒。
2. 完整链成本与多仓位费用分摊、价格证据、失败交易成本、NFT 转移对价和样本外净收益。
3. 修复并复验依赖审计项；完成专用密钥扫描和独立安全验证。
4. 每条目标链各自的 pinned fork、真实 Permit2 完整生命周期、shadow、canary 和安全退出。

没有 Git commit、部署、签名、广播、线上配置变更或资金支出。工作区在开始时已有大量未提交
改动，本轮仅增量修改相关配置、文档、适配器和测试，未回滚其他贡献者工作。
