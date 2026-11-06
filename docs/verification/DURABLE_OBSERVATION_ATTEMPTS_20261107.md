# Durable autonomous observation attempts — 2026-11-07

The live strategy previously kept its consecutive full-observation failure
counter only in process memory. A crash and restart during or after a failed
observation could restore its retry allowance while RPC charges remained
durable.

The strategy now journals a bounded observation reservation before beginning
full NAV acquisition. Three unsuccessful or interrupted full observations
latch HALT before a fourth observation can start. A complete observation
resets the counter; a light in-range check does not. A deterministic NAV refusal
still halts immediately. Original local checkpoint verification now precedes
settlement RPC reads. STOP, signing-attempt handling and existing receipt
recovery keep their precedence. The paired rearm path resets the counter only
through its existing fresh NAV, receipt history and explicit apply checks.

Old journal payloads without the optional field decode at zero. The read-only
source-compatibility run used the two stopped services' original volumes in a
network-disabled, keyless container. It exited 0, replayed both complete action
histories and reproduced the five-receipt digest
`ca219b0937d5a4334d646fee01bb5c28cf341bc0e5fc9753d3b3c6dff19394ab`. Its report self-hash is
`f31e77efafbfa7b7e42aabf59dc23e81b15afb733a2ed9ea1b66ac95ef82120d`; raw output is retained in
`reports/review-20261107/durable-observation/history-v2.stdout`. Original volumes
were mounted read-only. No production state was migrated.

Synthetic regressions interrupt each full observation after its durable
reservation, reconstruct a loop and journal reader, and verify that exhaustion
halts with no fourth observation. The paired rearm regression checks both
counters reset without changing old journal rows, original budgets or STOP.
Earlier lint line-length and sandbox cache failures were corrected or rerun
with caching disabled. No production RPC, key access, signature, broadcast,
service start or policy change occurred in this increment.

Real read-only receipts and burn simulation are recorded in
[LIVE_PAID_EVIDENCE_20261107.md](LIVE_PAID_EVIDENCE_20261107.md).
Fresh NAV is still blocked. [Chainlink's freshness guidance](https://docs.chain.link/data-feeds/overview)
makes the consumer responsible for checking the answer timestamp; paying for
another RPC cannot update that timestamp. A separate source requires its own
authenticated fresh package and review before it can affect production. The
[RedStone EVM examples](https://github.com/redstone-finance/redstone-evm-examples)
describe authenticated gateways for signed packages, and
[Pyth's current upgrade documentation](https://docs.pyth.network/pro/pyth-core-upgrade)
requires a Hermes API key. No corresponding credential was available in the
current shell; the project's strategy environment was not searched or changed.

Full `make live-quality` exited 0: **4716 tests**, **138 subtests**, 11 warnings,
251 type-checked files and eight frontend modules. The secrets check made no
credential verification requests, Bandit reported zero medium/high findings,
and the Compose check made zero network calls. All 680 frozen inputs remained
unchanged. Report self-hash: `983f97b43a5cf6bc744df10160571a8b82114af3e189acd75e625476f4580069`;
outer exit record: `317370a82a07fc0f4c386b25fd99eb64a16d54026f84be722912f56ec70c20cf`.
