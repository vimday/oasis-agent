# Ethereum Aave 残差与 Metric 同父块调用树

2026-11-14，基线 `21c6bd1`。Codex 实现和自审。两个实验使用公开、匿名、免费只读数据。
Base LP 保持暂停，多链研究继续保留。所有费用为零。并未使用钱包权限。

## Aave 五笔残差

先查询块 `26301494` 的交易 `0x8e47af73769d93575dd82fd549d516bb5147a7ddf471a038792a0e9e352a7ca5`。
Blockscout 返回 213 帧。两笔分别为 306.304114217826086956 ETH 和 15.330087416375547247 ETH
的内部 CALL 都在两个分支出现。四个叶调用本身均返回成功，其中一组的祖先
`[0,2,4,0,5]` 回滚，另一组所在分支通过。Routescan 地址索引和按哈希索引都将被撤销金额计作成功。
对剩余四笔重复执行同一核验，共取得 1,732 帧。根发送方、接收方、value、完整 calldata 与
另一个来源的固定交易及成功收据一致。验证每帧路径、父子位置、子帧数和 delegatecall 执行地址；
只有 CALL 才移动原生 value，任何回滚祖先都会撤销子调用的资金移动。
[原收据和整块约束](AAVE_FIVE_PRIORITY_TRACES_20261114.md)
[Blockscout raw trace 接口](https://github.com/blockscout/blockscout-api-v2-swagger/blob/main/swagger.yaml)

| 块 | 调用帧 | 重复组 | 索引多计 ETH | 去除回滚金额后的块残差 |
| --- | ---: | ---: | ---: | ---: |
| 26301494 | 213 | 2 | 321.634201634201634203 | 0 |
| 26301276 | 233 | 1 | 19.003208300000000000 | 0 |
| 26301278 | 163 | 1 | 34.000000000000000000 | 0 |
| 26301283 | 327 | 1 | 16.412283587716412283 | 0 |
| 26301492 | 796 | 1 | 30.954124128000000000 | 0 |

把全窗原始 `-509203424865618933788` wei 与 trace 证实的
`+422003817649918046486` wei 校正分别留存，校正后余额减索引为
**`-87199607215700887302` wei**。原索引和上一版证据保持原状。
另有 126 笔收据待补，仍需核对剩余差额和完整净收益。Blockscout 执行树是独立于 Routescan
索引的来源，校正同时受到已留存的五个区块余额差约束，未对原生转账重新执行全区块验证。

## Metric × Uniswap V3

通过 dRPC 取得原四种路径各有无 Titan 两槽的八棵调用树。保持原 Ethereum 父块
`26410516` 哈希、目标块 `26410517` 的六项 block override、探针 code、原 caller、限额和 calldata。
八个根回包与原 `eth_call` 逐字节一致。实际 swap 路径中的 `inSwap()` 检查成功；
Metric-first 的外层 swap 立即失败，V3-first 在 V3 回调内部到达 Metric 后失败。
[原同父块对照](METRIC_TITAN_PAIRED_20261114.md)
[dRPC Ethereum debug 接口](https://drpc.org/docs/ethereum-api/debugandtrace)

- 父块原样中，第一个 Pyth ETH/USD 读数已经 42 秒旧，超过该层 15 秒上限，
  provider 随即 `FeedStalled()`，未读取第二条 Pyth feed。
- 两槽覆盖后，Pyth ETH/USD、USDC/USD 均为 4 秒旧，且各自的 guard 查询成功。
  AnchoredPriceProvider 的 `source()` 返回
  `0xad75dfdc62d65ad8416e510b6764bd165903bbad`。该合约调用
  `0xe844701934d9c6085b292e428f41b911cc0b3872` 的 `getOracleData(bytes32)`，
  得到 mid `190469189632` E8、spread `76/74`（0.01 bps）、时间 `1794626687000` 毫秒。
  压缩 oracle 的读调用成功，其上层自定义报价合约在价格 guard 前回滚 `FeedStalled()`。
  历史合约账户与相关压缩槽通过父块 `stateRoot` 的 MPT 核验，两合约代码匹配相应证明。
- 在原两槽覆盖基础上，仅修改已证明的压缩槽中 56 位时间字段，保持报价数值不变。
  对目标块整秒，年龄 0 秒通过，1、2、4、5、8、10、15、16 秒均失败。源字节码也显示
  毫秒转秒后与 `TIMESTAMP` 比较的分支（`0x5268`–`0x527d`、
  `0x4fb4`–`0x4fe8`）。本场景的报价源要求当前块同秒的更新。原外层 Pyth 15 秒限制
  属于另一层。自定义报价合约未找到已验证源码，归因依据历史字节码及固定输入变更后的调用树。
  [Metric oracle 实现](https://github.com/Metricxyz/oracle-contracts)
  [Titan 状态格式](https://docs.titanbuilder.xyz/propamms/takers)

同一份 Titan 快照恰好包含该压缩槽，时间为 `1794626711000` 毫秒。只加入这一个槽，四种路径
都完成第二次 swap，随后第一腿回调因本金不足触发探针 `Rejected(11)`。下表为回滚前第二
腿实际返回，未扣 gas、builder 费用和运维成本。四条路线均无可结算本金。

| 起始资产及本金 | 腿序 | 第二腿返回 | 本金差额 |
| --- | --- | ---: | ---: |
| 2 USDC | Metric → V3 | 1.993028 USDC | 0.006972 USDC |
| 2 USDC | V3 → Metric | 1.993903 USDC | 0.006097 USDC |
| 0.001 WETH | Metric → V3 | 0.000996951288133921 WETH | 0.000003048711866079 WETH |
| 0.001 WETH | V3 → Metric | 0.000996513691541090 WETH | 0.000003486308458910 WETH |

只使用三个明确的存储槽，未应用其余 Titan 声明或目标块交易前缀。时间字段实验属于单变量
诊断，报价行为和三槽结果均为同一 dRPC 端点的模拟。样本外套利机会、单独提供者模拟、
完整构块顺序与成交结算仍待验证。
[始终回滚的探针](METRIC_ATOMIC_PROBE_20261114.md)

## 留存和回放

42 次匿名免费读取，包括 Aave raw trace 5 次、dRPC 成功 trace 21 次、固定历史
代码与证明 13 次、Blockscout 合约详情 3 次。另计 3 次失败，合计 **45 次**。
失败分别是 Tenderly 429、Alchemy 公共入口的 trace 401、dRPC opcode tracer 缺少
命名 tracer 后返回 400。三个原范围均在首笔失败停止。成功的 dRPC 调用树范围各自独立留存。
原预算、请求/响应原文、检索时刻、失败和工程日志存于
`reports/research/ethereum-traces-20261114/`。此前已停止的范围未重启。

```sh
.venv/bin/python -m scripts.research_ethereum_traces \
  --research reports/research \
  --output reports/research/ethereum-traces-20261114/replay-review.json
```

该命令只读原始采集，重用上轮收据、余额、Titan 和父块验证，校验原预算和请求响应摘要，
对账现有索引、完整调用树和各块余额。原始回放结果和第二次离线回放的 SHA-256 一致。

| 留存文件 | SHA-256 |
| --- | --- |
| 最大残差原始 raw trace | `4bc926c9e8bffca198cd969c89f26dc0d73e20d8318b4a8847eaadc8cba12b9e` |
| 五笔与 Metric 全部结果 | `f46cfa0aceec3461ae4b100985cae160bceaaf94f336559503f960221ca97826` |

下一阶段仍需完成 Aave 全窗收据和余量对账。Metric 的同秒压缩 feed 可作为未来只读实验的
过滤条件，选定窗口后比较独立报价、路由深度、净经济成本与成交条件。

## 工程核验

| 检查 | 结果 |
| --- | --- |
| 相关回归 | 103 项通过 |
| `make check` | 6,211 项 Python 测试、138 项子测试、9 项前端模块、82 项 Foundry 测试通过 |
| `make security-check` | 退出 0，依赖漏洞、静态分析与密钥扫描通过 |
| `make contract-security-check` | 退出 0，Slither 分析 85 个合约，0 条发现 |
| 文档与回放 | 55 个本地链接、文档命令及两次可重复离线回放通过 |
