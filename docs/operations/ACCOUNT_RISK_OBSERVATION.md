# Persist independent account observations

`python -m scripts.observe_account_risk --input REQUEST --evidence EVIDENCE_DB
--journal EXISTING_RISK_DB --output NEW_REPORT` replays retained account evidence
and appends the resulting reconciliation to the existing global risk journal.
It performs no acquisition, signing, broadcasting or live-loop startup.

The strict request schema is `oasis.risk-observation.v1`, with `manifest`,
`opening` and `closing` fields. The latter two map every manifest account key to
the existing typed `AccountObservation` raw evidence references. The schema
accepts no asserted NAV or replacement risk state. Both boundary inventories
are reconstructed from the retained proofs. The opening must match the actual
persisted boundary, including its block, quantities, valuation and evidence
hashes. Only prior-interval movements and flow credit are excluded from that
boundary comparison. Every closing account must carry complete raw outer-account
gas coverage; native scalar overrides are forbidden. Restricted ERC-20 owned-LP
cash evidence is consumed through the existing account reader.

The command requires an already initialized, policy-bound global journal. It
does not create a missing journal, reconstruct deleted history, import the live
loop's float NAV, or establish a new opening balance. No fabricated commitment
should be admitted just to initialize an operator journal. Initial account and
release preparation remains a separate prerequisite.

The journal rechecks its whole state under a SQLite write transaction before
appending. A concurrent reservation, release or observation causes a refusal
when it changed the baseline. Each accepted observation retains the canonical
request SHA-256 in the hash-linked event. Gross losses and existing halts persist
without requiring a new signing request. Newly observed investment units settle
pending commitments by the same rule as existing admission; reservation records
are retained, and no caller-directed settlement/release is offered.

An exact replay of the current closing frame validates raw evidence and current
freshness again, then returns the existing journal digest without another loss
or event. A missing exact-midnight reconciliation, unexplained cash, stale price,
wrong baseline or incomplete proof refuses. The trusted clock is system time;
there is no CLI override to admit historical proofs as fresh.

The journal commit happens before report publication. If a crash or output error
loses the report, preserve the journal and replay the same request to a new output
path while evidence remains fresh. Once stale, obtain a genuinely new complete
authorized observation; do not reset history or change timestamps. Exit 3 can
mean a persisted loss halt or a refused observation/report; inspect the retained
journal and report, not just the exit code. A successfully persisted halt is not
rolled back because reporting failed.

This is an accounting observer, not a transaction-release or exit authority.
The staged live adapter does not consume it yet. Native internal cash coverage,
external funding-purpose authentication, complete transaction/receipt/finality
binding, LP exit settlement, candidate admission and multi-week economic/OOS
acceptance remain separate requirements. Reports keep `execution_eligible=false`
and `net_pnl=null`. Existing USD 500/200/500 limits and release rules are unchanged.
