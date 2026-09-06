# 续接收尾：依赖审计、双链 pinned fork 与全成本模型

日期：2026-09-06（Asia/Shanghai）  
范围：续接另一 AI 未竟工作；只读采集、本地 fork 验证、核算模型扩展；无部署、无广播、无资金。
结论：依赖漏洞清零、bandit 清零、slither 可复现；Robinhood 与 Base Sepolia 的 pinned fork
生命周期测试均 PASS；全成本模型补入失败交易成本与机会成本。shadow、独立安全审计、样本外
净收益与多仓位费用分摊仍未闭合，不放行上线。

## 一、分步提交

工作树按逻辑分四组提交，随后续接工作再分两组提交，共六笔：

| commit | 内容 |
|---|---|
| `2ca694b` | slither 加入 dev 依赖；离线重新生成哈希锁定文件 |
| `58515f6` | RPC 传输异常收窄为 `(ValueError, TypeError)`，注释 B310 白名单控制 |
| `ab1cf99` | L2 fork 头经 `l1BlockNumber` 记录；状态探测诊断非 archive 端点 |
| `c02f68f` | 归档依赖审计输出（pip-audit）并刷新质量 manifest |
| `48178fa` | L2 fork 钉块改为时间戳 + 暴露 L1 编号 + codehash 状态钉 |
| `892cb05` | 归档 robinhood mainnet 与 base sepolia pinned fork 证据 |

## 二、门禁与安全扫描

`make check` 全绿（exit 0）：compileall、ruff check/format（116 文件）、mypy（67 源文件）、
pytest **356 passed, 70 subtests passed**、forge fmt/build、`FOUNDRY_PROFILE=ci forge test`
（18 项/组全过）、forge lint（仅 advisory notes）。

`make security-check` 全绿（exit 0）：

- pip-audit（`--require-hashes -r requirements-dev.lock`）：**No known vulnerabilities found**。
  旧档记录的 4 包 52 条 advisory（aiohttp 3.9.1 等）已通过锁定升级消除。
- bandit `-ll`（src scripts）：**No issues identified**，0 Medium/0 High；此前唯一 MEDIUM
  （B310 urlopen）以 `# nosec B310` 加白名单/禁重定向说明闭合。
- slither 0.11.6：在 `VIRTUAL_ENV` 指向项目 `.venv` 时把 solc-select artifacts 重定向到
  可写目录，可复现运行；分析 22 contracts、102 detectors，仅 Informational（assembly、
  naming-convention、too-many-digits，多数来自 node_modules 依赖），无 High/Medium/Low。
  沙箱禁止写 `~/.solc-select`，故必须带 `VIRTUAL_ENV`；CLI 因 Informational 结果仍非零退出，
  不作为绿色门槛，仅作为证据归档。

## 三、Robinhood 链 pinned fork

预检（`scripts/run_pinned_fork.py --network robinhood --block 55780568`）记录：

- 链 4663，L2 高度 55780568，块哈希 `0x4dad0afe…`，父哈希 `0x003ba0d2…`，
  L1 编号 25916654，头时间戳 1788677834。
- 状态探测 `eth_getCode`（foundry UA）在历史块可用（`available=true`），
  三份合约 runtime keccak 全部取得：PoolManager/PositionManager/Permit2。
- 证据归档：`reports/evidence/robinhood-fork-2026-09-06/`（preflight/result/forge.log/
  summary/manifest）。

关键实证（`ProbeFork.t.sol`，本链 L2 语义）：

- Foundry 1.8.1 把 fork 头 EVM `block.number` 置为 L1 编号 25916654；`block.timestamp`
  与预检记录一致。
- blockhash 窗口为标准 256 块（`num-257→0`、`num→0`、`num-256..num-1` 非零），但
  `blockhash(num-1)` 的值（`0x5fd591aa…`）既不是 L2 块 25916653 的哈希（`0xcf1c7beb…`），
  也不是以太坊 L1 块 25916653 的哈希（`0x30e12758…`，经两个独立公共 RPC 确认），与
  keccak 变换也不匹配——推断为 Foundry 按自身 header 编码派生，无法独立复现，故不作钉点。
- 因此 L2 分支钉块改为：暴露 L1 编号相等 + 头时间戳相等 + 三份 codehash 相等（状态级最强
  钉点），blockhash 窗口仅作节点健康检查；标准链分支保留父哈希 blockhash 钉。

结果：`test_mintSwapCollectRevokeUnwindRealPermit2` **PASS**（gas 1893314），exit 0。

## 四、Base Sepolia 回归

`--network base-sepolia --block 46455055`：标准链分支（l1=0）不受影响，
同一生命周期测试 **PASS**（gas 1893278），exit 0。
证据归档：`reports/evidence/base-sepolia-fork-2026-09-06/`。

## 五、全成本模型扩展

`CostModel` 原只有 gas/rebalancing/adverse-selection/inventory-risk 四项；按章程
"失败执行、机会成本"入账要求，补入两个必填非负项（缺省即构造失败，杜绝静默为零）：

- `failed_tx_cost`：失败交易成本；
- `opportunity_cost`：闲置资本机会成本。

`calculate_lp_net_yield` 将两项计入总成本后求净收益。新增三条测试：总成本合计、失败交易+
机会成本确实被扣减、负值构造被拒。`tests/unit/test_farming.py` 8 项全过。

## 六、仍未闭合（不构成放行）

1. shadow 运行与 canary 部署门槛（需要外部签名器与最小资金账户策略）。
2. 独立安全审计与独立 RPC 提供方最终性；单一公共端点证据不视为最终性证明。
3. 多仓位费用分摊（时间加权流动性份额）、价格证据接入、失败交易占比统计、NFT 转移对价、
   样本外净收益基线对比。
4. 真实 Permit2 生产生命周期、恶意代币/重入攻击面、EOA 原生币（native）收付款归属。
