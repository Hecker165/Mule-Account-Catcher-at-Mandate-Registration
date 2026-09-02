# Mule Account Catcher at Mandate Registration

## Buildathon architecture and parallel delivery plan

### 1. Product definition

This project is a merchant-layer risk-control system for UPI Autopay mandate registration. It captures customer/session signals before the customer is sent to their UPI app, evaluates the confirmed or rejected Razorpay mandate webhook, and produces an explainable `ALLOW`, `CHALLENGE`, or `BLOCK` outcome.

It is deliberately complementary to bank infrastructure such as MuleHunter.AI; it does not claim to call MuleHunter.AI, receive bank KYC, know the real account holder, or obtain Razorpay network-wide data. A demo-only shared namespace simulates cross-merchant velocity and must be labelled as such in the UI and submission.

The MVP's primary outcome is simple and credible: suspicious confirmed mandates are identified and queued for Razorpay token revocation before the next value debit, with the reason and every action visible in an audit dashboard.

### 2. Important product and timing decisions

| Moment | System action | Possible outcome |
|---|---|---|
| Before redirect to UPI | Store checkout session telemetry; run a lightweight pre-check if enough data exists. | `ALLOW` or `CHALLENGE` only. Challenge means complete a step-up screen before the UPI redirect. |
| Razorpay `token.confirmed` webhook | Verify raw-body HMAC, deduplicate, look up the pre-registration session, extract features, score and persist an immutable assessment. | `ALLOW`, or `BLOCK` which creates a revoke job. |
| Razorpay `token.rejected` webhook | Verify and record; detect exposed NPCI/payer-bank risk wording. | Audit/alert; no revoke is needed for a rejected mandate. |
| Async action worker | Executes and retries the queued `POST /v1/tokens/{token_id}/revoke` call. | `REVOKED`, `RETRYING`, or `FAILED` shown to the operator. |

`CHALLENGE` cannot be created by the `token.confirmed` webhook: that webhook arrives after NPCI has registered the mandate. The pre-check is therefore a separate, deliberate path.

Also, the webhook request IP belongs to Razorpay, not the customer. Customer IP, user agent, device fingerprint and flow duration must be captured at checkout and joined through a server-issued `risk_session_id` saved in Razorpay order notes/metadata (or an equivalent internal order-to-session mapping).

### 3. Architecture

```text
Browser / demo simulator
  | checkout telemetry + risk_session_id
  v
Next.js web app -----> FastAPI API -----> PostgreSQL (auditable system of record)
  |                       |  \----------> Redis (windowed counters / idempotency)
  |                       |
  |                       +-----> Razorpay Orders / Token Revoke API
  |                                  ^
  | Razorpay checkout                 | webhook, raw body + signature
  +-----------------------------------+
                                     FastAPI webhook route
                                              |
                    feature extraction -> deterministic rule engine -> outbox
                                              |                         |
                                              v                         v
                                     assessment + explanation     action worker
                                              |
                                              v
                                      dashboard / live event feed

Optional, asynchronous only: redacted assessment -> LiteLLM gateway -> case narrative
```

Use a modular monolith for the buildathon: one Python service, one worker process, one Next.js application, PostgreSQL and Redis. It is much faster to assemble and demo than microservices, while clear module boundaries allow later extraction.

#### Component responsibilities

| Component | Responsibility | Must not do |
|---|---|---|
| Web app | Checkout telemetry, pre-check/step-up UI, dashboard, demo controls. | Make final fraud decisions or retain secrets. |
| API | Authenticated application API, session capture, webhook validation, synchronous feature/rule evaluation, audit persistence and outbox creation. | Call an LLM in the payment decision path. |
| Redis | Sliding-window velocity counters, short-lived idempotency keys, demo shared-merchant namespace. | Be the permanent audit store. |
| PostgreSQL | Events, sessions, assessments, feature snapshots, action attempts, case status and append-only audit chain. | Store raw VPA/IP unnecessarily. |
| Worker | Revoke jobs, optional reputation enrichment, notification and optional investigation narrative. | Re-score a request inconsistently with the API. |
| Razorpay adapter | Order-note correlation, signature verification helper and typed token-revoke client. | Leak the API secret to logs or browser code. |

### 4. Decisioning design

#### 4.1 Deterministic enforcement first

The production-critical engine is a versioned rule ensemble. It returns a score, outcome, reason codes and feature values from exactly the same immutable input. This is safer, faster and more explainable than an LLM or an unvalidated fraud model.

Initial rules should cover:

- IP, device, customer and VPA-hash velocity in 5-minute, 1-hour and 24-hour windows;
- a new device for an established customer;
- impossible/very short flow duration and bot-like user agent;
- VPN/proxy/reputation signal, when the provider is enabled;
- explicit NPCI or payer-bank risk/frozen/block wording in a rejection;
- unusual amount/frequency/expiry combinations; and
- demo-only shared-test-merchant velocity.

Every rule has an identifier, points, explanation template and feature version. Scores are clamped to 0–100. Start with `ALLOW 0–29`, `CHALLENGE 30–69` only in pre-check, and `BLOCK 70–100` after confirmation. The thresholds are configuration, never frontend constants.

#### 4.2 ML is a shadow-mode extension, not an MVP dependency

An optional model agent can train a calibrated LightGBM/XGBoost model from synthetic labelled events. It initially writes `model_score` beside the rule decision and never changes enforcement. Promote it only after an agreed labelled evaluation set, precision/recall review and explanation checks. This keeps the buildathon demo reliable even with limited genuine fraud labels.

#### 4.3 Optional LLM investigation agent

The only LLM feature is a post-decision "case narrative" for a dashboard analyst. It receives a redacted feature snapshot, rule reasons and action history, then writes a short explanation and recommended review questions. It cannot invoke Razorpay, change a score, alter an action, or see raw VPA/IP values.

Use an OpenAI-compatible LiteLLM gateway:

```text
API/worker -> LITELLM_BASE_URL + LITELLM_API_KEY -> LiteLLM -> any configured provider/model
```

Application code has no provider SDK and no provider-specific branches. Providers and models live in `infra/litellm/config.yaml`; provider credentials are added once as deployment secrets, not copied into code or agent prompts. A developer may point `LITELLM_BASE_URL` at a hosted LiteLLM proxy, local proxy, or compatible gateway. If no gateway is configured, the narrative button is simply disabled and all risk controls continue to work.

### 5. Canonical contracts: the merge anchor

Before parallel implementation, the contracts owner publishes these stable artifacts. All other agents import or mock them; none edits them.

| Contract | Producer | Consumers | Key contents |
|---|---|---|---|
| `RiskSession` | checkout/session API | web app, webhook join, simulator | session/order/customer IDs, pseudonymised network/device data, timing, mandate intent |
| `MandateWebhookEvent` | webhook adapter | feature extractor, audit | raw event ID, event type, token ID, VPA components, failure text, timestamps |
| `FeatureSnapshot` | feature extractor | rules, model, dashboard, narrative | version, numeric/categorical feature values, source availability and demo flags |
| `RiskAssessment` | rule engine | outbox, dashboard, simulator | score, decision, rule IDs, human reasons, engine version, measured latency |
| `ActionRequest` / `ActionAttempt` | decision API / worker | Razorpay adapter, dashboard | token ID, idempotency key, status, error-safe response summary |
| `AuditEvent` | all command handlers | dashboard | chained hash, actor, timestamp, event type and redacted payload |

The source of truth is Pydantic models under `apps/api/app/contracts/`. CI exports `openapi.json`; `openapi-typescript` generates `apps/web/src/lib/api-types.ts`. This means Python owns API correctness and the browser receives types without hand-maintained duplicates. `data/fixtures/` contains valid JSON examples for every contract so agents can work in parallel before the API is running.

### 6. Proposed repository structure

This is the planned structure for implementation. This task adds the architecture document only; the scaffold should be created by the foundation agent so each directory has the correct package metadata and tooling.

```text
.
├── PROJECT_ARCHITECTURE_AND_AGENT_PLAN.md
├── README.md
├── .env.example                     # names only; never real keys
├── .gitignore
├── docker-compose.yml               # web, api, worker, postgres, redis, optional LiteLLM
├── Makefile                         # dev, test, lint, demo commands
├── docs/
│   ├── contracts.md
│   ├── threat-model.md
│   ├── demo-runbook.md
│   └── limitations-and-claims.md
├── apps/
│   ├── api/
│   │   ├── pyproject.toml
│   │   ├── app/
│   │   │   ├── main.py
│   │   │   ├── api/routes/          # sessions, precheck, webhooks, dashboard, demo
│   │   │   ├── contracts/           # Pydantic request/response/event models (contract owner)
│   │   │   ├── core/                # settings, DB, logging, security, time/IDs
│   │   │   ├── domain/              # entities and rule configuration
│   │   │   ├── repositories/        # PostgreSQL persistence only
│   │   │   ├── services/            # session, ingestion, features, scoring, audit
│   │   │   ├── integrations/        # razorpay, IP reputation, LiteLLM clients
│   │   │   └── workers/             # outbox consumer, revocation, narrative jobs
│   │   ├── migrations/
│   │   └── tests/
│   │       ├── unit/ integration/ contract/ performance/
│   └── web/
│       ├── package.json
│       ├── src/app/                 # checkout, challenge, dashboard, demo pages
│       ├── src/components/
│       ├── src/lib/                 # API client, display masking, event stream
│       ├── src/lib/api-types.ts      # generated; do not hand-edit
│       └── tests/
├── data/
│   ├── fixtures/                     # signed webhook samples, sessions, expected outcomes
│   └── scenarios/                    # legitimate, bot burst, NPCI-risk, retry cases
├── scripts/
│   ├── export_openapi.py
│   ├── generate_demo_events.py
│   ├── benchmark_pipeline.py
│   └── verify_audit_chain.py
├── infra/
│   ├── litellm/config.yaml
│   ├── postgres/init.sql
│   └── observability/                # Prometheus/Grafana optional dashboard config
└── .github/workflows/ci.yml
```

Preferred backend dependencies: Python 3.12, FastAPI, Pydantic v2, SQLAlchemy, Alembic, Redis, HTTPX, pytest, Ruff and mypy. Preferred web dependencies: Node 22, Next.js/React, TypeScript, Tailwind, Playwright and Vitest. Keep `requirements.txt` out of the API if `pyproject.toml` is used; one dependency authority prevents drift.

### 7. Parallel coding work packages

Give each coding agent the listed directory ownership, inputs and completion check. This prevents merge conflicts. "Model level" refers to the coding model you should assign, not an LLM used in enforcement.

| ID | Work package and owned paths | Depends on | Model level | Handoff / completion check |
|---|---|---|---|---|
| A0 | Foundation and contracts: repo tooling, Docker Compose, Pydantic contracts, fixtures, OpenAPI export. Owns root config, `apps/api/app/contracts`, `data/fixtures`, `scripts/export_openapi.py`. | None | Strong | `make test-contract` validates all fixtures; generated web types compile. Freeze contracts before Wave 1. |
| A1 | Persistence and audit: SQLAlchemy models, Alembic migration, repositories, HMAC hash-chain audit. Owns `repositories`, migrations and persistence tests. | A0 | Strong | A stored event, assessment and action can be reloaded; `verify_audit_chain.py` detects a manually tampered row. |
| A2 | Checkout session and telemetry API. Owns session/precheck routes and session service. | A0, A1 interface | Medium | Creates/updates a `RiskSession`; missing required correlation returns clear validation errors. |
| A3 | Razorpay webhook gateway. Owns raw-body signature verifier, idempotency, event parser and webhook route. | A0, A1 interface | Strong | Valid signed fixture is accepted once; invalid signature is rejected; repeat delivery does not rescore/revoke. |
| A4 | Redis feature store and feature extraction. Owns Redis adapter and `services/features`. | A0 | Medium | Frozen-clock tests prove 5m/1h windows, isolated namespaces and safe counter keys. |
| A5 | Rule engine and explanation renderer. Owns `domain/rules` and scoring service. | A0, A4 fixture interface | Strong | Golden fixtures yield exact score, decision, reason IDs and threshold-edge behaviour. |
| A6 | Razorpay token-revoke adapter and transactional outbox worker. Owns `integrations/razorpay`, worker/action modules. | A0, A1 interface | Strong | Mock server records one idempotent revoke; retry/backoff then eventual success is displayed. |
| A7 | Checkout and challenge experience. Owns `apps/web/src/app/checkout`, `challenge` and telemetry components. | A0 fixtures/types | Medium | Playwright shows a suspicious pre-check being challenged before redirect; it never exposes secrets. |
| A8 | Dashboard and live demo UX. Owns dashboard/demo routes and components. | A0 fixtures/types | Medium | Visual page shows masked VPA, score, reasons, lifecycle and latency for latest 50 events. |
| A9 | Simulator and labelled scenarios. Owns `data/scenarios`, generator/benchmark scripts and demo control route. | A0 contracts | Medium | Runs normal, bot-burst, NPCI-risk, duplicate-webhook and revoke-retry cases reproducibly. |
| A10 | Security, observability and CI. Owns threat model, redaction/logging tests, metrics, GitHub workflow. | A0 | Strong | Secret scan/lint/type/unit checks run in CI; logs do not contain raw VPAs or API secrets. |
| A11 | Optional ML shadow model. Owns isolated `services/model` and evaluation notebook/script. | A0, A5 snapshot format | Strong / data-capable | Generates a model score/explanation without changing rule decision; produces evaluation report. |
| A12 | Optional LiteLLM case-narrative agent. Owns LiteLLM config and asynchronous narrative worker/UI control. | A0, A1, A8 | Strong | A fake OpenAI-compatible endpoint receives redacted input only; a gateway outage does not affect webhooks. |

Easiest assignments are A7, A8 and A9; A2 and A4 are moderate. Reserve the strongest coding models for A0, A1, A3, A5, A6, A10, A11 and A12 because they involve contracts, payment security, transactional reliability, or evaluation. A11 and A12 are explicitly optional and should not delay the core demo.

### 8. Parallel execution and integration order

```text
Wave 0 (one strong agent): A0 contracts + fixtures + local stack
                                |
             contract tag / generated OpenAPI / fixture bundle
                                |
Wave 1 in parallel: A1 A2 A3 A4 A5 A7 A8 A9 A10
                                |
Wave 2 in parallel: A6 (uses A1+A3)  |  web integration (A7+A8)  | simulator-to-API (A9)
                                |
Wave 3: end-to-end demo, performance, security and visual acceptance
                                |
Optional Wave 4 in parallel: A11 shadow ML and A12 narrative agent
```

The actual runtime join is:

```text
checkout telemetry (A7) -> RiskSession (A2/A1) -> session ID in order mapping
Razorpay webhook (A3) -> stored event (A1) -> features (A4) -> score/reasons (A5)
                         -> assessment + audit/outbox (A1) -> revoke worker (A6)
                         -> dashboard event/API (A8)
scenario generator (A9) ------------------------------------------------^ 
security/CI (A10) validates every arrow; optional A11/A12 only read assessment outputs.
```

Merge sequence:

1. Merge A0 alone and tag it `contracts-v1`.
2. Rebase each Wave 1 branch on that tag. Agents may add files only in their owned paths; contract changes need a separate reviewed PR.
3. Merge A1, then A4/A5, then A2/A3, because the API routes plug into persistence and scoring. A7/A8/A9/A10 can merge independently once generated types are present.
4. Merge A6 after its mock-based tests pass against the outbox interface.
5. Run the docker-compose end-to-end suite and resolve only interface defects in a small integration branch. Do not merge optional ML/LLM work until the core suite is green.

Use one pull request per work package. Each PR includes: changed paths, the contract versions consumed/produced, commands run, screenshots for UI work, and known limitations. Do not let two agents edit `contracts/`, `docker-compose.yml`, generated API types or root package files concurrently.

### 9. Test and benchmark acceptance criteria

#### Per-package automated checks

| Area | Minimum acceptance test |
|---|---|
| Contracts | Every request/response/webhook fixture validates; breaking schema change fails generated-client/type check. |
| Webhook | HMAC uses the untouched raw body; invalid/missing signature is 4xx; unrelated event is acknowledged as ignored; same provider event ID is idempotent. |
| Session correlation | A webhook joins the pre-registration session by the server mapping, not webhook-source IP; absent mapping is safely marked `context_unavailable`. |
| Feature windows | With a frozen clock, counts expire exactly at the window; distinct events in the same second are not overwritten; simulated merchant data has a visible `demo_simulated` flag. |
| Rules | Golden inputs cover allow/challenge/block, score clamping, threshold 29/30/69/70, NPCI-risk rejection and every displayed reason. |
| Audit | Every decision/action produces an ordered audit event; tampering with an historical payload makes the hash-chain verifier fail. |
| Revoke worker | Uses a stable idempotency key, retries transient errors, does not retry permanent 4xx errors indefinitely, and never blocks webhook acknowledgement. |
| UI | Playwright can complete a pre-check challenge and can see the decision, reasons, masked identifiers, revoke status and latency. |
| LLM optional feature | Prompt contains only redacted structured facts; response is async; a failed provider leaves the assessment/action unchanged. |

#### End-product demonstration suite

Run the following from the dashboard's Demo page and save screenshots or a short recording:

1. **Legitimate flow:** send 20 distinct normal sessions. All should be `ALLOW`; false positives must be zero for this controlled fixture set.
2. **Bot burst:** send 20 registrations sharing device/IP and a sub-three-second flow. All should be visibly blocked or challenged according to the chosen fixture threshold; aim for at least 18 detected and explain the two allowed cases if the scenario intentionally includes borderline traffic.
3. **NPCI-risk rejected event:** display score 100 / strongest risk reason (according to the configured rule), while clearly showing that the mandate was already rejected and no revoke was attempted.
4. **Confirmed high-risk event:** show a `BLOCK`, audit-chain events, one queued revocation and its final mock/Test-mode status.
5. **Duplicate webhook:** replay the same signed payload. The UI count, risk assessment and revoke-attempt count must remain one.
6. **Worker failure and recovery:** force the first revoke call to fail, then show retry and final success without duplicate revocation.
7. **Shared demo merchant velocity:** use three demo merchant namespaces and display the explicit "simulated shared test namespace" badge.

#### Performance targets

Measure on the same Docker machine with Redis/PostgreSQL running locally and exclude third-party network latency from the decision target. Report sample size, p50 and p95 in the dashboard.

- 100 signed webhook fixtures: webhook-to-persisted-risk-assessment p95 under **200 ms**.
- 100 pre-check requests: p95 under **150 ms**.
- Event-to-dashboard visibility p95 under **1 second**.
- 100 duplicated webhook deliveries: exactly one assessment and, for a blocking confirmed event, exactly one revoke job.
- Audit-chain verification of 1,000 events completes under **5 seconds** locally.

If your machine cannot meet a number, show the real measured result and isolate the cause; never claim a provider API call is included in the 200 ms scoring latency.

### 10. Security, privacy and submission guardrails

- Verify Razorpay signatures with constant-time comparison over the exact raw request bytes.
- Use idempotency keys at webhook, assessment and action layers; write audit/outbox records in the same database transaction.
- Store a keyed HMAC of VPA/IP/device for lookup and velocity whenever possible; encrypt raw values only if genuinely needed, mask them everywhere in the UI, and set short retention policies.
- Put all API keys in a secret manager or local `.env`; commit only `.env.example`.
- Do not make a fraud/revocation decision from LLM output. Do not log webhook signatures, API secrets, raw VPAs or bank data.
- Document the demo simulation and all unavailable data sources in `docs/limitations-and-claims.md` and in the dashboard footer.
- Phrase the product honestly: it detects merchant-visible risk at/around registration, challenges before redirect when possible, and can request immediate post-confirmation revocation. It does not guarantee prevention of every debit or identify a person as a mule account.

### 11. First implementation milestone

For the strongest 1–2 day buildathon slice, finish A0–A10 except A11/A12, with one Razorpay adapter that has a mock mode and one Test-mode integration path. The winning demo is the complete auditable loop—not a complicated model:

```text
simulated suspicious mandate -> signed webhook -> <200 ms rule decision
-> visible reasons/audit -> one revoke job -> dashboard shows outcome
```

Only add shadow ML or the narrative agent after this loop passes every acceptance scenario.
