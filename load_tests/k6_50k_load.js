// k6 load test for POST /api/v1/agent/query.
// Brief thresholds are kept as written. The ramp is scaled by PEAK_VUS (design.md D11, §12).
// Full 50K run: k6 run -e PEAK_VUS=50000 load_tests/k6_50k_load.js  (needs matching infrastructure)
// Scaled run:   k6 run -e PEAK_VUS=20 --summary-export data/results/k6_summary.json load_tests/k6_50k_load.js
//
// Deviation from brief: tenants are tenant_a / tenant_b (the vault's tenants), not tenant_0..tenant_49.
import http from 'k6/http';
import { check, sleep } from 'k6';

const PEAK = parseInt(__ENV.PEAK_VUS || '50', 10);
const BASE = __ENV.BASE_URL || 'http://127.0.0.1:8000';

export const options = {
  stages: [
    { duration: '1m', target: Math.max(1, Math.round(PEAK * 0.2)) },
    { duration: '1m', target: PEAK },
    { duration: '2m', target: PEAK },
    { duration: '30s', target: 0 },
  ],
  thresholds: {
    'http_req_duration': ['p(50)<250', 'p(95)<450', 'p(99)<600'], // Hard 200-600ms SLA (upper bound enforced)
    'http_req_failed': ['rate<0.0005'],                           // < 0.05% error rate
    'checks': ['rate>0.999'],
  },
};

export default function () {
  const vuId = __VU;
  const tenant = vuId % 2 === 0 ? 'tenant_a' : 'tenant_b';
  const payload = JSON.stringify({
    tenant_id: tenant,
    user_id: `user_${vuId}`,
    query: 'Verify batch LOT-88392 safety protocol and return triage status',
  });
  const params = { headers: { 'Content-Type': 'application/json', 'X-Tenant-ID': tenant } };
  const res = http.post(`${BASE}/api/v1/agent/query`, payload, params);
  check(res, {
    'status is 200': (r) => r.status === 200,
    'latency within 600ms SLA': (r) => r.timings.duration <= 600,
    'no cross-tenant leak': (r) => r.json('tenant_id') === tenant,
  });
  sleep(1);
}
