# LP 收尾复核：续接会话门禁重确认

日期：2026-09-07（Asia/Shanghai）。本记录续接前一 AI 会话（在 `make security-check` 处因会话
额度中断）的未竟之业；基线为 `90e19d2` 提交加工作区修改。本轮没有提交、部署、签名或广播。

## 续接前已完成（上会话确认，本轮复核未再改动）

- `shadow/features.py`：波动率相邻价格配对改用 pairwise 迭代，任意窗口长度 ≥2 均正确；
  时间戳与区块身份类型边界补强；双提供者一致、无未来数据、同链、Decimal 计算的整数输入约束。
- `farming/backtest.py`：预注册 `dataset_hash` 与实际观察序列重算比对，不匹配即硬失败
  （`backtest.py:276`）。
- `evidence/pool_access.py`：`accessible` 需两个不同提供者确认（`pool_access.py:48`）。
- `execution/lp_signer_policy.py`：`account_nonce`（EOA 交易 nonce）与 `executor_last_nonce`
  （执行器内部单调 nonce）分离；执行前状态与模拟结果内容哈希均由策略重算，双提供者一致。
- HALT 恢复：释放执行锁需两家独立提供者对同一恢复载荷一致（`lp_journal.py:106`），
  并绑定最终区块、零流动性、余额与授权撤销证据。

## 本轮重新执行的门禁（全部实测通过）

| 门禁 | 命令 | 结果 |
|---|---|---|
| 依赖漏洞 | pip-audit（hash-locked） | 无已知漏洞 |
| 静态安全 | bandit -ll | 0 中危/高危 |
| 密钥门禁 | check_secrets | 通过，无未复核候选 |
| Python 全仓 | `make python-check` | ruff/format/mypy 通过（86 源文件）；**603 passed + 96 subtests** |
| 合约全仓 | `make contract-check` | exit 0；**40 passed / 0 failed / 0 skipped**，5 套件 |
| 链上回执复放 | `verify_lp_e2e.py`（本地） | exit 0；35 笔 / 4 swap / 8 守恒；digest `cba46b49…` |
| 链上回执复放 | `verify_lp_e2e.py`（测试网 46630） | exit 0；35 笔 / 4 swap / 8 守恒；gas 241,732,800,000,000 wei；digest `38adb8e3…` |
| 证据清单 | 全部 8 个 `MANIFEST.sha256` 包 | 覆盖完整、哈希一致、无多余条目 |
| diff 卫生 | `git diff --check` | exit 0 |

安全门禁首次运行因外部沙箱注入 `PYTHONHOME/PYTHONPATH` 导致 venv Python 无法启动
（`No module named 'encodings'`），与代码和依赖无关；`env -u PYTHONHOME -u PYTHONPATH`
清理后按仓库原命令全部通过，未改动任何门禁脚本或依赖锁定。

## 证据归档

- 本轮门禁日志与 MANIFEST：`reports/evidence/lp-closure-recheck-2026-09-07/`。
- 上会话既有证据包未改写：`lp-continuation-2026-09-07/`、`lp-e2e-*`、`lp-fork-*` 等保持原状。

## 交付结论（confirmed）

代码可独立完成的 LP 功能（特征生产、池准入、回放记账、四格回测、持久决策日志、签名前策略、
HALT 恢复、测试网/本地 35 笔生命周期）已全部通过门禁。仍依赖外部市场观察、独立审计与生产
托管的门槛继续由机器明确阻断：`execution_eligible` 恒为 False，`lp_readiness` WP0–WP8 顺序
门禁未获授权通过；真实样本外经济回放、长期 shadow/canary 观察、独立 signer 接线与发布审计
不在本轮交付范围。工作区改动保持未提交，待用户决定提交与独立复核安排。
