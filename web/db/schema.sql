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

-- A connected account at an integration provider (Composio). Tokens stay at Composio;
-- `id` is Composio's connected-account id, whose user_id is the workspace id.
CREATE TABLE IF NOT EXISTS connections (
  id            text PRIMARY KEY,
  workspace_id  text NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  toolkit       text NOT NULL,
  status        text NOT NULL CHECK (status IN ('connecting', 'connected', 'error', 'disconnected')),
  error         text,
  created_by    text NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS connections_workspace ON connections (workspace_id, toolkit);

-- One sheet (tab) chosen for syncing, with the column mapping the user confirmed.
CREATE TABLE IF NOT EXISTS sheet_sources (
  id              bigserial PRIMARY KEY,
  workspace_id    text NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  connection_id   text NOT NULL REFERENCES connections(id) ON DELETE CASCADE,
  spreadsheet_id  text NOT NULL,
  spreadsheet_name text NOT NULL,
  sheet           text NOT NULL,
  mapping         jsonb NOT NULL,
  created_by      text NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (connection_id, spreadsheet_id, sheet)
);

CREATE TABLE IF NOT EXISTS sync_runs (
  id            bigserial PRIMARY KEY,
  workspace_id  text NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  source_id     bigint NOT NULL REFERENCES sheet_sources(id) ON DELETE CASCADE,
  started_at    timestamptz NOT NULL DEFAULT now(),
  finished_at   timestamptz,
  status        text NOT NULL CHECK (status IN ('running', 'succeeded', 'failed')),
  report        jsonb,
  error         text
);

CREATE INDEX IF NOT EXISTS sync_runs_source ON sync_runs (source_id, started_at DESC);

-- Telegram. The app issues a one-time code (only its hash is stored); the bot consumes it
-- from "/start <code>" and binds that chat to the workspace.
CREATE TABLE IF NOT EXISTS telegram_link_codes (
  code_hash     text PRIMARY KEY,
  workspace_id  text NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  user_id       text NOT NULL,
  expires_at    timestamptz NOT NULL,
  used_at       timestamptz
);

CREATE TABLE IF NOT EXISTS telegram_chats (
  chat_id       bigint PRIMARY KEY,          -- a chat serves one workspace at a time
  workspace_id  text NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  user_id       text NOT NULL,               -- Clerk user who linked it
  username      text,
  alerts        boolean NOT NULL DEFAULT true,
  linked_at     timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS telegram_chats_workspace ON telegram_chats (workspace_id);

-- One row per alert sent to a chat; the primary key is what stops repeats.
CREATE TABLE IF NOT EXISTS alert_deliveries (
  workspace_id  text NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  chat_id       bigint NOT NULL,
  fingerprint   text NOT NULL,
  statement     text NOT NULL,
  sent_at       timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (workspace_id, chat_id, fingerprint)
);

-- Provider details found when a connection is first used (e.g. Zoho's data centre and
-- organisation), and sync runs that belong to a whole connection rather than one sheet.
ALTER TABLE connections ADD COLUMN IF NOT EXISTS config jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE sync_runs ALTER COLUMN source_id DROP NOT NULL;
ALTER TABLE sync_runs ADD COLUMN IF NOT EXISTS connection_id text REFERENCES connections(id) ON DELETE CASCADE;
CREATE INDEX IF NOT EXISTS sync_runs_connection ON sync_runs (connection_id, started_at DESC);
