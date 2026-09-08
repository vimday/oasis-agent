# Oasis Agent Observation Server — Delivery Verification

Date: 2026-09-08
Scope: P0–P3 backend server (FastAPI) + single-file frontend dashboard, delivered in full under
strict code standards.

## 1. What was delivered

| Area | Path | Notes |
| --- | --- | --- |
| Application factory | `src/oasis_agent/server/app.py` | FastAPI app, lifespan ownership, strict config wiring |
| Strict config | `src/oasis_agent/server/config.py` | Unknown keys, embedded credentials, secret values and malformed types rejected; token only via env var |
| Read-only data layer | `src/oasis_agent/server/databases.py` | `mode=ro` + `PRAGMA query_only`; hash-linked chains re-validated on every read; fail closed with 503 |
| Operations journal | `src/oasis_agent/server/audit.py` | Append-only ops/runs/approvals DB; redacted details; parameter digests |
| Authorization | `src/oasis_agent/server/auth.py` | Constant-time token check; comma-separated multi-operator tokens; startup fail-closed |
| Run coordinator | `src/oasis_agent/server/runner.py` | Isolated subprocess runs, concurrency + timeout control |
| Multi-chain registry | `src/oasis_agent/server/chains.py` | Read-only whitelisted RPC methods; request caching |
| Alerts | `src/oasis_agent/server/alerts.py` | Feed staleness, gate expiry, open intents, failed runs; verified data only |
| P0 read routes | `routers_observe.py` | health, status, observations, decisions(+detail), evidence(+detail), readiness, journal/lp, chains pool, WebSocket stream |
| P1 ops routes | `routers_ops.py` | runs trigger/list/detail, ops audit log |
| P2 console routes | `routers_ops.py` | halt-proofs, gate approvals (two-person confirm) |
| P3 routes | `routers_p3.py` | chains list/status, alerts |
| Dashboard | `server/static/index.html` | Single-file HTML/CSS/JS, zero external dependencies |
| Entry point | `scripts/run_server.py` | `uvicorn` launcher with strict config |
| Example config | `configs/server.example.toml` | Documented; never stores tokens |

## 2. Design invariants

- The API process never imports signing or broadcast capability (enforced by
  `test_server_package_never_imports_broadcast_capability`).
- Every journal is opened read-only; every hash-linked chain (observations, decisions, execution
  events) is fully re-validated per request; tampering surfaces as 503, never partial data.
- Canonical payloads with their `digest` are served verbatim so the dashboard shows exactly what
  the audit chain contains.
- Operations are disabled by default; a missing token fails closed (startup refusal when
  required, 503 otherwise); tokens never live in the config file.
- Two-person gate approval requires two distinct operator tokens; actor identity is derived from
  the token hash and confirmation by the requesting actor is rejected (409).
- SQL identifiers are allowlist-constrained and all values parameter-bound; static analysis
  (bandit B608) passes with zero High/Medium severity findings.

## 3. Gate results (2026-09-08, reproducible commands)

```
make python-check     PASS  ruff check/format clean (206 files), mypy 104 files clean,
                              767 tests passed
make security-check   PASS  pip-audit (hashed lockfile) clean; bandit -r src scripts -ll:
                              0 High / 0 Medium severity; check_secrets: 0 unreviewed candidates
```

The `python-check` gate covers lint, format, typecheck and the full test suite including all nine
`tests/unit/test_server_*.py` modules (API end-to-end, authorization, read-only hash validation,
audit redaction, alerts, chains, runner, config strictness) and the no-broadcast-import static
test. `security-check` covers pip-audit against `requirements-dev.lock`, bandit at `-ll`, and the
secret scanner; four synthetic fixture candidates were reviewed and added to
`configs/secret-scan-baseline.json` with explicit non-secret reasons (three server fixtures plus
the non-empty hookData bytes in `contracts/test/OasisPositionExecutorAdversarial.t.sol`).

## 4. Second-round hardening: performance and textbook-grade elegance

A dedicated review pass tightened the hot paths and raised the code to the same standard as the
rest of the repository. All changes keep the security boundary intact (observe-only, fail closed,
zero external frontend dependencies).

### 4.1 Data layer (`databases.py`)

- **Memoized hash-chain validation.** Every read previously re-walked the full hash-linked chains.
  A cheap probe (row count, tail digest, policy hash) now proves the cached validation is still
  authoritative, because the journals are append-only. Unchanged probe -> cached snapshot served;
  any tamper or append -> full re-validation and fail closed. The memo lives on the single-threaded
  event loop, so it needs no lock.
- **SQL-pushed evidence queries.** `evidence_page` pushes kind/chain/source filters plus
  `LIMIT/OFFSET` into SQL with a `COUNT(*)` total, so the API no longer loads and filters whole
  tables in Python. Identifiers come from a fixed allowlist; values are always bound parameters.
- **Readiness manifest parse cache.** The manifest is replaced atomically by the producer; a
  content-hash probe proves a cached parse is still valid, avoiding a parse on every request.
- **Type-safe row decoding.** `_row_to_record` normalizes the row and validates every cell type
  with explicit checks; a schema-violating storage cell raises `ReadOnlyError` (503) instead of
  being coerced into a half-typed record.

### 4.2 API layer

- **Concurrent RPC.** Chain health and chain-status endpoints issue their independent JSON-RPC
  calls with `asyncio.gather`, cutting wall-clock latency from serial round-trips to one.
- **WebSocket stream** now consumes the memoized validated snapshot and drops the loop cleanly on
  any transport failure, with a strict heartbeat when no new decisions exist.
- **Security response headers** applied by a middleware on every response: `Content-Security-Policy`
  (`default-src 'self'`, `frame-ancestors 'none'`), `X-Content-Type-Options: nosniff`,
  `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`. The interactive docs page is exempted
  from CSP only because it requires inline scripts.
- **Cache control.** The dashboard serves `Cache-Control: no-store` because it reflects live state;
  a cached copy could show a stale, unsafe picture.
- **Runner resilience.** A subprocess spawn failure (`OSError`) is journaled as a failed run
  instead of leaving the journal stuck; a kill race during timeout handling is absorbed.

### 4.3 Frontend (`static/index.html`, zero external dependencies)

- **Event delegation.** A single document-level click/submit listener dispatches all interactive
  actions via `data-action`, instead of per-element handlers.
- **Generic table renderer** `renderTable(id, columns, rows, emptyText)` removes duplicated DOM
  building across the seven tables and escapes every cell (XSS-safe).
- **Request timeout** via `AbortSignal.timeout`, with distinct toast messaging for timeout versus
  network failure and graceful 401/503 handling.
- **Visibility-aware polling.** The 30s status poll pauses when the tab is hidden and resumes on
  `visibilitychange`, eliminating background work.
- **Adaptive canvas.** The fee chart redraws only when visible/pending and honors the element's
  client width with `requestAnimationFrame`, avoiding wasted GPU work on hidden tabs.
- **Accessibility.** Tablist/tabpanel roles, `aria-live` status bar, `aria-label`s, `:focus-visible`
  outlines, and a `visually-hidden` utility; keyboard and screen-reader usable.
- All server-side HTML is escaped on output; the CSP `default-src 'self'` plus `no-store` closes
  the main frontend attack surfaces.

### 4.4 Third-round hardening: extensibility and textbook-grade structure

A second review pass reorganized the API around dependency injection, centralized error
translation and self-describing metadata, and modularized the dashboard without adding any
runtime dependency. The security boundary (observe-only, fail closed, zero external frontend
dependencies) is unchanged.

#### 4.4.1 Backend structure

- **Typed dependency injection** (`deps.py`). Routes receive a frozen `AppState` value object
  (config, databases, journal, authorizer, registry, runner) through `Depends` instead of reaching
  into `request.app.state`; signatures are self-documenting and services are swappable in tests
  without touching route code.
- **Pydantic response models** (`schemas.py`). Derived envelopes (health, status, chains, alerts,
  meta) are validated on output and fully described in OpenAPI. Canonical hash-linked journal
  records are intentionally unmodeled so evidence fields can never be dropped by a response model;
  this split is the documented extensibility contract for new endpoints.
- **Centralized exception handling** (`app.py`). Domain errors (`ReadOnlyError`, `RpcUnavailableError`,
  `RpcProtocolError`, `OpsDisabledError`, `ConfigurationError`) are translated to 503/502/500 by
  registered handlers; routes no longer repeat try/except blocks.
- **Middleware triad.** A server-generated request id (never client-supplied) stamps every response
  and one structured access log line per request; a pure-ASGI ETag middleware buffers successful
  GET bodies, publishes `ETag` (SHA-256) and answers `If-None-Match` hits with 304 (the console
  page and mutating requests are excluded); security headers run outermost so error responses are
  covered too.
- **Self-describing `/api/v1/meta`.** Reports server name/version, the canonical data schemas and
  envelope schemas (versioned constants in `schemas.py`), and every live `/api/v1` route with its
  methods, derived from the OpenAPI document.
- **Centralized route registration.** `_ROUTERS` in `app.py` is the single place new routers are
  registered; the P0/P1/P2/P3 dependency order is documented.

#### 4.4.2 Frontend (`static/index.html`)

- **View registry.** Every console section registers a `load()` (and optional poll) function under
  its tab name; startup fails loudly if a navigation button has no view, enforcing the
  extensibility contract that adding a tab always ships with a wired view.
- **Delegated action registry.** Interactive elements declare `data-action`; one document-level
  click/submit listener dispatches to registered handlers.
- **DocumentFragment batch rendering.** `renderTable` builds one fragment and appends once, so the
  browser performs a single layout pass per refresh.
- **Intl formatting**, input debouncing (250 ms) for filters, and exponential retry backoff with
  jitter for failed polls.
- Existing performance features (visibility-aware polling, adaptive canvas, request timeout,
  escaped cells, accessibility roles) are preserved.

## 5. Live server smoke test

A real `scripts/run_server.py` process was launched against freshly built shadow/evidence/
execution/readiness databases with a strict TOML config and `OASIS_SERVER_OPS_TOKEN` set. Verified
over HTTP:

- `GET /api/v1/health` → 200, all sources `verified`
- `GET /api/v1/status` → 200, `execution_eligible: false`, `decision_count: 1`
- `GET /api/v1/readiness` → 200, 9 gates, `execution_eligible: false`
- `GET /api/v1/observations`, `/api/v1/chains`, `/api/v1/alerts`, `/` (dashboard) → 200
- `POST /api/v1/runs/shadow` → 401 without token, 401 with wrong token, 200 (queued) with token

## 6. Operational notes

- Start: `OASIS_SERVER_OPS_TOKEN=<token> .venv/bin/python scripts/run_server.py --config configs/server.toml`
- The dashboard is served at `/`; interactive API docs at `/docs`.
- The optional `[shadow_runner]` section lets the dashboard trigger observation runs as isolated
  subprocesses; output tails are redacted and truncated in the journal.
- The optional `[[chains]]` registry supports only whitelisted non-mutating JSON-RPC methods; the
  server never submits transactions.

## 7. Known boundaries

- This server is an observation/operations console for the shadow and execution journals. It is
  not a strategy, does not hold keys, and cannot sign or broadcast; `execution_eligible` and all
  `lp_readiness` gates remain closed.
- Alert evaluation is on-demand (request-time), not a daemon; there is no external notification
  integration yet.
- Two research/intermediate folders (`oasis-server-frontend-research/`, `.trae-html-share-packages/`)
  remain untracked in the workspace and are candidates for removal before commit.
