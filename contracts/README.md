# Oasis Phase 1 contracts

This Foundry workspace contains the minimal Robinhood Chain Uniswap v4 hook and its tests. It is a
research artifact until every gate in `docs/security/V4_HOOK_PRODUCTION_STANDARD.md` passes.

The complete npm dependency graph is locked by `package-lock.json`, including registry integrity
hashes. The direct contract dependencies are `@uniswap/v4-core` 1.0.2,
`@uniswap/v4-periphery` 1.0.3 (published from commit
`60cd93803ac2b7fa65fd6cd351fd5fd4cc8c9db5`), and `@openzeppelin/contracts` 5.4.0. The separate commit in
`configs/v4-upstream.example.toml` pins Python differential research vectors; it must not be
misrepresented as the provenance of the npm Solidity package. Before promotion, the release manifest
must independently record and verify the source commit and artifact hashes for every Solidity
dependency.

Builds use Foundry 1.8.1, Solidity 0.8.26, Cancun EVM semantics, optimizer runs 200, no upgradeable
proxy, and deterministic metadata settings. `make contract-check` runs formatting, strict compilation,
10,000-case fuzz tests under the CI profile, the complete contract suite, and Foundry lint. The current
suite includes a real local canonical `PoolManager` callback/fee-override path and an official
v4-periphery `PositionManager` first-mint/reject/revoke/remove lifecycle. That lifecycle isolates the
hook boundary with a minimal Permit2 transfer mock; a real Permit2 campaign, Robinhood-pinned fork,
external audit, and production approval are still absent.

No script in this directory broadcasts by default. Deployment, signing, and funding require a
separate reviewed manifest and explicit operator authorization.

From the repository root:

```bash
make contract-install
make contract-check
```
