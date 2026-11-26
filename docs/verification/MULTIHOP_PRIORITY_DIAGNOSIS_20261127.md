# Base 未决金额的首个失败池与跨块吞吐

2026-11-27 北京时间，代码基线 `d6d4bdc`。复用 11 月 24 日 Base 四个连续头和原始 66 池状态，沿用当时两种起始资产、路线、整数金额区间和费用参数。此次重新归因属于校准数据。源窗口、三阶段负控及可复算入口见[原始验证](BASE_WHOLE_SURVIVAL_20261124.md)。

## 4,752 个原金额逐腿重放

先验收归因哈希链和每块原始清单，再通过相同 Rust 图对每个未决区间试算 `lo + floor((hi-lo)×j/32)`，其中 `j=0…32`。逐腿核对输入资产和金额，记录第一个失败池和跳次，其余金额按原费用算完整环路。每块 36 个区间，四块 144 个，完整报价 236 个，正净额 0 个。其余 4,516 个整数点均返回 `insufficient pool liquidity`。按同一保留时钟和原报价顺序复算两次。

| 首次失败的池或 PoolId | 类型 | 次数 |
| --- | --- | ---: |
| `0x4e392fbfe4d0557c82d2f97f02ec39daa31516dd`，WETH/USDC，tickSpacing 1 | Slipstream | 3,700 |
| `0xdbc6998296caa1652a810dc8d3baf4a8294330f1`，WETH/USDC，另一厂 | Slipstream | 352 |
| `0x22aee3699b6a0fed71490c103bd4e5f3309891d5`，WETH/cbBTC | Slipstream | 336 |
| `0xa975393c7d58e873eb6e23617e6e24b441d156063e4befa4e2042bf019a9ede1` | v4 | 56 |
| `0x96d4b53a38337a5733179751781178a2613306063c511b78cd02684739288c0a` | v4 | 52 |
| `0x272ebdef2a48efba45135b9db30fc8d8e51e4bbeb47ba287e8754f1c3f9f4534` | v4 | 20 |

首次失败在第一、二、三跳分别为 2,616、844、1,056 次。三个 Slipstream 池占 97.17%，第一池占 81.93%。这是优先取哪份状态的依据。一个点修复首腿后仍可能在后续腿失败，跨块可能重复同一路与金额。各项比率仅以这组定向试算点为分母。

地址池用当前活跃流动性和池内两币余额构造对称 tick 区间。v4 读五个 bitmap 字并构造最近活动边界。真实跨 tick 报价还要按方向处理初始化 tick 的 liquidityNet 和合约整数舍入。两个 WETH/USDC 池虽具有相同 tickSpacing，却属于不同 factory。定位方向、报价合约和部署版本都须绑定工厂。[地址池区间模型](../../src/oasis_agent/fastpath/multicall_universe.py) · [v4 状态模型](../../src/oasis_agent/fastpath/base_mixed_snapshot.py) · [Slipstream CLPool](https://github.com/aerodrome-finance/slipstream/blob/main/contracts/core/CLPool.sol)

## 对首次失败最多的池做双源实查

另行冻结一次匿名读取，选择上述列表的第一池，在四块中的第一块取留存顺序中该池第一次失败时的原始输入。Base 官方源与 PublicNode 各 3 读，总计 6/6 个免费读。两源确认高度 61,476,050、哈希 `0x74b7cf02a8f8cb5b17a5c249b932be553f568884fcf477e96ac68ea0ffb249eb`，以该哈希和 `requireCanonical` 调用。双方返回同一个 factory `0xf8f2eb4940cfe7d13603dddd87f123820fc061ef`、池、币种、tickSpacing 1 和池费 80 pips。

第三腿输入 54,834,356 个 USDC 最小单位。对应 Slipstream QuoterV2 的两份响应均输出 **30,378,959,162,042,405 WETH wei**，跨 **5 个初始化 tick**，末端 sqrtPriceX96 为 **3,402,687,413,640,448,204,929,318**，报价器的单腿 gas 估算为 406,307。两源单次往返约 287／159 ms。原始输入处本地模型拒绝，这笔真实历史状态报价为补充 tick 数据提供了可执行的回归向量。保留前两腿模型并接入本次第三腿报价，减原始本金 32,218,750,000,000,000 wei 后，拼接毛差为 **−1,839,790,837,957,595 wei**。本次只查一腿，整条路的可实现净额未知。[Aerodrome QuoterV2](https://github.com/aerodrome-finance/slipstream/blob/main/contracts/periphery/lens/QuoterV2.sol)

仓库内的[公开取证包](BASE_SLIPSTREAM_QUOTER_20261127.json)带六组原始 UTF-8 请求与响应、各自哈希、来源与接收时钟，可独立复核。留存路径 `reports/research/slipstream-probe-20261127/` 含冻结计划、原始请求与响应、账本、停止记录和程序。计划 SHA-256 为 `bc6efb67d05a08d0e2416bf9ef09bad5f165d9f8ce93446dd4ea640859722b3a`，报告为 `d2846341044edac441090fa5922bd4d2f9afbaf276f3744122d085f31b940db2`。离线入口验证报告字节一致。

```sh
PYTHONPATH=. .venv/bin/python reports/research/slipstream-probe-20261127/probe.py replay
```

## 单源限速如何约束排队

| 高度 | 接收到开始取数 ms | 开始取数到状态就绪 ms | 状态就绪到发现完成 ms |
| ---: | ---: | ---: | ---: |
| 61,476,050 | 14.610 | 1,079.915 | 81.716 |
| 61,476,051 | 966.150 | 910.274 | 77.970 |
| 61,476,052 | 1,751.018 | 822.016 | 73.165 |
| 61,476,053 | 2,475.348 | 821.410 | 64.740 |

本机实际相邻收头间隔为 216.175、208.348、175.828 ms。具备可核直接子块的三次发现全部晚于本机收到子块。第四块等待占发现总耗时 73.64%。即使完全消除这组排队，四次独立状态获取耗时仍均超过 750 ms。窗口很短，无法估总体尾延迟。

按 11 月 24 日采集清单，两路来源各设置 250 ms 最短请求间隔。每个头需要 PublicNode 两读、官方三读。按五个头/秒全量运行，分别需要 10 和 15 读/秒，而本配置各源的长期上限约 4 读/秒。官方路径对应全量头的理论吞吐上限约 1.33 个/秒，尚未计传输、失败和 Quoter。这个上限属于本次客户端策略和请求形态。多开 worker 仍共享原账本。可实验的变量是每头逻辑依赖、每源实际远程往返、事件驱动复用，以及是否明确抽样。[并行原账本与请求角色](BASE_DUAL_HEADER_OVERLAP_20261124.md) · [Base Denim 的规范区块节奏](https://www.base.org/blog/denim)

## 可复算性

源文件为 `reports/research/base-newheads-overlap-four-20261124/run/shadow_attribution.jsonl`，SHA-256 为 `674b42fed06b683964761c89b49b275b27ff3f8e7f26cb6ae769f64ae68e20ed`。本地保留的重放程序和输出位于 `reports/research/multihop-priorities-20261127/`。程序 SHA-256 为 `e4b43fb27b776122103ba0521dfc799470f6b70a5ce70583c88a1b65752f11fa`，输出为 `d28e4afe685c684a17aacba4946d211e55c7cd9dc12dc3d6e232e8e19ef27dc2`。四头数据取自原只读采集，本段离线回放消耗零 RPC。

```sh
set -o pipefail
scope=reports/research/multihop-priorities-20261127
PYTHONPATH=. .venv/bin/python "$scope/replay.py" | cmp - "$scope/replay.json"
```

仅用 Git 内的取证包即可验证六组请求与响应的哈希，检查报价状态绑定并解出两份相同的整数结果。以下命令在仓库根目录运行。

```sh
python3 - <<'PY'
import hashlib
import json
from pathlib import Path

pack = json.loads(Path("docs/verification/BASE_SLIPSTREAM_QUOTER_20261127.json").read_bytes())
sample = pack["sample"]
quotes = {}
for entry in pack["exchanges"]:
    for role in ("request", "response"):
        body = entry[role + "_body_utf8"].encode()
        assert hashlib.sha256(body).hexdigest() == entry[role + "_sha256"]
    req = json.loads(entry["request_body_utf8"])
    res = json.loads(entry["response_body_utf8"])
    assert entry["status"] == 200 and req["id"] == res["id"]
    if req["method"] == "eth_getBlockByNumber":
        assert res["result"]["hash"] == sample["block"]["block_hash"]
        continue
    call, pin = req["params"]
    assert pin == {"blockHash": sample["block"]["block_hash"], "requireCanonical": True}
    if call["to"] == sample["quoter"]:
        raw = bytes.fromhex(res["result"][2:])
        assert len(raw) == 128
        quotes[entry["source"]] = tuple(int.from_bytes(raw[i:i+32], "big") for i in range(0, 128, 32))
assert set(quotes) == {"official", "publicnode"}
assert quotes["official"] == quotes["publicnode"] == (30378959162042405, 3402687413640448204929318, 5, 406307)
PY
```

本次提交范围为研究文档与公开只读取证包。正文中的取证包验算、原始网格逐字节回放、独立 Quoter 报告回放、四份文档的 301 个相对链接、`git diff --check` 和秘密材料检查均通过。相关 Base 生存率、混合快照和资金链解码的 22 项定向测试通过。

```sh
.venv/bin/python -m pytest -q tests/unit/test_base_survival.py tests/unit/test_base_mixed_snapshot.py tests/unit/test_swap_flow_trace.py
```
