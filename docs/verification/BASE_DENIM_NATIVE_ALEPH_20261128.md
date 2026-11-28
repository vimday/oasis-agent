# Aleph Base 原生 Denim 同秒与跨秒复核

在 Aleph WSL 使用 Base 官方 Linux x86_64 Anvil v1.5.1，复核两组 2026 年历史父子块及同一个固定本金的 Uniswap V3 → Aerodrome Slipstream 环。显式采用公开子块的块号、整秒时间与费用上下文后，两组的成交日志、返回值与 gas 都和已保存的公开模拟一致。独立的 EVM 探针在两组都读到规范父块哈希。该发行版的 `eth_simulateV1` 响应仍在 `parentHash` 写全零，因此严格入口仍会拒绝两组结果。[逐组原始字节摘要和账本计数](BASE_DENIM_NATIVE_ALEPH_20261128.json) · [Base 官方 Linux 构建](https://github.com/base/base-anvil/releases/tag/v1.5.1)

## 固定证据与结果

冻结 Base 8453、原探针运行码、原两池和 **67,280,448,940,895,129 wei** WETH 本金，交易本身使用同一调用者、nonce、金额和 calldata。每组父子头由官方与 PublicNode 分别按哈希读取，逐字段 RLP 后独立重算 Keccak，核对原始块哈希、父子链接和 `timestampMs` 相差 200 ms。已有官方和 dRPC 整笔结果的固定包络一致性分别见历史正控和同头配对验证记录。Aleph 只运行回环 fork，访问的是有界只读代理。历史同秒组复用了上一轮留存的父块状态，其余状态按该组规范父块哈希读取。[历史正控](BASE_SIMULATOR_LATENCY_20261128.md) · [归档回放入口](fixtures/replay_base_denim_aleph.py)

| 组 | 父块 → 直接子块 | Denim 毫秒时钟 | 第二池回款（WETH wei） | 公开和 Aleph gas | 公开链费后净额（WETH wei） |
| --- | --- | --- | ---: | ---: | ---: |
| 同秒 | 63,043,739 → 63,043,740 | 1795799123400 → 1795799123600 | 67,284,190,259,874,398 | 293,025 | +1,682,277,530,217 |
| 跨秒 | 63,217,836 → 63,217,837 | 1795833942800 → 1795833943000 | 67,260,157,018,483,493 | 301,204 | −22,401,040,569,431 |

费用用父块锁定的 L1 费用上界、operator fee 上界、模拟 gas 和子块费率。公开模拟是给定父块状态下的条件回放，表中的经济值不代表历史交易真实利润。子块的规范哈希证实头部上下文，未重放该块的其他交易或构造整块状态根。

## 原生环境与 RPC 口径

默认调用在两组复现整笔资金流与 gas，但返回块号仍为 H，coinbase 和父哈希全零。跨秒组默认沿用 H 的整秒时间。按历史子块显式覆写后，两组的块号、时间、base fee、coinbase 和 gasLimit 都与规范子块及公开整笔输出一致，只剩 Aleph 响应的 parentHash 为零。

每组另跑一笔无资金的 EVM 探针，通过 `BLOCKHASH(H)`、`NUMBER`、`TIMESTAMP`、`BASEFEE`、`COINBASE`、`CHAINID` 读取执行上下文。两组均读取到精确父哈希和 H+1 块号，两组费率分别为 **5,004,582** 和 **5,000,000 wei/gas**，coinbase 均为规范费用收款地址。探针独立于整笔资金流，避免把人为拼接的 parentHash 误当成原生 RPC 输出。该轮未验证合成块中的 BaseTime 预部署状态更新，规范头给出的精确毫秒时钟与 EVM 的整秒 `TIMESTAMP` 分别列证。

整笔原生默认往返含控制机与 Aleph 的 SSH 转发。同秒 289 ms，跨秒的冷状态首次为 26.222 s。显式上下文往返分别约 739、751 ms，包含补取状态的成本。这组历史状态控制只用于验证语义，实时可用性由独立新窗测量。

## 预算、停止与复算

正式范围预留 **114** 次免费 RPC，其中 PublicNode 112 次和官方 2 次。PublicNode 包含两份跨秒头和 110 次状态读；同秒组新增三次 fee vault 读，跨秒组读取 107 项。首次范围取得 4 份同秒双源头后，因本地代理未给可选 `eth_getAccountInfo` 正确返回方法不存在而停止。第二次在共享 SSH 转发启动时停止，RPC 预留为零。第三次隔离 SSH 转发并复用首次两源同秒头。合计 **118** 次免费预留，支出 USD 0。三份停止记录、冻结脚本、账本和原始请求响应留在采集机。实验仅执行无签名的回环模拟。

复算逐字节一致，Python 夹具通过 Ruff。Base v1.5.6 的 Reth 下一块上下文推导通过研读源码定位，它从已封存父头取哈希和派生 base fee。该分支仍需编译并纳入正式回放，Aleph 的本轮实际输出来自上述固定 Linux Anvil 构建。[Base 原生上下文源码](https://github.com/base/base/blob/v1.5.6/crates/execution/evm/src/config.rs)

```sh
set -o pipefail
PYTHONPATH=. .venv/bin/python docs/verification/fixtures/replay_base_denim_aleph.py replay \
  reports/research/base-denim-aleph-pairs-v3-20261128 |
  cmp - docs/verification/BASE_DENIM_NATIVE_ALEPH_20261128.json
```
