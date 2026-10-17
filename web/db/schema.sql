-- Node & Nerve control plane. Company knowledge lives in FalkorDB, one graph per
-- workspace; this database holds who may see which workspace and how it is set up.
-- Every statement is idempotent: `npm run db:migrate` can run any number of times.

CREATE TABLE IF NOT EXISTS workspaces (
  id            text PRIMARY KEY CHECK (id ~ '^ws-[a-z0-9]{12}$'),  -- also the graph id
  clerk_org_id  text NOT NULL UNIQUE,                                -- Clerk organization
  name          text NOT NULL,
  created_by    text NOT NULL,                                       -- Clerk user id
  created_at    timestamptz NOT NULL DEFAULT now(),
  onboarding    jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS audit_log (
  id            bigserial PRIMARY KEY,
  workspace_id  text NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  actor         text NOT NULL,
  action        text NOT NULL,
  detail        jsonb NOT NULL DEFAULT '{}'::jsonb,
  at            timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS audit_log_workspace_at ON audit_log (workspace_id, at DESC);
