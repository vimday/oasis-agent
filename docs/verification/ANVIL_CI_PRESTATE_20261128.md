# Linux Anvil 的父块与交易位置

GitHub Actions 在提交 `81b41a5` 上的 Python 与 Solidity 作业均止于同一项真实 Anvil 回归。父块与交易索引合并断言的错误为 `preflight state is not the settled transaction prestate`；两个作业的进程退出码分别为 2 和 1。Rust 与部署隔离通过。[失败运行及原始日志](https://github.com/vimday/oasis-agent/actions/runs/46031482401)

在 Aleph WSL 上取与 runner 相同的官方 Foundry 1.10.0 Linux amd64 包，发布包 SHA-256 为 `90729233bd9a683b872783ae2e3217fb56876fa2a3c37185f13e39dca8525041`，构建提交为 `083e22994195d6a7e3ad3104d0b57f478c9b9062`。单机匿名临时账户的 32 次独立交易全部落在父块的直接子块中，但交易索引都是 2。索引 0 和 1 是类型 0x7e 的两笔系统存入交易，分别调用 L1Block 和 BaseTime。关闭自动挖矿再按需出块，32 次仍是索引 2。本机同提交 Homebrew 构建的默认模式中，32 次都是索引 0。[Foundry 发布记录](https://github.com/foundry-rs/foundry/releases/tag/v1.10.0)

该演练要求从父块状态做正、负整笔预检，再用完全相同的路径核对第一笔用户交易的结算。正常测试入口现显式使用 Ethereum EVM 与合约编译目标 Cancun，保留签名域 chain ID 8453。两种构建各 32 次都得到索引 0。回放还验证子块高度、哈希、首笔交易哈希及收据位置，报告记录网络模式、硬分叉和父子块关系。在该无 L1 费用、发送人无额外价值转移的本地演练中，ETH 实际支出与收据 gas 乘单价必须精确相等到 1 wei。本地 EVM 记录不提供 Base 的系统交易排序或跨层费用证据。真实 Base 的交易位置、ETH 收支和 L1 收费另由区块与收据样本核对。[测试入口](../../scripts/verify_atomic_vault_anvil.py) · [Base 收据与失败成本](BASE_EXECUTOR_ATTEMPTS_20261128.md)

修复后的本地回归覆盖 CI 的 37 项 ABI 与 Anvil 用例。完整 `make check` 退出 0，Python 6,842 项及 138 个子测试、前端 9 个模块、Foundry 132 项通过。`make security-check` 与 `make contract-security-check` 均退出 0。原始 GitHub 失败链接保留在上文，本地最终日志位于采集机 `reports/research/ci-anvil-margin-20261128/`。
