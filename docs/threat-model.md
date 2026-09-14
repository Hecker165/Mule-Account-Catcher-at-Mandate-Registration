# Threat model

Mandate Guardian is merchant-layer, explainable UPI mandate-risk detection. This
document is the buildathon's security self-assessment: what the system protects,
where trust boundaries sit, which controls exist (and which package/test
enforces them), and which controls are deliberately deferred. It claims no
capabilities the system does not have.

## 1. Scope and assets

Assets:

- **Risk sessions** — pseudonymised registration/checkout context (hashed VPAs,
  device fingerprints, flow timings).
- **Mandate webhook events** — raw provider deliveries stored with signature
  verification and idempotent ingestion; VPAs are HMAC-pseudonymised at parse time.
- **Risk assessments and rule evaluations** — scores, decisions and reason codes.
- **Audit chain** — append-only, hash-linked event log with per-aggregate
  verification (`verify_audit_chain`, A1; scale-proven by A10's performance test).
- **Action requests/attempts and the outbox** — revocation intent and delivery
  state; integrity protected by transactional outbox (A1) and idempotency keys (A6).
- **API credentials** — Razorpay key secret, webhook secret, HMAC pepper. Held as
  `SecretStr`, never serialised (A0 settings; proven by A10's settings test).

Data flow (from the architecture): browser -> checkout session capture ->
pre-check evaluation -> redirect/challenge -> Razorpay mandate confirmation ->
signed webhook -> evaluation -> audit + outbox -> worker -> revoke API.

## 2. Trust boundaries

1. **Browser <-> API** — the web app is untrusted; the API trusts no browser
   state. No auth on dashboard/demo routes is a documented deferral (below).
2. **Razorpay <-> webhook route** — the only provider entry point; every
   delivery is HMAC-SHA256-verified over raw bytes (A3) or rejected (401);
   with no secret configured the route fails closed (503).
3. **Worker <-> Razorpay revoke API** — the worker holds the API credentials
   (A6); failure handling is retry-with-backoff with idempotency keys.
4. **Demo controls <-> production guard** — `/v1/demo/*` and demo-webhook
   headers return 404 when `app_env == "production"` (A9/A3, guarded by tests).

## 3. STRIDE-lite threat table

| Threat | Vector | Existing control | Residual risk |
|---|---|---|---|
| Spoofing | Forged webhook call | HMAC-SHA256 over raw bytes (A3); fail-closed 503 without a secret | Secret rotation deferred |
| Spoofing | Forged demo webhook header | `X-Demo-Event` ignored in production (A3/A9 guards) | - |
| Tampering | Assessment or audit modification | Append-only audit chain with hash links (A1) | No external notarisation |
| Tampering | Outbox loss/duplication | Transactional outbox + `SKIP LOCKED` + idempotency keys (A1/A6) | Stale PROCESSING reclaim deferred (A6) |
| Repudiation | "We never blocked that mandate" | Audit chain with actor, payload and hash verification (A1); dashboard chain panel (A8) | No auth on dashboard (deferral) |
| Information disclosure | Raw VPA in logs or errors | Pseudonymisation at parse time (A3); redaction tests over fail-closed paths (A10 dynamic) | Access logs at the reverse-proxy layer are out of scope |
| Information disclosure | Secrets in settings output | `SecretStr` settings (A0); repr/str leakage test (A10) | - |
| Information disclosure | Identifiers in metrics | Label cardinality guard: method/route-template/status only (A10 `core/metrics.py`) | - |
| Denial of service | Webhook floods | Signature check is constant-time and cheap; bounded transactions | Rate limiting deferred |
| Elevation of privilege | Demo controls in production | `app_env` guard returning 404 (A9) | - |
| Elevation of privilege | Worker double-execution | `SKIP LOCKED` + idempotency keys (A6) | Stale PROCESSING reclaim deferred (A6) |

## 4. Deferred controls (with residual risk)

Each deferral is documented by the package that owns the surface:

- **Authentication/authorisation on dashboard and demo routes** (A8/A9): anyone
  with network access to the API can read redacted assessments and run demo
  scenarios. Residual risk: information disclosure of pseudonymised demo data;
  the production guard keeps the demo surface out of production.
- **Rate limiting / WAF** (A3): a webhook flood costs signature computations and
  DB round-trips. Residual risk: resource exhaustion under load.
- **Proxy trust** (A0): `X-Forwarded-For` is not trusted for velocity signals;
  peers behind one proxy share an IP bucket. Residual risk: reduced IP-signal
  fidelity, never a wrong ALLOW on its own (device velocity is independent).
- **Stale PROCESSING reclamation** (A6): a worker crash mid-attempt leaves the
  message PROCESSING until restart. Residual risk: delayed revocation, never a
  duplicate (idempotency key).
- **Secret rotation** (A0/A3/A6): rotating the webhook secret or HMAC pepper
  requires a deploy; the pepper change re-pseudonymises VPAs. Residual risk:
  a leaked secret is valid until rotation.

## 5. Out of scope

- MuleHunter.AI or any bank/mule-account intelligence integration (A12 is an
  optional extension, not part of the core loop).
- KYC or identity verification of any kind.
- Guaranteeing prevention of debits: the system challenges and revokes; it does
  not control the payment rails and never blocks a confirmed mandate itself.

