# Robinhood 主 feed 与延迟备用 feed 的同机对照

2026-11-24（北京时间），Codex。范围为 Robinhood Chain 4663 的公开主、备用 WebSocket。
在同一进程开两个独立连接，各只尝试一次，各上限 144 帧、12 MiB、连接阶段 7 秒、读取阶段
25 秒，费用 USD 0。使用此前已留存并验证的官方链配置和 Nitro 字典，启动前固定两个服务
地址。留存各臂的握手、完整帧和本机单调接收时钟。无认证、签名和交易。
[Robinhood 官方节点与双 feed](https://docs.robinhood.com/chain/run-a-full-node/) ·
[Nitro feed 格式](https://docs.arbitrum.io/run-a-node/sequencer/read-sequencer-feed) ·
[先前单 feed 样本](ROBINHOOD_NITRO_FORWARD_20261123.md)

对照预先按序号和区块哈希配对，要求相同的 L2 有效负载摘要和交易顺序。负载冲突、同序
不同哈希或串用不同时钟范围时拒绝出延迟值。只单独记录同一捕获范围内主 feed 的内部缺口
和备用可补的哈希提示，窗口边缘不计缺口。配对结果是本机看到同一块的时间差。链端排序、
最终性、可用池状态及真值验收需额外证据。两端均由 Robinhood 运营。

## 同机到达时间

北京时间 00:02:03–00:02:17，两端均按帧上限结束，各解出 136 个不同序号。成功匹配
**132 个同哈希、同负载块**，范围 121,533,513–121,533,644。主 feed 样本内无缺号，
本次的实际缺口修复分母为零。重放保持备用来源与主来源各自的原始证据。

| 同块本机延迟差，备用减主 | 毫秒 |
| --- | ---: |
| 最小 | 86.774 |
| 中位 | 505.139 |
| p95，最近秩 | 603.037 |
| 最大 | 844.128 |

132 次配对中主 feed 均先到。这个窗口支持把备用并联为间隙补齐来源。备用给出的哈希
提示仍需 RPC 核验，有重组或冲突时停止该范围。采集器尚未测短断线后在线恢复耗时。

用本轮原始帧导出的主序列离线构造一次故障，移去 121,533,579–121,533,584 六个连续提示。
已有连续性游标停在首个缺口并缓冲 64 条后续消息。输入备用帧后六个哈希均还原，游标推进
至 121,533,649。这个本地对照验证补缺逻辑，实网没有观察到待修缺口。

## 独立区块与收据锚定

另设一个 USD 0 的固定只读范围。预先只取有签名交易的配对块的首、末两个。官方 RPC 和
PublicNode 各验证链 ID 和两块头，PublicNode 另按精确哈希返回两块全部收据。账本 **8/8**，
官方 3、PublicNode 5。两个来源的块哈希、父哈希、状态根、交易根和收据根一致，历史查询
未参与实时延迟计算。对照本地解码的 43 笔签名交易哈希与同序块内位置，另有两个系统位置。
用 45 份收据重建 **2/2** 个收据根。
[官方连接文档](https://docs.robinhood.com/chain/connecting/) ·
[PublicNode Robinhood](https://robinhood.publicnode.com/)

| 高度 | feed 交易 / 整块收据 | 目标 PoolManager Swap |
| --- | ---: | ---: |
| 121,533,513 | 22 / 23 | 1 条，1 个 PoolId |
| 121,533,644 | 21 / 22 | 2 条，同笔交易的 2 个 PoolId |

后一块交易 `0x444f619d4ca6d77064af82dde7d7bffa09e628e598835825506086e9bfd62bf1`
的两条事件中，一正一负的 `39,476,819,164,457,339` 中间单位正好抵消。用户付款与收款
涉及不同 ERC-20，实际发送 `43,901,365,420,935,007` 个 `0xd5f3…de68` 最小单位，收到
`28,606,416` 个 `0x5fc5…d168` 最小单位，回执执行费 `5,619,659,752,000` ETH wei。
内部货币和 hook 路径需要 PoolKey 与 trace 复核。两块用于证明 feed 提示能落到明确的交易
位置与变池事件。机会收益和从提示到状态可用的实时差值无测量分母。

## 留存与复跑

忽略目录 `reports/research/robinhood-feed-pair-20261124/` 保存原始范围、两个 feed 的逐帧
字节和计时、8 次 RPC 原始往返及原始账本。两份结果离线复算逐字节相同。

```sh
set -o pipefail
evidence=reports/research/robinhood-feed-pair-20261124
.venv/bin/python -m scripts.capture_robinhood_nitro_feed compare-pair "$evidence/run" \
  --sources-dir reports/research/multichain-forward-20261122/robinhood/sources \
  | cmp - "$evidence/pair.json"
PYTHONPATH=. .venv/bin/python "$evidence/anchor.py" replay | cmp - "$evidence/anchors.json"
PYTHONPATH=. .venv/bin/python "$evidence/gap_control.py" | cmp - "$evidence/gap_control.json"
```

`pair.json` SHA-256 为 `0a2a35177d39179336db0082de2d176cb19226fb0a15d2c0677ddb4ea999d190`，
`anchors.json` 为 `4ab0ec1a42bf6aa9c25c80d6be877df2654e48a99bdda5b56c814ffe4a7103c8`。
