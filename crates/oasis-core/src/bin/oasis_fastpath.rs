use std::path::PathBuf;
use std::process::ExitCode;
use std::time::Instant;

use oasis_core::ffi::{ScanCyclesInput, execute_scan_cycles};
use oasis_core::graph_arb::{PoolGraph, PoolKind, PoolState};
use oasis_core::pool_math::{TickLiquidity, sqrt_price_at_tick};
use oasis_core::u256_math::U256;

fn print_usage() {
    eprintln!(
        "Usage:\n  oasis-fastpath benchmark [--iterations N] [--hops 2|3]\n  oasis-fastpath scan --input <file.json>"
    );
}

fn synthetic_addr(byte: &str) -> String {
    format!("0x{}", byte.repeat(20))
}

fn build_benchmark_graph() -> anyhow::Result<(PoolGraph, String)> {
    let mut graph = PoolGraph::new();
    let weth = synthetic_addr("11");
    let usdc = synthetic_addr("22");
    let usdt = synthetic_addr("33");

    // Pool 1: V3 WETH/USDC (~3000 USDC/WETH: 1e18 -> 3e9, tick ~ -196258)
    let tick_v3 = -196_258;
    graph.upsert_pool(PoolState {
        chain_id: 8453,
        pool_id: synthetic_addr("a1"),
        kind: PoolKind::V3,
        token0: weth.clone(),
        token1: usdc.clone(),
        decimals0: 18,
        decimals1: 6,
        fee_pips: 500,
        reserve0: None,
        reserve1: None,
        sqrt_price_x96: Some(sqrt_price_at_tick(tick_v3)?),
        tick: Some(tick_v3),
        liquidity: Some(20_000_000_000_000_000),
        ticks: vec![
            TickLiquidity {
                tick: -196_800,
                liquidity_net: 10_000_000_000_000_000,
            },
            TickLiquidity {
                tick: -195_800,
                liquidity_net: -10_000_000_000_000_000,
            },
        ],
        block_number: 20_000_000,
        v4_tick_spacing: None,
        timestamp_ms: 1_760_000_000_000,
    })?;

    // Pool 2: AerodromeStable USDC/USDT (6 dec / 6 dec, slightly imbalanced)
    graph.upsert_pool(PoolState {
        chain_id: 8453,
        pool_id: synthetic_addr("a2"),
        kind: PoolKind::AerodromeStable,
        token0: usdc.clone(),
        token1: usdt.clone(),
        decimals0: 6,
        decimals1: 6,
        fee_pips: 100,
        reserve0: Some(U256::from_u64(5_000_000_000_000)),
        reserve1: Some(U256::from_u64(5_050_000_000_000)),
        sqrt_price_x96: None,
        tick: None,
        liquidity: None,
        ticks: vec![],
        block_number: 20_000_000,
        v4_tick_spacing: None,
        timestamp_ms: 1_760_000_000_000,
    })?;

    // Pool 3: Slipstream WETH/USDC (higher USDC price ~3150 USDC/WETH: tick ~ -195770)
    let tick_ss = -195_770;
    graph.upsert_pool(PoolState {
        chain_id: 8453,
        pool_id: synthetic_addr("a3"),
        kind: PoolKind::Slipstream,
        token0: weth.clone(),
        token1: usdc,
        decimals0: 18,
        decimals1: 6,
        fee_pips: 500,
        reserve0: None,
        reserve1: None,
        sqrt_price_x96: Some(sqrt_price_at_tick(tick_ss)?),
        tick: Some(tick_ss),
        liquidity: Some(20_000_000_000_000_000),
        ticks: vec![],
        block_number: 20_000_000,
        v4_tick_spacing: None,
        timestamp_ms: 1_760_000_000_000,
    })?;

    // Pool 4: V2 WETH/USDT (3250 USDT/WETH)
    graph.upsert_pool(PoolState {
        chain_id: 8453,
        pool_id: synthetic_addr("a4"),
        kind: PoolKind::V2,
        token0: weth.clone(),
        token1: usdt,
        decimals0: 18,
        decimals1: 6,
        fee_pips: 3000,
        reserve0: Some(U256::from_u128(100_000_000_000_000_000_000)),
        reserve1: Some(U256::from_u128(325_000_000_000)),
        sqrt_price_x96: None,
        tick: None,
        liquidity: None,
        ticks: vec![],
        block_number: 20_000_000,
        v4_tick_spacing: None,
        timestamp_ms: 1_760_000_000_000,
    })?;

    Ok((graph, weth))
}

fn run_benchmark(args: &[String]) -> anyhow::Result<()> {
    let mut iterations: usize = 200;
    let mut max_hops: usize = 3;
    let mut i = 0;
    while i < args.len() {
        match args[i].as_str() {
            "--iterations" => {
                i += 1;
                let raw = args
                    .get(i)
                    .ok_or_else(|| anyhow::anyhow!("missing value for --iterations"))?;
                iterations = raw.parse()?;
                anyhow::ensure!(iterations > 0, "--iterations must be > 0");
            }
            "--hops" => {
                i += 1;
                let raw = args
                    .get(i)
                    .ok_or_else(|| anyhow::anyhow!("missing value for --hops"))?;
                max_hops = raw.parse()?;
            }
            other => anyhow::bail!("unknown benchmark flag: {other}"),
        }
        i += 1;
    }

    let (graph, weth) = build_benchmark_graph()?;
    let min_in = U256::from_u128(10_000_000_000_000_000);
    let max_in = U256::from_u128(5_000_000_000_000_000_000);
    let gas_l2 = U256::from_u128(200_000_000_000_000);
    let gas_l1 = U256::from_u128(50_000_000_000_000);
    let min_profit = U256::from_u128(100_000_000_000_000);

    let start = Instant::now();
    let mut last_candidates = 0usize;
    let mut last_cycles = 0usize;
    let mut top_profit = String::from("0");
    for _ in 0..iterations {
        let report = graph.scan_cycles(
            std::slice::from_ref(&weth),
            max_hops,
            min_in,
            max_in,
            gas_l2,
            gas_l1,
            min_profit,
        )?;
        last_candidates = report.candidates.len();
        last_cycles = report.cycles_evaluated;
        if let Some(first) = report.candidates.first() {
            top_profit = first.net_profit.to_dec_string();
        }
    }
    let total_ns = u64::try_from(start.elapsed().as_nanos()).unwrap_or(u64::MAX);
    let ns_per_scan = total_ns / (iterations as u64);

    let summary = serde_json::json!({
        "iterations": iterations,
        "max_hops": max_hops,
        "cycles_evaluated_per_scan": last_cycles,
        "candidates_found": last_candidates,
        "top_net_profit": top_profit,
        "total_ns": total_ns,
        "ns_per_scan": ns_per_scan,
    });
    println!("{}", serde_json::to_string_pretty(&summary)?);
    Ok(())
}

fn run_scan(args: &[String]) -> anyhow::Result<()> {
    let mut input_path: Option<PathBuf> = None;
    let mut i = 0;
    while i < args.len() {
        match args[i].as_str() {
            "--input" => {
                i += 1;
                let raw = args
                    .get(i)
                    .ok_or_else(|| anyhow::anyhow!("missing path for --input"))?;
                input_path = Some(PathBuf::from(raw));
            }
            other => anyhow::bail!("unknown scan flag: {other}"),
        }
        i += 1;
    }
    let path = input_path.ok_or_else(|| anyhow::anyhow!("--input <file> is required"))?;
    let raw = std::fs::read_to_string(&path)?;
    let input: ScanCyclesInput = serde_json::from_str(&raw)?;
    let report = execute_scan_cycles(input)?;
    println!("{}", serde_json::to_string_pretty(&report)?);
    Ok(())
}

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().collect();
    if args.len() < 2 {
        print_usage();
        return ExitCode::from(2);
    }

    let result = match args[1].as_str() {
        "benchmark" => run_benchmark(&args[2..]),
        "scan" => run_scan(&args[2..]),
        _ => {
            print_usage();
            return ExitCode::from(2);
        }
    };

    match result {
        Ok(()) => ExitCode::SUCCESS,
        Err(err) => {
            eprintln!("error: {err}");
            ExitCode::FAILURE
        }
    }
}
