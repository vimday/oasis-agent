"""Isolated local/testnet LP lifecycle exerciser, never a production signer.

Only local Anvil (31337) or Robinhood testnet (46630). Uses newly deployed test
assets; native transaction value is always zero. The testnet wallet is read
inside this process only. No raw signing material or signed payload is logged.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from eth_abi import encode
from eth_account import Account
from eth_utils import keccak
from web3 import Web3
from web3.exceptions import ContractLogicError

ROOT = Path(__file__).resolve().parents[1]
ZERO = "0x" + "00" * 20
CAPITAL = 10**18
LIQUIDITY = 10**16
TESTNET = 46630
TESTNET_GAS_BUDGET = 10**15


def artifact(name: str) -> dict[str, Any]:
    return json.loads((ROOT / "contracts" / "out" / f"{name}.sol" / f"{name}.json").read_text())


def read_test_key(path: Path) -> bytes:
    """No shell evaluation/interpolation; accept exactly one explicit hex key."""
    candidates = []
    for line in path.read_text().splitlines():
        if "=" not in line or line.lstrip().startswith("#"):
            continue
        name, value = line.split("=", 1)
        if name.strip() not in {"PRIVATE_KEY", "OASIS_PRIVATE_KEY", "OASIS_TEST_PRIVATE_KEY"}:
            continue
        match = re.fullmatch(r"(?:0x)?([0-9a-fA-F]{64})", value.strip().strip("\"'"))
        if match is None:
            raise ValueError("invalid isolated test key format")
        candidates.append(bytes.fromhex(match.group(1)))
    if len(candidates) != 1:
        raise ValueError("one explicit test key required")
    return candidates[0]


def validate_transaction(tx: dict[str, Any], chain: int, remaining: int) -> int:
    if chain not in {31337, TESTNET} or tx.get("chainId") != chain:
        raise ValueError("test network required")
    if tx.get("value") != 0:
        raise ValueError("native value transfer forbidden")
    for field in ("gas", "gasPrice", "nonce"):
        if type(tx.get(field)) is not int or tx[field] < 0:
            raise ValueError("integer transaction bounds required")
    if not 0 < tx["gas"] <= 12_000_000 or not 0 < tx["gasPrice"] <= 5 * 10**9:
        raise ValueError("test gas bounds exceeded")
    cost = tx["gas"] * tx["gasPrice"]
    if cost > remaining:
        raise ValueError("total test gas budget exhausted")
    return cost


def retained_budget(raw: str, limit: int) -> int:
    """An interrupted append fails closed; never reset a partial budget record."""
    if raw and not raw.endswith("\n"):
        raise ValueError("incomplete wallet budget record")
    previous = 0
    for line in raw.splitlines():
        entry = json.loads(line)
        current = entry["reserved_wei"]
        if type(current) is not int or not previous <= current <= limit:
            raise ValueError("wallet budget regression or overflow")
        previous = current
    return previous


class Lifecycle:
    def __init__(self, network: str, output: Path):
        self.chain = TESTNET if network == "testnet" else 31337
        self.url = (
            "https://rpc.testnet.chain.robinhood.com"
            if network == "testnet"
            else "http://127.0.0.1:8545"
        )
        self.w3 = Web3(Web3.HTTPProvider(self.url, request_kwargs={"timeout": 20}))
        if self.w3.eth.chain_id != self.chain:
            raise ValueError("RPC chain mismatch")
        # Private material stays in this standalone process, never an argument or artifact.
        self.account = (
            Account.from_key(read_test_key(ROOT / ".env"))
            if network == "testnet"
            else Account.create()
        )
        if network == "local":
            self.w3.provider.make_request("anvil_setBalance", [self.account.address, hex(10**20)])
        self.output = output
        output.mkdir(parents=True, exist_ok=False)
        self.budget = TESTNET_GAS_BUDGET if network == "testnet" else 10**18
        locks = ROOT / "reports" / "generated"
        locks.mkdir(parents=True, exist_ok=True)
        self.wallet_lock = (locks / f"test-wallet-{self.chain}-{self.account.address}.json").open(
            "a+"
        )
        fcntl.flock(self.wallet_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.wallet_lock.seek(0)
        saved_budget = self.wallet_lock.read()
        self.reserved = retained_budget(saved_budget, self.budget)
        if not 0 <= self.reserved <= self.budget:
            raise ValueError("wallet budget state invalid")
        self.spent = 0
        self.sequence = 0
        self.previous = "0" * 64
        self.nonce = self.w3.eth.get_transaction_count(self.account.address, "latest")
        if self.nonce != self.w3.eth.get_transaction_count(self.account.address, "pending"):
            raise ValueError("pending wallet transactions block testing")
        self.initial_balance = self.w3.eth.get_balance(self.account.address)
        if self.initial_balance < self.budget:
            raise ValueError("test wallet cannot preserve gas budget")
        self.targets: set[str] = set()
        self.contracts: dict[str, Any] = {}
        self.audit(
            "start",
            {
                "chain_id": self.chain,
                "account": self.account.address,
                "max_gas_wei": self.budget,
                "initial_balance": self.initial_balance,
            },
        )

    def audit(self, kind: str, payload: dict[str, Any]) -> None:
        data = {
            "seq": self.sequence + 1,
            "previous": self.previous,
            "kind": kind,
            "retrieved_at": datetime.now(UTC).isoformat(),
            **payload,
        }
        raw = json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
        digest = hashlib.sha256(raw).hexdigest()
        with (self.output / "events.jsonl").open("ab") as handle:
            handle.write(
                json.dumps({"event": data, "digest": digest}, sort_keys=True).encode() + b"\n"
            )
            handle.flush()
            os.fsync(handle.fileno())
        self.sequence += 1
        self.previous = digest

    def send(self, label: str, data: str, to: str | None = None) -> Any:
        if to is not None and to.lower() not in self.targets:
            raise ValueError("target was not deployed by this test")
        head = self.w3.eth.get_block("latest")
        nonce = self.w3.eth.get_transaction_count(self.account.address, "pending")
        if nonce != self.nonce or self.w3.eth.chain_id != self.chain:
            raise ValueError("chain or nonce changed")
        tx = {
            "from": self.account.address,
            "chainId": self.chain,
            "nonce": nonce,
            "gasPrice": self.w3.eth.gas_price,
            "value": 0,
            "data": data,
        }
        if to is not None:
            tx["to"] = to
        gas = self.w3.eth.estimate_gas(tx, block_identifier=head["number"])
        tx["gas"] = (gas * 12 + 9) // 10
        cost = validate_transaction(tx, self.chain, self.budget - self.reserved)
        if self.w3.eth.get_block(head["number"])["hash"] != head["hash"]:
            raise ValueError("simulation block reorg")
        signed = self.account.sign_transaction(tx)
        tx_hash = Web3.to_hex(signed.hash)
        self.reserved += cost
        self.wallet_lock.write(json.dumps({"reserved_wei": self.reserved}) + "\n")
        self.wallet_lock.flush()
        os.fsync(self.wallet_lock.fileno())
        # Commit transaction identity before any uncertain network submission.
        self.audit(
            "signed",
            {
                "label": label,
                "nonce": nonce,
                "tx_hash": tx_hash,
                "reserved_wei": self.reserved,
                "simulation_block": head["number"],
                "simulation_hash": Web3.to_hex(head["hash"]),
            },
        )
        actual = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        if Web3.to_hex(actual) != tx_hash:
            raise ValueError("submission hash mismatch")
        receipt = self.w3.eth.wait_for_transaction_receipt(actual, timeout=120, poll_latency=1)
        if receipt["status"] != 1:
            raise ValueError("test transaction reverted")
        if self.w3.eth.get_block(receipt["blockNumber"])["hash"] != receipt["blockHash"]:
            raise ValueError("receipt reorg")
        self.spent += receipt["gasUsed"] * receipt["effectiveGasPrice"]
        self.nonce += 1
        self.audit(
            "receipt",
            {
                "label": label,
                "tx_hash": tx_hash,
                "nonce": nonce,
                "block": receipt["blockNumber"],
                "block_hash": Web3.to_hex(receipt["blockHash"]),
                "gas_used": receipt["gasUsed"],
                "spent_wei": self.spent,
                "raw_receipt": json.loads(Web3.to_json(receipt)),
            },
        )
        if to is None:
            self.targets.add(receipt["contractAddress"].lower())
        return receipt

    def deploy(self, name: str, *args: Any) -> Any:
        spec = artifact(name)
        contract = self.w3.eth.contract(abi=spec["abi"], bytecode=spec["bytecode"]["object"])
        data = contract.constructor(*args).build_transaction(
            {
                "from": self.account.address,
                "gas": 1,
                "gasPrice": 1,
                "nonce": 0,
                "chainId": self.chain,
            }
        )["data"]
        receipt = self.send("deploy:" + name, data)
        deployed = self.w3.eth.contract(address=receipt["contractAddress"], abi=spec["abi"])
        self.contracts[name] = deployed.address
        return deployed

    def call(self, label: str, function: Any) -> Any:
        tx = function.build_transaction(
            {
                "from": self.account.address,
                "gas": 1,
                "gasPrice": 1,
                "nonce": 0,
                "chainId": self.chain,
            }
        )
        return self.send(label, tx["data"], tx["to"])

    def balances(self) -> tuple[int, int, int, int]:
        head = self.w3.eth.get_block("latest")
        values = tuple(
            token.functions.balanceOf(who).call(block_identifier=head["number"])
            for who in (self.account.address, self.manager.address)
            for token in (self.token0, self.token1)
        )
        if self.w3.eth.get_block(head["number"])["hash"] != head["hash"]:
            raise ValueError("balance snapshot reorg")
        self.audit(
            "balances",
            {"block": head["number"], "block_hash": Web3.to_hex(head["hash"]), "values": values},
        )
        return values  # type: ignore[return-value]

    def conserved(self, before: tuple[int, int, int, int]) -> None:
        after = self.balances()
        if (
            before[0] + before[2] != after[0] + after[2]
            or before[1] + before[3] != after[1] + after[3]
        ):
            raise ValueError("per-token owner/PoolManager conservation failed")
        self.audit("conservation", {"before": before, "after": after})

    def plan(self, action: int, params: list[bytes]) -> bytes:
        return encode(["bytes", "bytes[]"], [bytes([action, 0x12, 0x12]), params])

    def run(self) -> None:
        self.manager = self.deploy("PoolManager", self.account.address)
        # Exact precompiled runtime from the pinned upstream local-test helper.
        helper = (
            ROOT
            / "contracts/node_modules/@uniswap/v4-periphery/lib/permit2/test/utils/DeployPermit2.sol"
        )
        runtime = bytes.fromhex(re.findall(r'hex"([0-9a-fA-F]+)"', helper.read_text())[0])
        # PUSH2 len; PUSH2 offset=15; PUSH1 0; CODECOPY; PUSH2 len; PUSH1 0; RETURN.
        size = len(runtime).to_bytes(2, "big")
        init = b"\x61" + size + b"\x61\x00\x0f\x60\x00\x39\x61" + size + b"\x60\x00\xf3" + runtime
        receipt = self.send("deploy:Permit2", "0x" + init.hex())
        permit2 = self.w3.eth.contract(
            address=receipt["contractAddress"], abi=artifact("IAllowanceTransfer")["abi"]
        )
        if bytes(self.w3.eth.get_code(permit2.address)) != runtime:
            raise ValueError("Permit2 runtime mismatch")
        self.contracts["Permit2"] = permit2.address
        positions = self.deploy(
            "PositionManager", self.manager.address, permit2.address, 100_000, ZERO, ZERO
        )
        first, second = self.deploy("TestERC20", CAPITAL), self.deploy("TestERC20", CAPITAL)
        self.token0, self.token1 = sorted((first, second), key=lambda token: token.address.lower())
        self.contracts.update(token0=self.token0.address, token1=self.token1.address)
        factory = self.deploy("OasisHookFactory")
        args = (
            self.manager.address,
            positions.address,
            self.account.address,
            self.account.address,
            self.account.address,
        )
        code_hash = factory.functions.initCodeHash(*args).call()
        flags = factory.functions.REQUIRED_HOOK_FLAGS().call()
        for candidate in range(200_000):
            salt = candidate.to_bytes(32, "big")
            address = keccak(b"\xff" + bytes.fromhex(factory.address[2:]) + salt + code_hash)[12:]
            if int.from_bytes(address, "big") & ((1 << 14) - 1) == flags:
                break
        else:
            raise ValueError("no bounded CREATE2 salt found")
        self.call("deploy:hook", factory.functions.deploy(salt, *args))
        hook_address = Web3.to_checksum_address(address)
        if not self.w3.eth.get_code(hook_address):
            raise ValueError("hook runtime missing")
        self.targets.add(hook_address.lower())
        hook = self.w3.eth.contract(
            address=hook_address, abi=artifact("OasisDynamicFeeHook")["abi"]
        )
        self.contracts["hook"] = hook.address
        key = (self.token0.address, self.token1.address, 0x800000, 60, hook.address)
        self.call(
            "configure",
            hook.functions.configurePool(
                key, (500, 2000, 5000, 10**18, 10**19, 10**18, 10**19, 1, True)
            ),
        )
        self.call("initialize", self.manager.functions.initialize(key, 1 << 96))
        self.call("whitelist", hook.functions.setWhitelist(self.account.address, True))
        deadline = int(self.w3.eth.get_block("latest")["timestamp"]) + 3600
        for token in (self.token0, self.token1):
            self.call("approve-token", token.functions.approve(permit2.address, CAPITAL))
            self.call(
                "approve-permit2",
                permit2.functions.approve(token.address, positions.address, CAPITAL, deadline),
            )
        token_id = positions.functions.nextTokenId().call()
        pair_params = [encode(["address"], [token.address]) for token in (self.token0, self.token1)]
        before = self.balances()
        mint = encode(
            [
                "(address,address,uint24,int24,address)",
                "int24",
                "int24",
                "uint256",
                "uint128",
                "uint128",
                "address",
                "bytes",
            ],
            [key, -120, 120, LIQUIDITY, CAPITAL, CAPITAL, self.account.address, b""],
        )
        self.call(
            "mint",
            positions.functions.modifyLiquidities(self.plan(2, [mint, *pair_params]), deadline),
        )
        if (
            positions.functions.ownerOf(token_id).call() != self.account.address
            or positions.functions.getPositionLiquidity(token_id).call() != LIQUIDITY
        ):
            raise ValueError("mint NFT mismatch")
        self.conserved(before)
        swapper = self.deploy("PoolSwapTest", self.manager.address)
        for token in (self.token0, self.token1):
            self.call("approve-swap", token.functions.approve(swapper.address, CAPITAL))
        for direction, amount in (
            (True, -(10**10)),
            (False, -(10**10)),
            (True, 10**9),
            (False, 10**9),
        ):
            before = self.balances()
            limit = 4295128740 if direction else 1461446703485210103287273052203988822378723970341
            self.call(
                "swap", swapper.functions.swap(key, (direction, amount, limit), (False, False), b"")
            )
            self.conserved(before)
        before = self.balances()
        collect = encode(
            ["uint256", "uint256", "uint128", "uint128", "bytes"], [token_id, 0, 0, 0, b""]
        )
        self.call(
            "collect",
            positions.functions.modifyLiquidities(self.plan(1, [collect, *pair_params]), deadline),
        )
        self.conserved(before)
        executor = self.deploy(
            "OasisPositionExecutor",
            self.manager.address,
            positions.address,
            self.account.address,
            self.account.address,
            LIQUIDITY,
        )
        self.call("approve-nft", positions.functions.approve(executor.address, token_id))
        new_id = positions.functions.nextTokenId().call()
        before = self.balances()
        intent = (
            1,
            deadline,
            0,
            key,
            token_id,
            -180,
            180,
            LIQUIDITY // 10,
            0,
            0,
            CAPITAL,
            CAPITAL,
            0,
            b"",
        )
        self.call("recenter", executor.functions.execute(intent))
        self.conserved(before)
        if (
            positions.functions.ownerOf(new_id).call() != self.account.address
            or positions.functions.getPositionLiquidity(new_id).call() != LIQUIDITY // 10
        ):
            raise ValueError("recenter NFT mismatch")
        self.call("revoke-whitelist", hook.functions.setWhitelist(self.account.address, False))
        increase = encode(
            ["uint256", "uint256", "uint128", "uint128", "bytes"],
            [new_id, 1, CAPITAL, CAPITAL, b""],
        )
        try:
            positions.functions.modifyLiquidities(
                self.plan(0, [increase, *pair_params]), deadline
            ).call({"from": self.account.address})
        except ContractLogicError:
            # Confirm RPC still serves coherent state; a provider failure is not a revert proof.
            if (
                self.w3.eth.chain_id != self.chain
                or hook.functions.isWhitelisted(self.account.address).call()
            ):
                raise ValueError("revocation could not be verified") from None
        else:
            raise ValueError("revoked owner could add liquidity")
        self.call("approve-new-nft", positions.functions.approve(executor.address, new_id))
        before = self.balances()
        remove = (2, deadline, 1, key, new_id, -180, 180, 0, 0, 0, 0, 0, 0, b"")
        self.call("remove", executor.functions.execute(remove))
        self.conserved(before)
        if positions.functions.getPositionLiquidity(new_id).call() != 0:
            raise ValueError("liquidity remains after removal")
        for token in (self.token0, self.token1):
            self.call(
                "revoke-permit2", permit2.functions.approve(token.address, positions.address, 0, 0)
            )
            self.call("revoke-token", token.functions.approve(permit2.address, 0))
            self.call("revoke-swap", token.functions.approve(swapper.address, 0))
            if (
                token.functions.allowance(self.account.address, permit2.address).call() != 0
                or token.functions.allowance(self.account.address, swapper.address).call() != 0
            ):
                raise ValueError("ERC20 allowance remains")
            if (
                permit2.functions.allowance(
                    self.account.address, token.address, positions.address
                ).call()[0]
                != 0
            ):
                raise ValueError("Permit2 allowance remains")
        final_balance = self.w3.eth.get_balance(self.account.address)
        if self.initial_balance - final_balance != self.spent:
            raise ValueError("native gas balance residual")
        head = self.w3.eth.get_block("latest")
        runtimes = {
            name: Web3.to_hex(keccak(self.w3.eth.get_code(address, head["number"])))
            for name, address in self.contracts.items()
        }
        if self.w3.eth.get_block(head["number"])["hash"] != head["hash"]:
            raise ValueError("runtime snapshot reorg")
        self.audit(
            "complete",
            {
                "contracts": self.contracts,
                "runtime_hashes": runtimes,
                "source": self.url,
                "block": head["number"],
                "block_hash": Web3.to_hex(head["hash"]),
                "spent_wei": self.spent,
                "final_balance": final_balance,
                "execution_eligible": False,
                "strategy_profitability": "unresolved",
            },
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--network", choices=("local", "testnet"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--broadcast", action="store_true")
    args = parser.parse_args()
    if not args.broadcast:
        parser.error("explicit --broadcast required for this isolated lifecycle test")
    runner = None
    try:
        runner = Lifecycle(args.network, args.output)
        runner.run()
    except Exception:  # noqa: BLE001 -- isolate all signer errors; never emit exception secrets
        if runner is not None:
            runner.audit(
                "halt",
                {"reason": "test_failed_details_redacted", "broadcast_may_have_occurred": True},
            )
        print("LP E2E halted; inspect public journal; exception details suppressed")
        raise SystemExit(1) from None
    print(
        f"LP E2E passed on chain {runner.chain}; gas spent {runner.spent} wei; evidence {args.output}"
    )


if __name__ == "__main__":
    main()
