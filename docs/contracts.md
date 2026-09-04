# Canonical API and Event Contracts

The canonical source of truth for all data shapes is the Pydantic models in `apps/api/app/contracts/`.

**Do not hand-edit TypeScript interfaces.** Browser types are generated automatically via `openapi-typescript` into `apps/web/src/lib/api-types.ts` from the OpenAPI JSON exported by the API.

## Contract Catalogue

| Model Name | Producer | Consumers | Responsibility |
|---|---|---|---|
| `RiskSessionCreateRequest` | Checkout UI | Session API | Initial creation of a risk tracking session at checkout. |
| `RiskSessionTelemetryUpdateRequest` | Checkout UI | Session API | Update session with telemetry captured during the flow. |
| `RiskSession` | Session API | Web app, Webhook Join | Holds pseudonymised session data, timing, and mandate intent. |
| `MandateWebhookEvent` | Webhook Adapter | Feature Extractor, Audit | Normalised webhook event stripped of raw bodies and credentials. |
| `FeatureSnapshot` | Feature Extractor | Rule Engine, Dashboard | Immutable point-in-time feature vector with data provenance. |
| `RiskAssessment` | Rule Engine | Outbox, Dashboard | Holds the score, decision, evaluated rules, and latency. |
| `ActionRequest` | Decision API | Revoke Worker | Queued request for a downstream action like token revocation. |
| `ActionAttempt` | Revoke Worker | Dashboard | Result of a single execution attempt of an action request. |
| `AuditEvent` | All Handlers | Dashboard | Hash-chained event forming an immutable audit trail. |
