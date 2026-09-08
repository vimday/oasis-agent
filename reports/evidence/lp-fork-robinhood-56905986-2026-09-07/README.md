# Robinhood mainnet pinned local-fork lifecycle

Read-only preflight pinned Robinhood chain 4663 at block 56,905,986 and retained the exact block,
parent, timestamp, public RPC exchanges, and runtime hashes for PoolManager, PositionManager, and
Permit2. Foundry then ran the synthetic-token lifecycle locally against those forked runtimes.

The single test passed mint, four swap direction/mode combinations, collect, whitelist revocation,
and full owner unwind. `broadcast` and `deployment_eligible` are both false. This is one public RPC
source and does not establish benchmark-pool access, token safety, routing, economics, finality
quorum, production release, or an independent audit.
