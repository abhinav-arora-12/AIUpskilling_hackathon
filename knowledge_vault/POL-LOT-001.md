---
id: POL-LOT-001
title: Lot Release Procedure
domain: quality
risk_tier: 3
version: 2.1
tenants: [tenant_a, tenant_b]
---
# Lot Release Procedure

This policy governs release of finished product lots (format LOT-#####) to shipping.

- QA sign-off is required within 48 hours of test completion. Lots without sign-off in that window must be re-tested.
- Lots older than 30 days at release require a second reviewer before release.
- Releasing a lot changes its state in the warehouse system, so it is a Tier 3 mutation and needs reviewer approval (see [[SOP-HITL-001]]).
- Lot safety status is checked with the [[POL-LOT-002]] protocol before release.
