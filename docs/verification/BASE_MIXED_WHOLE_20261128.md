# Base 同一对池的两种本金整笔复算

先前在 Base 高度 **63,043,739** 发现的 Uniswap V3 与 Aerodrome Slipstream 微利候选，已用原金额完成整笔 EVM 仿真。两个起始币在 H 的两腿回款与早前四份逐腿报价完全一致。扣所测 gas 及 L1 安全费上界后，WETH 起始的情景净额为 **+1,682,277,530,217 wei**，USDC 起始按第一腿实收比价折费后为 **+2,881 USDC 最小单位**。前一块和子块末状态两个方向都为负。它们是同一资金方向的两种起始资产，金额不可叠加。[整笔、请求和时钟](BASE_MIXED_WHOLE_20261128.json) · [原第三头逐腿发现](BASE_PRIOR_HEAD_POLICY_NATURAL_20261128.md)

| 取样父状态 | WETH 整笔 gas | WETH 毛差，wei | WETH 费后，wei | USDC 整笔 gas | USDC 毛差，最小单位 | USDC 条件费后，最小单位 |
|---|---:|---:|---:|---:|---:|---:|
| P 63,043,738 | 295,859 | −37,661,543,331,564 | −39,739,508,543,898 | 289,562 | −63,184 | −66,578 |
| H 63,043,739 | 293,025 | +3,741,318,979,269 | **+1,682,277,530,217** | 286,728 | +6,244 | **+2,881** |
| C 63,043,740 | 293,039 | −10,264,176,349,607 | −12,323,213,006,118 | 286,742 | −17,242 | −20,605 |

## 固定本金与费用

两家匿名服务确认三块的相同哈希、父哈希、state root、千分秒时间和 base fee。固定块哈希检查两个池的 WETH／USDC 币序和工厂 back-reference，分别绑定 Uniswap 100 pips 费率池与 Aerodrome tick spacing 100 池。H 留存两个池与两个 token 的 runtime 原件，复核仿真地址原运行码为空，两项余额都为零。两个池原始余额、逻辑和储备状态都由规范历史节点读取。整笔只用 Base 官方 `eth_simulateV1`，模拟块 `parentHash` 逐条绑定两源确认的状态。池在回调前交付输出的数据顺序来自规范源码，每腿都检查唯一的输入、输出与最后收款。[Base 模拟接口](https://docs.base.org/base-chain/api-reference/ethereum-json-rpc-api/eth_simulateV1) · [Uniswap V3 交换](https://github.com/Uniswap/v3-core/blob/v1.0.0/contracts/UniswapV3Pool.sol) · [Aerodrome Slipstream 回调](https://github.com/aerodrome-finance/slipstream/blob/main/contracts/core/CLPool.sol)

- **WETH** 由有资金证明的公共地址发起。十八位本金 **67,280,448,940,895,129 wei** 以原生 ETH 传入，在同一笔里存入 WETH 合约。H 第一池实付 WETH 后收 **112,289,127 USDC 最小单位**，第二池按同额实付 USDC，收 **67,284,190,259,874,398 WETH wei**，全部由探针付到固定收款地址。保留 WETH 与 ETH 同面值的结算估值。
- **USDC** 固定本金 **112,819,960** 六位最小单位，仿真只给探针的 USDC mapping slot 9 填该金额。H 第一池按原额实付 USDC 并收 **67,602,256,126,940,030 WETH wei**，第二池以上述 WETH 全额付款并将 **112,826,204 USDC 最小单位**送回收款人。C 的所测 WETH 中间回款相同，而第二池的 USDC 回款少了 **23,486**。该路线无真实本金提供方，费用换算取同一状态的实际第一腿 USDC／WETH 隐含价格并向上取整。H ETH 费 **2,014,686,870,399 wei** 折 **3,363 USDC 最小单位**，此处只供情景比较，缺独立的小额变现成交与资金成本。[Circle 存储布局](https://github.com/circlefin/stablecoin-evm/blob/master/test/helpers/storageSlots.behavior.ts)

同一个已核源码编译的探针，仅将运行码附到空地址的临时模拟覆盖。WETH 的原生 ETH 和池子状态均无覆盖。USDC 增加一项探针库存覆盖，存入前两币余额与代码身份均留证。冻结 type-2 费帽 250,000,000 wei/gas、tip 2,000,000 wei/gas、gas 上限 550,000、nonce 0 和 1 最小单位回款下限。费用以模拟后继块的 base fee 与小费计，加入固定父状态 oracle 给出的两种 unsigned type-2 长度对应 L1 费上界和 gas 上限的 operator fee 上界。H WETH L2 执行费 **2,052,517,640,550 wei**，L1 上界 **6,523,808,502 wei**，operator 上界为零。模拟调用使用显式地址和调用权限，实际没有签名、广播、付费或纳入排序证据，链下成本未知。[探针源码](fixtures/BaseMixedWholeProbe.sol) · [Base 费用说明](https://docs.base.org/specifications/transactions/network-fees)

## 规范收据定位窗口消失

另用独立匿名档案服务取得 H 的 110 份与 C 的 48 份完整收据，重算两个 MPT 收据根、头哈希、逐交易累计 gas 和日志索引。H 的 Uniswap 池只有两个方向均为 USDC → WETH 的 Swap，分别输入 **1,681.489650** 与 **210.658008 USDC**。到 C 时只出现一笔相反的 WETH → USDC，位于交易索引 4，实付 **383,463,892,634,854,442 WETH wei**，实收 **639.935241 USDC**，池 tick 由 H 末的 **−202120** 变到 **−202122**。该笔规范收据共有两条实转与一条 Swap。两块的目标 Slipstream 池都无日志，且 USDC 起始仿真的第一腿实收 WETH 在 H 和 C 完全相同。观测到的价格变化集中于 Uniswap 池；费用差远小于其导致的回款变化。[H 成交一](https://basescan.org/tx/0x06ec4fd51ea41d63139d0f353ba07a1277fab786ee6a3946062cdfe132210f1a) · [H 成交二](https://basescan.org/tx/0x093df523c482d12bf87eadcd0e0249c740b96709522f87c6da11ea308cbb3877) · [C 反向成交](https://basescan.org/tx/0x1aaea79e7f20e458b562fa8d8406754c1f72e1e69ee61bf485a391d75b374e6c) · [双块收据证据](BASE_MIXED_WHOLE_RECEIPTS_20261128.json)

## 时效与复算

六次全部按完整输入执行并结算。历史窗口中，H 的保守正净额到 C 变负。官方六次模拟 HTTP 在途约 **258–336 ms**，这些是归档重放的传输时间。原实时窗口从收到 H 到收到 C 的本地间隔是 **277.281 ms**，当时两份逐腿报价在 C 后才齐备。历史重放的成功无法替代前瞻的及时率。固定 WETH 本金可用来检验下一轮把 C 诊断移出实时队列后的时效。[原实时到头与报价时钟](BASE_PRIOR_HEAD_POLICY_NATURAL_20261128.json)

冻结范围 `fd19b97954261008eaa4faed53c8d4b2aaf768bb6d2e0ba8a9aca6092f28c458` 实际使用 **23 次**已预留免费读取，官方 10 次、PublicNode 13 次。收据补查在另行冻结的范围中使用 3 次匿名 dRPC 读取。合计 **26 次，USD 0**。原范围、运行码、只读账本和逐次响应位于采集机 `reports/research/base-mixed-whole-20261128/`。运行码用 Solidity 0.8.26 编译，优化 200 次、Cancun EVM、关闭元数据和 CBOR，运行码 Keccak 见 JSON。离线校验重新绑定链、池身份、固定本金、五笔支付、两腿回款、区块和 gas。[复算程序](fixtures/replay_base_mixed_whole.py)

```sh
set -o pipefail
PYTHONPATH=. .venv/bin/python docs/verification/fixtures/replay_base_mixed_whole.py replay |
  cmp - docs/verification/BASE_MIXED_WHOLE_20261128.json
PYTHONPATH=. .venv/bin/python docs/verification/fixtures/replay_base_mixed_receipts.py replay |
  cmp - docs/verification/BASE_MIXED_WHOLE_RECEIPTS_20261128.json
```
