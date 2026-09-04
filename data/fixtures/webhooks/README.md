# Webhook Fixtures

These are payload-shape examples for A3 integration testing, not valid live signatures.

- Signatures (`x-razorpay-signature`) are hardcoded to `fixture-not-a-real-signature` and will fail standard verification.
- No real VPAs, account IDs, or tokens are included.
- A3 must use a test-mode / override flag to allow processing these in its own unit tests.
