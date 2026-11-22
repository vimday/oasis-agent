# Robinhood 主网 sequencer feed 前瞻样本

北京时间 2026-11-23 00:04，从 Robinhood 官方免费 `wss://feed.mainnet.chain.robinhood.com`
收到 144 个 WebSocket 帧。其中 136 个是经 Nitro 字典压缩的数据帧，其余 8 个是 Ping。
136 个有序消息覆盖序号 120740508–120740643，样本内无缺号、重复或倒序。从消息中解出
371 个不同的签名交易哈希。采集在帧额度用完时停止，首末消息接收间隔为 14.507 秒，
从连接握手完成到停止为 14.847 秒。总帧字节 154,968，按 TLS 解密后的 WebSocket 帧计，
未计握手。接收时间为采集机记录，不能用于估计单程链路延迟。

## 握手、压缩与范围

首次握手获 HTTP 101，并协商 `Arbitrum-permessage-deflate`、
`server_no_context_takeover` 和 `client_no_context_takeover`。公共端点省略
`Arbitrum-Chain-Id` 和 `Arbitrum-Feed-Server-Version`，且初版采集器误将它们设为必需，
因此立即停止，未读数据帧、未调用 RPC。保留首次原始握手、STOP 和修正说明。官方 Nitro
默认将两个字段设为可选。如果服务返回字段，修正后的采集器仍校验其值。修正后按
`recovery-amendment.json` 同地址继续一次，整个范围累计两次连接，未增加
144 帧、12 MiB 或 25 秒采样额度，未切换到备用端点。
[连接文档](https://docs.robinhood.com/chain/connecting/) ·
[Nitro 客户端](https://github.com/OffchainLabs/nitro/blob/3599aca/broadcastclient/broadcastclient.go)

用官方 Nitro `3599aca` 的 `GetStaticCompressorDictionary` 中 20,023 字节字典解压。
扩展每消息使用新 raw-DEFLATE 上下文，拼回 WebSocket 压缩尾部后解码
`BroadcastMessage` v1。收到的 136 个消息均包含 `blockHash` 和 `signatureV2`，
本轮未独立验证签名。消息中的 `header.blockNumber` 为 Ethereum 的 L1 高度。
Robinhood 官方链配置给出 `GenesisBlockNum=0`，所以 L2 高度候选等于序号。
本地解码区分 Nitro 批次和签名交易，直接对 Ethereum 交易封装字节求 Keccak，
后续用 RPC 核对实际块和交易位置。
[字典源码](https://github.com/OffchainLabs/nitro/blob/3599aca/wsbroadcastserver/dictionary.go) ·
[L2 消息解析源码](https://github.com/OffchainLabs/nitro/blob/3599aca/arbos/parse_l2.go) ·
[Nitro feed 格式](https://docs.arbitrum.io/run-a-node/sequencer/read-sequencer-feed) ·
[Robinhood 官方链配置](https://cdn.robinhood.com/assets/generated_assets/hoodchain_docsite/chain-node-configs/robinhood-chain-info.json)

## RPC 对照与池执行

同一运营方的官方免费 RPC 先记账后读取，预约和发送均 4/4，无重试和失败，费用 USD 0。
`eth_chainId` 返回 4663。两次 `eth_getBlockByNumber` 均按 feed 推得的明确块号查询，
匹配时间戳和哈希，且 feed 内签名交易的本地哈希和顺序与 RPC 一致。

| Feed 序号和 L2 高度 | L1 高度 | L2 哈希 | 已对照 feed 交易在块中的位置 |
| --- | ---: | --- | --- |
| 120740543 | 26471730 | `0xc8c79e9f0867a431aeff9829ed494755b17be3d3d6ffed69a9acec349525e806` | 1 |
| 120740643 | 26471731 | `0xa9c57bca8c16c5e316f57d45a0d446b54a2fe401b221e47563884646a35ed2b1` | 1、2、3、4 |

选取 feed 中目标为已知 Uniswap Universal Router 的交易
`0xab4c1b4c174cf81dca7b357e0879e86e4797236bbd564dfdc44e6c6dda6254b7`。
唯一 `eth_getTransactionReceipt` 返回状态 1，交易位置为 120740543 块的索引 1。两条
日志中一条为官方 Uniswap v4 PoolManager 的 Swap，另一条为 ERC-20 Transfer。
Swap 的 PoolId 为 `0xa1ec4ae8f73473055aeec51c604eeb16962f284afd43a31c39e0f20cf3afc151`。
使用 134,588 gas，回执有效 gas price 为 20,022,000 wei，乘积为 2,694,720,936,000 wei。
[Uniswap 官方部署](https://developers.uniswap.org/docs/protocols/v4/deployments#robinhood-chain-4663)

这笔是已执行的单池交换。Feed 提供 sequencer 已排序的交易，回执来自 HTTP RPC。
本轮未获得交易前状态、第二提供商、L1 最终性、所有受益地址与资产差额，也没有测量捕获
多池机会的比例、报价竞争力或套利净收益。持续由本地状态复算交易需要 Nitro 全节点、
官方 genesis，以及 Ethereum L1 execution RPC 和 beacon/blob 数据。官方发布的全节点快照
为裁剪版本。备用 feed 是同运营方约落后 500 毫秒的传输备用。
[Robinhood 自建节点文档](https://docs.robinhood.com/chain/run-a-full-node/)

## 复现

仓库入口为 `python -m scripts.capture_robinhood_nitro_feed`。本次留存位于本机忽略目录
`reports/research/multichain-forward-20261122/robinhood/`。父目录有首次 `STOP`、修正说明、
恢复范围及原累计 RPC 账本；`bounded-validation/` 有第二次握手、逐帧的原始字节和 SHA-256、
接收 UTC 与单调时钟、采集摘要、离线解码，以及 4 份 RPC 原始请求和响应。新采集保存在
忽略目录，单次连接且不做 JSON-RPC 读取。

```sh
.venv/bin/python -m scripts.capture_robinhood_nitro_feed decode \
  reports/research/multichain-forward-20261122/robinhood/bounded-validation \
  --sources-dir reports/research/multichain-forward-20261122/robinhood/sources
```

留存样本的 `capture.json` SHA-256 为
`60d4a4690ce666d52e91d83d2f3dde5a1a9e75e9f9d93a563fcbfa9bdfb58fdf`，
仓库入口离线回放逐字节匹配留存的 `decode.json`。聚焦的握手、解压和批次解析回归 3/3 通过。
以下步骤针对另一次有权采集的样本，`prepare` 只取官方静态配置与源码，`decode` 离线。

```sh
.venv/bin/python -m scripts.capture_robinhood_nitro_feed prepare reports/robinhood-nitro-sample
.venv/bin/python -m scripts.capture_robinhood_nitro_feed capture reports/robinhood-nitro-sample
.venv/bin/python -m scripts.capture_robinhood_nitro_feed decode reports/robinhood-nitro-sample
```
