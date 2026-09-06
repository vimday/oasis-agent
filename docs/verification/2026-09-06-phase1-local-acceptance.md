# Phase 1 本地里程碑验收

日期：2026-09-06（Asia/Shanghai）  
范围：共享工作区的只读采集、证据重建、仓位事件回放；测试数据明确为合成数据。  
结论：本地证据回放里程碑可验收，完整自动 LP 和真实资金上线不予放行。

## 本轮交付

1. `farming/position_capture.py`：显式区块号采集、EIP-1898 block hash 固定读取、
   结束区块复查；从保存的 RPC 响应重新运行 PositionManager/StateView 解码，比较整个快照。
   owner、PoolKey/packed info、NFT/core liquidity、fee growth 和未领取费用必须一致。
2. `evidence/rpc_validation.py::restore_exchanges`：从数据库恢复原始请求/响应字节，
   严格检查配对数量、元数据类型、JSON 歧义、十六进制编码、RPC ID 和错误格式。
3. `farming/position_pipeline.py`：把两端快照、完整 PoolManager/NFT 事件接入同一仓位账本。
   实际读取数据库里的主/peer 证据并重新校验原始响应；仅伪造一组合法形状的引用不能通过。
4. 快照类型加固：拒绝用 `100.0` 冒充整数 `100`、用 `False` 冒充 fee growth 的 `0`、
   用 `0` 冒充布尔标记。活跃 NFT 读取拒绝零 owner 和超出协议范围的 tick。
5. 结构化回放结果保留完整证据引用（来源、区块、取证时间和各项哈希），分别输出 token0/token1
   未解决结算，不生成假利润；`execution_eligible=false`，`net_pnl=null`。

整个入口仍标记 `single_provider_research`：事件有 quorum 不能自动提高单来源快照的信任等级。
来源标签不是独立运营商证明；响应与解释一致也不是 RPC 诚实、合约源码一致或最终性的证明。

## 可重复验收

在项目根目录执行：

```bash
python3 -m pytest tests/unit/test_position_capture.py tests/unit/test_positions.py
make check
git diff --check
```

本次 Python 全量结果：**278 passed，67 subtests passed**；Ruff 检查/格式通过（108 文件），
mypy 通过（65 个源文件）。合约已有本地测试另含 18 项测试/组、20,000 个 fuzz cases，
两条组合 invariant 运行 512 轮、65,536 次 handler 调用、零 reverts。
最终 `make check` 和 `git diff --check` 均为 exit 0；Foundry 零失败、零跳过。

验收测试覆盖：

- 原始字节保存/恢复、两端快照和事件合并、NFT owner/liquidity 收尾一致；
- 修改快照数值而保留原始证据、错误 calldata/目标地址、`latest`、非布尔 canonical 标记；
- 漏 chain ID、漏调用、失败响应替代成功响应、漏结束区块、重复请求和区块重组；
- 注入额外调用上下文、数据库伪造原始 envelope、响应元数据错配和重复 JSON 字段；
- 原有自转账、转出转回、分叉所有权回放和未解决结算拒绝输出 PnL 的回归测试。

`make check` 只代表目前实现的工程检查，不代表所有上线门槛。Slither、独立安全审计、覆盖率
验收、依赖/密钥扫描和 Robinhood pinned fork 不得默认为通过；本轮没有部署或修改 Solidity。
现有 Foundry 运行有 advisory lint notes 和本地 signature-cache 权限警告。

## API 接线方式

每份快照使用新的有界记录会话，保留 RpcClient 初始化时的 `eth_chainId` 请求：

```python
# recorder -> JsonRpcTransport(recorder=recorder) -> RpcClient(expected_chain_id=...)
header, snapshot = collect_position_snapshot(
    rpc,
    position_manager=position_manager,
    state_view=state_view,
    token_id=token_id,
    block_number=block_number,
)
capture = PositionSnapshotCapture(
    "rpc-a", header, state_view, snapshot, recorder.exchanges,
)
# opening_capture / closing_capture 必须覆盖同一仓位的完整事件区间。
result = replay_captured_position_datasets(
    evidence_store,
    opening=opening_capture,
    closing=closing_capture,
    pool_manager=pool_manager,
    core=verified_core_range,
    ownership=verified_nft_range,
)
```

上述是库 API 接线示意，不是已有的一键实盘 CLI。RPC 端点和凭据不得放入此文档、
source 标签或取证报告；`rpc-a` 仅是非敏感来源别名。

## 今天不能签字放行的项目

| 验收项 | 当前结论 | 缺少的证据/实现 |
|---|---|---|
| 本地原始响应→快照→事件账本 | 可验收 | 合成场景通过，不冒充真实链运行 |
| 真实 Robinhood 历史回放 | 未验收 | 有界真实双来源快照/事件、独立性和 finality |
| 逐仓位完整 token 结算 | 未完成 | receipt/trace 绑定交易、动作、NFT、币种和付款/收款人 |
| 全成本真实收益 | 未完成 | 已验证结算、估值、gas/失败交易成本、所有权转移对价 |
| 动态手续费超额收益 | 未证明 | Python/Solidity 统一定点控制器、路由弹性、样本外对照 |
| 实盘安全及自动调仓 | 不放行 | runtime/角色证明、真实 Permit2、fork、shadow、安全审计和 canary |

下一实施优先级仍是逐仓位结算证据适配器，不是增加更多策略参数。至少要拒绝“同区块但另一个
交易的证据”“另一个 NFT 的现金流”“混合多仓位但无法归因的净转账”；没有完整 trace 时保持
unresolved。具体经济会计和上线标准继续以项目章程和 Auto-LP blueprint 为准。

本轮未提交 Git commit、签名、广播、修改线上配置或动用资金。保留已有贡献者工作，
在证据与类型边界上增量修改，未回滚或覆盖无关改动。
