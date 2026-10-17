"""Local-only canonical direct action/cash regression against a pinned Base fork.

Creates an ephemeral, artificially funded key and broadcasts only to the Anvil
child bound to loopback. Two capture aliases exercise replay plumbing against the
same local node; this is explicitly NOT independent mainnet provider evidence.
"""

import argparse
import json
import shutil
import socket
import subprocess
import tempfile
import time
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from typing import Any

from eth_abi.abi import encode
from eth_account import Account
from eth_hash.auto import keccak

from oasis_agent.evidence.rpc_capture import BoundedExchangeRecorder
from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.live.action_cash import reconcile_action
from oasis_agent.live.action_plan import action_call
from oasis_agent.live.approval_cash import reconcile_approval
from oasis_agent.live.chain import Rpc, _envelope, sign_transaction
from oasis_agent.live.config import LiveConfig
from oasis_agent.live.intent import SubmittedTransaction
from oasis_agent.live.positions import collect_position_outcome
from oasis_agent.live.receipts import RetainedReceiptCostReader
from oasis_agent.live.state import Position
from oasis_agent.live.v4lp import liquidity_for_amounts, position_amounts, range_ticks
from oasis_agent.live.wallet import capture_wallet, replay_wallet
from oasis_agent.models.common import BlockRef, canonical_json
from oasis_agent.rpc.budget import BudgetedPoster, BudgetPolicy
from oasis_agent.rpc.transport import JsonRpcTransport, _UrlLibPoster


class LocalFeeFixture:
    """Anvil omits OP fee receipt fields. Augmentation is TEST DATA, never production RPC."""

    def __init__(self, url: str, directory: Path) -> None:
        if not url.startswith("http://127.0.0.1:"):
            raise ValueError("fee fixture is strictly loopback-only")
        self.http, self.directory = _UrlLibPoster(url, 15), directory

    def post(self, payload: bytes) -> bytes:
        response = self.http.post(payload)
        with (self.directory / "unmodified-anvil-rpc.jsonl").open("ab") as out:
            out.write(
                canonical_json({"request": payload.hex(), "response": response.hex()}) + b"\n"
            )
        request, reply = json.loads(payload), json.loads(response)
        if request["method"] == "eth_getTransactionReceipt" and isinstance(
            reply.get("result"), dict
        ):
            reply["result"].setdefault("l1Fee", "0x0")
        if (
            request["method"] == "eth_call"
            and request["params"][0]["to"] == "0x420000000000000000000000000000000000000f"
            and request["params"][0]["data"][:10]
            == "0x" + keccak(b"getOperatorFee(uint256)")[:4].hex()
        ):
            reply["result"] = "0x" + encode(["uint256"], [0]).hex()
        return canonical_json(reply)


class TestKey:
    def __init__(self) -> None:
        self._key = Account.create()
        self.account = self._key.address.lower()

    def sign(self, digest: bytes) -> tuple[int, int, int]:
        signed = Account.unsafe_sign_hash(digest, self._key.key)
        return signed.r, signed.s, signed.v - 27


def exercise(url: str, directory: Path) -> dict[str, Any]:
    rpc = Rpc(url, retries=1)
    if (
        not url.startswith("http://127.0.0.1:")
        or "anvil" not in str(rpc.call("web3_clientVersion", [])).lower()
    ):
        raise ValueError("local Anvil identity required before artificial funding/signing")
    signer = TestKey()
    c = replace(LiveConfig.load(Path("configs/live.base-eth-usdc.json")), account=signer.account)
    c.validate()
    initial = rpc.block()
    rpc.call("anvil_setBalance", [c.account, hex(10**18)])
    rpc.call("evm_mine", [])
    sources = ("local_fork_a", "local_fork_b")
    policies = tuple(BudgetPolicy(s, 4000, 0) for s in sources)

    def sessions(source: str):
        recorder = BoundedExchangeRecorder()
        return (
            JsonRpcTransport(
                "https://unused.invalid",
                http=BudgetedPoster(
                    LocalFeeFixture(url, directory), directory / "budget.db", policies, source
                ),
                retries=0,
                recorder=recorder,
            ),
            recorder,
            source,
        )

    with closing(EvidenceStore(directory / "evidence.db")) as store:
        costs = RetainedReceiptCostReader(
            store,
            chain_id=c.chain_id,
            account=c.account,
            native_decimals=18,
            sources=sources,
            sessions=sessions,
        )
        position = None
        actions = []

        def run(kind: str, terms: dict[str, Any]) -> dict[str, Any]:
            nonlocal position
            h = rpc.block()
            block = BlockRef(c.chain_id, int(h["number"], 16), h["hash"], int(h["timestamp"], 16))
            opening = capture_wallet(store, c, block, sources=sources, sessions=sessions)
            to, data, value = action_call(c, block.timestamp, kind, terms, position)
            nonce = rpc.nonce(c.account, "latest")
            fields = _envelope(
                c.chain_id,
                nonce,
                c.priority_fee_wei,
                c.max_fee_per_gas_wei,
                1_500_000,
                to,
                value,
                data,
            )
            raw = sign_transaction(signer, fields)
            tx = SubmittedTransaction.from_signed(raw)
            rpc.send_raw(raw)
            receipt = rpc.receipt(tx.transaction_hash)
            for _ in range(50):
                if receipt is not None:
                    break
                time.sleep(0.1)
                receipt = rpc.receipt(tx.transaction_hash)
            if receipt is None or receipt["status"] != "0x1":
                if receipt is not None:
                    trace = rpc.call(
                        "debug_traceTransaction", [tx.transaction_hash, {"tracer": "callTracer"}]
                    )
                    print(
                        {
                            "local_action": kind,
                            "status": receipt["status"],
                            "error": trace.get("error"),
                        },
                        flush=True,
                    )
                raise ValueError("local action missing or reverted")
            if kind == "approve_usdc":
                old = replay_wallet(store, opening[0]).reconstruct()
                new_balance = rpc.balance(c.account)
                print(
                    {
                        "local_receipt_keys": sorted(receipt),
                        "native_debit": old.native - new_balance,
                        "execution_gas": int(receipt["gasUsed"], 16)
                        * int(receipt["effectiveGasPrice"], 16),
                        "operator_fee": rpc.call_abi(
                            "0x420000000000000000000000000000000000000f",
                            "getOperatorFee(uint256)",
                            ["uint256"],
                            [int(receipt["gasUsed"], 16)],
                            ["uint256"],
                        )[0],
                    },
                    flush=True,
                )
            receipt.setdefault("l1Fee", "0x0")  # explicit local fee fixture only
            proof = costs.observe(receipt, transaction_hash=tx.transaction_hash, target=to)
            if kind.startswith("approve"):
                _, refs = reconcile_approval(
                    store,
                    c,
                    kind,
                    terms,
                    tx,
                    opening,
                    proof.evidence,
                    sources=sources,
                    sessions=sessions,
                )
            else:
                _, refs = reconcile_action(
                    store,
                    c,
                    kind,
                    terms,
                    position,
                    tx,
                    opening,
                    proof.evidence,
                    sources=sources,
                    sessions=sessions,
                )
            end = proof.evidence[0].block
            assert end is not None
            if kind == "mint":
                nft = [
                    log
                    for log in receipt["logs"]
                    if log["address"] == c.position_manager
                    and len(log["topics"]) == 4
                    and log["topics"][1] == "0x" + "0" * 64
                ]
                if len(nft) != 1:
                    raise ValueError("unique local mint missing")
                position = Position(
                    int(nft[0]["topics"][3], 16),
                    terms["lower"],
                    terms["upper"],
                    terms["liquidity"],
                    block.timestamp,
                )
            if kind in {"mint", "burn"}:
                assert position is not None
                collect_position_outcome(
                    store,
                    c,
                    end,
                    token_id=position.token_id,
                    lower=position.lower,
                    upper=position.upper,
                    liquidity=0 if kind == "burn" else position.liquidity,
                    burned=kind == "burn",
                    sources=sources,
                    sessions=sessions,
                )
                if kind == "burn":
                    position = None
            actions.append(
                {
                    "kind": kind,
                    "transaction_hash": tx.transaction_hash,
                    "block": end.number,
                    "block_hash": end.block_hash,
                    "cash_hashes": [r.content_hash for r in refs],
                    "native_cost": proof.cost.total_native,
                }
            )
            print({"local_action": kind, "cash_replayed": True}, flush=True)
            return receipt

        run("approve_usdc", {"amount": c.approval_usdc})
        for spender in (c.position_manager, c.universal_router):
            timestamp = int(rpc.block()["timestamp"], 16)
            run(
                "approve_permit2",
                {
                    "amount": c.approval_usdc,
                    "spender": spender,
                    "expiration": timestamp + c.approval_days * 86400,
                },
            )

        def slot() -> tuple[int, int]:
            p, t, _, _ = rpc.call_abi(
                c.state_view,
                "getSlot0(bytes32)",
                ["bytes32"],
                [bytes.fromhex(c.pool_id[2:])],
                ["uint160", "int24", "uint24", "uint24"],
            )
            return int(p), int(t)

        sqrt, _ = slot()
        amount = 10**16
        run(
            "swap",
            {
                "zero_for_one": True,
                "amount_in": amount,
                "min_out": amount * sqrt**2 * 99 // (2**192 * 100),
            },
        )
        sqrt, _ = slot()
        amount = rpc.erc20_balance(c.pool.currency1, c.account) // 3
        run(
            "swap",
            {
                "zero_for_one": False,
                "amount_in": amount,
                "min_out": amount * 2**192 * 99 // (sqrt**2 * 100),
            },
        )
        sqrt, tick = slot()
        lower, upper = range_ticks(tick, 490, 10)
        liquidity = (
            liquidity_for_amounts(
                sqrt, lower, upper, 3 * 10**15, rpc.erc20_balance(c.pool.currency1, c.account)
            )
            * 99
            // 100
        )
        a0, a1 = position_amounts(sqrt, lower, upper, liquidity)
        run(
            "mint",
            {
                "lower": lower,
                "upper": upper,
                "liquidity": liquidity,
                "amount0_max": a0 + 100_000,
                "amount1_max": a1 + 100,
            },
        )
        sqrt, _ = slot()
        a0, a1 = position_amounts(sqrt, lower, upper, liquidity)
        run("burn", {"amount0_min": a0 * 98 // 100, "amount1_min": a1 * 98 // 100})
    return {
        "schema": "oasis.local-action-fork.v1",
        "execution_eligible": False,
        "fork_block": int(initial["number"], 16),
        "fork_hash": initial["hash"],
        "artificial_funding": True,
        "independent_providers": False,
        "synthetic_base_fee_metadata": ["l1Fee=0", "operatorFee=0"],
        "production_fee_validation": False,
        "local_network_family": "ethereum",
        "actions": actions,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fork-url", required=True)
    parser.add_argument("--block", type=int, required=True)
    parser.add_argument("--block-hash", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    h = Rpc(args.fork_url).call("eth_getBlockByNumber", [hex(args.block), False])
    if not isinstance(h, dict) or h.get("hash") != args.block_hash:
        raise ValueError("upstream fork pin mismatch")
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    with tempfile.TemporaryDirectory(prefix="oasis-fork-") as temp:
        directory = Path(temp)
        with (directory / "anvil.log").open("w") as log:
            process = subprocess.Popen(
                [
                    "anvil",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                    "--fork-url",
                    args.fork_url,
                    "--fork-block-number",
                    str(args.block),
                    "--chain-id",
                    "8453",
                    "--network",
                    "ethereum",
                    "--silent",
                ],
                stdout=log,
                stderr=log,
            )
            try:
                url = f"http://127.0.0.1:{port}"
                for _ in range(30):
                    try:
                        Rpc(url, retries=1).block()
                        break
                    except (OSError, RuntimeError):
                        time.sleep(1)
                report = exercise(url, directory)
                args.out.parent.mkdir(parents=True, exist_ok=True)
                for filename in ("evidence.db", "budget.db", "unmodified-anvil-rpc.jsonl"):
                    shutil.copy2(
                        directory / filename, args.out.parent / (args.out.stem + "." + filename)
                    )
                args.out.write_text(json.dumps(report, indent=2) + "\n")
            finally:
                process.terminate()
                process.wait(timeout=10)


if __name__ == "__main__":
    main()
