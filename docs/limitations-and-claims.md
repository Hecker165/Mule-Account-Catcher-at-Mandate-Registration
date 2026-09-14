# Limitations and claims

Honest scoping of what Mandate Guardian does and does not do. Every limitation
names the owning work package. The architecture's guardrails apply: the system
detects merchant-visible risk at or around mandate registration, challenges
suspicious registrations before redirect when possible, and requests revocation
after confirmation — nothing more.

## What the system does

- Captures mandate-registration context (device, flow timing, pseudonymised
  customer references) at a single merchant (A2).
- Scores that context against a transparent rule catalogue and returns
  ALLOW / CHALLENGE / BLOCK with reason codes (A5).
- Verifies signed Razorpay mandate webhooks and ingests them idempotently (A3).
- Correlates confirmations back to the registration that produced them, then
  requests token revocation through a transactional outbox worker (A1/A6).
- Records every consequential decision in a tamper-evident audit chain (A1).

## What the system does not claim

- **No guaranteed prevention of debits.** A challenge can be completed by the
  customer and a BLOCK only stops this merchant's mandate; confirmed mandates
  are revoked by request, not by force (A6). Razorpay may process a debit
  before revocation lands.
- **No mule-account identification.** The system flags merchant-visible risk
  patterns; it never identifies, labels or reports any person as a mule
  account (A5).
- **No MuleHunter.AI integration and no cross-merchant intelligence.** The
  system sees only its own merchant's sessions; there is no bank data feed,
  no NPCI data and no network reputation in this build (A4's reputation
  provider is an explicit unavailable placeholder; A12 would be optional).
- **No KYC.** Nothing in the system verifies identity (A0).
- **No real money.** Razorpay usage is Test-mode; the revoke client is a mock
  unless real credentials are configured, and the mock can simulate scheduled
  failures for demonstration (A6).

## Explicit demo-scope limitations

- **Shared-demo-merchant velocity is simulated.** The device-across-merchants
  counter demonstrated in scenario 7 exists only for the demo and is labelled
  `DEMO_SIMULATED` end to end (A4/A9). Real cross-merchant velocity would need
  data this system does not receive.
- **Namespaces isolate the demo.** Scenario traffic runs in dedicated
  `demo_*` namespaces so rehearsal cannot contaminate real signals (A9).
- **Uncorrelated events score with reduced context.** A confirmed webhook whose
  notes do not reference a risk session is still ingested and audited but
  evaluated with only provider-visible context (A3).
- **User-agent classification is deferred** (A2): bot detection rests on flow
  timing and device/IP velocity, not UA parsing.
- **Dashboard and demo surfaces have no authentication** (A8/A9): acceptable for
  a buildathon demo, documented in the threat model as deferred with residual
  risk. `/metrics` is likewise unauthenticated and carries counters only (A10).
- **Rate limiting, proxy trust, stale-PROCESSING reclamation and secret
  rotation are deferred** (A0/A3/A6); see the threat model for residual risks.
- **A11/A12 (optional extensions) are not part of the core loop** (A0 plan).

