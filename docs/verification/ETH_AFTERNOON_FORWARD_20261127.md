# Ethereum 下午新头与固定 type-2 对照 · 2026-11-27

北京时间 **15:49:10.685–15:49:28.373**（以下时钟为 UTC）独立前瞻。45 个已登记 V2 池构成 **7,976 条** WETH 起止的 2–6 池路径，按十档本金精算 **79,760 个**报价点。正毛额为 **0**，连续全金额毛额上界越过当块 21,000 gas 基础费下界的路径为 **0**。以原有三腿固定本金对照复核发现块、首次返回后最新头与直系子块。获证的正机会寿命分母为 0。

## 冻结与状态

PublicNode 范围先冻结，再取得区块 **26,505,152** 作为起点，并等待其直接子块成为发现块 H。按 canonical block hash 同一批读取全部储备、研究发送者 nonce 和高度复核。发现头在 **07:49:14.484** 收到，接头至状态 509 ms，至排名 1,172 ms。独立排名最高点为 WETH→SHIB→WETH，毛差仍为 −25,098,999,823 wei。整笔对照在本范围开始前固定为 WETH→USDT→LINK→WETH 和 **100,000,000,000,000 wei**，延续先前留存的池地址、方向和探针。旧范围的池与运行码注册证据用于接续，本窗口只覆盖公开已出块、登记的 V2 池。[原范围及研究包络](ETH_FORWARD_AND_TAIL_20261127.md)

同一无签名 type-2 调用含 chain ID 1、nonce 0、900,000 gas、空 access list、max fee **305,527,058 wei/gas** 和 max priority fee **50,000,000 wei/gas**。每个不同状态按哈希独立核对 nonce。只临时覆盖研究发送者的余额/空代码和探针地址的零余额/代码。包络 SHA-256 为 `ca88a3f4552c0a73f583c0936b4ba8b2991b916d2e846622a7e7170cb21895e4`，无签名 EIP-1559 摘要为 `0xca8f48de6434592ea92a1fad53e29534b6244aa5af721af27c0f5edf4105852e`。[EIP-1559](https://eips.ethereum.org/EIPS/eip-1559)

| 阶段 | 区块和时间 | WETH 输出（wei） | gas 封顶净额下界（wei） |
| --- | --- | ---: | ---: |
| H · 发现块 | 26,505,153，首次整笔返回 07:49:16.195 | 98,642,871,672,426 | −250,778,774,127,574 |
| K · 首次响应后首个最新头 | 07:49:16.407 发出，16.704 收到。最新头仍是 H 且哈希相同，复用 H 结果 | 98,642,871,672,426 | −250,778,774,127,574 |
| C · H 的直系子块 | 26,505,154，独立验证父哈希，整笔返回 07:49:28.373 | 98,642,871,672,426 | −247,313,008,227,574 |

两次整笔和独立逐腿整数计价吻合，毛差均 **−1,357,128,327,574 wei**。净额下界按各状态 `gross − gas_limit × min(base_fee + priority_fee, max_fee)`。跨状态单一费用上限下界为 −276,331,480,527,574 wei。K 的首个读取于 H 整笔回报后 212 ms 发出，509 ms 收到。两状态分别记录区块号、哈希、父哈希、原始请求和应答时间。

## 独立整笔 gas

07:50:05.132–07:50:06.073 独立冻结并完成两次 dRPC 匿名 `debug_traceCall`。分别重放 H、C 原 canonical block hash、同一完整调用字段和余额/代码覆盖。两个输出与相应 PublicNode 请求逐字节相同，顶层 `gasUsed` 同为 **283,936**，支持以整笔计费量做模拟情景。Geth 原生 callTracer 的根 gas 取模拟收据，含 intrinsic 并计退款和 calldata floor。此固定 calldata 的 intrinsic 为 24,340 gas，Prague calldata floor 为 29,350。函数体观测的 327,119 gas 另外留存。dRPC 未报告后端客户端版本或确切退款数量，表内按兼容 Geth 的语义计算。[Geth 顶层 gas](https://github.com/ethereum/go-ethereum/blob/v1.16.5/eth/tracers/native/call.go#L196-L216) · [Geth 收据](https://github.com/ethereum/go-ethereum/blob/v1.16.5/core/state_processor.go#L160-L175) · [退款与 gas 下限](https://github.com/ethereum/go-ethereum/blob/v1.16.5/core/state_transition.go#L410-L535)

| 状态 | 有效费用（wei/gas） | 模拟整笔费用（wei） | 模拟净额（wei） |
| --- | ---: | ---: | ---: |
| H、同哈希 K | 277,135,162 | 78,688,649,357,632 | −80,045,777,685,206 |
| C | 273,284,311 | 77,595,254,128,096 | −78,952,382,455,670 |

PublicNode 使用 **10/21 HTTP、15/33 逻辑调用**，dRPC 使用新账本 **2/2 HTTP**，返回 free 标记。共 **12 HTTP、17 逻辑调用、USD 0**，两份预登记范围均没有首次错误。全部为匿名只读，实际链上扣费和入块结果未观测。此次只扫一组已出块状态，跨 V3/V4 池、未登记池、交易级排序和新正机会的寿命不在结果内。

## 离线复算

原始请求、响应、账本、scope、墙钟和读取脚本保存在忽略目录 `reports/research/eth-afternoon-forward-20261127/`。两个重放分别按原预留顺序重算，核对全部已保留记录。

```sh
e=reports/research/eth-afternoon-forward-20261127
PYTHONPATH=. PYTHONDONTWRITEBYTECODE=1 .venv/bin/python "$e/run.py" replay
PYTHONPATH=. PYTHONDONTWRITEBYTECODE=1 .venv/bin/python "$e/gas-audit/run.py" replay
```

| 文件 | SHA-256 |
| --- | --- |
| 前瞻 scope | `ec1b0d0de00ba90a97620e590c6c4dd19cd4dbefc57e92a3672f9cf1a8ae563c` |
| 前瞻结果 | `b9324135f1d41efc0af5d8ca36d78d582a2943cb95e5f52b36bb47d627fbcfdf` |
| gas scope | `584a09aeccbdd8d4a00ce0ecbe2cb26dfac484baeb6a77069328db418d5883d1` |
| gas 结果 | `20a16ba173004ceb8adec70cba12cbaddf3879bdcda3ffe97e986cdfccc95038` |
