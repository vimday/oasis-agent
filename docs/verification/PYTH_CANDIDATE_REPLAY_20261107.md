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

## Receiver configuration and upgrade authority

Replay now emits schema `oasis.pyth-candidate-replay.v2`. It decodes the
receiver configuration in both observations using the [pinned Pyth receiver SDK
layout](https://github.com/pyth-network/pyth-crosschain/blob/c2b6cf416412b3282831e67f62b43fea496823d8/target_chains/solana/pyth_solana_receiver_sdk/src/config.rs).
The reviewed upstream file has SHA-256
`1c232967e9c53dea254053c708e6d44f0d49377b5d16b45ef49efed0bacbfbe8`.
The decoder bounds the emitter vector by the account allocation and retains a
digest of the unused tail. The historical account has nonzero bytes in that
tail. The full account digest continues to cover those bytes. Each loader
ProgramData entry also exposes its deployment slot, upgrade authority and a
digest of its allocated bytes after the loader header.

The historical official capture reported one emitter on chain 26, zero update
fee, and minimum signatures of 3. The emitter, fee and configured Wormhole
program agree with [Pyth governance proposal OP-PIP-131](https://forum.pyth.network/t/passed-op-pip-131-pyth-core-deprecation-svm/2671).
The receiver governance authority and both program upgrade authorities resolve
to `6oXTdojyfDS8m5VtTaYB9xRCxpKGSvKJFndLUPV3V3wT`. The receiver and push
programs reported last deployment slots 327047637 and 261073328 respectively.
The v2 replay hash for the original wire is
`8743f4a99aefdb83be2482adfd142d7a787e838a3b9e62495fb937f1fbdd914d`.
The earlier v1 report and digest are retained above.

Trust review still requires fresh observations from independent providers, the
configured bridge program and its ProgramData, the bridge state and active
guardian set, and verification of the deployed builds. The receiver config's
minimum signature count is a stored setting. The report keeps
`receiver_trust_verified`, `oracle_validated`, `nav_verified` and
`execution_eligible` false. Release still requires approved runtime pins and
a fresh, reconciled full NAV and recovery proof on Base.

The initial focused checks rejected the older synthetic configuration
placeholder. A subsequent test assertion used an incorrect tail length for the
two-emitter option case. After updating the fixture and correcting that
assertion, the Pyth suite passed 72 tests. Offline `make contract-check` exited
0 with 70 tests, zero failures and zero skips. The contract log SHA-256 is
`2adf31e0e50d0ad75a9efb3750317fb8a307dc8505008b56d2e79521ff5121b1`.

The [Pocket Network public RPC](https://docs.pocket.network/developers/supported-chains/)
was evaluated once as a new peer. Three free requests returned HTTP 200 and
the correct genesis. Its finalized slot 466127999 reported block time
1793972060, which was 19,807,932 ms behind the retrieval clock. ETH and USDC
publish ages were 19,852,932 ms and 19,860,932 ms. Replaying both accounts
reproduced the staleness refusal. The first provider stopped with exit 3
before ProgramData acquisition. The paired official request count was zero.
Raw evidence is in `reports/review-20261107/pyth-fresh-peer-pocket_public/`.
The terminal report and wire SHA-256 hashes are
`ac237b9e05b47d2e9b7efb9529bd73b61c5f2668b352f333e5ebba8816151969` and
`8d901ea8368ccdf535cbc47c30439ecedc829325896c0364a10b9fdb4cc00282`.

The final `make live-quality` exited 0 with 4,739 tests and 138 subtests.
All 683 frozen inputs matched. Python and frontend, secret scanning, Bandit,
and offline Compose each exited 0. The quality and outer evidence hashes are
`c744fc4c1cfdca82234cd13dc35190e747a91c38b1a6d8ab68fc4d00c24ba4fe` and
`fa529cb198e07edbb31182ca75f7e7b159063f11d6f04bc41bee07c02c6fa94b`.
