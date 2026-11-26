# Base Slipstream 同哈希 tick 深度核验

北京时间 2026-11-27。对既有 66 池窗口报价失败最多的 Slipstream 池补齐可观察深度，
沿用四个留存区块和原有金额网格。单池精确重放使可闭合的模型报价从 236 增至 2,436，
四块均无扣原模型费用后为正的点。仍有 2,316 个点缺完整报价。
[原四块和未决区间](BASE_WHOLE_SURVIVAL_20261124.md) · [先前单腿诊断](MULTIHOP_PRIORITY_DIAGNOSIS_20261127.md)

## 固定范围与原始证据

池 `0x4e392fbfe4d0557c82d2f97f02ec39daa31516dd` 是 WETH/USDC，池厂
`0xf8f2eb4940cfe7d13603dddd87f123820fc061ef`，Quoter
`0x514c8b5f54112481e28028f1166bd78501089259`。只读范围固定区块 `61,476,050–61,476,053`
和每块活跃 word 左右各八个 word。每块对 Base 官方 RPC 和 PublicNode 独立执行两次
`eth_call`，均使用 EIP-1898 的区块哈希与 `requireCanonical=true`。第一批核对链 ID、
`slot0`、活跃流动性、池与 Factory fee、代币、tick spacing、Factory `getPool` 与 Quoter
的 Factory，随后读取 17 个 bitmap word。第二批按两源一致的 set bit 查询 `ticks(int24)`，
每块预先限定 128 个初始化 tick，核对 Solidity ABI、`initialized`、总流动性与 `liquidityNet`。
每源预留最多 8 次，最少间隔 250 ms，首个拒绝即停止。历史范围内实际完成 16 次，
各 8 次，USD 0。请求与回复原文、来源、时间、哈希以及原始 SQLite 账本均已保留。
[EIP-1898](https://eips.ethereum.org/EIPS/eip-1898) · [Aerodrome CLPool](https://github.com/aerodrome-finance/slipstream/blob/main/contracts/core/CLPool.sol)

| 区块 | 规范哈希 |
| ---: | --- |
| 61,476,050 | `0x74b7cf02a8f8cb5b17a5c249b932be553f568884fcf477e96ac68ea0ffb249eb` |
| 61,476,051 | `0xd2c4fe79cec78aadd8393176417f45b233bc7c79c4c1aafc3f0babd7ca1489e3` |
| 61,476,052 | `0x6455bd83786aff40a357d4ac135978e3d8911c8751e36e7e1bd97f5a6c774838` |
| 61,476,053 | `0xe9782766eebe6972a1826e48b8de87b1a87d055902f4a39fb913d0ac641dc7b7` |

两个来源在四个区块分别给出的状态相同。每块覆盖 word `[-797, -781]`，含 18 个初始化
tick，活跃 tick `−201924`，活跃流动性 `15201239936812`，tick spacing `1`，输入费
`80 pips`。采集接收时间为北京时间 `00:59:48.699–00:59:52.655`。池和 Quoter 以同哈希
合约读取核对身份和配置，本次试验的整数对照来自先前双源 Quoter 回复。

## 精确单腿对照与网格重放

以首块原失败腿的 `54,834,356` USDC 最小单位作为固定输入，按真实 bitmap 在每个
word 边界及初始化 tick 做整数步进。重放与此前两份 Quoter 原始响应的三个字段一致。
越过所读深度时返回显式覆盖缺失。
[精确步进和边界](../../src/oasis_agent/fastpath/slipstream_depth.py) · [Aerodrome QuoterV2](https://github.com/aerodrome-finance/slipstream/blob/main/contracts/periphery/lens/QuoterV2.sol)

| 对照字段 | 两源 Quoter 与本地重放 |
| --- | ---: |
| 输出，WETH wei | 30,378,959,162,042,405 |
| 末端 `sqrtPriceX96` | 3,402,687,413,640,448,204,929,318 |
| Quoter 初始化 tick 计数 | 5 |

四块分别重跑原 36 个未决区间的 33 个固定金额，合计 4,752 点。只替换目标池这一腿，
其余池、起始币、投入和原固定 L1/L2 成本保持相同。结果如下。

| 指标 | 原合成区间 | 真实 tick 深度 |
| --- | ---: | ---: |
| 完整模型闭环报价 | 236 | 2,436 |
| 未取得完整报价 | 4,516 | 2,316 |
| 原模型成本后的正点 | 0 | 0 |

新增 2,200 个完整报价，原有 236 个均保留，其中 64 个整数输出改变。每块 WETH 起始币
可报价点从 39 增至 332，USDC 从 20 增至 277。剩余未定价中 1,440 点超出这次读取的
bitmap，876 点的首个失败落在其他池，其中 `0xdbc6998296caa1652a810dc8d3baf4a8294330f1`
为 412 点。距盈亏线最近的 WETH 点仍为 `−3,542,217,466,529 wei`，USDC 为 `−7,115`
最小单位。该试验报告有限金额网格上的单腿改进。其他腿仍由旧模型估算，整笔模拟、
真实费用和 144 个连续金额区间的闭合情况保持未知。

## 离线复核

原文保留在忽略目录 `reports/research/base-slipstream-depth-20261127/`。下列 SHA-256
分别绑定冻结范围、采集摘要及网格比对。两种复核均从原始 RPC 请求和回复重解码。

| 文件 | SHA-256 |
| --- | --- |
| `plan.json` | `c43d00073ad367c88379edd53c715b87c9b0353cee0b655aed312ed29eae0006` |
| `run/capture.json` | `611152f1a5fe67a2f15cf818073f126794b46ed436546934fef6cb9f4fcc7a37` |
| `grid.json` | `e66f86be209ee4abe96baf8ed111c596a37bc82da07f921acf6b36bd0e2846db` |

```sh
set -o pipefail
scope=reports/research/base-slipstream-depth-20261127
archive=reports/research/base-newheads-overlap-four-20261124/run
quote=reports/research/slipstream-probe-20261127
.venv/bin/python -m scripts.replay_base_slipstream_depth replay \
  --scope "$scope" --archive "$archive" --quote-control "$quote"
.venv/bin/python -m scripts.replay_base_slipstream_depth compare-grid \
  --scope "$scope" --archive "$archive" --quote-control "$quote" | cmp - "$scope/grid.json"
```

Ruff、两个新增 Python 源文件的聚焦 mypy、16 项 Slipstream 和 mixed state 回归、
两个离线复核及 `git diff --check` 均通过。新增回归覆盖负 tick 的 bitmap 压缩、
双方向整数边界、离开已观测范围和真实 Quoter 对照。
