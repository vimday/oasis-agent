// Offline benchmark for bounded, synthetic CPMM route/amount screening.
// CPU: c++ -std=c++17 -O3 -pthread -x c++ scripts/benchmark_multihop_cuda.cu -o /tmp/multihop-screen
// CUDA: nvcc -std=c++17 -O3 -arch=sm_89 -Xcompiler=-pthread scripts/benchmark_multihop_cuda.cu -o /tmp/multihop-screen
// Run: /tmp/multihop-screen [routes=10000] [amounts_per_route=16] [samples=30] [cpu_threads=1]

#include <algorithm>
#include <chrono>
#include <cstdint>
#include <cstdlib>
#include <iomanip>
#include <iostream>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

#ifdef __CUDACC__
#include <cuda_runtime.h>
#define HOST_DEVICE __host__ __device__
#else
#define HOST_DEVICE
#endif

namespace {

constexpr std::uint64_t fee_numerator = 997;
constexpr std::uint64_t fee_denominator = 1'000;
constexpr std::uint64_t max_reserve = 32'768;
constexpr std::uint64_t amount_step = 64;
constexpr std::uint64_t synthetic_cost_per_hop = 10;
constexpr unsigned max_amounts = 64;
constexpr unsigned max_hops = 8;
static_assert(max_reserve >= amount_step * max_amounts);
static_assert(max_reserve * max_reserve <= UINT64_MAX / fee_numerator);

struct Pool {
    std::uint64_t reserve_in;
    std::uint64_t reserve_out;
};

struct Route {
    unsigned hops;
    Pool pools[max_hops];
};

HOST_DEVICE std::uint64_t input_amount(std::size_t index, unsigned amounts) {
    return (index % amounts + 1) * amount_step;
}

HOST_DEVICE std::uint64_t minimum_profitable_output(const Route& route, std::uint64_t amount) {
    return amount + synthetic_cost_per_hop * route.hops + 1;
}

// A no-price-impact quote rounded up is an upper bound on the floored swap
// output. Each bound is monotone, so composing them preserves the inequality.
HOST_DEVICE bool retain(const Route& route, std::uint64_t amount) {
    auto upper = amount;
    for (unsigned hop = 0; hop < route.hops; ++hop) {
        const auto pool = route.pools[hop];
        const auto numerator = upper * fee_numerator * pool.reserve_out;
        const auto denominator = fee_denominator * pool.reserve_in;
        upper = (numerator + denominator - 1) / denominator;
        if (upper > pool.reserve_out) {
            upper = pool.reserve_out;
        }
    }
    return upper >= minimum_profitable_output(route, amount);
}

std::uint64_t exact_output(const Route& route, std::uint64_t amount) {
    for (unsigned hop = 0; hop < route.hops; ++hop) {
        const auto pool = route.pools[hop];
        const auto input_after_fee = amount * fee_numerator;
        amount = input_after_fee * pool.reserve_out /
                 (pool.reserve_in * fee_denominator + input_after_fee);
    }
    return amount;
}

template <typename Visit>
void parallel_spans(std::size_t count, unsigned threads, Visit visit) {
    const auto stride = (count + threads - 1) / threads;
    std::vector<std::thread> workers;
    workers.reserve(threads - 1);
    for (unsigned worker = 1; worker < threads; ++worker) {
        const auto begin = worker * stride;
        if (begin < count) {
            workers.emplace_back(visit, worker, begin, std::min(begin + stride, count));
        }
    }
    visit(0, 0, std::min(stride, count));
    for (auto& worker : workers) {
        worker.join();
    }
}

void screen_cpu(const std::vector<Route>& routes, unsigned amounts, unsigned threads,
                std::vector<std::uint8_t>& mask) {
    parallel_spans(mask.size(), threads, [&](unsigned, std::size_t begin, std::size_t end) {
        for (auto i = begin; i < end; ++i) {
            mask[i] = retain(routes[i / amounts], input_amount(i, amounts));
        }
    });
}

struct Result {
    std::uint64_t retained = 0;
    std::uint64_t profitable = 0;
    std::uint64_t best_profit = 0;

    bool operator==(const Result& other) const {
        return retained == other.retained && profitable == other.profitable &&
               best_profit == other.best_profit;
    }

    void add(const Result& other) {
        retained += other.retained;
        profitable += other.profitable;
        best_profit = std::max(best_profit, other.best_profit);
    }
};

Result evaluate_cpu(const std::vector<Route>& routes, unsigned amounts, unsigned threads,
                    const std::uint8_t* mask = nullptr) {
    std::vector<Result> partials(threads);
    parallel_spans(routes.size() * amounts, threads,
                   [&](unsigned worker, std::size_t begin, std::size_t end) {
        Result result;
        for (auto i = begin; i < end; ++i) {
            if (mask && !mask[i]) {
                continue;
            }
            ++result.retained;
            const auto& route = routes[i / amounts];
            const auto input = input_amount(i, amounts);
            const auto output = exact_output(route, input);
            const auto minimum_output = minimum_profitable_output(route, input);
            if (output >= minimum_output) {
                ++result.profitable;
                result.best_profit = std::max(result.best_profit, output - minimum_output + 1);
            }
        }
        partials[worker] = result;
    });
    Result result;
    for (const auto& partial : partials) {
        result.add(partial);
    }
    return result;
}

#ifdef __CUDACC__

void check_cuda(cudaError_t status) {
    if (status != cudaSuccess) {
        throw std::runtime_error(cudaGetErrorString(status));
    }
}

template <typename T>
class DeviceBuffer {
public:
    explicit DeviceBuffer(std::size_t count) {
        check_cuda(cudaMalloc(reinterpret_cast<void**>(&data), count * sizeof(T)));
    }
    ~DeviceBuffer() { cudaFree(data); }
    DeviceBuffer(const DeviceBuffer&) = delete;
    DeviceBuffer& operator=(const DeviceBuffer&) = delete;
    T* data = nullptr;
};

__global__ void screen_kernel(const Route* routes, unsigned amounts, std::size_t count,
                              std::uint8_t* mask) {
    const auto i = static_cast<std::size_t>(blockIdx.x) * blockDim.x + threadIdx.x;
    if (i < count) {
        mask[i] = retain(routes[i / amounts], input_amount(i, amounts));
    }
}

void screen_gpu(const std::vector<Route>& routes, unsigned amounts,
                const DeviceBuffer<Route>& device_routes,
                const DeviceBuffer<std::uint8_t>& device_mask, std::vector<std::uint8_t>& mask) {
    check_cuda(cudaMemcpy(device_routes.data, routes.data(), routes.size() * sizeof(Route),
                          cudaMemcpyHostToDevice));
    constexpr unsigned block_size = 256;
    const auto blocks = (mask.size() + block_size - 1) / block_size;
    screen_kernel<<<static_cast<unsigned>(blocks), block_size>>>(
        device_routes.data, amounts, mask.size(), device_mask.data);
    check_cuda(cudaGetLastError());
    check_cuda(cudaMemcpy(mask.data(), device_mask.data, mask.size(), cudaMemcpyDeviceToHost));
}

#endif

using Clock = std::chrono::steady_clock;

double milliseconds(Clock::time_point start, Clock::time_point end) {
    return std::chrono::duration<double, std::milli>(end - start).count();
}

struct Timings {
    std::vector<double> screen;
    std::vector<double> recheck;
    std::vector<double> total;

    void add(Clock::time_point start, Clock::time_point screened, Clock::time_point finished) {
        screen.push_back(milliseconds(start, screened));
        recheck.push_back(milliseconds(screened, finished));
        total.push_back(milliseconds(start, finished));
    }
};

void print_percentiles(std::vector<double> values) {
    std::sort(values.begin(), values.end());
    const auto at = [&](std::size_t percentile) {
        return values[(percentile * values.size() + 99) / 100 - 1];
    };
    std::cout << "{\"p50\":" << at(50) << ",\"p95\":" << at(95) << ",\"p99\":" << at(99) << '}';
}

void print_timings(const Timings& times) {
    std::cout << "{\"screen_ms\":";
    print_percentiles(times.screen);
    std::cout << ",\"recheck_ms\":";
    print_percentiles(times.recheck);
    std::cout << ",\"total_ms\":";
    print_percentiles(times.total);
    std::cout << '}';
}

// The generator uses a specified integer recurrence for the same fixture on every host.
std::uint64_t next_random(std::uint64_t& state) {
    state = state * 6'364'136'223'846'793'005ULL + 1'442'695'040'888'963'407ULL;
    return state >> 32;
}

unsigned argument(int argc, char** argv, int index, unsigned fallback, unsigned maximum) {
    if (argc <= index) {
        return fallback;
    }
    const std::string value(argv[index]);
    std::size_t parsed = 0;
    const auto number = std::stoul(value, &parsed);
    if (parsed != value.size() || number == 0 || number > maximum) {
        throw std::runtime_error("argument outside workload limits");
    }
    return static_cast<unsigned>(number);
}

}  // namespace

int main(int argc, char** argv) try {
    if (argc > 5) {
        throw std::runtime_error("expected arguments: routes amounts_per_route samples cpu_threads");
    }
    const auto route_count = argument(argc, argv, 1, 10'000, 1'000'000);
    const auto amounts = argument(argc, argv, 2, 16, max_amounts);
    const auto samples = argument(argc, argv, 3, 30, 1'000);
    const auto threads = argument(argc, argv, 4, 1, 64);
    constexpr unsigned seed = 20261122;
    std::uint64_t random_state = seed;
    std::vector<Route> routes(route_count);
    for (auto& route : routes) {
        route.hops = 2 + static_cast<unsigned>(next_random(random_state) % (max_hops - 1));
        for (unsigned hop = 0; hop < route.hops; ++hop) {
            auto& pool = route.pools[hop];
            pool.reserve_in = 4'096 + next_random(random_state) % 12'288;
            pool.reserve_out = 4'096 + next_random(random_state) % 12'288;
        }
    }
    const auto batch_size = static_cast<std::size_t>(route_count) * amounts;
    std::vector<std::uint8_t> cpu_mask(batch_size);
    Timings cpu_times;
    std::vector<double> cpu_exact_times;
    Result totals;

#ifdef __CUDACC__
    const auto cold_start = Clock::now();
    const DeviceBuffer<Route> device_routes(routes.size());
    const DeviceBuffer<std::uint8_t> device_mask(batch_size);
    std::vector<std::uint8_t> gpu_mask(batch_size);
    screen_gpu(routes, amounts, device_routes, device_mask, gpu_mask);
    const auto cold_start_ms = milliseconds(cold_start, Clock::now());
    Timings gpu_times;
#endif

    screen_cpu(routes, amounts, threads, cpu_mask);
    for (unsigned sample = 0; sample < samples; ++sample) {
        auto& changed_pool = routes[next_random(random_state) % routes.size()].pools[0];
        changed_pool.reserve_out = 4'096 + next_random(random_state) % 12'288;

        const auto run_cpu = [&] {
            const auto start = Clock::now();
            screen_cpu(routes, amounts, threads, cpu_mask);
            const auto screened = Clock::now();
            const auto report = evaluate_cpu(routes, amounts, threads, cpu_mask.data());
            cpu_times.add(start, screened, Clock::now());
            return report;
        };
        const auto run_cpu_exact = [&] {
            const auto start = Clock::now();
            const auto report = evaluate_cpu(routes, amounts, threads);
            cpu_exact_times.push_back(milliseconds(start, Clock::now()));
            return report;
        };
#ifdef __CUDACC__
        const auto run_gpu = [&] {
            const auto start = Clock::now();
            screen_gpu(routes, amounts, device_routes, device_mask, gpu_mask);
            const auto screened = Clock::now();
            const auto report = evaluate_cpu(routes, amounts, threads, gpu_mask.data());
            gpu_times.add(start, screened, Clock::now());
            return report;
        };
        Result cpu;
        Result gpu;
        Result oracle;
        for (unsigned arm = 0; arm < 3; ++arm) {
            switch ((sample + arm) % 3) {
                case 0: cpu = run_cpu(); break;
                case 1: gpu = run_gpu(); break;
                case 2: oracle = run_cpu_exact(); break;
            }
        }
        if (!(cpu == gpu) || cpu_mask != gpu_mask) {
            throw std::runtime_error("CPU and GPU results disagree");
        }
#else
        Result cpu;
        Result oracle;
        if (sample % 2 == 0) {
            cpu = run_cpu();
            oracle = run_cpu_exact();
        } else {
            oracle = run_cpu_exact();
            cpu = run_cpu();
        }
#endif
        if (cpu.profitable != oracle.profitable || cpu.best_profit != oracle.best_profit) {
            throw std::runtime_error("screening discarded a positive candidate");
        }
        totals.add(cpu);
    }

    std::cout << std::fixed << std::setprecision(6)
              << "{\"evidence_kind\":\"synthetic_offline\",\"execution_eligible\":false,"
                 "\"release_permitted\":false,\"seed\":" << seed << ",\"routes\":" << route_count
              << ",\"amounts_per_route\":" << amounts << ",\"samples\":" << samples
              << ",\"max_hops\":" << max_hops << ",\"synthetic_cost_per_hop_base_units\":" << synthetic_cost_per_hop
              << ",\"cpu_threads\":" << threads << ",\"route_amount_evaluations\":" << batch_size * samples
              << ",\"retained\":" << totals.retained << ",\"profitable\":" << totals.profitable
              << ",\"missed_profitable\":0,\"best_profit_synthetic_base_units\":" << totals.best_profit
              << ",\"cpu_exact_total_ms\":";
    print_percentiles(cpu_exact_times);
    std::cout << ",\"cpu_screen_then_recheck\":";
    print_timings(cpu_times);
#ifdef __CUDACC__
    std::cout << ",\"cuda\":";
    print_timings(gpu_times);
    std::cout << ",\"cuda_matches_cpu\":true,\"cuda_initialization_and_first_screen_ms\":" << cold_start_ms
              << ",\"cuda_host_to_device_bytes_per_sample\":" << routes.size() * sizeof(Route)
              << ",\"cuda_device_to_host_bytes_per_sample\":" << batch_size;
#else
    std::cout << ",\"cuda\":null,\"cuda_matches_cpu\":null";
#endif
    std::cout << "}\n";
    return EXIT_SUCCESS;
} catch (const std::exception& error) {
    std::cerr << "multihop benchmark: " << error.what() << '\n';
    return EXIT_FAILURE;
}
