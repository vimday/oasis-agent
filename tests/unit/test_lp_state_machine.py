import unittest

from oasis_agent.errors import ExecutionBlockedError
from oasis_agent.execution.lp_state_machine import (
    LpExecutionLedger,
    LpExecutionStateMachine,
    LpIntent,
    LpIntentMode,
    LpState,
    RuntimePin,
    RuntimeRole,
    TickRange,
)


def make_intent(**overrides: object) -> LpIntent:
    base = {
        "intent_id": "rebalance-1",
        "chain_id": 4663,
        "pool_id": "0x" + "ab" * 32,
        "position_token_id": 7,
        "mode": LpIntentMode.RECENTER,
        "target": "0x" + "44" * 20,
        "calldata_hash": "0x" + "56" * 32,
        "old_range": TickRange(-120, 120, 60),
        "new_range": TickRange(-180, 60, 60),
        "liquidity": 100_000,
        "amount0_min": 100_000,
        "amount1_min": 200_000,
        "amount0_max": 1_000_000,
        "amount1_max": 2_000_000,
        "max_swap_input": 0,
        "max_fee_pips": 50_000,
        "max_gas_units": 3_000_000,
        "nonce": 3,
        "state_block": 46_440_623,
        "state_hash": "0x" + "cd" * 32,
        "deadline_timestamp": 1_800_000_000,
        "config_hash": "0x" + "ef" * 32,
        "runtime_pins": (
            RuntimePin(RuntimeRole.EXECUTOR, "0x" + "44" * 20, "0x" + "12" * 32),
            RuntimePin(RuntimeRole.POSITION_MANAGER, "0x" + "55" * 20, "0x" + "34" * 32),
            RuntimePin(RuntimeRole.POOL_MANAGER, "0x" + "66" * 20, "0x" + "56" * 32),
        ),
    }
    base.update(overrides)
    return LpIntent(**base)  # type: ignore[arg-type]


class LpIntentTests(unittest.TestCase):
    def test_intent_hash_is_stable_and_nonce_sensitive(self) -> None:
        first = make_intent()
        second = make_intent()
        self.assertEqual(first.intent_hash, second.intent_hash)
        self.assertNotEqual(first.intent_hash, make_intent(nonce=4).intent_hash)

    def test_unaligned_ticks_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "align"):
            make_intent(new_range=TickRange(-185, 60, 60))

    def test_invalid_intent_fields_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "positive"):
            make_intent(chain_id=0)
        with self.assertRaisesRegex(ValueError, "nonnegative"):
            make_intent(max_swap_input=-1)
        with self.assertRaises(TypeError):
            make_intent(nonce=True)  # type: ignore[arg-type]
        with self.assertRaisesRegex(ValueError, "runtime_pins"):
            make_intent(runtime_pins=())


class LpStateMachineTests(unittest.TestCase):
    def test_full_lifecycle_to_closed(self) -> None:
        machine = LpExecutionStateMachine(make_intent())
        self.assertEqual(machine.propose(), LpState.PROPOSED)
        self.assertEqual(machine.reserve(), LpState.RESERVED)
        self.assertEqual(machine.pin_simulated(True), LpState.PINNED_SIMULATED)
        self.assertEqual(machine.policy_approve(True), LpState.POLICY_APPROVED)
        self.assertEqual(machine.sign(), LpState.SIGNED)
        self.assertEqual(machine.submit("0x" + "99" * 32), LpState.SUBMITTED)
        self.assertEqual(machine.soft_confirm(), LpState.SOFT_CONFIRMED)
        self.assertEqual(machine.reconcile(), LpState.RECONCILING)
        self.assertEqual(machine.post_l1(), LpState.L1_POSTED)
        self.assertEqual(machine.finalize(), LpState.FINALIZED)
        self.assertEqual(machine.close(), LpState.CLOSED)
        self.assertTrue(machine.consumed)
        self.assertEqual(len(machine.history), 12)

    def test_expire_from_observed_or_proposed(self) -> None:
        machine = LpExecutionStateMachine(make_intent())
        self.assertEqual(machine.expire(), LpState.EXPIRED)
        with self.assertRaisesRegex(ExecutionBlockedError, "invalid"):
            machine.propose()

    def test_halt_then_only_closed(self) -> None:
        machine = LpExecutionStateMachine(make_intent())
        machine.propose()
        machine.reserve()
        self.assertEqual(machine.pin_simulated(False), LpState.HALTED)
        # Only verified risk reduction (closing/unwinding) may proceed.
        with self.assertRaisesRegex(ExecutionBlockedError, "invalid"):
            machine.reserve()
        self.assertEqual(machine.close(), LpState.CLOSED)

    def test_halt_from_uncertain_submitted_state(self) -> None:
        machine = LpExecutionStateMachine(make_intent())
        machine.propose()
        machine.reserve()
        machine.pin_simulated(True)
        machine.policy_approve(True)
        machine.sign()
        machine.submit("0x" + "77" * 32)
        self.assertEqual(machine.halt("receipt missing"), LpState.HALTED)
        with self.assertRaisesRegex(ExecutionBlockedError, "invalid"):
            machine.expire()

    def test_policy_denial_halts(self) -> None:
        machine = LpExecutionStateMachine(make_intent())
        machine.propose()
        machine.reserve()
        machine.pin_simulated(True)
        self.assertEqual(machine.policy_approve(False), LpState.HALTED)

    def test_invalid_transition_is_blocked(self) -> None:
        machine = LpExecutionStateMachine(make_intent())
        machine.propose()
        with self.assertRaisesRegex(ExecutionBlockedError, "proposed -> submitted"):
            machine.submit("0x" + "55" * 32)

    def test_submit_requires_transaction_hash(self) -> None:
        machine = LpExecutionStateMachine(make_intent())
        machine.propose()
        machine.reserve()
        machine.pin_simulated(True)
        machine.policy_approve(True)
        machine.sign()
        with self.assertRaisesRegex(ValueError, "transaction hash"):
            machine.submit("")

    def test_transaction_hash_is_recorded(self) -> None:
        machine = LpExecutionStateMachine(make_intent())
        machine.propose()
        machine.reserve()
        machine.pin_simulated(True)
        machine.policy_approve(True)
        machine.sign()
        machine.submit("0x" + "42" * 32)
        self.assertEqual(machine.transaction_hash, "0x" + "42" * 32)


class LpExecutionLedgerTests(unittest.TestCase):
    def test_consumed_intent_cannot_create_second_rebalance(self) -> None:
        ledger = LpExecutionLedger()
        intent = make_intent()
        machine = ledger.create(intent)
        machine.propose()
        machine.reserve()
        machine.pin_simulated(True)
        machine.policy_approve(True)
        machine.sign()
        machine.submit("0x" + "11" * 32)
        self.assertTrue(ledger.is_consumed(intent.intent_hash))
        with self.assertRaisesRegex(ExecutionBlockedError, "already consumed"):
            ledger.create(intent)

    def test_unconsumed_retry_returns_same_machine(self) -> None:
        ledger = LpExecutionLedger()
        intent = make_intent()
        first = ledger.create(intent)
        first.propose()
        second = ledger.create(intent)
        self.assertIs(first, second)
        self.assertFalse(ledger.is_consumed(intent.intent_hash))


if __name__ == "__main__":
    unittest.main()


def test_submitted_consumption_survives_halt_and_expiry():
    for terminal in (LpState.HALTED, LpState.EXPIRED):
        ledger = LpExecutionLedger()
        intent = make_intent()
        machine = ledger.create(intent)
        machine.propose()
        machine.reserve()
        machine.pin_simulated(True)
        machine.policy_approve(True)
        machine.sign()
        machine.submit("0x" + "99" * 32)
        machine.transition(terminal)
        assert machine.consumed
        assert ledger.is_consumed(intent.intent_hash)
        with unittest.TestCase().assertRaises(ExecutionBlockedError):
            ledger.create(intent)


def test_rejected_submit_does_not_mutate_transaction_identity():
    machine = LpExecutionStateMachine(make_intent())
    with unittest.TestCase().assertRaises(ExecutionBlockedError):
        machine.submit("0x" + "99" * 32)
    assert machine.transaction_hash is None
    assert machine.state is LpState.OBSERVED


def test_canonical_tick_bounds():
    for bounds in ((-887273, 0, 1), (0, 887273, 1), (0, 32768, 32768)):
        with unittest.TestCase().assertRaises(ValueError):
            TickRange(*bounds)


def test_intent_rejects_malformed_hashes_and_evm_overflow():
    cases = (
        {"pool_id": "not-a-hash"},
        {"state_hash": "0x01"},
        {"config_hash": "0x01"},
        {"runtime_pins": ()},
        {"max_fee_pips": 1_000_001},
        {"amount0_max": 1 << 128},
        {"nonce": 1 << 256},
        {"new_range": TickRange(-120, 120, 30)},
    )
    for case in cases:
        with unittest.TestCase().assertRaises(ValueError):
            make_intent(**case)


def test_boolean_policy_flags_cannot_be_truthy_strings():
    machine = LpExecutionStateMachine(make_intent())
    machine.propose()
    machine.reserve()
    with unittest.TestCase().assertRaises(TypeError):
        machine.pin_simulated("false")
    assert machine.state is LpState.RESERVED
    machine.pin_simulated(True)
    with unittest.TestCase().assertRaises(TypeError):
        machine.policy_approve("false")
    assert machine.state is LpState.PINNED_SIMULATED
