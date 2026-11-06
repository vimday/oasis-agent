"""Full-transaction authority intended to run outside the strategy process.

No digest-signing RPC is exposed. The authority owns its configuration, account
journal and raw evidence, replays a fresh NAV, re-derives the exact permitted
strategy action, and reserves the nonce before contacting its key provider.
"""

import time
from dataclasses import asdict
from fractions import Fraction
from typing import Any

from ..models.common import BlockRef, EvidenceRef, canonical_json, sha256_hex
from ..reporting.live_release import require_live_action
from .action_plan import action_call
from .authority_simulation import simulate
from .chain import ChainError, DigestSigner, sign_transaction
from .gas_oracle import require_gas_oracle_pins
from .intent import SubmittedTransaction, pending_plan_hash
from .recovery import authority_status
from .runner import LiveLoop, Market
from .state import Pending, day_of
from .valuation import nav_fresh_until, replay_nav


class TransactionAuthority:
    def __init__(self, loop: LiveLoop, provider: DigestSigner, *, ttl_seconds: int = 3600) -> None:
        if loop.mode != "live" or loop.secondary is None or loop.receipt_costs is None:
            raise ValueError("authority requires independent live reconciliation")
        if loop.signer is not None or provider.account != loop.c.account:
            raise ValueError("authority must own a separate signing provider")
        loop.c.validate()
        loop.store.bind(sha256_hex(canonical_json(asdict(loop.c))))
        loop.state = loop.store.load("live")
        if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 86400:
            raise ValueError("bounded authority lifetime required")
        self.expires = time.monotonic() + ttl_seconds
        self.loop, self.provider = loop, provider
        self.simulation_hashes: tuple[str, str] | None = None
        self.simulation_evidence: tuple[EvidenceRef, EvidenceRef] | None = None

    def status(self) -> dict[str, Any]:
        return authority_status(self.loop.store, self.loop.c)

    def status_for_nonce(self, nonce: int) -> dict[str, Any]:
        return authority_status(self.loop.store, self.loop.c, nonce)

    def sign(self, fields: list[object], context: dict[str, Any]) -> bytes:
        # The signing boundary must enforce release independently of its caller.
        # Status and receipt recovery remain available without new signing authority.
        # Before state or RPC, the proposal's own kind/nonce may only match the permit;
        # the captured account nonce is bound to it again before key use.
        require_live_action(
            context.get("kind") if isinstance(context, dict) else None,
            fields[1] if isinstance(fields, list) and len(fields) == 9 else None,
        )
        loop, c = self.loop, self.loop.c
        loop.state = loop.store.load("live")
        s = loop.state
        if time.monotonic() >= self.expires or (
            loop.stop_file is not None and loop.stop_file.exists()
        ):
            raise ValueError("authority expired or stopped")
        if s.halted or s.signing_attempt is not None or s.legacy_gas_cost_incomplete:
            raise ValueError("authority halted or signing outcome unresolved")
        try:
            if s.pending:
                loop._poll_pending()
                loop.store.save(s)
                if s.pending or s.halted:
                    raise ValueError("previous authority transaction unresolved")
            loop._check_settlements()
            loop._check_nav_checkpoint()
        except ChainError:
            # Receipt/ancestry checks can latch HALT before raising. Persist it
            # before the socket refuses this request: the next one reloads state.
            if s.halted:
                loop.store.save(s)
            raise
        require_gas_oracle_pins(c.expected_pins)
        if (
            set(context) != {"kind", "intent", "block", "block_hash"}
            or type(context["block"]) is not int
            or context["block"] < 0
            or not isinstance(context["intent"], dict)
        ):
            raise ValueError("invalid transaction proposal")
        header = loop.rpc.call("eth_getBlockByNumber", [hex(context["block"]), False])
        if not isinstance(header, dict) or header.get("hash") != context["block_hash"]:
            raise ValueError("proposed decision block is not canonical")
        try:
            market = loop.observe(header)
        except ChainError:
            if s.halted:
                loop.store.save(s)
            raise
        reader = loop.receipt_costs
        assert reader is not None
        deadline = nav_fresh_until(reader.store, market.nav_evidence, c, sources=reader.sources)
        try:
            self._admit(market, context)
            self._envelope(fields, market, context)
        except (ValueError, ChainError):
            # Persist risk latches and observations even when the proposal is refused.
            loop.store.save(s)
            raise
        opening = tuple(replay_nav(reader.store, ref, c).wallet for ref in market.nav_evidence)
        if len(opening) != 2:
            raise ValueError("authority requires its own opening proof pair")
        s.signing_attempt = {
            "nonce": fields[1],
            "request_hash": sha256_hex(canonical_json(context)),
            "block": market.block,
            "block_hash": market.block_hash,
            "simulation_hashes": self.simulation_hashes,
            "simulation_evidence": (
                [asdict(ref) for ref in self.simulation_evidence]
                if self.simulation_evidence is not None
                else None
            ),
        }
        loop.store.save(s)
        # This is the only key-provider call, after durable admission. Any exception
        # leaves the reservation intact, including a crash after key use.
        if (
            time.monotonic() >= self.expires
            or not market.timestamp <= int(time.time()) <= deadline
            or (loop.stop_file is not None and loop.stop_file.exists())
        ):
            raise ValueError("authority expired before key use")
        # Recheck after slow observation/simulation and reservation, bound to the nonce
        # the authority itself captured from both providers (enforced in _envelope).
        assert market.valuation is not None
        require_live_action(context["kind"], market.valuation.wallet.nonce)
        raw = sign_transaction(self.provider, fields)
        tx = SubmittedTransaction.from_signed(raw)
        s.pending = Pending(
            context["kind"],
            tx.transaction_hash,
            tx.nonce,
            int(time.time()),
            dict(context["intent"]),
            tx.target,
            tx,
            pending_plan_hash(
                asdict(c),
                context["kind"],
                context["intent"],
                tx,
                asdict(s.position) if s.position else None,
                tuple(asdict(r) for r in opening),
            ),
            opening,
        )
        s.signing_attempt = None
        loop.store.save(s)
        return raw

    def _admit(self, m: Market, context: dict[str, Any]) -> None:
        loop, c, s = self.loop, self.loop.c, self.loop.state
        value = m.valuation
        if value is None:
            raise ValueError("independent exact NAV missing")
        if s.last_units is not None and s.last_units != m.units:
            s.halted = "authority account movement is unexplained"
            raise ValueError(s.halted)
        if s.acknowledge_flow:
            raise ValueError("authority requires external flows reconciled before proposals")
        risk = s.integer_risk()
        today = day_of(m.timestamp)
        if risk.day != today:
            s.actions_today = 0
        daily, cumulative = risk.observe(
            day=today, at=m.timestamp, nav=value.nav_micros, checkpoint=s.checkpoint_at == 0
        )
        s.checkpoint_at = risk.checkpoint_at
        s.last_units = m.units
        s.nav_evidence = m.nav_evidence
        if (
            daily >= c.max_daily_loss_usd * 1_000_000
            or cumulative >= c.max_cumulative_loss_usd * 1_000_000
        ):
            s.halted = "authority loss budget exhausted"
            raise ValueError(s.halted)
        if s.actions_today >= c.max_actions_per_day or m.deviation_bps > c.max_oracle_deviation_bps:
            raise ValueError("authority action budget or price deviation exceeded")
        if not loop._pins_ok(m):
            raise ValueError("authority runtime policy refused")
        expected = loop._decide(m)
        if s.halted or expected != (context["kind"], context["intent"]):
            raise ValueError("proposal differs from independently derived bounded action")
        # Reserve the full configured native gas reserve as the conservative
        # transaction-cost allowance, in addition to committed investment.
        gas_usd = Fraction(c.gas_reserve_wei * value.eth_answer, 10**20)
        commitment = Fraction(value.position_micros)
        args = context["intent"]
        if context["kind"] == "mint":
            commitment += Fraction(args["amount0_max"] * value.eth_answer, 10**20) + Fraction(
                args["amount1_max"] * value.usdc_answer, 10**8
            )
        elif context["kind"] == "swap":
            commitment += (
                Fraction(args["amount_in"] * value.eth_answer, 10**20)
                if args["zero_for_one"]
                else Fraction(args["amount_in"] * value.usdc_answer, 10**8)
            )
        if context["kind"] != "burn" and commitment + gas_usd > c.max_capital_usd * 1_000_000:
            raise ValueError("authority committed capital plus gas reserve exceeds cap")
        if gas_usd >= min(
            c.max_daily_loss_usd * 1_000_000 - daily,
            c.max_cumulative_loss_usd * 1_000_000 - cumulative,
        ):
            raise ValueError("authority remaining loss budget cannot cover gas reserve")

    def _envelope(self, fields: list[object], m: Market, context: dict[str, Any]) -> None:
        loop, c = self.loop, self.loop.c
        if len(fields) != 9 or fields[8] != []:
            raise ValueError("only canonical EIP-1559 without access list is supported")
        chain, nonce, priority, maximum, gas, target, value, data, _ = fields
        for amount in (chain, nonce, priority, maximum, gas, value):
            if type(amount) is not int or not 0 <= amount < 2**256:
                raise ValueError("envelope integers must be canonical")
        assert isinstance(nonce, int) and isinstance(priority, int) and isinstance(maximum, int)
        assert isinstance(gas, int) and isinstance(value, int)
        if type(target) is not bytes or len(target) != 20 or type(data) is not bytes:
            raise ValueError("invalid transaction target/data")
        expected = action_call(
            c, m.timestamp, context["kind"], context["intent"], loop.state.position
        )
        if (
            chain != c.chain_id
            or ("0x" + target.hex(), data, value) != expected
            or not 0 < gas <= loop.fees.max_gas
            or not 0 <= priority <= c.priority_fee_wei
            or not priority <= maximum <= c.max_fee_per_gas_wei
            or maximum == 0
        ):
            raise ValueError("transaction differs from independently encoded intent or fee policy")
        if m.valuation is None or nonce != m.valuation.wallet.nonce:
            raise ValueError("transaction nonce differs from captured account")
        if m.eth - value < c.gas_reserve_wei or gas * maximum > c.gas_reserve_wei:
            raise ValueError("native value/gas reserve exceeded")
        tx = {
            "from": c.account,
            "to": "0x" + target.hex(),
            "data": "0x" + data.hex(),
            "value": hex(value),
            "gas": hex(gas),
            "maxFeePerGas": hex(maximum),
            "maxPriorityFeePerGas": hex(priority),
            "nonce": hex(nonce),
        }
        reader = loop.receipt_costs
        if reader is None:
            raise ValueError("raw simulation reader missing")
        self.simulation_evidence = simulate(
            reader,
            c,
            BlockRef(c.chain_id, m.block, m.block_hash, m.timestamp),
            tx,
            nonce=nonce,
            gas=gas,
            maximum=maximum,
        )
        self.simulation_hashes = (
            self.simulation_evidence[0].content_hash,
            self.simulation_evidence[1].content_hash,
        )
        if not 0 <= int(time.time()) - m.timestamp <= 120:
            raise ValueError("decision expired during authority checks")
