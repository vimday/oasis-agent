# Robinhood 双 feed 到达顺序回放与收据绑定

北京时间 2026-11-24。使用同一进程已留存的 Robinhood 主 feed 和延迟备用 feed，
按 WebSocket 数据帧的本机单调时钟交错回放。原始样本释放了 **136 个连续区块提示**，
其中 **132 个来自两路**，自然缺口为零。离线移去主 feed 六个提示后，备用在第一个
可见缺口出现 **411.928 ms** 后补回所有缺失序号。回放复用了已留存的官方和 PublicNode
RPC 字节，重新计算两个区块哈希与 45 份收据的 trie 承诺。本轮没有新增网络读取。
[双 feed 采集、RPC 范围与时钟口径](ROBINHOOD_FEED_PAIR_20261124.md)

## 按原时钟重放连续性

[回放入口](../../scripts/replay_robinhood_nitro_pair.py) 重新解码两路原始压缩帧，验证原有
采集停止记录、范围、源文件和逐帧摘要。每帧中的消息在一个 SQLite 事务内入账。同一
纳秒时主来源先入账。游标只释放连续前缀，重复消息和冲突沿用原 [Nitro 日志](../../src/oasis_agent/evidence/robinhood_nitro.py) 的核验。起点采用主订阅首条序号 **121,533,513**，
所以备用在起点之前的 4 个提示不在分母内。匹配来源的 SHA-256、交易位置和历史释放时钟
留在新报告。

| 臂 | 新增 / 重复 / 早于起点 | 恢复 | 末尾已释放 |
| --- | ---: | --- | ---: |
| 同机原始到达顺序 | 136 / 132 / 4 | 0 个可见缺口 | 121,533,648 |
| 移去主来源 121,533,579–121,533,584 | 136 / 126 / 4 | 1 次发现后回补，用时 411.928 ms | 121,533,648 |
| 关闭并重启，重放同一故障投影 | 0 / 262 / 4 | 0 个新缺口或新释放时钟 | 121,533,648 |

备用在主 feed 的后继帧使缺口可见之前已送达 121,533,579。因此故障臂首次看到的缺口
为 **121,533,580–121,533,584**。备用送来最后一条缺失提示时，前缀一次推进至
121,533,589。故障投影保留原有两条连接的接收时钟。本轮实网断线和自然恢复耗时的分母为零。
两路源均由同一运营方提供，两个独立网关的历史区块核验另有作用。
[此前主备时差结果](ROBINHOOD_FEED_PAIR_20261124.md#同机到达时间)

## 从已释放 feed 到区块和收据

可选的 `--rpc-capture` 接受上述实验留存的 **8 次** RPC 范围。回放要求配对报告摘要、
已停止原账本、来源、请求编号和方法、响应摘要、链 ID、交易顺序全部对应。用同一 Nitro
头部格式分别重算两个来源的区块哈希并比较状态根。用 PublicNode 的有序收据重算
`receiptsRoot`，同时核对累计 gas、全块日志索引和 Bloom。首个系统位置之后的
**43 个交易位置**逐一匹配 feed 交易哈希。交易根由区块头承诺，交易根未单独重建。
[复用的 Nitro 承诺验证](../../src/oasis_agent/evidence/nitro_block.py)

| 高度 | 两路 feed 在连续前缀中释放的本机单调时钟，ns | 真值收据 / 绑定 feed 交易 | PoolManager 观察 |
| --- | ---: | ---: | --- |
| 121,533,513 | 1,048,880,877,303,416 | 23 / 22 | 位置 22，1 条 Swap |
| 121,533,644 | 1,048,893,597,789,583 | 22 / 21 | 位置 14，2 条 Swap |

位置 14 的两条 Swap 来自不同逻辑池和同一事件 caller。回放从收据精确提取
`23 → 24` 的 **39,476,819,164,457,339** 相反符号金额，同时保留每条事件的价格、
流动性、tick 及费率。收据只能提出金额匹配，输出保留 `asset=null` 和
`funding_verified=false`。已留存的独立调用树能把这笔候选进一步绑定到中间资产
`0x117cc2133c37b721f49de2a7a74833232b3b4c0c`，并核对 PoolKey、两腿由同一笔
`unlock` 结算、首尾资产相异以及 **5,619,659,752,000 ETH wei** 的手续费。
收据候选生成器不会把该次 trace 归因外推到其他候选。
[精确两腿归因](ROBINHOOD_FEED_PAIR_20261124.md#区块-121533644-的两腿结算复核)

区块头验证提供确定的状态根锚。事件中的 Swap 结束值和根哈希不足以重建交易前储备、
完整初始化 tick 或动态费率。输出保持 `quote_state_verified=false`，
历史 RPC 不计入首次 feed → 可报价状态延迟。在线的后续验收需实际断线恢复窗口、
同一接收时钟下精准状态读取的完成时间和完整报价校验。

## 离线复验

原始数据留在 `reports/research/robinhood-feed-pair-20261124/`，这次派生输出存于
`reports/research/robinhood-arrival-replay-20261124/`。下面按次序创建临时 SQLite
日志并对照三个既存输出。原有 `compare-pair` 报告也已单独重放，逐字节一致。

```sh
set -o pipefail
raw=reports/research/robinhood-feed-pair-20261124
sources=reports/research/multichain-forward-20261122/robinhood/sources
out=reports/research/robinhood-arrival-replay-20261124
tmp=$(mktemp -d)
replay() {
  .venv/bin/python -m scripts.replay_robinhood_nitro_pair "$raw/run" \
    --sources-dir "$sources" --rpc-capture "$raw/rpc" "$@"
}
replay --checkpoint "$tmp/natural.sqlite3" | cmp - "$out/natural.json"
replay --checkpoint "$tmp/omission.sqlite3" --omit-primary 121533579:121533584 \
  | cmp - "$out/omission.json"
replay --checkpoint "$tmp/omission.sqlite3" --omit-primary 121533579:121533584 \
  | cmp - "$out/restart.json"
```

| 派生输出 | SHA-256 |
| --- | --- |
| `natural.json` | `6d05951de925724a07d62a5a6532ff596b91e4c6a376fde15f50926a50836e87` |
| `omission.json` | `123c68dc1213f5de55670a62b0314d7368cf669b43c72cc7eec57009f79def71` |
| `restart.json` | `eb9931bf062796c5d6fff9a382d67028fdd7f6c29ada5f991109ff1f9c01162e` |

四组针对性测试共 34 个通过，覆盖同帧原子入账、较早的备用消息、持久恢复、冲突后清空
可用提示以及金额归因歧义。新增源文件的 Ruff 和 mypy 检查通过。

## 实际关闭本地主连接后的备用接续

另冻结一次匿名双 feed 试验，每个端点最多连接一次，建连阶段上限 7 秒，读帧阶段上限
14 秒，整轮 watchdog 25 秒。两个官方 feed 在同一进程并联。本地主连接完成第 32 帧
后主动退出 TLS/WebSocket 连接，记录退出前及完成后的单调时钟，无重连。已在运行的
备用连接持续接收至 144 帧上限。费用 USD 0，无 RPC 请求。
[Robinhood 官方双 feed](https://docs.robinhood.com/chain/run-a-full-node/)

离线按原接收时钟还原实际到达顺序，先校验 29 个重叠序号的区块哈希和有效负载一致。
本地主连接关闭时已释放至 **121,577,333**。关闭完成后 **515.101667 ms**，备用第 38 帧
带来并释放 **121,577,334**。含此条在内，备用连续带来 **102 个**主连接未看到的新高度，前缀到
**121,577,435**，回放没有序号缺口或冲突。515.101667 ms 的起点是已知的本地连接退出，
终点是收录首条后续连续提示的接收时钟。这轮是实际连接上的人为断开，区块数据可用性、
可报价状态和上游自然故障仍无测量分母。

原始范围、握手、压缩帧、双臂停止记录、断开标记和逐序号证据位于忽略目录
`reports/research/robinhood-primary-cut-20261124/`。结果 SHA-256 为
`6cbb75c27805fdbdb13014d52ce24dd840210e6937a87593d54f62e6a3af6e5c`。

```sh
set -o pipefail
dir=reports/research/robinhood-primary-cut-20261124
PYTHONPATH=. PYTHONDONTWRITEBYTECODE=1 .venv/bin/python "$dir/verify.py" | cmp - "$dir/verified.json"
```
