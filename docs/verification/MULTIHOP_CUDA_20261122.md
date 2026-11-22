# 多跳 CPMM 批量筛选 · RTX 4070 Ti 离线基准

2026-11-22。Aleph WSL，Linux x86_64，Intel Core i7-13700，NVIDIA GeForce RTX 4070 Ti，驱动
621.14，CUDA `nvcc` 12.6.68，目标 `sm_89`。基准源文件基于工作树 `14260a4` 新增。合成夹具
没有链 ID、账户、真实市价或交易；本轮无链上请求、签名或广播。[基准源码](../../scripts/benchmark_multihop_cuda.cu)

## 工作量和计时

- 固定种子 `20261122`。每个配置 120 个样本，512、5,000 或 50,000 条已给定的路，每条 2–8 跳，
  16 个离散投入档位。逐样本更新一个合成储备。固定 30 bps 逐腿池费，整数逐腿向下舍入，固定开销为
  每跳 10 个合成起始资产最小单位。量级用于计算负载比较。
- 三臂轮换执行顺序。CPU 直接逐金额精算作为基线；CPU 筛选和 CUDA 筛选共用同一整数上界，均在
  CPU 精算所有入围点。GPU 每次上传完整路线/储备缓冲区并回传标志，均纳入计时。CPU 的工作线程
  创建与回收计入相应阶段。夹具生成、分配与预热不计入热态，CUDA 初始化和首轮筛选另报。
- 每样本比对 CPU/GPU 筛选标志及精算汇总，并与 CPU 完整金额网格核对盈利点数与最大净额。六轮均
  逐位一致，金额网格内零漏报。端到端指内存夹具就绪到 CPU 精算结束，范围不含路径发现、RPC、真实
  池状态、完整交易模拟、CL tick 或 V4 hook。

## 结果

下表为每样本耗时，单位 ms。各单元格为 p50 / p99，百分位使用 nearest-rank。每个配置只运行一轮，
未估计 p99 置信区间，表中差异仅描述本次样本。

| 路线 | CPU 线程 | CPU 直接精算 | CPU 筛选＋精算 | CUDA 筛选＋CPU 精算 |
| ---: | ---: | ---: | ---: | ---: |
| 512 | 1 | 0.101 / 0.146 | 0.153 / 0.200 | 0.108 / 0.207 |
| 512 | 8 | 0.167 / 0.295 | 0.334 / 0.477 | 0.234 / 0.378 |
| 5,000 | 1 | 1.026 / 1.691 | 1.559 / 1.874 | 0.665 / 1.081 |
| 5,000 | 8 | 0.351 / 0.527 | 0.583 / 0.797 | 0.383 / 0.560 |
| 50,000 | 1 | 10.279 / 10.822 | 15.666 / 17.272 | 6.110 / 7.064 |
| 50,000 | 8 | 1.474 / 2.680 | 2.367 / 4.115 | 1.701 / 2.427 |

50,000 路线、8 线程时，CUDA 相对 CPU 直接精算的 p99 下降 9.43%，p50 增加 15.46%；512 和 5,000
路线时，最快 CPU 直接精算的 p99 更短。CUDA 初始化及首轮筛选为 139.83–188.62 ms。本轮未达到
研究 TODO 的 30% p99 改善或 10% 召回提升门槛。计算费用比与真实窗口的召回仍未测量。
[实验门槛](../strategies/MULTIHOP_ARBITRAGE_TODO.md) · [六行原始 JSON](MULTIHOP_CUDA_20261122.jsonl)

## 复现

在仓库根目录运行。`aleph` 是已有的 SSH 别名。远端只用临时目录，编译与六次运行退出 0。

```sh
ssh -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=yes -o UpdateHostKeys=no aleph '
set -eu
work=$(mktemp -d)
cleanup() { rm -f -- "$work/bench.cu" "$work/multihop-screen"; rmdir -- "$work"; }
trap cleanup EXIT
cat > "$work/bench.cu"
cuda_compiler=$(command -v nvcc || printf "%s" /usr/local/cuda/bin/nvcc)
"$cuda_compiler" -std=c++17 -O3 -arch=sm_89 -Xcompiler=-pthread "$work/bench.cu" -o "$work/multihop-screen"
for routes in 512 5000 50000; do
    for threads in 1 8; do
        "$work/multihop-screen" "$routes" 16 120 "$threads"
    done
done
' < scripts/benchmark_multihop_cuda.cu
```

本地 macOS arm64 的 `c++ -std=c++17 -O3 -pthread -Wall -Wextra -Werror -x c++` CPU 构建通过。
`clang++` 的 AddressSanitizer 和 UndefinedBehaviorSanitizer 在 512 路线、64 档、5 样本、8 线程
的配置上通过。零路线参数按预期拒绝。

| 文件 | SHA-256 |
| --- | --- |
| `scripts/benchmark_multihop_cuda.cu` | `f94933a3ec5e505d7350ebc8260a7d0a3180119f5c387e5012c09ea20b605e38` |
| `docs/verification/MULTIHOP_CUDA_20261122.jsonl` | `731d3b129b5f346509e7b1ccc6194eba7520b9661f6651248b87210892544adc` |
