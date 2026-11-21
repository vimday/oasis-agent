"""Loopback Anvil E2E settlement & verification for OasisAtomicExecutor and OasisIntentVault.

Spawns an ephemeral Anvil child bound strictly to `127.0.0.1:<free_port>` (chain ID 8453),
verifies loopback Anvil identity before generating ephemeral runtime keys, executes 4 real
EIP-1559 signed on-chain drills against compiled Solidity bytecode, reconciles token deltas,
native gas, event logs, and rolling revert-budget state, and writes an immutable JSON report.
"""

import argparse
import json
import os
import shutil
import socket
import subprocess  # nosec B404 - bounded local anvil/forge execution only
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from types import TracebackType
from typing import Any, Final, Self

import httpx
from eth_abi.abi import decode, encode
from eth_account import Account
from eth_account.signers.local import LocalAccount
from eth_hash.auto import keccak
from eth_utils import to_checksum_address

from oasis_agent.execution.atomic_bundle import (
    AtomicCycleIntent,
    PrecheckGuardSpec,
    SwapLegSpec,
    encode_execute_cycle_calldata,
)
from oasis_agent.models.common import ZERO_ADDRESS, canonical_json, normalize_address, sha256_hex
from oasis_agent.risk.revert_budget import (
    CYCLE_EXECUTED_TOPIC0,
    EARLY_ABORTED_TOPIC0,
    AttemptOutcome,
    ReceiptClassificationInput,
    ReceiptLog,
    RevertBudgetStatus,
    RollingRevertBudgetPolicy,
    RollingRevertBudgetTracker,
    classify_execution_receipt,
)

ANVIL_E2E_SCHEMA_VERSION: Final[str] = "oasis-anvil-profit-e2e-v1"
BASE_CHAIN_ID: Final[int] = 8453
INITIAL_SQRT_PRICE: Final[int] = 1 << 96
MAX_EARLY_ABORT_GAS: Final[int] = 65_000
ERC1271_MAGIC: Final[str] = "0x1626ba7e"
ERC1271_INVALID: Final[str] = "0xffffffff"

QUOTE_DOMAIN_TYPEHASH: Final[bytes] = keccak(
    b"OasisIntentQuote(uint256 chainId,address vault,uint256 signerEpoch,"
    b"uint256 deadline,bytes32 orderDigest)"
)
PROFIT_SWEPT_TOPIC0: Final[str] = (
    "0x" + keccak(b"ProfitSwept(address,address,uint256,address)").hex()
)
REACTOR_FILL_EXECUTED_TOPIC0: Final[str] = (
    "0x" + keccak(b"ReactorFillExecuted(address,address,uint256,address)").hex()
)

REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[1]
DEFAULT_CONTRACTS_OUT_DIR: Final[Path] = REPO_ROOT / "contracts" / "out"

_ARTIFACT_REL_PATHS: Final[dict[str, str]] = {
    "OasisAtomicExecutor": "OasisAtomicExecutor.sol/OasisAtomicExecutor.json",
    "MockAtomicERC20": "OasisAtomicExecutor.t.sol/MockAtomicERC20.json",
    "MockV3Pool": "OasisAtomicExecutor.t.sol/MockV3Pool.json",
    "MockV2Pair": "OasisAtomicExecutor.t.sol/MockV2Pair.json",
    "OasisIntentVault": "OasisIntentVault.sol/OasisIntentVault.json",
    "MockVaultERC20": "OasisIntentVault.t.sol/MockVaultERC20.json",
    "MockVaultV3Pool": "OasisIntentVault.t.sol/MockVaultV3Pool.json",
    "MockSwapRouter": "OasisIntentVault.t.sol/MockSwapRouter.json",
    "MockUniswapXReactor": "OasisIntentVault.t.sol/MockUniswapXReactor.json",
}


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def _require_gas_delta(
    native_delta: int,
    gas_used: int,
    effective_price: int,
    label: str,
) -> None:
    """Require the operator's native spend to be gas cost, allowing base-fee clock drift.

    Anvil can report a receipt's effective gas price that lags the one-block base-fee
    adjustment by a fraction of a percent. Any real value transfer is orders of magnitude
    larger, so a 2% (or 0.00001 ETH) tolerance preserves the invariant.
    """
    expected = gas_used * effective_price
    tolerance = max(expected // 50, 10**13)
    _require(
        abs(native_delta - expected) <= tolerance,
        f"{label} native gas delta mismatch: {native_delta} != {expected}",
    )


def _selector(signature: str) -> bytes:
    return keccak(signature.encode("utf-8"))[:4]


def _encode_call(signature: str, types: Sequence[str] = (), values: Sequence[Any] = ()) -> bytes:
    encoded_args = encode(list(types), list(values)) if types else b""
    return _selector(signature) + encoded_args


def _load_or_build_artifacts(contracts_out_dir: Path) -> dict[str, dict[str, Any]]:
    missing = [
        name for name, rel in _ARTIFACT_REL_PATHS.items() if not (contracts_out_dir / rel).is_file()
    ]
    if missing:
        forge_bin = shutil.which("forge")
        if forge_bin is None or not Path(forge_bin).is_file():
            raise RuntimeError(
                f"compiled artifacts missing ({', '.join(missing)}) and forge binary not found"
            )
        contracts_root = contracts_out_dir.parent
        subprocess.run(  # nosec B603 - fixed local foundry build invocation
            [forge_bin, "build", "--root", str(contracts_root)],
            shell=False,
            check=True,
            capture_output=True,
            text=True,
            timeout=180,
        )

    artifacts: dict[str, dict[str, Any]] = {}
    for name, rel in _ARTIFACT_REL_PATHS.items():
        path = contracts_out_dir / rel
        if not path.is_file():
            raise RuntimeError(f"missing compiled artifact after forge build: {path}")
        loaded = json.loads(path.read_text(encoding="utf-8"))
        bc_obj = loaded.get("bytecode", {}).get("object")
        if not isinstance(bc_obj, str) or not bc_obj.startswith("0x") or len(bc_obj) <= 4:
            raise RuntimeError(f"artifact {name} lacks creation bytecode")
        artifacts[name] = loaded
    return artifacts


class _AnvilRpc:
    """Strictly loopback-only JSON-RPC client using httpx.Client."""

    def __init__(self, url: str, *, timeout_s: float = 20.0) -> None:
        if not isinstance(url, str) or not url.startswith("http://127.0.0.1:"):
            raise ValueError("Anvil RPC URL must start with http://127.0.0.1:")
        parsed = httpx.URL(url)
        if (
            parsed.scheme != "http"
            or parsed.host != "127.0.0.1"
            or parsed.port is None
            or parsed.username
            or parsed.password
        ):
            raise ValueError("Anvil RPC URL failed loopback host/port validation")
        self.url = url
        self._id = 0
        self._client = httpx.Client(timeout=timeout_s, follow_redirects=False, trust_env=False)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def call(self, method: str, params: list[Any]) -> Any:
        self._id += 1
        req_id = self._id
        resp = self._client.post(
            self.url,
            json={"jsonrpc": "2.0", "id": req_id, "method": method, "params": params},
            headers={"Content-Type": "application/json"},
        )
        if resp.status_code != 200:
            raise RuntimeError(f"Anvil RPC {method} returned HTTP {resp.status_code}")
        payload = resp.json()
        if not isinstance(payload, dict) or payload.get("id") != req_id:
            raise RuntimeError(f"Anvil RPC {method} returned malformed envelope")
        if "error" in payload:
            raise RuntimeError(f"Anvil RPC {method} error: {payload['error']}")
        if "result" not in payload:
            raise RuntimeError(f"Anvil RPC {method} missing result")
        return payload["result"]

    def verify_local_anvil(self) -> str:
        client_version = str(self.call("web3_clientVersion", []))
        _require(
            self.url.startswith("http://127.0.0.1:") and "anvil" in client_version.lower(),
            f"unexpected non-Anvil or non-loopback endpoint: {client_version!r}",
        )
        chain_id = int(str(self.call("eth_chainId", [])), 16)
        _require(chain_id == BASE_CHAIN_ID, f"unexpected Anvil chainId {chain_id} != 8453")
        return client_version

    def latest_block(self) -> dict[str, Any]:
        block = self.call("eth_getBlockByNumber", ["latest", False])
        _require(isinstance(block, dict), "latest block must be an object")
        return dict(block)

    def balance(self, address: str) -> int:
        return int(str(self.call("eth_getBalance", [to_checksum_address(address), "latest"])), 16)

    def code(self, address: str) -> bytes:
        raw_hex = str(self.call("eth_getCode", [to_checksum_address(address), "latest"]))
        _require(raw_hex.startswith("0x"), "eth_getCode response must start with 0x")
        return bytes.fromhex(raw_hex[2:])

    def eth_call(self, to: str, data: bytes, block: str = "latest") -> bytes:
        raw_hex = str(
            self.call(
                "eth_call",
                [{"to": to_checksum_address(to), "data": "0x" + data.hex()}, block],
            )
        )
        _require(raw_hex.startswith("0x"), "eth_call response must start with 0x")
        return bytes.fromhex(raw_hex[2:])

    def call_abi(
        self,
        to: str,
        signature: str,
        types: Sequence[str],
        values: Sequence[Any],
        returns: Sequence[str],
        block: str = "latest",
    ) -> tuple[Any, ...]:
        raw = self.eth_call(to, _encode_call(signature, types, values), block=block)
        return tuple(decode(list(returns), raw))

    def erc20_balance(self, token: str, holder: str) -> int:
        return int(
            self.call_abi(
                token,
                "balanceOf(address)",
                ["address"],
                [to_checksum_address(holder)],
                ["uint256"],
            )[0]
        )

    def erc20_allowance(self, token: str, owner_addr: str, spender_addr: str) -> int:
        return int(
            self.call_abi(
                token,
                "allowance(address,address)",
                ["address", "address"],
                [to_checksum_address(owner_addr), to_checksum_address(spender_addr)],
                ["uint256"],
            )[0]
        )

    def send_eip1559(
        self,
        signer: LocalAccount,
        *,
        to: str | None,
        data: bytes,
        value: int = 0,
        gas_limit: int | None = None,
    ) -> dict[str, Any]:
        sender = to_checksum_address(signer.address)
        nonce = int(str(self.call("eth_getTransactionCount", [sender, "pending"])), 16)
        latest = self.latest_block()
        base_fee = int(str(latest.get("baseFeePerGas", "0x0")), 16)
        priority_fee = 1_000_000_000
        max_fee = max(2 * base_fee + priority_fee, 2_000_000_000)

        call_obj: dict[str, Any] = {
            "from": sender,
            "data": "0x" + data.hex(),
            "value": hex(value),
        }
        if to is not None:
            call_obj["to"] = to_checksum_address(to)

        if gas_limit is None:
            est_hex = str(self.call("eth_estimateGas", [call_obj]))
            estimated = int(est_hex, 16)
            resolved_gas = max(100_000, (estimated * 13) // 10)
        else:
            resolved_gas = gas_limit

        tx_dict: dict[str, Any] = {
            "type": 2,
            "chainId": BASE_CHAIN_ID,
            "nonce": nonce,
            "maxPriorityFeePerGas": priority_fee,
            "maxFeePerGas": max_fee,
            "gas": resolved_gas,
            "value": value,
            "data": "0x" + data.hex(),
        }
        if to is not None:
            tx_dict["to"] = to_checksum_address(to)

        signed = signer.sign_transaction(tx_dict)
        raw_bytes = bytes(signed.raw_transaction)
        expected_hash = "0x" + keccak(raw_bytes).hex()
        tx_hash = str(self.call("eth_sendRawTransaction", ["0x" + raw_bytes.hex()]))
        _require(tx_hash == expected_hash, f"tx hash mismatch: {tx_hash} != {expected_hash}")

        receipt: dict[str, Any] | None = None
        for _ in range(150):
            found = self.call("eth_getTransactionReceipt", [tx_hash])
            if isinstance(found, dict):
                receipt = found
                break
            time.sleep(0.02)
        _require(receipt is not None, f"receipt timeout for tx {tx_hash}")
        confirmed_receipt = dict(receipt)  # shallow copy for typed return
        return confirmed_receipt

    def deploy(
        self,
        signer: LocalAccount,
        artifact: dict[str, Any],
        ctor_types: Sequence[str] = (),
        ctor_values: Sequence[Any] = (),
    ) -> tuple[str, dict[str, Any]]:
        creation_hex = str(artifact["bytecode"]["object"])
        init_bytes = bytes.fromhex(creation_hex[2:])
        if ctor_types:
            init_bytes += encode(list(ctor_types), list(ctor_values))
        receipt = self.send_eip1559(signer, to=None, data=init_bytes)
        _require(
            int(str(receipt["status"]), 16) == 1,
            f"contract deployment reverted: {receipt.get('transactionHash')}",
        )
        contract_addr = receipt.get("contractAddress")
        _require(isinstance(contract_addr, str) and len(contract_addr) == 42, "missing address")
        return normalize_address(str(contract_addr)), receipt


def _wait_for_anvil_ready(url: str, *, timeout_s: float) -> _AnvilRpc:
    deadline = time.monotonic() + timeout_s
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        rpc = _AnvilRpc(url, timeout_s=5.0)
        try:
            rpc.verify_local_anvil()
            rpc.close()
            return _AnvilRpc(url, timeout_s=20.0)
        except (httpx.HTTPError, OSError, RuntimeError, ValueError) as exc:
            last_exc = exc
            rpc.close()
            time.sleep(0.05)
    raise RuntimeError(f"Anvil failed to become ready within {timeout_s}s: {last_exc}")


def _receipt_logs(receipt: dict[str, Any]) -> tuple[ReceiptLog, ...]:
    return tuple(
        ReceiptLog(
            address=str(entry["address"]),
            topics=tuple(str(t) for t in entry["topics"]),
            data=str(entry.get("data", "0x")),
        )
        for entry in receipt.get("logs", [])
    )


def _run_executor_drills(
    rpc: _AnvilRpc,
    artifacts: dict[str, dict[str, Any]],
    *,
    owner: LocalAccount,
    operator: LocalAccount,
    guardian: LocalAccount,
    treasury: LocalAccount,
    revenue_sink: LocalAccount,
) -> tuple[dict[str, Any], dict[str, Any]]:
    executor_addr, _ = rpc.deploy(
        owner,
        artifacts["OasisAtomicExecutor"],
        ["address", "address", "address", "address"],
        [owner.address, guardian.address, treasury.address, revenue_sink.address],
    )
    token_a, _ = rpc.deploy(
        owner, artifacts["MockAtomicERC20"], ["string", "string"], ["Token A", "TKA"]
    )
    token_b, _ = rpc.deploy(
        owner, artifacts["MockAtomicERC20"], ["string", "string"], ["Token B", "TKB"]
    )

    v3_pool, _ = rpc.deploy(
        owner,
        artifacts["MockV3Pool"],
        ["address", "address", "uint160", "uint256", "uint256", "uint256", "uint256"],
        [
            to_checksum_address(token_a),
            to_checksum_address(token_b),
            INITIAL_SQRT_PRICE,
            2,
            1,
            1,
            2,
        ],
    )
    v2_pair, _ = rpc.deploy(
        owner,
        artifacts["MockV2Pair"],
        ["address", "address", "uint24"],
        [to_checksum_address(token_a), to_checksum_address(token_b), 30],
    )

    pool_seed = 1_000 * 10**18
    executor_seed = 100 * 10**18
    for token_addr, recipient, amount in (
        (token_a, v3_pool, pool_seed),
        (token_b, v3_pool, pool_seed),
        (token_a, v2_pair, pool_seed),
        (token_b, v2_pair, pool_seed),
        (token_a, executor_addr, executor_seed),
    ):
        rec = rpc.send_eip1559(
            owner,
            to=token_addr,
            data=_encode_call(
                "mint(address,uint256)",
                ["address", "uint256"],
                [to_checksum_address(recipient), amount],
            ),
        )
        _require(int(str(rec["status"]), 16) == 1, "mock ERC20 mint failed")

    sync_rec = rpc.send_eip1559(owner, to=v2_pair, data=_encode_call("syncReserves()"))
    _require(int(str(sync_rec["status"]), 16) == 1, "syncReserves failed")

    for to_target, sig, types, vals in (
        (executor_addr, "setOperator(address,bool)", ["address", "bool"], [operator.address, True]),
        (
            executor_addr,
            "setApprovedToken(address,bool)",
            ["address", "bool"],
            [to_checksum_address(token_a), True],
        ),
        (
            executor_addr,
            "setApprovedToken(address,bool)",
            ["address", "bool"],
            [to_checksum_address(token_b), True],
        ),
        (
            executor_addr,
            "setApprovedPool(address,uint8)",
            ["address", "uint8"],
            [to_checksum_address(v3_pool), 2],
        ),
        (
            executor_addr,
            "setApprovedPool(address,uint8)",
            ["address", "uint8"],
            [to_checksum_address(v2_pair), 1],
        ),
    ):
        rec = rpc.send_eip1559(owner, to=to_target, data=_encode_call(sig, types, vals))
        _require(int(str(rec["status"]), 16) == 1, f"executor setup call {sig} failed")

    executor_code_hash = "0x" + keccak(rpc.code(executor_addr)).hex()
    block_ts = int(str(rpc.latest_block()["timestamp"]), 16)
    initial_amount = 10 * 10**18
    min_net_profit = 8 * 10**18

    legs = (
        SwapLegSpec(
            pool=v3_pool,
            token_in=token_a,
            token_out=token_b,
            zero_for_one=True,
            sqrt_price_limit_x96=INITIAL_SQRT_PRICE - 1,
            v2_fee_bps=0,
        ),
        SwapLegSpec(
            pool=v2_pair,
            token_in=token_b,
            token_out=token_a,
            zero_for_one=False,
            sqrt_price_limit_x96=0,
            v2_fee_bps=30,
        ),
    )
    guard = PrecheckGuardSpec(
        pool=v3_pool,
        min_sqrt_price_x96=INITIAL_SQRT_PRICE - 100,
        max_sqrt_price_x96=INITIAL_SQRT_PRICE + 100,
    )

    # Drill 1: Profitable two-hop V3 -> V2 arbitrage cycle + sweepProfit to revenue_sink
    intent1 = AtomicCycleIntent(
        chain_id=BASE_CHAIN_ID,
        executor_address=executor_addr,
        executor_code_hash=executor_code_hash,
        nonce=1,
        deadline=block_ts + 300,
        profit_token=token_a,
        initial_amount=initial_amount,
        min_net_profit=min_net_profit,
        guard=guard,
        legs=legs,
    )
    calldata1, calldata1_sha256 = encode_execute_cycle_calldata(intent1)

    op_eth_before = rpc.balance(operator.address)
    exec_tok_before = rpc.erc20_balance(token_a, executor_addr)

    rec1 = rpc.send_eip1559(operator, to=executor_addr, data=calldata1)
    _require(int(str(rec1["status"]), 16) == 1, "Drill 1 executeCycle reverted")

    op_eth_after = rpc.balance(operator.address)
    exec_tok_after = rpc.erc20_balance(token_a, executor_addr)
    gas_used_1 = int(str(rec1["gasUsed"]), 16)
    eff_price_1 = int(str(rec1["effectiveGasPrice"]), 16)
    native_delta_1 = op_eth_before - op_eth_after
    _require_gas_delta(native_delta_1, gas_used_1, eff_price_1, "Drill 1")

    cycle_logs = [
        log
        for log in rec1.get("logs", [])
        if normalize_address(str(log["address"])) == executor_addr
        and log["topics"][0].lower() == CYCLE_EXECUTED_TOPIC0
    ]
    _require(len(cycle_logs) == 1, "Drill 1 missing unique CycleExecuted event log")
    decoded_init, decoded_profit = decode(
        ["uint256", "uint256"], bytes.fromhex(cycle_logs[0]["data"][2:])
    )
    net_profit = exec_tok_after - exec_tok_before
    _require(int(decoded_init) == initial_amount, "Drill 1 logged initialAmount mismatch")
    _require(
        int(decoded_profit) == net_profit,
        "Drill 1 logged netProfit disagrees with balance delta",
    )
    _require(net_profit >= min_net_profit, f"Drill 1 net profit {net_profit} < {min_net_profit}")

    sweep_eth_before = rpc.balance(operator.address)
    sweep_rec = rpc.send_eip1559(
        operator,
        to=executor_addr,
        data=_encode_call(
            "sweepProfit(address,uint256)",
            ["address", "uint256"],
            [to_checksum_address(token_a), net_profit],
        ),
    )
    _require(int(str(sweep_rec["status"]), 16) == 1, "Drill 1 sweepProfit reverted")
    sweep_eth_after = rpc.balance(operator.address)
    sweep_gas_used = int(str(sweep_rec["gasUsed"]), 16)
    sweep_eff_price = int(str(sweep_rec["effectiveGasPrice"]), 16)
    _require_gas_delta(
        sweep_eth_before - sweep_eth_after,
        sweep_gas_used,
        sweep_eff_price,
        "Drill 1 sweep",
    )
    sweep_logs = [
        log
        for log in sweep_rec.get("logs", [])
        if normalize_address(str(log["address"])) == executor_addr
        and log["topics"][0].lower() == PROFIT_SWEPT_TOPIC0
    ]
    _require(len(sweep_logs) == 1, "Drill 1 missing ProfitSwept event log")

    rev_bal = rpc.erc20_balance(token_a, revenue_sink.address)
    treasury_bal = rpc.erc20_balance(token_a, treasury.address)
    op_tok_bal = rpc.erc20_balance(token_a, operator.address)
    exec_tok_final = rpc.erc20_balance(token_a, executor_addr)
    _require(rev_bal == net_profit, "Drill 1 revenue_sink did not receive exact profit")
    _require(treasury_bal == 0 and op_tok_bal == 0, "Drill 1 profit leaked outside revenue_sink")
    _require(exec_tok_final == exec_tok_before, "Drill 1 executor principal mismatch after sweep")

    drill1_report = {
        "passed": True,
        "tx_hash": str(rec1["transactionHash"]),
        "sweep_tx_hash": str(sweep_rec["transactionHash"]),
        "calldata_sha256": calldata1_sha256,
        "gas_used": gas_used_1,
        "sweep_gas_used": sweep_gas_used,
        "effective_gas_price_wei": eff_price_1,
        "native_gas_delta_wei": native_delta_1,
        "profit_token": token_a,
        "initial_amount_raw": initial_amount,
        "min_net_profit_raw": min_net_profit,
        "net_profit_raw": net_profit,
        "swept_to_revenue_sink_raw": rev_bal,
        "treasury_delta_raw": treasury_bal,
        "operator_token_delta_raw": op_tok_bal,
        "cycle_executed_event_verified": True,
    }

    # Drill 2: Early-abort under 65k gas when slot0 moves outside guard band
    push_rec = rpc.send_eip1559(
        owner,
        to=v3_pool,
        data=_encode_call("setSqrtPriceX96(uint160)", ["uint160"], [INITIAL_SQRT_PRICE - 500]),
    )
    _require(int(str(push_rec["status"]), 16) == 1, "Drill 2 setSqrtPriceX96 failed")

    intent2 = AtomicCycleIntent(
        chain_id=BASE_CHAIN_ID,
        executor_address=executor_addr,
        executor_code_hash=executor_code_hash,
        nonce=2,
        deadline=block_ts + 300,
        profit_token=token_a,
        initial_amount=initial_amount,
        min_net_profit=0,
        guard=guard,
        legs=legs,
    )
    calldata2, calldata2_sha256 = encode_execute_cycle_calldata(intent2)

    tok_before_abort = rpc.erc20_balance(token_a, executor_addr)
    op_eth_before_2 = rpc.balance(operator.address)
    rec2 = rpc.send_eip1559(operator, to=executor_addr, data=calldata2)
    _require(int(str(rec2["status"]), 16) == 1, "Drill 2 early abort transaction must succeed")

    op_eth_after_2 = rpc.balance(operator.address)
    tok_after_abort = rpc.erc20_balance(token_a, executor_addr)
    gas_used_2 = int(str(rec2["gasUsed"]), 16)
    eff_price_2 = int(str(rec2["effectiveGasPrice"]), 16)
    native_delta_2 = op_eth_before_2 - op_eth_after_2
    _require_gas_delta(native_delta_2, gas_used_2, eff_price_2, "Drill 2")
    _require(tok_after_abort == tok_before_abort, "Drill 2 altered profit token balance")
    _require(
        0 < gas_used_2 < MAX_EARLY_ABORT_GAS,
        f"Drill 2 early abort gas {gas_used_2} >= {MAX_EARLY_ABORT_GAS}",
    )

    abort_logs = [
        log
        for log in rec2.get("logs", [])
        if normalize_address(str(log["address"])) == executor_addr
        and log["topics"][0].lower() == EARLY_ABORTED_TOPIC0
    ]
    _require(len(abort_logs) == 1, "Drill 2 missing unique EarlyAborted event log")

    policy = RollingRevertBudgetPolicy(
        max_hourly_gas_burn_usd_micros=5_000_000,
        max_deep_reverts_per_hour=3,
        max_early_abort_gas=MAX_EARLY_ABORT_GAS,
    )
    tracker = RollingRevertBudgetTracker(policy)
    parsed_input = ReceiptClassificationInput(
        executor_address=executor_addr,
        status=int(str(rec2["status"]), 16),
        gas_used=gas_used_2,
        logs=_receipt_logs(rec2),
    )
    outcome = classify_execution_receipt(parsed_input, policy)
    _require(
        outcome is AttemptOutcome.EARLY_ABORT_PROBE,
        f"Drill 2 receipt misclassified as {outcome.value}",
    )
    snap = tracker.record_receipt(
        timestamp_s=block_ts + 1,
        receipt=parsed_input,
        gas_cost_usd_micros=10_000,
        realized_profit_usd_micros=0,
    )
    _require(
        snap.status is RevertBudgetStatus.ACTIVE,
        f"Drill 2 tracker halted unexpectedly: {snap.halt_reasons}",
    )

    drill2_report = {
        "passed": True,
        "tx_hash": str(rec2["transactionHash"]),
        "calldata_sha256": calldata2_sha256,
        "gas_used": gas_used_2,
        "effective_gas_price_wei": eff_price_2,
        "native_gas_delta_wei": native_delta_2,
        "early_aborted_event_verified": True,
        "profit_token_delta_raw": tok_after_abort - tok_before_abort,
        "revert_budget_outcome": outcome.value,
        "revert_budget_status": snap.status.value,
    }
    return drill1_report, drill2_report


def _run_vault_drills(
    rpc: _AnvilRpc,
    artifacts: dict[str, dict[str, Any]],
    *,
    owner: LocalAccount,
    operator: LocalAccount,
    guardian: LocalAccount,
    treasury: LocalAccount,
    revenue_sink: LocalAccount,
    quote_signer: LocalAccount,
    quote_signer_2: LocalAccount,
    swapper: LocalAccount,
) -> tuple[dict[str, Any], dict[str, Any]]:
    vault_addr, _ = rpc.deploy(
        owner,
        artifacts["OasisIntentVault"],
        ["address", "address", "address", "address"],
        [owner.address, guardian.address, treasury.address, revenue_sink.address],
    )

    # Drill 3: ERC-1271 quote signature validation and signerEpoch rotation revocation
    rot1_rec = rpc.send_eip1559(
        owner,
        to=vault_addr,
        data=_encode_call("rotateQuoteSigner(address)", ["address"], [quote_signer.address]),
    )
    _require(int(str(rot1_rec["status"]), 16) == 1, "Drill 3 rotateQuoteSigner(1) failed")
    epoch_before = int(rpc.call_abi(vault_addr, "signerEpoch()", [], [], ["uint256"])[0])
    _require(epoch_before == 1, f"Drill 3 expected signerEpoch=1, got {epoch_before}")

    onchain_typehash = bytes(
        rpc.call_abi(vault_addr, "QUOTE_DOMAIN_TYPEHASH()", [], [], ["bytes32"])[0]
    )
    _require(onchain_typehash == QUOTE_DOMAIN_TYPEHASH, "Drill 3 QUOTE_DOMAIN_TYPEHASH mismatch")

    block_ts = int(str(rpc.latest_block()["timestamp"]), 16)
    deadline = block_ts + 300
    order_digest = keccak(b"oasis-anvil-drill3-order-digest")
    quote_digest = keccak(
        encode(
            ["bytes32", "uint256", "address", "uint256", "uint256", "bytes32"],
            [
                QUOTE_DOMAIN_TYPEHASH,
                BASE_CHAIN_ID,
                to_checksum_address(vault_addr),
                1,
                deadline,
                order_digest,
            ],
        )
    )
    onchain_quote_digest = bytes(
        rpc.call_abi(
            vault_addr,
            "computeQuoteDigest(bytes32,uint256)",
            ["bytes32", "uint256"],
            [order_digest, deadline],
            ["bytes32"],
        )[0]
    )
    _require(onchain_quote_digest == quote_digest, "Drill 3 computeQuoteDigest mismatch")

    sig_epoch1 = Account.unsafe_sign_hash(quote_digest, quote_signer.key)
    raw_sig_1 = (
        sig_epoch1.r.to_bytes(32, "big")
        + sig_epoch1.s.to_bytes(32, "big")
        + bytes([int(sig_epoch1.v)])
    )
    encoded_sig_1 = encode(["uint256", "bytes"], [deadline, raw_sig_1])
    _require(len(encoded_sig_1) == 192, "Drill 3 encoded ERC-1271 signature must be 192 bytes")

    magic_before = (
        "0x"
        + bytes(
            rpc.call_abi(
                vault_addr,
                "isValidSignature(bytes32,bytes)",
                ["bytes32", "bytes"],
                [order_digest, encoded_sig_1],
                ["bytes4"],
            )[0]
        ).hex()
    )
    _require(
        magic_before == ERC1271_MAGIC,
        f"Drill 3 pre-rotation isValidSignature returned {magic_before}",
    )

    rot2_rec = rpc.send_eip1559(
        owner,
        to=vault_addr,
        data=_encode_call("rotateQuoteSigner(address)", ["address"], [quote_signer_2.address]),
    )
    _require(int(str(rot2_rec["status"]), 16) == 1, "Drill 3 rotateQuoteSigner(2) failed")
    epoch_after = int(rpc.call_abi(vault_addr, "signerEpoch()", [], [], ["uint256"])[0])
    _require(epoch_after == 2, f"Drill 3 expected signerEpoch=2, got {epoch_after}")

    magic_after = (
        "0x"
        + bytes(
            rpc.call_abi(
                vault_addr,
                "isValidSignature(bytes32,bytes)",
                ["bytes32", "bytes"],
                [order_digest, encoded_sig_1],
                ["bytes4"],
            )[0]
        ).hex()
    )
    _require(
        magic_after == ERC1271_INVALID,
        f"Drill 3 post-rotation epoch-1 signature must return 0xffffffff, got {magic_after}",
    )

    quote_digest_2 = keccak(
        encode(
            ["bytes32", "uint256", "address", "uint256", "uint256", "bytes32"],
            [
                QUOTE_DOMAIN_TYPEHASH,
                BASE_CHAIN_ID,
                to_checksum_address(vault_addr),
                2,
                deadline,
                order_digest,
            ],
        )
    )
    sig_epoch2 = Account.unsafe_sign_hash(quote_digest_2, quote_signer_2.key)
    raw_sig_2 = (
        sig_epoch2.r.to_bytes(32, "big")
        + sig_epoch2.s.to_bytes(32, "big")
        + bytes([int(sig_epoch2.v)])
    )
    encoded_sig_2 = encode(["uint256", "bytes"], [deadline, raw_sig_2])
    magic_epoch2 = (
        "0x"
        + bytes(
            rpc.call_abi(
                vault_addr,
                "isValidSignature(bytes32,bytes)",
                ["bytes32", "bytes"],
                [order_digest, encoded_sig_2],
                ["bytes4"],
            )[0]
        ).hex()
    )
    _require(
        magic_epoch2 == ERC1271_MAGIC,
        f"Drill 3 epoch-2 signature returned {magic_epoch2}",
    )

    drill3_report = {
        "passed": True,
        "rotate_tx_hash": str(rot2_rec["transactionHash"]),
        "gas_used": int(str(rot2_rec["gasUsed"]), 16),
        "epoch_before_rotation": epoch_before,
        "epoch_after_rotation": epoch_after,
        "pre_rotation_magic": magic_before,
        "post_rotation_magic": magic_after,
        "epoch2_magic": magic_epoch2,
    }

    # Drill 4: executeReactorFill + profit check + strict zero allowance residuals
    weth, _ = rpc.deploy(
        owner,
        artifacts["MockVaultERC20"],
        ["string", "string", "uint8"],
        ["Wrapped Ether", "WETH", 18],
    )
    usdc, _ = rpc.deploy(
        owner, artifacts["MockVaultERC20"], ["string", "string", "uint8"], ["USD Coin", "USDC", 6]
    )
    v3_pool, _ = rpc.deploy(owner, artifacts["MockVaultV3Pool"], ["uint160"], [INITIAL_SQRT_PRICE])
    router, _ = rpc.deploy(
        owner,
        artifacts["MockSwapRouter"],
        ["uint256", "uint256"],
        [2_600 * 10**6, 10**18],
    )
    reactor, _ = rpc.deploy(owner, artifacts["MockUniswapXReactor"])

    vault_seed_usdc = 500 * 10**6
    for token_addr, holder, amount in (
        (weth, reactor, 100 * 10**18),
        (usdc, router, 1_000_000 * 10**6),
        (usdc, vault_addr, vault_seed_usdc),
    ):
        rec = rpc.send_eip1559(
            owner,
            to=token_addr,
            data=_encode_call(
                "mint(address,uint256)",
                ["address", "uint256"],
                [to_checksum_address(holder), amount],
            ),
        )
        _require(int(str(rec["status"]), 16) == 1, "Drill 4 mock ERC20 mint failed")

    swap_selector = _selector("swapExactInput(address,address,uint256,address)")
    for sig, types, vals in (
        ("setFillerOperator(address)", ["address"], [operator.address]),
        (
            "setReactorAllowed(address,bool)",
            ["address", "bool"],
            [to_checksum_address(reactor), True],
        ),
        (
            "setRouterAllowed(address,bool)",
            ["address", "bool"],
            [to_checksum_address(router), True],
        ),
        ("setRouterSelectorAllowed(bytes4,bool)", ["bytes4", "bool"], [swap_selector, True]),
        ("setPoolAllowed(address,bool)", ["address", "bool"], [to_checksum_address(v3_pool), True]),
    ):
        rec = rpc.send_eip1559(owner, to=vault_addr, data=_encode_call(sig, types, vals))
        _require(int(str(rec["status"]), 16) == 1, f"Drill 4 vault setup {sig} failed")

    fill_deadline = int(str(rpc.latest_block()["timestamp"]), 16) + 300
    input_weth = 10**18
    output_usdc = 2_500 * 10**6
    min_step_out = 2_550 * 10**6
    min_net_profit_usdc = 90 * 10**6

    order_info = (
        to_checksum_address(reactor),
        swapper.address,
        1,
        fill_deadline,
        ZERO_ADDRESS,
        b"",
    )
    input_token = (to_checksum_address(weth), input_weth, input_weth)
    outputs = [(to_checksum_address(usdc), output_usdc, swapper.address)]
    resolved_order = (
        order_info,
        input_token,
        outputs,
        b"",
        keccak(b"oasis-anvil-drill4-single-order"),
    )
    resolved_order_type = (
        "((address,address,uint256,uint256,address,bytes),"
        "(address,uint256,uint256),"
        "(address,uint256,address)[],bytes,bytes32)"
    )
    encoded_order = encode([resolved_order_type], [resolved_order])
    signed_order = (encoded_order, b"")

    step_calldata = _encode_call(
        "swapExactInput(address,address,uint256,address)",
        ["address", "address", "uint256", "address"],
        [
            to_checksum_address(weth),
            to_checksum_address(usdc),
            input_weth,
            to_checksum_address(vault_addr),
        ],
    )
    steps = [
        (
            to_checksum_address(router),
            to_checksum_address(v3_pool),
            to_checksum_address(weth),
            to_checksum_address(usdc),
            input_weth,
            min_step_out,
            step_calldata,
        )
    ]
    step_array_type = "(address,address,address,address,uint256,uint256,bytes)[]"
    callback_data = encode([step_array_type], [steps])
    reactor_calldata = _encode_call(
        "executeWithCallback((bytes,bytes),bytes)",
        ["(bytes,bytes)", "bytes"],
        [signed_order, callback_data],
    )
    plan = (
        to_checksum_address(reactor),
        to_checksum_address(usdc),
        min_net_profit_usdc,
        (to_checksum_address(v3_pool), INITIAL_SQRT_PRICE - 10, INITIAL_SQRT_PRICE + 10),
        steps,
    )
    fill_calldata = _encode_call(
        "executeReactorFill((address,address,uint256,(address,uint160,uint160),"
        "(address,address,address,address,uint256,uint256,bytes)[]),bytes)",
        [
            (
                "(address,address,uint256,(address,uint160,uint160),"
                "(address,address,address,address,uint256,uint256,bytes)[])"
            ),
            "bytes",
        ],
        [plan, reactor_calldata],
    )

    bal_before = rpc.erc20_balance(usdc, vault_addr)
    swapper_before = rpc.erc20_balance(usdc, swapper.address)
    op_eth_before_4 = rpc.balance(operator.address)

    rec4 = rpc.send_eip1559(operator, to=vault_addr, data=fill_calldata)
    _require(int(str(rec4["status"]), 16) == 1, "Drill 4 executeReactorFill reverted")

    op_eth_after_4 = rpc.balance(operator.address)
    bal_after = rpc.erc20_balance(usdc, vault_addr)
    swapper_after = rpc.erc20_balance(usdc, swapper.address)
    gas_used_4 = int(str(rec4["gasUsed"]), 16)
    eff_price_4 = int(str(rec4["effectiveGasPrice"]), 16)
    native_delta_4 = op_eth_before_4 - op_eth_after_4
    _require_gas_delta(native_delta_4, gas_used_4, eff_price_4, "Drill 4")

    fill_logs = [
        log
        for log in rec4.get("logs", [])
        if normalize_address(str(log["address"])) == vault_addr
        and log["topics"][0].lower() == REACTOR_FILL_EXECUTED_TOPIC0
    ]
    _require(len(fill_logs) == 1, "Drill 4 missing unique ReactorFillExecuted event log")
    (logged_profit,) = decode(["uint256"], bytes.fromhex(fill_logs[0]["data"][2:]))
    net_profit_4 = bal_after - bal_before
    _require(int(logged_profit) == net_profit_4, "Drill 4 logged netProfit disagrees with balance")
    _require(
        bal_after >= bal_before + min_net_profit_usdc,
        f"Drill 4 balance delta {net_profit_4} < {min_net_profit_usdc}",
    )
    _require(
        swapper_after - swapper_before == output_usdc,
        "Drill 4 swapper did not receive exact output amount",
    )

    allowances = {
        "vault_router_token_in": rpc.erc20_allowance(weth, vault_addr, router),
        "vault_router_token_out": rpc.erc20_allowance(usdc, vault_addr, router),
        "vault_reactor_token_in": rpc.erc20_allowance(weth, vault_addr, reactor),
        "vault_reactor_token_out": rpc.erc20_allowance(usdc, vault_addr, reactor),
    }
    _require(
        all(val == 0 for val in allowances.values()),
        f"Drill 4 non-zero allowance residuals: {allowances}",
    )

    drill4_report = {
        "passed": True,
        "tx_hash": str(rec4["transactionHash"]),
        "gas_used": gas_used_4,
        "effective_gas_price_wei": eff_price_4,
        "native_gas_delta_wei": native_delta_4,
        "profit_token": usdc,
        "min_net_profit_raw": min_net_profit_usdc,
        "vault_balance_before_raw": bal_before,
        "vault_balance_after_raw": bal_after,
        "net_profit_raw": net_profit_4,
        "swapper_received_raw": swapper_after - swapper_before,
        "reactor_fill_executed_event_verified": True,
        "allowance_residuals": allowances,
    }
    return drill3_report, drill4_report


def _write_immutable_json(out_path: Path, payload: dict[str, Any]) -> None:
    if out_path.is_symlink():
        raise ValueError(f"refusing symlink output path: {out_path}")
    if out_path.exists():
        raise FileExistsError(f"refusing to overwrite existing E2E report: {out_path}")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    raw = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    with out_path.open("xb") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())


def run_anvil_profit_e2e(
    out_path: Path | None = None,
    *,
    contracts_out_dir: Path | None = None,
    anvil_timeout_s: float = 15.0,
) -> dict[str, Any]:
    """Run all 4 real on-chain EIP-1559 drills against an ephemeral loopback Anvil node."""
    anvil_bin = shutil.which("anvil")
    if anvil_bin is None or not Path(anvil_bin).is_file():
        raise RuntimeError("anvil binary not found on PATH")

    resolved_out_dir = (
        contracts_out_dir if contracts_out_dir is not None else DEFAULT_CONTRACTS_OUT_DIR
    )
    artifacts = _load_or_build_artifacts(resolved_out_dir)

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = int(reservation.getsockname()[1])

    url = f"http://127.0.0.1:{port}"
    _require(url.startswith("http://127.0.0.1:"), "Anvil URL must bind strictly to 127.0.0.1")

    process = subprocess.Popen(  # nosec B603 - fixed local anvil binary on loopback port
        [
            anvil_bin,
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--chain-id",
            str(BASE_CHAIN_ID),
            "--silent",
        ],
        shell=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        with _wait_for_anvil_ready(url, timeout_s=anvil_timeout_s) as rpc:
            client_version = rpc.verify_local_anvil()

            # Ephemeral accounts created ONLY after local Anvil loopback verification
            owner = Account.create()
            operator = Account.create()
            treasury = Account.create()
            revenue_sink = Account.create()
            guardian = Account.create()
            quote_signer = Account.create()
            quote_signer_2 = Account.create()
            swapper = Account.create()

            funding_wei = hex(100 * 10**18)
            for funded in (owner, operator):
                rpc.call("anvil_setBalance", [funded.address, funding_wei])
            rpc.call("evm_mine", [])

            drill1, drill2 = _run_executor_drills(
                rpc,
                artifacts,
                owner=owner,
                operator=operator,
                guardian=guardian,
                treasury=treasury,
                revenue_sink=revenue_sink,
            )
            drill3, drill4 = _run_vault_drills(
                rpc,
                artifacts,
                owner=owner,
                operator=operator,
                guardian=guardian,
                treasury=treasury,
                revenue_sink=revenue_sink,
                quote_signer=quote_signer,
                quote_signer_2=quote_signer_2,
                swapper=swapper,
            )

            report: dict[str, Any] = {
                "schema_version": ANVIL_E2E_SCHEMA_VERSION,
                "environment": "loopback_anvil_ephemeral",
                "chain_id": BASE_CHAIN_ID,
                "anvil_client_version": client_version,
                "execution_eligible": False,
                "all_drills_passed": True,
                "drills": {
                    "executor_two_hop_profit_and_sweep": drill1,
                    "executor_early_abort_under_65k_gas": drill2,
                    "vault_erc1271_and_epoch_revocation": drill3,
                    "vault_reactor_fill_and_zero_allowances": drill4,
                },
            }
            report["report_sha256"] = sha256_hex(canonical_json(report))
            if out_path is not None:
                _write_immutable_json(out_path, report)
            return report
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def build_parser() -> argparse.ArgumentParser:
    """Construct CLI parser for Anvil E2E settlement verification."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        type=Path,
        required=True,
        help="Immutable output path for the Anvil profit E2E JSON report.",
    )
    parser.add_argument(
        "--contracts-out-dir",
        type=Path,
        default=None,
        help="Directory holding compiled Foundry JSON artifacts (default: contracts/out).",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run all 4 Anvil E2E drills and persist an immutable verification report."""
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        report = run_anvil_profit_e2e(
            out_path=args.out,
            contracts_out_dir=args.contracts_out_dir,
        )
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"anvil E2E verification failed: {exc}", file=sys.stderr)
        return 2

    drills = report["drills"]
    print(
        json.dumps(
            {
                "schema_version": report["schema_version"],
                "environment": report["environment"],
                "chain_id": report["chain_id"],
                "all_drills_passed": report["all_drills_passed"],
                "execution_eligible": report["execution_eligible"],
                "gas_used": {
                    "executor_two_hop": drills["executor_two_hop_profit_and_sweep"]["gas_used"],
                    "executor_sweep": drills["executor_two_hop_profit_and_sweep"]["sweep_gas_used"],
                    "executor_early_abort": drills["executor_early_abort_under_65k_gas"][
                        "gas_used"
                    ],
                    "vault_rotate_signer": drills["vault_erc1271_and_epoch_revocation"]["gas_used"],
                    "vault_reactor_fill": drills["vault_reactor_fill_and_zero_allowances"][
                        "gas_used"
                    ],
                },
                "report_sha256": report["report_sha256"],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
