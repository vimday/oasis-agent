"""Both isolated ledgers survive SIGKILL between receipt proof and state publication.

Synthetic raw receipts only. No production account, key, network or broadcast.
Socket lost-reply adoption is covered separately in test_live_process_recovery.
"""

import multiprocessing
from contextlib import closing
from pathlib import Path

from oasis_agent.evidence.store import EvidenceStore
from oasis_agent.live.receipts import RetainedReceiptCostReader
from oasis_agent.live.runner import LiveLoop
from oasis_agent.live.state import StateStore
from tests.unit.test_live_approval_cash import TEST_CONFIG, ApprovalFixture, approval_loop
from tests.unit.test_live_lp import ScriptedRpc
from tests.unit.test_live_process_recovery import stop
from tests.unit.test_lp_receipts import PRIMARY, SECONDARY, sessions


def receipt_worker(directory, ready, parked, crash):
    root = Path(directory)
    root.mkdir(exist_ok=True)
    with closing(EvidenceStore(root / "evidence.db")) as evidence:
        a, b = ApprovalFixture(), ApprovalFixture()
        if crash:
            loop = approval_loop(root, evidence, a, b)

            def before_publish(state):
                # Proof acquisition and in-memory accounting have finished, but
                # the old pending remains the durable truth until publication.
                assert state.pending is None and state.gas_spent_wei == 218
                assert state.actions_today == 1
                ready.put("proofs-retained-before-publication")
                parked.wait(30)
                raise AssertionError("parent failed to kill the test process")

            loop.store.save = before_publish
        else:
            loop = LiveLoop.recovery(
                TEST_CONFIG,
                ScriptedRpc(receipt=a._receipt()),
                StateStore(root / "live.state.json"),
                "live",
            )
            loop.secondary = ScriptedRpc(receipt=b._receipt())
            loop.receipt_costs = RetainedReceiptCostReader(
                evidence,
                chain_id=TEST_CONFIG.chain_id,
                account=TEST_CONFIG.account,
                native_decimals=18,
                sources=(PRIMARY, SECONDARY),
                sessions=sessions({PRIMARY: a, SECONDARY: b}),
            )
            assert loop.state.pending is not None and loop.state.gas_spent_wei == 0
        result = loop.tick()
        assert "confirmed" in result
        ready.put((loop.state.gas_spent_wei, loop.state.actions_today, loop.state.pending))


def test_two_private_ledgers_recover_receipt_after_process_death(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    roots = [tmp_path / role for role in ("authority", "strategy")]
    queues = [ctx.Queue() for _ in roots]
    parked = ctx.Event()
    processes = []
    try:
        for root, ready in zip(roots, queues, strict=True):
            process = ctx.Process(target=receipt_worker, args=(str(root), ready, parked, True))
            process.start()
            processes.append(process)
        for ready in queues:
            assert ready.get(timeout=15) == "proofs-retained-before-publication"
        for process in processes:
            stop(process)
        for root in roots:
            state = StateStore(root / "live.state.json").load("live")
            assert state.pending is not None and state.gas_spent_wei == 0
            assert state.actions_today == 0
        for root, ready in zip(roots, queues, strict=True):
            process = ctx.Process(target=receipt_worker, args=(str(root), ready, parked, False))
            process.start()
            processes.append(process)
        for ready in queues:
            assert ready.get(timeout=15) == (218, 1, None)
        for process in processes[2:]:
            process.join(timeout=5)
            assert process.exitcode == 0
        for root in roots:
            # Another reopen must not count a settled receipt twice.
            for _ in range(2):
                state = StateStore(root / "live.state.json").load("live")
                assert state.pending is None and state.gas_spent_wei == 218
                assert state.actions_today == 1 and len(state.history) == 1
    finally:
        for process in processes:
            stop(process)
        for ready in queues:
            ready.close()
            ready.join_thread()
