# Base Denim 头部时间戳：首次观察与证据管线校验

2026-11-15（Asia/Shanghai）；作者/内部自审 Codex，无独立 reviewer sign-off。本记录只处理
只读链上观察与离线代码核对，不评估收益、不构造交易。

## 问题

外部研究报告称，Base 已于 2026-11-05 18:00 UTC 激活 Denim 升级，原生区块间隔变为
200 ms，EVM `block.timestamp` 仍按整秒递增，精确毫秒时间只通过区块头 `timestampMs`
或 BaseTime 预部署合约 `0x4200000000000000000000000000000000000030` 暴露；报告同时
怀疑本仓库仍依赖已退役的 Flashblocks 订阅或按 `block.number` 推算秒数。本轮先验证
头部字段是否真实存在，再核对本仓库代码是否依赖旧接口。

## 事先固定的范围

- 来源：`publicnode_base`（8453），公开匿名 endpoint，无认证、无 value、无广播参数。
- scope：2 次请求、60 秒上限；scope SHA-256
  `8c118c905a73c834de3a5b910cf62db7df3ea8a89d19f19528c1c96b92cac606`。
- head plan：`latest`，plan SHA-256
  `4c073ac1cde60a0fba4e78d713acfddf6a3ffd126a06081339619c790721af02`。
- 采集器先读链 ID，再读块头；两次请求均为 HTTP 200，原始字节与预算计数已保留。

## 实际观察

| 字段 | 值 |
| --- | --- |
| 块号 | 57,794,191 |
| 块 hash | `0xb1f95b9e94ae6f750cefb48940ceb4965f51dedd0ec6d4a513eaea3b5fc3cd35` |
| `timestamp` | `0x6af9b31d` = 1,794,749,213 = 2026-11-15T13:26:53Z |
| `timestampMs` | `0x1a1df63ac68` = 1,794,749,213,800 = 2026-11-15T13:26:53.800Z |
| 毫秒部分 | 800 |
| capture SHA-256 | `0db8631ddf295bef0421bc2e480e7a69599ba8d55616d450e8affa5184ed2363` |

`timestampMs // 1000 == timestamp` 成立，毫秒部分为 800。该块头包含 `timestampMs`，
不包含 `blockTimestampMs`；这与报告描述的“整秒 timestamp + 毫秒扩展”一致。观察为
单来源、本机接收时钟，不是独立最终性证明。

## 代码核对

- `src/` 与 `scripts/` 中没有 `flashblock`、`newFlashblocks` 或 `flashblock_index`
  的实际调用；只有设计文档提及 Flashblocks。
- `sniping/event_wakeup.py` 使用标准 `eth_subscribe("logs")`，不是 Flashblocks 专用
  订阅；断线后用 HTTP `eth_getLogs` 回补。
- 没有发现 `block.number * 2` 或按块号估算秒数的执行路径。
- Solidity 中的 `block.timestamp` 只用于报价有效期和 intent deadline；没有用秒级差值
  计算波动率或冷却窗口。
- `sniping/config.py` 的 `poll_interval_ms=2000` 是 WebSocket 不可用时的回退轮询间隔；
  事件流可用时会被提前唤醒。它仍是公开 RPC 场景下的延迟限制，但不是 Denim 协议断裂。
- `max_age_blocks` 当前未被使用；实际块头鲜度由 `max_age_seconds=120` 约束。200 ms
  区块下 120 秒约等于 600 个区块，属于宽松上限，不是块号计时错误。

## 代码改动

新增 `src/oasis_agent/evidence/base_denim.py`：

- `parse_header_timestamp_ms` 严格校验 `timestampMs` 为规范十六进制、未溢出，且
  `timestampMs // 1000 == timestamp`；
- `validate_header_timestamp_ms` 只对 Base 8453 生效，Denim 激活后的块头缺少
  `timestampMs` 时拒绝，激活前的历史块允许省略；
- `parse_base_time` 交叉校验 BaseTime 的 `timestampMs` 与 `timestampMillisPart`；
- 选择器常量 `timestampMs()` = `0x5745a677`，`timestampMillisPart()` = `0x7b2fea99`。

`arbitrage/cycle_evidence.py` 的块头解析现在调用该校验。新增单元测试覆盖合法 800 ms
块头、缺失字段、秒级不一致、毫秒不一致、溢出和激活前历史块。该改动不改变
`BlockRef` schema，不引入网络调用，也不产生执行权限。

## 材料哈希

| 材料 | SHA-256 |
| --- | --- |
| `scope.json` | `8c118c905a73c834de3a5b910cf62db7df3ea8a89d19f19528c1c96b92cac606` |
| `head.json` | `4c073ac1cde60a0fba4e78d713acfddf6a3ffd126a06081339619c790721af02` |
| capture | `0db8631ddf295bef0421bc2e480e7a69599ba8d55616d450e8affa5184ed2363` |
| replay | `ad587dad0a75009528755491b3c73dde98703ea945f6e1d9eb3cc938f1562775` |

## 限制与下一步

本轮只验证了区块头 `timestampMs`，没有读取 BaseTime 预部署合约的实际返回值；后者需要
在固定块或单读取 latest 计划下单独采集。Base 200 ms 区块使多读取 latest 计划容易跨块，
因此 BaseTime 验证应优先使用归档 provider 或单读取计划。没有 live 权限变化；Base LP
仍处于 owner 要求的暂停状态。
