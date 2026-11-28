# Aleph Base Reth 整笔返回父哈希复核

Aleph 上的 Base Reth 1.5.6 在仅回环、无对等节点的内置 Base 创世状态上，返回了准确的 `eth_simulateV1.parentHash` 和下一高度。两组近期 Denim 主网父块仍缺可用的规范状态，历史整笔父哈希、到账和 gas 仍待在 Reth 上验证。已有的两组历史主网对照来自官方公共模拟与上轮 Base Anvil 回放。[本轮原件摘要](BASE_RETH_PARENT_20261128.json) · [上轮主网与 Anvil 对照](BASE_DENIM_NATIVE_ALEPH_20261128.md)

## 已核项目

- 发行包 `base-reth-node-v1.5.6-x86_64-unknown-linux-gnu.tar.gz` 的 SHA-256 为 `1342108c0660e91ecd460dc88b144816e8e188045f5dd25c83e00a27b5927469`，与 GitHub 发行元数据一致。解包后二进制的 SHA-256 为 `ea1d85e687336851141c0da42c34309740dc9fa118537d5e4a538e1f1649ff94`。[Base Reth v1.5.6 发行](https://github.com/base/base/releases/tag/v1.5.6)
- 固定版本的 `simulate_v1` 从指定块取得封存父头，以此建立下一块的执行环境。Base EVM 的 `context_for_next_block` 取该父头的 `hash()` 填入执行上下文。源码解释了返回父哈希应绑定的位置，实际响应仍需检验。[Base RPC 源码](https://github.com/base/base/blob/v1.5.6/crates/execution/rpc/src/eth/call.rs) · [Base EVM 源码](https://github.com/base/base/blob/v1.5.6/crates/execution/evm/src/config.rs)
- 规范对照仍是同秒父块 **63,043,739** 和跨秒父块 **63,217,836**。先前按哈希保留的路径访问数据每组涉及 17 个地址。Aleph 未找到可直接提供这两个父块状态的已同步 Base Reth。本轮未导入这两个高度与头部状态根一致的完整状态。

## 停止点

Aleph 直接下载在 21,315,653 字节处超时，随后续传并核对整个发行包摘要。第一次自定义 genesis 的启动在发送模拟前停止。原始 CLI 日志确认两组发现服务参数互斥。跨秒组未启动。Aleph 根卷可用约 516 GiB，之前保留的是路径接触到的少量状态，缺少这两个高度可以给 Base Reth 提供的完整规范数据库。官方最小主网快照约 782.5 GB 且解包后更大。[官方快照说明](https://docs.base.org/specifications/node-operators/snapshots)

## 原生 RPC 实测

独立启动内置 Base 主网创世链规格，绑定回环并限制对等节点为零。链 ID 为 8453。用头字段的 RLP 与 Keccak 重算父块 0 的哈希 `0xf712aa9241cc24369b143cf6dce85f0902a9731e70d66818a3a5845b296c73dd`。对创世状态整笔模拟一次零金额 identity 预编译调用，未改写的 RPC 响应返回同一个父哈希、下一高度 1、成功状态、原字节 `0x42617365` 和 **21,082 gas**。三次请求全在 Aleph 本机。公开 RPC 预留与支出均为零。

旧日志、本机回环原件与旧测试输入保留在 `reports/research/base-reth-parent-20261128/`。下面的入口同时复核初次启动记录和成功的原生 RPC 证据。历史路径仍要求取得近期规范父块状态，然后在 Reth 上逐项核对同秒与跨秒的现金流、父哈希和费用。

```sh
PYTHONPATH=. .venv/bin/python docs/verification/fixtures/replay_base_reth_parent.py --replay
```
