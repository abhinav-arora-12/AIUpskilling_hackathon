---
id: POL-LOT-002
title: Batch Safety Verification Protocol
domain: quality
risk_tier: 2
version: 1.4
tenants: [tenant_a, tenant_b]
---
# Batch Safety Verification Protocol

Use this protocol to verify a batch before its triage status is reported.

- Check the temperature log for the batch. An excursion above 8 C lasting more than 30 minutes fails the check.
- Triage status is one of GREEN (all checks pass), AMBER (one check fails and needs review), or RED (two or more checks fail; the lot is quarantined).
- Lots that show RED are handled under [[POL-COLD-001]] for storage and [[POL-LOT-001]] for release.
