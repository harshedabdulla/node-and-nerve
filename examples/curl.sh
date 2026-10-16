#!/usr/bin/env sh
# Example requests. Set COMPANY_BRAIN_API_KEY if authentication is enabled.
set -eu
BASE="${COMPANY_BRAIN_URL:-http://localhost:8000}"
KEY="${COMPANY_BRAIN_API_KEY:-}"
H='Content-Type: application/json'

curl -s "$BASE/health"; echo
curl -s "$BASE/ready"; echo

# Investigate a business question
curl -s -X POST "$BASE/v1/companies/northstar/investigations" -H "$H" -H "X-API-Key: $KEY" \
  -d '{"question": "Why is Project Atlas behind schedule, who owns the blockers?", "as_of": "2026-10-16"}'
echo

# Entity with its relationships
curl -s "$BASE/v1/companies/northstar/entities/ticket:atlas-142" -H "X-API-Key: $KEY"; echo

# Decision provenance: supporting vs. merely mentioning sources
curl -s "$BASE/v1/companies/northstar/decisions/decision:dec-004/sources" -H "X-API-Key: $KEY"; echo

# Bounded path discovery between two entities
curl -s "$BASE/v1/companies/northstar/connections?from=ticket:atlas-142&to=incident:inc-009&max_depth=3" \
  -H "X-API-Key: $KEY"; echo

# Record a change: reassign the blocked ticket (idempotent; safe to replay)
curl -s -X POST "$BASE/v1/companies/northstar/events" -H "$H" -H "X-API-Key: $KEY" \
  -d '{"type": "ownership_changed", "event_id": "evt-001", "entity_id": "ticket:atlas-142",
       "new_owner_id": "person:lena-ortiz", "occurred_at": "2026-10-16T10:00:00Z"}'
echo

# Cashflow Radar: validate CSV records without writing, then investigate
curl -s -X POST "$BASE/v1/companies/varsha-electricals/cashflow/imports" -H "$H" -H "X-API-Key: $KEY" \
  -d '{"files": {"customers.csv": "customer_id,name,city\nCUST-001,Shree Ganesh Contractors,Pune\n"},
       "dry_run": true}'
echo
curl -s -X POST "$BASE/v1/companies/varsha-electricals/cashflow/investigations" -H "$H" -H "X-API-Key: $KEY" \
  -d '{"as_of": "2026-10-16", "horizon_days": 7}'
echo
