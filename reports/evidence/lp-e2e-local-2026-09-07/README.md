# Actual LP lifecycle evidence

`events.jsonl` is the append-only hash-linked run journal. It includes original receipts, gas, fixed-block balances and runtime hashes. `verification.json` is independently recomputed by `scripts/verify_lp_e2e.py`. See `docs/verification/2026-09-07-real-lp-e2e-delivery.md` for exact scope.

This uses test assets and a single RPC provider; it is not profitability or production approval. No private key, raw signature or raw signed transaction is included.
