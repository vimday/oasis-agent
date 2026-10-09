"""Owner-authorized live LP loop for one hookless Uniswap v4 pool on Base.

The strategy process never holds key material: every transaction is signed by
digest through the separately run signer daemon (`scripts/run_lp_signer_daemon.py`).
"""
