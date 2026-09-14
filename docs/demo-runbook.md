# Demo runbook

A 10-minute judge script. Every step runs against the real stack (PostgreSQL,
Redis, API, web) with the mock revoke client; nothing is pre-baked.

## Prerequisites and stack start

```powershell
git clone https://github.com/Hecker165/Mule-Account-Catcher-at-Mandate-Registration.git
cd Mule-Account-Catcher-at-Mandate-Registration
make bootstrap        # uv + npm installs (A0)
docker compose up -d  # postgres + redis (A0)
# apps/api/.env from .env.example; set RAZORPAY_WEBHOOK_SECRET=local-test-secret
make api              # FastAPI on :8000 (terminal 1)
make web              # Next.js on :3000 (terminal 2)
```

Migrations apply automatically (`alembic upgrade head` runs in `make api`'
environment or via `uv --directory apps/api run alembic upgrade head`).

## URL map

| URL | What it shows |
|---|---|
| `http://localhost:3000/checkout` | Checkout + mandate summary (A7) |
| `http://localhost:3000/challenge` | Step-up challenge surface (A7) |
| `http://localhost:3000/dashboard` | Live assessments, reasons, audit-chain panel (A8) |
| `http://localhost:3000/demo` | One-click scenario runner (A8 renders A9's API) |
| `http://localhost:8000/docs` | OpenAPI docs (A0 contracts) |
| `http://localhost:8000/healthz`, `/readyz` | Liveness/readiness (A0) |
| `http://localhost:8000/metrics` | Prometheus counters (A10) |

## Fast path (recommended, ~5 minutes)

1. Open `/demo`, press **Run** on each of the seven scenarios
   (or run all: legitimate flow, bot burst, NPCI-risk rejection, confirmed
   high-risk block, duplicate webhook, worker failure/recovery, shared demo
   merchant velocity). Each returns per-check verdicts — green means the
   real stack proved it, and a red check is shown honestly, never hidden (A9).
2. Open `/dashboard`: assessments with masked VPAs, reason chips, latency,
   the audit-chain **verified** badge, and `DEMO_SIMULATED` labels on the
   shared-merchant counter (A8).
3. Point out `/metrics`: counters with method/route-template/status labels
   only — no identifiers (A10).

## Manual path

1. **ALLOW flow** — on `/checkout`, register normally with a fresh customer and
   a >10-second flow: ALLOW with a transparent score breakdown (A5).
2. **Forced CHALLENGE** — register five times quickly with the *same* device
   fingerprint: from the fifth same-device session in 5 minutes, the velocity
   rule adds 25 points → CHALLENGE with the reason chip visible (A5/A7).
3. **Signed webhook replay** — with `RAZORPAY_WEBHOOK_SECRET=local-test-secret`:

   ```powershell
   uv --directory apps/api run python -c "<sign data/fixtures/webhooks/token_confirmed_body.json and POST it>"
   ```

   or use `scripts/generate_demo_events.py --scenario duplicate_webhook` and
   POST both lines: the second delivery is acknowledged as a duplicate with
   `X-Idempotent-Replay: true` (A3).
4. **Worker failure/recovery** — run A9's `worker_failure_recovery` scenario:
   the first mock revoke fails, retry succeeds, exactly two attempts, no
   duplicate revocation (A6/A9).

## What to point out on screen

- Masked VPA and pseudonymised identifiers everywhere (A3 privacy hashing).
- Reason chips that name the rule and points that fired (A5/A8).
- Latency numbers per request; benchmark evidence in
  `scripts/benchmark_pipeline.py` output (A9).
- Audit-chain verified badge — click an assessment to see the chain (A8/A1).
- `DEMO_SIMULATED` badges on simulated signals (A4/A8).

## Recovery notes

- **Fresh velocity windows**: restart Redis (`docker compose restart redis`) —
  all counters are Redis-side (A4).
- **Re-run any scenario safely**: devices/customers are fresh per run; only
  5-minute velocity windows are shared, and the scenarios stay green within
  documented re-run budgets (A9).
- **Dashboard polling**: the dashboard polls live; new webhook events appear
  within seconds (A8).
- If the API is restarted mid-demo, re-run the migration check (`alembic
  upgrade head` is idempotent) and `/healthz` before continuing.

