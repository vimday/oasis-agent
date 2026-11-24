# Ethereum Bancor 资金解码与七池新样本验证

北京时间 2026-11-24。通用资金回放现在能直接用区块、收据和带日志的调用树解码
Ethereum 的 Bancor legacy 转换，并验证 V2 型、V3 型和 Uniswap V4 的整数资金流。
以原来人工核验的交易复跑，得到 **9 个不同池、8 段资金衔接、0 个未解事件**。
另一个冻结区间验证了新成交的 **7 池 WETH 闭环**，扣除实际 gas 后可见净额
**+3,650,823,715,852 ETH 等值 wei**。
[九池原始证据](ETH_FUNDED_HOLDOUT_20261124.md) · [通用回放入口](../../scripts/replay_swap_flow.py)

## 资金证据的支持范围

- **Bancor legacy Ethereum。** `Conversion` 的两个有索引币种、交易者以及两个整数金额，
  必须同时和已知 BancorNetwork 的单转换 `convertByPath`、实际池的 `convert`、
  两层调用返回值与唯一的入、出 ERC-20 Transfer 相符。按转换池地址计独立池。缺少转账、
  地址冲突、同额支付歧义、不同网络及超出该单转换 ABI 的变体留下未解事件。
  [BancorNetwork 源码](https://github.com/bancorprotocol/contracts-solidity/blob/master/contracts/BancorNetwork.sol)
- **V2、V3、V4。** V2/V3 型按 calldata、事件、付款与收币地址、转账金额核对。V3 输出的
  两个有符号整数也与事件互证。V4 PoolId 从 PoolKey 重算，再核对事件和返回 BalanceDelta。
  同一 `unlock` 中相邻、同 caller、无 Hook、等额反向的中间币差额可以形成一段内部资金
  衔接，要求回调原文一致并排除同回调该币的外部转账。对 V3 calldata 以及 V4 的外层
  `bytes` 封装，允许省略完整参数后的零填充，缺失实质字节仍留未解。
  [V3 付款回调](https://github.com/Uniswap/v3-core/blob/main/contracts/UniswapV3Pool.sol#L700-L722) · [V4 解锁与清算](https://github.com/Uniswap/v4-core/blob/main/src/PoolManager.sol)
- **资金衔接。** 保存直接同额池间转账、同一个执行器的连续同币同额转账与上述 V4 内账
  衔接。中间同币余额有其他移动、转账被重复申请、分流或汇总无法唯一解释时，输出保留
  部分路径。一个可信的资金链仍受具体协议、交易前状态、费用与完整资产余额的限制。
  [解码及残差输出](../../src/oasis_agent/evidence/swap_flow_trace.py)

原九池结果自动重建 Swap 顺序
`557 → 559 → 562 → 565 → 568 → 574 → 578 → 585 → 592`。Bancor 两池的实际转换
分别在索引 578、585。用原收据及其块头绑定轨迹，资金回放使用整个成功收据的精确
地址、topics、data 及日志顺序。两次 Bancor 转换的整数输出为
**12,765,394,420,910,310,408** 和 **8,356,418,047,912,439,167,969**，
都与下一腿实际输入一致。人工核算的净额继续沿用原证据。

## 新冻结区间

冻结 **26,478,800–26,478,879** 的 80 个连续高度，比原九池留出区间晚。查询预设八种
事件形态，含上述 Bancor Conversion。取整数金额候选最长的一笔及事件最多的一笔作对照。
共 **3,678 条匹配，2,025 笔交易，78 个有匹配的区块**。事件数 ≥5／≥10／≥20／≥50／
≥100／≥200 分别为 **102／28／13／3／0／0** 笔。根据原始金额不区分币种的有限筛选，
最多找到 7 腿候选。事件分母和两笔资金验证的分母分别保留。
[Nodely Ethereum RPC 限制](https://nodely.io/docs/ethereum)

两个入选交易同在 **26,478,800**，UTC 2026-11-23 15:42:35。原预算最多 7 次免费匿名
请求，实际使用 **5 次**且全成功，包括 Nodely 2、PublicNode 1、dRPC 2。从原始区块头
重新计算区块哈希，从 **650 份有序收据**重算收据根
`0xf1d89cdb3685297ef8673b0b752fa23b10e0df20d09644ccfc983161c731b96a`。
分别将两笔独立 trace 的 18、224 条日志与收据逐条对账。

| 入选交易 | 资金归因 | 可见费用与净额 |
| --- | --- | --- |
| 索引 161，7 条事件与 7 池 | `816 → 819 → 820 → 821 → 825 → 828 → 831`。5 段转账衔接与 1 段 V4 内账。2 个 V3 型、2 个 V4 PoolId、3 个 V2 型。 | 毛差 502,410,132,837,562 WETH wei。431,535 gas，支付 498,759,309,121,710 wei，无额外原生币净流。净额 **3,650,823,715,852 wei**。 |
| 索引 44，59 条匹配与 59 池 | 通用回放解析了 58 条，位于 58 个各一笔 Swap 的根调用分组。索引 110 为仍未支持的 Curve TwoCrypto 事件。这一范围内没有发现等额全量的资金衔接。 | gas 6,880,950,605,364,486 wei，另见向块费用接收方支付 85,031,010,665,306,261 wei。多资产余额和其他支付保留，盈亏未知。 |

七池交易由执行器先付 **8,166,023,460,648,881 WETH wei**，最终收到
**8,668,433,593,486,443 WETH wei**。V4 两池的共同中间币 `0xe5ac…f499` 在同一
`unlock` 记录 **339,207,192,608** 个最小单位的正、负余额差，等额抵消。成本吃掉
99.27% 的毛差，固定成交金额和 gas 时，有效 gas 单价只剩 **8,460,087 wei/gas** 余量。
两个选中的样本中没有验证到 ≥10 池串行闭环。整个区间的其他资金路径和失败尝试仍未
逐笔核验。
[七池成交](https://etherscan.io/tx/0x739aaa569427cb89f376cc965810216c2631ea65865eec04ee7c70ec920332cd) · [59 事件对照](https://etherscan.io/tx/0x12076997044122d9fcb07f9bb3ca4ab3563030a2bfc0ef7884987052a2e3c407)

## 复验

忽略目录 `reports/research/eth-bancor-decoder-20261124/` 保存范围、账本、原始往返、
挑选结果、由整块收据投影的单笔收据和派生资金报告。新版入口对九池原始样本与新窗口
都无需提供手工资金分配。

```sh
set -o pipefail
src=reports/research/eth-funded-holdout-20261124
dir=reports/research/eth-bancor-decoder-20261124
.venv/bin/python -m scripts.replay_swap_flow --chain 1 \
  --block "$src/26477242-header.response.json" --receipt "$dir/retained-81-receipt.json" \
  --call-trace-request "$src/ce5a52338f6a-trace.request.json" \
  --call-trace "$src/ce5a52338f6a-trace.response.json" | cmp - "$dir/retained-81-result.json"
PYTHONPATH=. PYTHONDONTWRITEBYTECODE=1 .venv/bin/python "$dir/verify.py" | cmp - "$dir/verified.json"
```

| 保留件 | SHA-256 |
| --- | --- |
| `scope.json` | `8e67832465aec6af5c0e72b5da5c2ba63091645800024e67d386865ba6628180` |
| `selection.json` | `35be61382f0cf945def47663968aa1440e184b3e6ee54e70a0d776ffb772c822` |
| `retained-81-result.json` | `ce814af11bd1c6f4e4384b1b46e239d34e614832672f0e2cc5c0ecadd53b9091` |
| `verified.json` | `af4ba58c0322805cd230f3afe267da1551981900dd917080e9bea9b52ff1f3fc` |

聚焦回归覆盖整数金额、冲突返回、重复转账、内部资金衔接、错误收据及零填充。新文件 Ruff、
mypy 通过。两个离线输出逐字节匹配。
