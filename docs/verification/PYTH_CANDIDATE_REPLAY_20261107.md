# Pyth candidate wire replay, 2026-11-07

The offline verifier replays five retained Solana mainnet exchanges per source.
It binds the account requests to the sponsored ETH and USDC accounts, the
receiver and push programs, the receiver config, and the ProgramData pointers
derived from the opening program accounts. Each account response is paired
with the finalized block named by its context slot. The verifier checks
response digests and request IDs, rejects deployment or config changes during
capture, and retains both context identities and the account byte digests.
The account digests are in request order: ETH, USDC, receiver, push, config,
then receiver and push ProgramData in the closing observation.

Historical inspection, with no RPC:

```sh
python -m scripts.verify_pyth_candidate --wire reports/review-20261107/pyth-solana-official-observation/attempt/wire.jsonl
```

For a second independently collected capture, pass `--wire` twice and
`--at-ms` with the explicit Unix millisecond review clock. The comparison
requires equal closing price, deployment and config bytes, rejects a copied
capture that only changes source alias, and rejects disagreement at any shared
slot. It distinguishes agreement at the same finalized slot from matching
bytes at distinct slots. A comparison before either capture was complete or
after a price or block exceeds 1,800 seconds refuses. The CLI does not
authenticate the clock, provider independence, Solana state, Wormhole
signatures, receiver trust config, or the deployed build. Its output is
candidate evidence only; all oracle, NAV and execution acceptance flags stay
false. No production reader or release gate consumes this report.

Confirmed by replay of the retained official response: closing slot
`466206055`, block hash `BhApE6V4yPU6UJUYRKWS8dFAW6fzVZMhSCcXU7mqET8X`;
observed ETH and USDC ages were 57,606 ms and 42,606 ms, respectively.
Original wire SHA-256:
`31f1f311ae2a860ea850dbb6225879ee85cf617d634399a2a6f9d4dd44eeb218`.
Single-source replay evidence hash:
`25e83821bedd4be0f921060dc2a63b9c42994e0e5a67d5bc8001846576585d0f`.
These ages describe the historical collection only. There is no second
successful capture, no fresh complete production NAV, and no release
approval from this work. No RPC, budget changes, signing, or broadcasts were
needed for replay. Earlier receipts, simulations, failures and raw evidence
remain documented in [the previous evidence record](LIVE_SETTLEMENT_AND_PYTH_20261107.md).

The first full quality attempt passed Python and frontend checks, then exited
2 at the secret gate. It flagged three public chain identifiers and the
standard Base58 alphabet. Only their exact file/type/value fingerprints were
added to the reviewed public-value baseline. The first gate's original
logs remain under `reports/review-20261107/pyth-candidate-replay-quality/`;
the outer exit record self-hash is
`1dd4717866120d8a4b1fc256cb5cb7483a8b90944ae646691b58888cfbd50f0d`.
The targeted secret check then exited 0 without credential verification.

Final `make live-quality` exited 0: 4,730 tests and 138 subtests,
eight frontend modules, and each of the secret, Bandit and Compose checks
exited 0. All 683 frozen inputs matched; Bandit reported no medium/high
issues and the Compose gate recorded zero network calls. The report
self-hash is
`8069574e51bcd07ebba45f3070fd866467f5c11d670a89e2eb626ebb9e91b4a4`,
and the outer record self-hash is
`458a88061fa4317ff3d3069dd7b952c4a60605094826049b4e7da8b644a99759`.
The default local startup readiness check separately exited 3 with
`gas_oracle_runtime_pins_missing`; no service was started.
