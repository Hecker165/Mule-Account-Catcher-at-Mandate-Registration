# Demo scenarios

A9 owns this directory: one declarative JSON definition per architecture demonstration
scenario, consumed by `DemoScenarioRunner` (demo control API) and
`scripts/generate_demo_events.py` (offline replay files). See
`docs/work-packages/A9_SIMULATOR_AND_SCENARIOS.md` for the exact schemas and parameters.

Each file is `{"name", "title", "description", "expected_outcome", "params": {...}}`.

## The seven scenarios

| File | What it proves |
|---|---|
| `legitimate_flow.json` | 20 distinct normal sessions in isolated namespaces (`demo_legit_01`..`demo_legit_20`), all `ALLOW`, zero false positives. Namespace isolation keeps the shared localhost peer IP of the demo from creating artificial IP velocity; in production, IP velocity across one merchant's customers is a true signal. |
| `bot_burst.json` | 24 registrations sharing one device and peer IP with a 2-second flow. Sessions 1-4 score 20 (`implausible_flow_duration` only) and stay `ALLOW`; from session 5 the device/IP 5-minute velocity rules add 25+25 for 70 (`CHALLENGE`). At least 18 of 24 challenged. |
| `npci_risk_rejection.json` | A `token.rejected` webhook with NPCI risk wording: score 100, `BLOCK` at `REJECTION_AUDIT`, and no revoke queued (the mandate was already rejected). |
| `confirmed_high_risk_block.json` | 21 seeded sessions plus a correlated confirmed webhook: `BLOCK`, a valid audit chain and exactly one queued revocation. |
| `duplicate_webhook.json` | The same signed payload delivered twice: one event, one assessment, one revoke. |
| `worker_failure_recovery.json` | One scheduled mock revoke failure, then retry and success: first attempt `RETRYING`, final `SUCCEEDED`, exactly two attempts, no duplicate revocation. |
| `shared_demo_merchant_velocity.json` | One device at three simulated demo merchants: the third session is challenged with the labelled demo counter at 3. |

## Re-run safety

Every run uses fresh device fingerprints (`demo-device-<uuid8>`) and fresh customer
references, so shared-merchant counts are exact and cross-run contamination is limited
to Redis velocity windows: re-running within 5 minutes may raise velocity counts (the
bot burst stays green; the legitimate flow stays green up to four runs per 5-minute
window). Generated replay files go to `data/scenarios/generated/` (git-ignored).
