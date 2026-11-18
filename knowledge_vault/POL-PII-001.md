---
id: POL-PII-001
title: PII Handling
domain: compliance
risk_tier: 2
version: 2.2
tenants: [tenant_a, tenant_b]
---
# PII Handling

Personal data includes email addresses, phone numbers, and national identifiers such as SSNs.

- Personal data must be redacted before it is stored in logs or sent to a model.
- Obfuscated forms such as name[at]domain.com and spaced digits are also personal data.
- Redacted conversation records are kept for 90 days, then deleted. See [[POL-TEN-001]].
