# 2026 年 Ethereum、Arbitrum 与 Optimism 多跳扩样

三条链的新窗口覆盖 **1,296 块**，登记 **489 笔**此前目录外的多事件候选，逐笔核验 14 笔。Ethereum 一笔三池 WETH 闭环扣收据 gas 和对区块收款地址的直接付款后，按发送人和执行器同属一方计算余 **4,401,040,591,897 wei**。Arbitrum 的真实整块回放发现了 EIP-7702 收据覆盖缺口，本轮在正常入口修复。[候选哈希、账本和逐笔证据](Y2026_MULTIHOP_NEXT_20261128.json)

## 新窗口与证据分母

按预先冻结的区块偏移检查 2026 年数据，剔除旧目录中的交易，断言与前五份归档所列范围不相交。筛查使用 16 个事件签名，保留 ≥200 开放尾档。Ethereum 比对 PublicNode 区块头与 Nodely 完整收据，重建 16 个头哈希和收据根，覆盖 **5,234 份收据与 63 笔回滚**。Arbitrum 官方索引与 PublicNode 独立核对六个完整收据根。Optimism 官方日志索引与 dRPC 独立核对六份单笔收据，并重建区块头哈希。Optimism 在第三个窗口触及响应体积上限，第四、第五个窗口未发出日志请求，分母只含前两个完整窗口。[Ethereum RPC](https://ethereum.org/developers/docs/apis/json-rpc/) · [Arbitrum 官方网络资料](https://docs.arbitrum.io/chain-info)

| 链 | 已完成区块 | ≥2 事件 | ≥3 | ≥5 | ≥10 | ≥20／50／100／200 | 最大事件数 | 逐笔核验 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Ethereum | 16 | 164 | 89 | 29 | 10 | 0／0／0／0 | 19 | 2 |
| Arbitrum | 1,120 | 95 | 34 | 3 | 0 | 0／0／0／0 | 8 | 6 |
| Optimism | 160 | 230 | 104 | 2 | 0 | 0／0／0／0 | 7 | 6 |

## 实付、到账与费用

- **Ethereum** `0x041b20f0` 的收据逐额绑定三池。实际付 **2,601,337,474,354,078 WETH wei**，收回 **2,713,144,341,570,261**，执行器 WETH 净收 **111,806,867,216,183 wei**。发送人付 **34,313,610,482,193 ETH wei** gas，另转入执行器 **950 wei**。调用树显示执行器向区块头费用地址付 **73,092,216,142,093 wei**，发送人和执行器的原生前后余额均逐额吻合。将两地址视为共同主体且按 ETH/WETH 面值，条件余额为 **4,401,040,591,897 wei**。只扣 gas 会留下 77,493,256,733,990 wei，约为计入直接付款后余额的 17.6 倍。同批最大 19 事件样本的最长已证接续仅一腿。[三池成交](https://etherscan.io/tx/0x041b20f09c632ac1d590afc45294b52acc703dcad2e0b50412252931579f8b1d) · [19 事件对照](https://etherscan.io/tx/0x0ac4937def57cd24210364ff1ea8ad013205c9eb476f867be3a47144d76562b0)
- **Optimism** `0x850e9eab` 的四个市场事件里有三腿等额接续，执行地址 WETH 净增 **10,870,371,202,861 wei**。按成交块哈希调用 GasPriceOracle，operator fee 为 **0 wei**。扣该笔 L1 与 L2 费后，合并两角色的条件余额为 **10,865,699,782,926 wei**。同口径下 7 事件的 `0xb1e1cf09` 余 **2,761,074,882,132 wei**，只绑定两腿。两笔的实转尚未证明向同一本金持有人闭合，原生款项及地址归属留空。[4 事件成交](https://optimistic.etherscan.io/tx/0x850e9eab85e15a4ca4864fdc248387c176008329dabe66e6507d62501398c6cb) · [7 事件成交](https://optimistic.etherscan.io/tx/0xb1e1cf099264a6d0e7f6e55ab11469b5f3f84f2d6a15cd5f1f40b51379bd3d2e) · [OP 费用规则](https://docs.optimism.io/op-stack/transactions/fees)
- **Arbitrum** `0xe840dd41` 的八个事件核到一腿接续。执行地址剩 WETH、WBTC 和 USDC 三种资产，WETH 净增 **2,247,122,015 wei**，全笔收据网络费为 **42,101,323,750,560 ETH wei**。另一笔四事件 `0x1bb7bcf2` 已接上两腿，终点币不同，逐币余额保留原单位。[8 事件成交](https://arbiscan.io/tx/0xe840dd41ad703552f5562ce3d059904b51eeb1fbb185e3ca28e20078e9718781) · [4 事件成交](https://arbiscan.io/tx/0x1bb7bcf22ae6c05f6f55f6a3477a0ed5b6c3944e0b0fc6d65f6c536b09617735)

## Nitro 收据兼容性

Arbitrum 块 **528,015,694** 含一笔与所选套利候选无关的类型 4 交易 `0xb7910ef3`。旧入口在这一项拒绝整块。Nitro v3.9.6 所固定的 Geth 源码对类型 4 使用带类型前缀的普通收据 RLP。修正仅允许 Arbitrum One 链 ID 42161 下的该类型，仍核对交易位置、全部日志、累计 gas、Bloom 和最终收据根。真实 11 份收据在新入口重建 `0x656b2f7180f5f6d5e2742c779296c69bbc1438282a8ec04f6d8c4ef4a40871a0`。回归覆盖正确类型、其他链拒绝和错误的类型前缀。该修正只验证收据承诺。[固定 Nitro 收据源码](https://github.com/OffchainLabs/go-ethereum/blob/a983a4af77a05584a9929ff7c68db98921c22116/core/types/receipt.go#L465) · [ArbOS 40](https://docs.arbitrum.io/run-a-node/arbos-releases/arbos40) · [EIP-7702](https://eips.ethereum.org/EIPS/eip-7702)

## 预算与复算

合计 **96 次免费 RPC 预留，支出 USD 0**。首次本地资金解析把筛查用签名列表当作带 ABI 类型的映射，保留原终止日志、账本、区块头和已收取的首份回执。修正后只请求此前还没发出的收据校验。第二阶段记录 Arbitrum 旧解码器的正式停止和 Ethereum 调用树缺日志的停止。最终阶段用旧响应通过修正后的 Nitro 入口，并且只补取两个未请求的 Arbitrum 块、一个带日志的 Ethereum 调用树与两笔固定块上的 OP operator fee。三份原始范围、驱动、账本及响应保留在 `reports/research/y2026-multihop-next*-20261128/`。

```sh
set -o pipefail
PYTHONPATH=. .venv/bin/python docs/verification/fixtures/complete_y2026_multihop_next.py replay \
  reports/research/y2026-multihop-next-proof-20261128 |
  cmp - docs/verification/Y2026_MULTIHOP_NEXT_20261128.json
```
