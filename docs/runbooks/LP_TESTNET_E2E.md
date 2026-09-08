# LP 本地与 Robinhood 测试网端到端验证

此流程验证 LP 生命周期，不验证收益或生产自动化。测试网 chain ID 固定为 `46630`，RPC 为
`https://rpc.testnet.chain.robinhood.com`，与 [Robinhood 官方说明](https://docs.robinhood.com/chain/add-network-to-wallet/) 一致。

## 已实现路径

`scripts/testnet_lp_e2e.py` 是独立测试进程，依次部署 PoolManager、Permit2 runtime、PositionManager、
两种测试 ERC-20、factory/hook、swap 测试路由器和 bounded executor。随后执行：

1. 配置并初始化独立池，开启测试 owner 白名单。
2. 有界 ERC-20 / Permit2 授权，mint 一个 LP NFT。
3. 两个方向各执行 exact-input / exact-output swap；collect。
4. executor 原子 RECENTER，检查新 NFT 和 liquidity。
5. 撤销白名单，eth_call 验证增加流动性被拒绝；executor REMOVE 全部退出。
6. 清零 ERC-20、Permit2、swap 路由器授权，核对逐币守恒和实际 ETH gas。

这是直接脚本操作的协议生命周期，不是策略引擎自动选择实盘动作。测试 owner/governor/guardian
使用同一测试账户只用于这个自有测试资产场景，不能替代生产角色隔离。

## 复现

先运行 `make check security-check contract-security-check`。Slither 会清理产物，执行脚本前再运行
`cd contracts && forge build`，使测试依赖产物也存在。

本地 Anvil 应仅绑定回环地址，chain ID 为 31337。脚本生成随机本地账户，使用本地余额注入。

```bash
anvil --host 127.0.0.1 --chain-id 31337 --port 8545 --silent
PYTHONPATH=src .venv/bin/python scripts/testnet_lp_e2e.py \
  --network local --broadcast --output reports/generated/local-lp-e2e-new
```

测试网仅在用户已授权使用测试账户和测试 gas 时执行：

```bash
PYTHONPATH=src .venv/bin/python scripts/testnet_lp_e2e.py \
  --network testnet --broadcast --output reports/generated/testnet-lp-e2e-new
```

私钥仅由独立进程从 `.env` 的一个显式 key 字段读取，不通过命令行或环境展开传递；
没有输出原始私钥、签名或 raw transaction 的接口。网络仅有 local/testnet 两个选择，交易
`value=0`；所有被调用的目标来自本轮部署。总 gas 预留上限为 `0.001` 测试 ETH，按账户持久化，
不随重启清零；实际花费可能低于预留。钱包锁与预留文件位于忽略的 `reports/generated/`。
不要删除该文件来绕过预算，也不要在已有未完成交易时启动另一轮。

任何失败都停止流程并记录 HALT。不得以新 nonce 盲目重试；先核对已有 tx hash、receipt 和
账户 pending nonce。此脚本不包含自动恢复或自动清理失败流程的能力，失败后的剩余测试仓位
应根据已部署地址、NFT 与收据独立制定退出步骤。

## 收据复放

```bash
PYTHONPATH=src .venv/bin/python scripts/verify_lp_e2e.py <output>/events.jsonl
```

复放验证 hash chain、35 笔签名/收据关联、nonce 顺序、实际 PoolId、四次 swap 的方向与 500-pip
费率、mint/collect/recenter/remove 的 liquidity delta、8 次逐币守恒及 ETH gas 余额。
原始收据、区块号/hash、采集时间、runtime 哈希保存在日志。仍是单 RPC 提供者研究证据；
对日志的整体重写需要外部锚点检测，成功 receipt 不等于 L1 最终性。

## shadow 与执行协调

`scripts/run_lp_shadow.py --policy ... --input cycles.jsonl --evidence ... --journal ...` 是可运行的
LP 证据消费者。JSONL 提供完整 `LpCycle`；header 和全部策略输入必须与 EvidenceStore 的载荷和
原始证据哈希匹配。金额使用十进制字符串，基础单位使用整数。配置和代码变化需要新日志，
HALT 不会自动解除；该入口永远没有签名权限。真实特征采集器和长期观察仍需另外接入。

`LpExecutionJournal` 按 chain/account/config 绑定 SQLite，序列化 nonce 预留和带证据引用的
状态迁移，并拒绝旧 digest 写入。它不是独立签名策略或最终性证明器，不能单靠调用 `advance`
使一笔交易获得实盘授权。

## 历史研究和发布门禁

`scripts/run_lp_backtest.py INPUT --output RESULT` 接收 `oasis.lp-backtest-input.v1`。输入必须包含
预注册实验锁、walk-forward 的 calibration/purge/evaluation 大小，以及按 block/transaction/log
排序的逐事件 LP 增量。每个事件要求两个独立来源对相同解释达成一致；残差、重复 JSON key、
未知字段、跨链数据、窗口不足和试验次数越界均失败。输出比较 R00/R01/R10/R11 并报告回撤、
尾部损失和逐 fold 的 R11 相对持币下界；`execution_eligible` 永远为 false。

`execution/lp_signer_policy.py` 是无密钥静态授权边界。它规范解码 `OasisPositionExecutor.execute`
calldata，并与完整 LP intent、双来源 pinned simulation、风险决策、代码哈希、nonce、deadline、
gas 和资本上限逐项匹配。它没有签名或广播能力；生产仍要求独立 sidecar/HSM 和角色隔离。

`reporting/lp_readiness.py` 要求 WP0–WP8 每项各有一个带失效时间的结果。后置 gate 通过不能
绕过前置 gate；WP5、WP7、WP8 通过必须记录独立 reviewer。只有全部九项连续有效通过时，
`execution_eligible` 才可能为 true。
