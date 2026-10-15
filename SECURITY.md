# Security

## Reporting a vulnerability

Please open a private security advisory on GitHub (Security → Report a vulnerability) rather than a public issue.

## What the service does

- Validates all input with Pydantic; unknown fields and properties are rejected.
- Executes only fixed, named Cypher templates with parameterized values. There is no endpoint that accepts Cypher, and the store refuses raw strings. Read paths use `GRAPH.RO_QUERY`.
- Bounds traversals (max depth 4) and limits results. Applies a per-query timeout (`FALKORDB_QUERY_TIMEOUT_MS`); the bundled compose file also sets FalkorDB `TIMEOUT_MAX`.
- Isolates tenants in separate graphs. Graph names are derived from validated company ids with a fixed prefix; callers cannot select arbitrary graphs.
- Scopes API keys to companies and compares them in constant time. Unknown keys and keys for another company get the same `403`.
- Returns `503` when FalkorDB is unavailable instead of empty or fabricated results. Database error details are logged server-side, not returned.
- Never returns or logs FalkorDB credentials (`SecretStr`).

## Limitations — read before deploying

- **Authentication is off when `COMPANY_BRAIN_API_KEYS` is empty.** That mode is for local development only.
- API keys are static shared secrets from configuration: no users, roles, per-entity permissions, rotation, expiry or audit log. Anyone with a key can read and write everything in its companies.
- No rate limiting. Put the service behind a gateway or reverse proxy that provides TLS and rate limits.
- The bundled `docker-compose.yml` runs FalkorDB without a password and publishes ports 6379 and 3000 on the host. Set `--requirepass` (see the commented `REDIS_ARGS`), set `FALKORDB_PASSWORD`, and do not publish those ports outside a trusted network.
- Source `excerpt`s and names are stored as given. Do not ingest secrets or personal data you are not allowed to process.

This is not an enterprise authorization system and does not claim to be.
