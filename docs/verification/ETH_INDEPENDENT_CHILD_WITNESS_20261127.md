# Ethereum 独立子头见证 · 2026-11-27 UTC

在真实 45 池影子窗口，固定负控的 H 整笔模拟于接头后 **2,906.115 ms** 双源齐备。同机独立 WebSocket 在 **11,523.525 ms** 收到并验证直接子块 C。两者相差 **8,617.410 ms**。这个结果衡量本机从指定来源收到消息的先后。自然正例为零，及时存续率仍未知。[此前仅有事后 HTTP 观测的窗口](ETH_BATCH_HKC_AND_BASE_RPC_20261127.md)

## 冻结范围和证据规则

14:26:14.421 UTC 冻结，采用原 45 池、WETH 起止、7,976 条路径、十档本金，及原两段有意复用相同三池的负控。H、K、C 仍由 PublicNode HTTP 头绑定，整笔调用由 PublicNode 与 dRPC HTTP 对拍。选用 `--independent-newheads` 时，dRPC 公共 WSS 在 H 请求前完成链号和订阅回执，应用线程独立记录每帧到达。WSS 与主头 HTTP 是不同提供者。整笔模拟的次来源和 WSS 同属 dRPC。该观测无权限执行或释放资金。[dRPC 匿名端点](https://drpc.org/docs/ethereum-api) · [dRPC newHeads](https://drpc.org/docs/ethereum-api/subscriptions/eth_subscribe) · [影子入口](../../scripts/run_eth_v2_batch_shadow.py)

每帧使用同机单调时钟，并记录原始字节、SHA-256 和壁钟。按 Ethereum 共识头字段重算哈希，要求 feed 中 H 与 C 连续且与 HTTP 的高度、哈希、父哈希完全一致。等同重复保留首到时间。错链、迟订阅、断线、缺头、重组、时钟或原件冲突都使及时性保持未知。C 的独立相对到达时刻与 C 的 HTTP 状态时钟分别使用，后者继续约束整笔成本和现金证据。[见证实现](../../src/oasis_agent/evidence/eth_child_witness.py) · [聚焦反例](../../tests/unit/test_eth_child_witness.py)

WSS 单独预留一次连接、一次 `eth_chainId` 和一次订阅，共 3 项，最多接收 16 帧，订阅就绪上限 12 秒，观测上限 55 秒，获知 HTTP 子块后等待匹配帧最多 5 秒。HTTP 额度仍为 PublicNode 24、dRPC 9。首次拒绝结束范围，无重连、密钥、签名、广播或付费流量。实际分别使用 PublicNode HTTP **14**、dRPC HTTP **6**、dRPC WSS **3** 项，USD 0。

## 三时点与实际时钟

| 事件 | 从 H 的 HTTP 收头起，ms |
| --- | ---: |
| WSS 订阅回执 | −10,133.147 |
| WSS 收到 H | −62.289 |
| H 双源池状态齐备 | 755.487 |
| H 报价和组合排名完成 | 1,429.793 |
| H 首个整笔回报 | 2,889.317 |
| H 整笔双源齐备 | 2,906.115 |
| K 的 HTTP 回报 | 3,691.124 |
| WSS 收到 C | 11,523.525 |
| C 的 HTTP 回报 | 11,945.972 |

两帧连续。H 为 **26,507,132**，哈希 `0xf6e5f3ca2050c343eaad265f19daedfa63e485f263e555ca04374d955b6218fb`，WSS 于 14:26:37.054110 UTC 收到。C 为 **26,507,133**，哈希 `0x45542dc6eb2d8a2fc092c3812b4ca4cea1c535933d1cf164dd772ae721f10c08`，WSS 于 14:26:48.639912 UTC 收到。C 的父哈希等于 H。K 返回同一 H，因此复用整笔结果，保留 K 的独立 HTTP 查询时钟。C 的 WSS 比本次 HTTP 读取早 **422.447 ms**。该差值适用于这一组实际请求，HTTP 查询过的时间仍受轮询计划影响。

网格 79,760 个整数点中 6 个正毛差，0 个跨过 21,000 gas 基础费门槛。组合菜单 30 项，评估 116 次逐腿预算单位，净正短名单为空。故三阶段使用预先冻结负控，包络摘要 `dd24ffbe41bea189b122639d7ddc87b2d9831504c59f91321e49fd63d4bebfa9`。H、K、C 毛差均为 **−1,943,739,370,587 WETH wei**。dRPC 两个 trace 都报告 **466,986** 模拟整笔 gas，单一来源、无实际扣费。按各自有效费率，扣模拟 gas 后 H/K 为 **−264,354,661,093,173**，C 为 **−250,803,284,954,457 WETH 等值 wei**。同一现金结果完成了子头时序核对，未测试自然正例获利或纳入。

## 本地逐字节复算

数据保存在本地忽略目录 `reports/research/eth-independent-child-20261127/`。v3 的五份冻结源码原字节保存在 `frozen-sources/`。当前源码有变化时，离线入口要求原预注册摘要，并逐个核对留存源码的哈希和计划内容，再用当前解释器从原始响应复算并与原结果逐字节比较。留存源码仅供身份验证。新实时采集仍强制核对当前源码与新计划。

v1 两笔已完成负控和 v2 原窗也逐字节复算成功，均以原预注册摘要进入离线回放。v1 保留当时的本地 HTTP 字段，仅在完成的负控回放中投影。v2 复算事后轮询观测，其子块及时性仍未知。聚焦测试 25 项及源码规范门禁通过，包含重复头、错父、跳高、同高冲突、假哈希、断线、迟就绪、篡改和早于事后 HTTP 的独立子头。Ruff、格式、mypy 和 `git diff --check` 通过。

| 原件 | SHA-256 |
| --- | --- |
| scope.json | `9947362536b14a74403848ce9a065d01b6a1f7aa9d0fbf272078f85ca70129e8` |
| result.json | `848dc3ed8031c858268ded71a1c32b102f9f8f7b3245637d78d9a9478729a68e` |
| STOP.json | `47f8e365d3a4bfb5a21bb1c2df378aa562068c18c6af2cce3a881441e813fc63` |
| rpc-budget.sqlite3 | `03c93dd0088df72cfe8695282643689aff15a45bee1db41fea1aa888536734e9` |
| ethereum-newheads/STOP.json | `d7a1ab42bc9d7aecc1ce5780c13ce2d6fe32625c84e089cdfeabaf2952703229` |
| ethereum-newheads/original-budget.sqlite3 | `d569b834553887904e7a68d14d90d9b0b051cd39dbfcd87ced80375d5644f09e` |

```sh
.venv/bin/python -m scripts.run_eth_v2_batch_shadow replay \
  reports/research/eth-independent-child-20261127 \
  --scope-sha256 9947362536b14a74403848ce9a065d01b6a1f7aa9d0fbf272078f85ca70129e8
```
