# Base launch urgent review — 2026-09-09 15:20 CST

## Follow-up: entry priority and RPC verification

The user subsequently emphasized acquiring qualifying low-FDV tokens. The working
tree now continues checking other pools and later snapshots after a high-FDV
quote instead of permanently abandoning tranche two. The cumulative 0.2 ETH
schedule remains unchanged. The buy-cycle boundary checks budget exhaustion,
stops, and unresolved submissions before attempting another order.

Fee discovery now starts all four fee tiers on both RPC sources concurrently,
preserves fee ordering and rejects conflicting pool identities. Four regression
tests failed before these changes and pass afterward. Full Python gate:
872 passed, 108 subtests passed, one existing warning. The design's second-tranche
description was updated to record the user's revised instruction.

RPC configuration inspection (no credentials retained): primary is an Alchemy
HTTPS endpoint with a custom credential path; secondary is Base's public endpoint.
The current broadcast method sends only through primary. Alchemy's documentation
states that standard Base endpoints include MEV protection:
https://www.alchemy.com/docs/reference/mev-protection . This does not establish
account-specific routing, inclusion order, latency or fill guarantees.

New diagnostic sample at Base block 51,074,726, hash
0x6d6d5d565a78d43af0b0c7cf5486fb4eb232674535d0e5cd0922987c14d216fc:
snapshot 1,291 ms, dual-source concurrent pool discovery 559 ms, first-pool
0.1 ETH quote failed in 130 ms. This is a single sample, not a latency guarantee.
An independent allowance probe encountered HTTP failure on the public secondary
RPC; the initial probe therefore did not establish current sell authorization.
The previous missing-approval finding describes missing execution code, not proof
that no approvals have previously been broadcast by another operator.

No live process was restarted and no transaction was signed by this follow-up.

Allowance retry succeeded at Unix time 1788938870: both sources agreed at Base
block 51,074,760, hash
0xb41a7e1798efc47442e6293d1434799b9eeea7e04389af122f5f6c02ae30928b.
ERC-20 allowance to Permit2 is max uint256; Permit2 allowance to the configured
Universal Router is max uint160, expiry 1791478885 (not expired at retrieval).
Thus preexisting sell authorization is present for this wallet at that block.
This corrects any interpretation of the earlier code-review finding as proof of
missing on-chain allowance. It does not prove token sellability or future approval
validity. No approval transaction was sent during this review.

User explicitly authorizes the minimally funded `.env` account for launch execution.
No private key was read by this review; no transaction was signed or broadcast.

## Implemented fixes in the shared working tree

- `sniping/monitor.py`: after durable signed intent, keep `submission_unknown` set
  until successful receipt AND accounting/journal completion. A settlement failure
  must not result in another buy of the same tranche.
- Read post-trade token balances from the receipt's canonical block hash on both
  RPC sources, rather than the pre-trade pinned snapshot. Update the snapshot
  after successful accounting so subsequent exits see the acquired tokens.
- Reject missing receipt status or mismatched transaction hash; validate receipt
  block hash presence. This is not a complete receipt/finality verification system.
- `sniping/rpc.py`: reuse an HTTPX connection pool instead of opening a fresh
  urllib connection per request. Retain concurrent dual-provider requests.

Three independent settlement regression tests failed before the fixes and pass
afterward. `make python-check`: 868 passed, 108 subtests passed, one existing
deprecation warning; lint/format/type checking passed. Secret gate passed;
dependency audit found no known vulnerabilities. Bandit completed successfully.
Contracts were unchanged; their tests were not rerun in this review.

## Runtime handoff — required before claiming these fixes are active

Observed PID 90667 runs `scripts/run_launch_monitor.py --live`, started 10:56:25 CST.
The edited monitor and RPC files were modified at 15:17:44 and 15:18:28 respectively.
That process predates these fixes. This review did not start a competing process
or restart the other operator's live process.

Read-only live journal inspection: 1,374 buy_failed and 42 monitor_error records,
no broadcast or confirmed trade records. This is journal evidence, not independent
on-chain reconciliation of all wallet activity.

## Remaining correctness and latency findings

1. No ERC-20 approval to Permit2 or Permit2 allowance to Universal Router is
   established in the current sniping execution path. The sell builder uses
   payer_is_user=True. Preexisting bounded allowances must be verified, or the
   authorized bounded approval flow must be implemented before relying on exits.
2. `_next_nonce` uses latest, not pending; concurrent wallet writers are not
   coordinated. Run only one owner for this account and reconcile pending intent.
3. Router calldata uses execute(bytes,bytes[]) without an on-chain deadline.
4. Snapshot acquisition, four fee-tier lookups, quote, simulation, gas estimation,
   nonce and balance acquisition remain multiple sequential network stages;
   2-second sleep is added after the entire cycle. It is not a 2-second total cycle.
5. The first pool's 0.1 ETH quote still reverted in both samples. Presence of a
   pool address is not evidence of executable liquidity. Sell evaluation selects
   the first pool only, so an unusable first pool can conceal a usable later pool.

## Diagnostic timing samples, not a controlled benchmark

Before connection reuse: snapshot 1,602 ms; four pool lookups 1,488 ms; failed
first-pool quote 391 ms. Base block 51,074,428, hash
0x5bf04ba37ebdd4b25dedd7e146360f2c3d72fb4c9a8918395ef9b56a0b259d0c.

After: snapshot 16,730 ms (slow outlier), pool lookups 528 ms, failed quote 134 ms.
Base block 51,074,491, hash
0xfde171f3da5f537ebe49c200b2aab6fcd56d73844c4535bf83f8586b7a4b0fe2.
Additional snapshots in one HTTP session took 1,943 / 1,197 / 938 ms. These samples
do not establish tail latency, execution readiness or fill probability. RPC URLs
are deliberately excluded. Timing probes are diagnostic only and did not collect
the full bytecode/raw-response provenance required for release evidence.
