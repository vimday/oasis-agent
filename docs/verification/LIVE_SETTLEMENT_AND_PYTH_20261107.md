# Durable settlement polling and Pyth candidate evidence — 2026-11-07

Automatic pending-receipt polling now writes its attempt to the service journal
before reading providers. A crash preserves the attempt and pending transaction.
After five unresolved or interrupted attempts, the service latches HALT before
another automatic poll. The strategy tick, immediate post-submission path and
signing authority share this behavior. A successful poll resets the streak.
Explicit reconcile-only entry points retain their existing recovery authority,
and a settled or discarded pending transaction clears the attempt count.

The previous state format decodes with a count of zero. The two stopped
production ledgers were mounted read-only in a keyless, network-disabled
container. Their five settlements and full cash and gas histories replayed with
exit 0 under the new state format. The retained review hash is
`2ecbd96649ec02d242464064fa4c8b69e7d57e561fa0b9871a377f37fc1fd97f`.
The cash, position, approval and revert regressions still compare every other
state field. The process-death test now permits the pre-RPC journal write,
kills the process before publishing a confirmed result, then verifies that
recovery settles once and clears the attempt count.

## Fresh price discovery

The Solana Foundation public endpoint completed five read-only RPC requests at
finalized slot **466206055**, block
`BhApE6V4yPU6UJUYRKWS8dFAW6fzVZMhSCcXU7mqET8X`, block time
`1793988821`. Offline replay checked the recorded request order, RPC IDs,
account metadata, ProgramData, and response hashes. Both sponsored Pyth
accounts decoded within the 1800-second ceiling at retrieval.

| Feed | Price in USD | Publish time | Age at retrieval |
| --- | ---: | ---: | ---: |
| ETH/USD | 2224.52427059 | 1793988767 | 57.606 seconds |
| USDC/USD | 0.99983552 | 1793988782 | 42.606 seconds |

Raw responses, reservations and decoded observations are under
`reports/review-20261107/pyth-solana-official-observation/`.
The capture report hash is
`e97a73d4ab88e8655bcf969f76bb7de3eaaf6279763e89a7e644cd9899e60ac1`.
The offline audit hash is
`35be26468938d2b5578d98a2a392c4a7b1ba3150884cbdb12a4b6dd3b81b7cd1`.
The audit records both ProgramData hashes and the receiver configuration hash.
It establishes one endpoint's account observations at capture time. A second
source, approved deployed-program and receiver trust verification, Base decision
clock binding and full Base NAV remain open.

Four separate peer probes each stopped after one request. Solana Tracker
returned HTTP 401 and requested an API key. MoltNode and dRPC public returned
HTTP 403 with edge error 1010. The existing Alchemy project returned HTTP 403
with `SOLANA_MAINNET is not enabled for this app`. A fresh pair can be collected
once Solana Mainnet is enabled for that project or a working Solana RPC is
provisioned. Solana's sponsored-account policy and endpoint formats are documented
by [Pyth](https://docs.pyth.network/pro/sponsored-feeds),
[Solana](https://solana.com/docs/rpc) and
[Alchemy](https://www.alchemy.com/docs/reference/node-supported-chains).

This discovery reserved eight free requests and one potentially billable
Alchemy request. Its conservative allowance is **USD 0.000021** and the known
cumulative upper bound is **USD 0.007005** under the owner's USD 0.10 cap.
The estimate uses Alchemy's published
[compute units](https://www.alchemy.com/docs/reference/compute-unit-costs)
and [public pricing](https://www.alchemy.com/docs/reference/pricing-plans).
Billing remains unverified. Original Base service budgets and states were not
written. The signed-price and candidate paths leave execution eligibility false.

The previous Base [paid evidence report](LIVE_PAID_EVIDENCE_20261107.md)
contains the five real dual-source receipts and the unsigned dual-source exit
simulation. Same-head fresh NAV, recovery, and operational release remain
pending. The fresh Pyth observations above are time-bound evidence for source
qualification.

The no-argument startup readiness command returned exit 3 with
`gas_oracle_runtime_pins_missing`. It lists the proxy, implementation slot and
implementation code roles. The earlier dual-source candidate pins are retained
under `reports/review-20261107/paid-joint-001/attempt/`. Applying them requires
the paired runtime transition with fresh same-head evidence. The local
configuration and service volumes were left at their existing state.

The initial full run exposed snapshot assertions that assumed every failure
preserved the state file byte for byte, and a process-death fixture that
intercepted the new reservation instead of the settlement publication. The
revised tests compare every field other than the intended count and preserve
the original kill boundary. The final `make live-quality` exited 0 with
**4717 tests and 138 subtests**. All 680 frozen inputs matched and Bandit
reported zero medium or high findings. The gate report hash is
`0214ce7bbfcd5334461f228cb24113578fce9aecd0a42512b190e7e4f35119c8` and outer record hash is
`5e9f5cadc602135832652c264a4b5e8b97771cdbf3761d548eda7692daef361d`.
