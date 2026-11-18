---
id: POL-TEN-001
title: Tenant Data Isolation
domain: security
risk_tier: 1
version: 1.1
tenants: [tenant_a, tenant_b]
---
# Tenant Data Isolation

- A tenant can only read documents it is entitled to.
- Every cache key and every database query includes the tenant identifier.
- Cross-tenant access attempts are blocked and recorded in the audit log. See [[POL-PII-001]].
