# Getting started — keys and final setup

This is the one document to read before running the demo. The short version:
**no Razorpay account is needed** — the system runs fully in local/demo mode
out of the box. Razorpay credentials are optional and only change *which*
revoke client runs; there is no key the app cannot start without.

## What each key does

| Variable | Required? | Without it | With it |
|---|---|---|---|
| `RAZORPAY_WEBHOOK_SECRET` | **Yes (demo)** | Webhook gateway fails closed: every delivery returns 503 "not configured", and A9's four webhook scenarios report honest failures instead of green checks | Signed deliveries verify (401 on bad signature); A9 scenarios run green |
| `RAZORPAY_KEY_ID` / `RAZORPAY_KEY_SECRET` | No | `MockTokenRevokeClient` — revocations are simulated locally (deterministic, can schedule failures for the demo) | `RazorpayTokenRevokeClient` — real calls to Razorpay's token-revoke API. **Test mode only** |
| `HMAC_PEPPER` | Local dev value | Falls back to a documented development default | VPAs are pseudonymised with *your* pepper; keep it stable — changing it re-keys all stored hashes |
| `LITELLM_*` | No | Unused (reserved for the optional A12 extension) | Unused in the core loop |
| `NEXT_PUBLIC_API_BASE_URL` (web) | **Leave unset** | Correct: the browser uses the same-origin `/api` proxy | Setting it makes the browser call the API cross-origin; the API serves no CORS headers **by design**, so every page fetch would be blocked. Do not set it for the web app. |

## What I have already done

- Created `apps/api/.env` (git-ignored) with:
  - a freshly generated `RAZORPAY_WEBHOOK_SECRET` (local demo value),
  - a freshly generated `HMAC_PEPPER`,
  - all Razorpay/LiteLLM fields left empty (mock mode).
- Restarted the API so the values are live.

## The one optional step: real Razorpay Test-mode keys

Only do this if you want the worker to call Razorpay's real token-revoke API
instead of the mock. The demo is identical otherwise.

1. Sign up / log in at <https://dashboard.razorpay.com> (Test Mode is the
   default for new accounts).
2. **Settings → API Keys → Generate Test Key**. You get `rzp_test_...`
   (Key Id) and a Key Secret shown once.
3. Put them in `apps/api/.env`:

   ```powershell
   notepad apps\api\.env
   #   RAZORPAY_KEY_ID=rzp_test_xxxxxxxxxxxx
   #   RAZORPAY_KEY_SECRET=xxxxxxxxxxxxxxxxxxxx
   ```

4. Restart the API: stop the `make api` terminal and run `make api` again.
5. Verify: `uv --directory apps/api run python -c "from app.main import create_app; from fastapi.testclient import TestClient; app = create_app(); print(type(app.state.token_revoke_client).__name__)"` → `RazorpayTokenRevokeClient` instead of `MockTokenRevokeClient`.

Never use live keys: the generators and benchmark scripts refuse
`rzp_live_*`, and the static scans fail CI on live key material.

## Run it

```powershell
docker compose up -d          # postgres + redis
make api                      # terminal 1 → http://localhost:8000
make web                      # terminal 2 → http://localhost:3000
```

Then open **http://localhost:3000/demo** and run the seven scenarios, and
**http://localhost:3000/dashboard** to watch assessments, reason chips and the
verified audit chain live. The full 10-minute judge script is in
`docs/demo-runbook.md`.

## Verify the setup took effect

```powershell
curl http://localhost:8000/healthz                                  # 200
curl http://localhost:8000/metrics | Select-String http_requests    # counters
```

In the demo page (`/demo`), the four webhook scenarios
(`npci_risk_rejection`, `confirmed_high_risk_block`, `duplicate_webhook`,
`worker_failure_recovery`) return green checks only when the webhook secret is
configured — if they ever show the check "webhook signature verification is
not configured", the API process was started without it.

## Safety notes

- `apps/api/.env` is git-ignored; never commit real credentials.
- The webhook secret never leaves the API process (A9 signs server-side);
  it must not appear in logs, results, or the repo (enforced by A10's tests).
- Test mode keys still should be treated as secrets: they can act on your
  Razorpay test data.
