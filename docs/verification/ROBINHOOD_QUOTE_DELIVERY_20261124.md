# Robinhood 从新头到 20、24 和 37 跳整路径报价

北京时间 2026-11-24。两个独立冻结窗口使用主 sequencer feed 提示各取一对直接父子块。
两个匿名 RPC 提供者重算 Nitro 块头哈希，与 feed 的块号、哈希、时间和系统位置后的有序
交易相符后，再按该哈希核对四个合约的运行码、静态池状态和有限金额报价。两窗口各使用
一个 WebSocket 和 24 次免费 HTTP 读取，费用 USD 0。所有输入为 **0.0001 ETH**，输出以
原生 ETH wei 结算。实验验证固定 PoolKey 的状态及 Quoter 返回，整笔交易余额对照数为零。
[Robinhood 官方接入](https://docs.robinhood.com/chain/run-a-full-node/) · [Uniswap 官方部署](https://developers.uniswap.org/docs/protocols/v4/deployments#robinhood-chain-4663) · [EIP-1898](https://eips.ethereum.org/EIPS/eip-1898)

## 同哈希有限金额和下一块

路线来自更早的 120,754,470 池身份快照，按费率、PoolId 和定向邻接冻结。使用静态费且无
hook 的官方 V4 池，每条路线池身份互异，允许币重复。首个范围取 2、4、10、20 跳见证，
并取得 2 和 20 跳的反方向。截止每个状态在两源上核对 20 个池的 `slot0` 与活跃流动性。
六路整路径报价通过 V4Quoter 的同次 `unlock`，依次使用前腿整数产出。Quoter 对输入未
完全消耗的腿会回滚。每条报价的模拟状态会还原，因此不同报价共享同一个初始哈希。
[旧池身份快照](MULTICHAIN_TAIL_DEPTH_20261123.md) · [V4Quoter 输入路径](https://github.com/Uniswap/v4-periphery/blob/main/src/lens/V4Quoter.sol) · [全量输入检查](https://github.com/Uniswap/v4-periphery/blob/main/src/base/BaseV4Quoter.sol)

| 路线 | H/H+1 gross，wei | H 乐观净额，wei | H+1 乐观净额，wei | Quoter gasEstimate |
| --- | ---: | ---: | ---: | ---: |
| 2 跳 | +37,410,166,200 | −423,455,833,800 | −424,127,833,800 | 68,327 |
| 2 跳反向 | −131,529,286,643 | −592,395,286,643 | −593,067,286,643 | 69,288 |
| 4 跳 | −447,510,513,516 | −908,376,513,516 | −909,048,513,516 | 123,917 |
| 10 跳 | −771,727,723,829 | −1,232,593,723,829 | −1,233,265,723,829 | 329,154 |
| 20 跳 | −1,939,016,527,775 | −2,399,882,527,775 | −2,400,554,527,775 | 633,415 |
| 20 跳反向 | −4,151,660,074,217 | −4,612,526,074,217 | −4,613,198,074,217 | 614,785 |

H/H+1 为 **121,589,162 → 121,589,163**。两块的 20 个池所读字段匹配，六条路线的
整数输出也相同。表中净额只扣各块 base fee × 21,000，分别为 460,866,000,000 和
461,538,000,000 wei，所有报价连这个门槛都过不了。`gasEstimate` 是 Quoter 内测量的
gas，实际交易还需计算外层执行、数据费、融资与失败成本。本窗口一条两跳毛差为正，
通过最低成本筛选的路线 0/6，整笔余额模拟 0/6。
[Robinhood 费用口径](https://docs.robinhood.com/chain/gas-and-fees/)

第二个范围用固定种子的去桥边和偶数度子图构造更长路线。冻结的 58 个每跳费率不超过
20,000 pips 的池中，去桥后闭环所在最大组件 27 池，构造出 24 池见证。放宽费率后的
77 池图上界 43 池，构造出 37 池见证。这些上界只适用于旧快照的本次 ETH、无 hook、
不同池口径。本轮读到 **121,590,367 → 121,590,368** 的 37 个池状态。

- 两跳对照两块均回到 **100,039,645,928,900 wei**，扣基础费用下界后净额分别为
  −421,220,071,100、−421,262,071,100 wei。
- 24、37 跳均在两块回滚，四个原始结果的外层为 `UnexpectedRevertBytes(bytes)`，内层
  `NotEnoughLiquidity(bytes32)`。PoolId `0xb7ef53d0545e8e4f00524a3bbdfd92e3f41a0c969551b37b752f03c71bfd05c1`
  在两路均为 **第 4 腿**。已取到该池状态，报价仍无法消耗整腿金额，之后的腿无可验证报价。
  未推算根本无法成交的路线净额。[Quoter 回滚封装](https://github.com/Uniswap/v4-periphery/blob/main/src/libraries/QuoterRevert.sol)

| 从 feed 收帧起，秒 | 20 跳范围 H | 20 跳范围 H+1 | 24/37 跳范围 H | 24/37 跳范围 H+1 |
| --- | ---: | ---: | ---: | ---: |
| 两家块头一致 | 0.545 | 2.588 | 0.634 | 2.518 |
| 两家报价回复完整 | 1.987 | 7.314 | 2.248 | 5.253 |

计时包含本机捕获四帧并留档、选择前两个连续哈希、逐块核验、250 ms 来源节流和各次
网络响应。H+1 在首块处理期间等待，因此两个 H+1 时钟包含排队。四个 feed 到双源报价的块样本
均超过 750 ms。单对实际 Quoter/状态聚合 HTTP 响应的较慢臂用时约 280–327 ms，整个交付
的其余时段包括多次代码身份读取和排队。数据支持下一轮把同哈希的独立读取做受控并行或
批取对照，仍需保留其完整回执和来源计数。这里没有足够样本估计 p95 或部署收益。

## 复算和原账本

原始档位于 `reports/research/robinhood-quote-forward-20261124/`，`run` 与 `depth-run` 子树含
冻结方案、原始 feed 帧、逐个 HTTP 读的请求/响应/接收时钟及原 SQLite 用量账本。
固定区块的两家 `eth_getCode` 逐字节相同，PoolManager、StateView 和 Quoter 与既有运行码
指纹相同，Multicall3 的指纹亦在四块一致。回放重新解码 feed、核序和块承诺、重构请求并
解码全部结果。保留原始链字节在忽略目录中。
[采集与复算入口](../../scripts/measure_robinhood_quote_delivery.py) · [金额与 ABI 校验](../../src/oasis_agent/evidence/robinhood_quote.py)

| 留存件 | 20 跳范围 SHA-256 | 24/37 跳范围 SHA-256 |
| --- | --- | --- |
| `plan.json` | `b3c93d714e24bcb2f7d7d200b13baf2632a625d6acf82a0a9816f423cf26c9fa` | `f33ae4e58af59f25b8c74e7377fab6d3d6d6ba39d76d2e7bb401ca307fea89b4` |
| `report.json` | `3a61e0e37a4fbe48c0adbe8a6e5742f3739dc981835e209fef9f3fed1514b8e9` | `e7ae265620366e50e33c4e4b57b5036a0a1ba2325aa356ab230d00fdc6ee3d7a` |

```sh
set -o pipefail
scope=reports/research/robinhood-quote-forward-20261124
sources=reports/research/multichain-forward-20261122/robinhood/sources
replay() {
  .venv/bin/python -m scripts.measure_robinhood_quote_delivery "$scope/$1" --sources-dir "$sources" \
    | cmp - "$scope/$2"
}
replay run live.log
replay depth-run depth.log
```

离线失败对照在第一范围原响应中注入非法协议费，代码在每源已用六次读取后保留 `STOP`，
不传播无效池状态和报价。接收完第一对响应后才允许下一阶段发出请求。两段成功捕获用更新后
的校验器均逐字节复算通过。专项覆盖合法闭环 ABI、开环拒绝、重复池、整数上界、费用和
子调用失败。

## 集成验证

评审发现非法协议费的异常类型会绕开终止标记。改为统一的状态验证错误后，中断此前的
全量检查并从头复跑。首轮密钥扫描把三个公开运行码指纹识别为高熵候选，随后改为复用
既有 observer 和 route-probe 配置中的指纹及合约身份。保留首次检查和复验日志于
`reports/research/multihop-continuous-checks-20261124/`。

最终 `make check`、`make security-check`、`make contract-security-check` 和离线
`make rust-check` 均退出 0。6,645 个 Python 测试和 138 个子测试通过，保留 10 个已有警告。
前端检查通过 9 个模块，Foundry 127 个测试通过，Slither 保留 6 条既有提示，Rust 30 个
测试通过。两份 Robinhood、两份 Base、Ethereum 留出与原调用、BSC 两组证据均已复算。
265 个相对文档链接及 `git diff --check` 通过。
