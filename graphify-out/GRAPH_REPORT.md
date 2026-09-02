# Graph Report - .  (2026-09-02)

## Corpus Check
- Corpus is ~3,134 words - fits in a single context window. You may not need a graph.

## Summary
- 20 nodes · 19 edges · 4 communities (3 shown, 1 thin omitted)
- Extraction: 89% EXTRACTED · 11% INFERRED · 0% AMBIGUOUS · INFERRED: 2 edges (avg confidence: 0.7)
- Token cost: 0 input · 0 output

## Community Hubs (Navigation)
- Registration Signals
- Decision Actions and Audit
- Domain Scope and Constraints
- Velocity Intelligence

## God Nodes (most connected - your core abstractions)
1. `Feature Extraction Engine` - 8 edges
2. `Risk Scoring Model` - 3 edges
3. `Allow Challenge Block Decisioning` - 3 edges
4. `Mule Account Catcher at Mandate Registration Idea` - 2 edges
5. `UPI Autopay Mandate Registration` - 2 edges
6. `Razorpay Webhook Receiver` - 2 edges
7. `token.confirmed Webhook Event` - 2 edges
8. `token.rejected Webhook Event` - 2 edges
9. `Mandate Velocity Features` - 2 edges
10. `Step-Up Authentication` - 2 edges

## Surprising Connections (you probably didn't know these)
- `Feature Extraction Engine` --shares_data_with--> `Risk Scoring Model`  [EXTRACTED]
  Idea.md → Idea.md  _Bridges community 0 → community 1_
- `Feature Extraction Engine` --implements--> `Mandate Velocity Features`  [EXTRACTED]
  Idea.md → Idea.md  _Bridges community 0 → community 3_

## Hyperedges (group relationships)
- **Mandate Registration Risk Pipeline** — idea_razorpay_webhook_receiver, idea_feature_extraction_engine, idea_risk_scoring_model, idea_allow_challenge_block, idea_audit_dashboard [EXTRACTED 1.00]
- **Risk Feature Categories** — idea_velocity_features, idea_device_behavior_features, idea_network_features, idea_npci_risk_flag, idea_mandate_parameter_features [EXTRACTED 1.00]

## Communities (4 total, 1 thin omitted)

### Community 0 - "Registration Signals"
Cohesion: 0.29
Nodes (8): Device and Behavioral Features, Feature Extraction Engine, Mandate Parameter Features, Network Reputation Features, NPCI Registration-Time Risk Flag, Razorpay Webhook Receiver, token.confirmed Webhook Event, token.rejected Webhook Event

### Community 1 - "Decision Actions and Audit"
Cohesion: 0.33
Nodes (6): Allow Challenge Block Decisioning, Explainable Audit Dashboard, Razorpay Token Revoke Action, Risk Scoring Model, Step-Up Authentication, Third-Party Validation

### Community 2 - "Domain Scope and Constraints"
Cohesion: 0.50
Nodes (4): Mule Account Catcher at Mandate Registration Idea, Mule Account, MuleHunter.AI, UPI Autopay Mandate Registration

## Knowledge Gaps
- **9 isolated node(s):** `Mule Account`, `Device and Behavioral Features`, `Network Reputation Features`, `NPCI Registration-Time Risk Flag`, `Mandate Parameter Features` (+4 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **1 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `Feature Extraction Engine` connect `Registration Signals` to `Decision Actions and Audit`, `Velocity Intelligence`?**
  _High betweenness centrality (0.506) - this node is a cross-community bridge._
- **Why does `Risk Scoring Model` connect `Decision Actions and Audit` to `Registration Signals`?**
  _High betweenness centrality (0.316) - this node is a cross-community bridge._
- **What connects `Mule Account`, `Device and Behavioral Features`, `Network Reputation Features` to the rest of the system?**
  _9 weakly-connected nodes found - possible documentation gaps or missing edges._