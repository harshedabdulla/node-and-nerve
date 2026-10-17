import "server-only";

import { getAccount } from "@/lib/composio";
import { sql } from "@/lib/db";
import { readSheet } from "@/lib/sheets";
import { audit, type Workspace } from "@/lib/workspace";

export const SHEETS = "googlesheets";

export interface Connection {
  id: string;
  toolkit: string;
  status: "connecting" | "connected" | "error" | "disconnected";
  error: string | null;
  updated_at: string;
}

export interface TaskMapping {
  id: string;
  name: string;
  status?: string;
  owner?: string;
  due?: string;
  project?: string;
  blocked_by?: string;
  priority?: string;
}

export interface SheetSource {
  id: number;
  connection_id: string;
  spreadsheet_id: string;
  spreadsheet_name: string;
  sheet: string;
  mapping: TaskMapping;
  last_status: "running" | "succeeded" | "failed" | null;
  last_at: string | null;
  last_report: SyncReport | null;
  last_error: string | null;
}

export interface SyncReport {
  rows_read: number;
  tasks: number;
  people: number;
  projects: number;
  links: number;
  sources: number;
  skipped_rows: number;
  warnings: string[];
  written: boolean;
}

/** The workspace's current connection for a toolkit (latest, not disconnected). */
export async function connectionFor(workspaceId: string, toolkit: string): Promise<Connection | null> {
  const rows = (await sql()`SELECT id, toolkit, status, error, updated_at FROM connections
    WHERE workspace_id = ${workspaceId} AND toolkit = ${toolkit} AND status <> 'disconnected'
    ORDER BY updated_at DESC LIMIT 1`) as Connection[];
  return rows[0] ?? null;
}

/**
 * A connection still "connecting" is re-checked with Composio, so a sign-in that was
 * abandoned or failed shows as such instead of waiting forever.
 */
export async function refreshPending(
  workspaceId: string,
  connection: Connection | null,
  actor: string,
): Promise<Connection | null> {
  if (!connection || connection.status !== "connecting") return connection;
  try {
    const account = await getAccount(connection.id);
    if (account.user_id !== workspaceId) return connection;
    if (account.status === "ACTIVE") {
      await setConnection(workspaceId, connection.id, connection.toolkit, "connected", actor);
      return { ...connection, status: "connected", error: null };
    }
    if (account.status === "INITIATED" || account.status === "INITIALIZING") return connection;
    const error =
      account.status_reason || `Google sign-in ended with status ${account.status.toLowerCase()}.`;
    await setConnection(workspaceId, connection.id, connection.toolkit, "error", actor, error);
    return { ...connection, status: "error", error };
  } catch {
    return connection; // Composio unreachable: keep the last known state
  }
}

export async function setConnection(
  workspaceId: string,
  id: string,
  toolkit: string,
  status: Connection["status"],
  actor: string,
  error: string | null = null,
): Promise<void> {
  await sql()`INSERT INTO connections (id, workspace_id, toolkit, status, error, created_by)
    VALUES (${id}, ${workspaceId}, ${toolkit}, ${status}, ${error}, ${actor})
    ON CONFLICT (id) DO UPDATE SET status = EXCLUDED.status, error = EXCLUDED.error,
      updated_at = now()
    WHERE connections.workspace_id = ${workspaceId}`;
}

export async function sourcesFor(workspaceId: string): Promise<SheetSource[]> {
  return (await sql()`SELECT s.id, s.connection_id, s.spreadsheet_id, s.spreadsheet_name, s.sheet,
      s.mapping, r.status AS last_status, COALESCE(r.finished_at, r.started_at) AS last_at,
      r.report AS last_report, r.error AS last_error
    FROM sheet_sources s
    JOIN connections c ON c.id = s.connection_id AND c.status <> 'disconnected'
    LEFT JOIN LATERAL (
      SELECT status, started_at, finished_at, report, error FROM sync_runs
      WHERE source_id = s.id ORDER BY started_at DESC LIMIT 1
    ) r ON true
    WHERE s.workspace_id = ${workspaceId}
    ORDER BY s.created_at`) as SheetSource[];
}

export async function saveSource(
  workspaceId: string,
  connectionId: string,
  spreadsheetId: string,
  spreadsheetName: string,
  sheet: string,
  mapping: TaskMapping,
  actor: string,
): Promise<number> {
  const rows = (await sql()`INSERT INTO sheet_sources
      (workspace_id, connection_id, spreadsheet_id, spreadsheet_name, sheet, mapping, created_by)
    VALUES (${workspaceId}, ${connectionId}, ${spreadsheetId}, ${spreadsheetName}, ${sheet},
      ${JSON.stringify(mapping)}::jsonb, ${actor})
    ON CONFLICT (connection_id, spreadsheet_id, sheet)
      DO UPDATE SET mapping = EXCLUDED.mapping, spreadsheet_name = EXCLUDED.spreadsheet_name
    RETURNING id`) as { id: number }[];
  return rows[0].id;
}

/**
 * Read the sheet through Composio and import it into the workspace graph. A run already in
 * progress for the same source (started in the last 10 minutes) is not started again.
 */
export async function syncSource(
  workspace: Workspace,
  sourceId: number,
  actor: string,
): Promise<{ ok: true; report: SyncReport } | { ok: false; error: string }> {
  const db = sql();
  const found = (await db`SELECT s.id, s.connection_id, s.spreadsheet_id, s.spreadsheet_name,
      s.sheet, s.mapping FROM sheet_sources s
    JOIN connections c ON c.id = s.connection_id AND c.status = 'connected'
    WHERE s.id = ${sourceId} AND s.workspace_id = ${workspace.id}`) as SheetSource[];
  const source = found[0];
  if (!source) return { ok: false, error: "This sheet is not set up in this workspace." };

  const started = (await db`INSERT INTO sync_runs (workspace_id, source_id, status)
    SELECT ${workspace.id}, ${source.id}, 'running'
    WHERE NOT EXISTS (SELECT 1 FROM sync_runs WHERE source_id = ${source.id}
      AND status = 'running' AND started_at > now() - interval '10 minutes')
    RETURNING id`) as { id: number }[];
  if (!started[0]) return { ok: false, error: "A sync for this sheet is already running." };
  const runId = started[0].id;

  try {
    const values = await readSheet(
      workspace.id,
      source.connection_id,
      source.spreadsheet_id,
      source.sheet,
    );
    if (values.length === 0) throw new Error("the sheet is empty");
    const [header, ...rows] = values;
    const res = await fetch(
      `${process.env.NN_API_URL?.replace(/\/$/, "")}/v1/companies/${workspace.id}/imports/tasks`,
      {
        method: "POST",
        headers: { "content-type": "application/json", "x-api-key": process.env.NN_APP_API_KEY ?? "" },
        body: JSON.stringify({
          source_system: "google_sheets",
          document_id: source.spreadsheet_id,
          document_name: source.spreadsheet_name,
          table: source.sheet,
          uri: `https://docs.google.com/spreadsheets/d/${source.spreadsheet_id}`,
          header,
          rows,
          mapping: source.mapping,
        }),
        cache: "no-store",
      },
    );
    const body = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(body?.detail ? String(body.detail).slice(0, 300) : `graph API ${res.status}`);
    const report = body as SyncReport;
    await db`UPDATE sync_runs SET status = 'succeeded', finished_at = now(),
      report = ${JSON.stringify(report)}::jsonb WHERE id = ${runId}`;
    await audit(workspace.id, actor, "sheet.synced", {
      sheet: `${source.spreadsheet_name} / ${source.sheet}`,
      tasks: report.tasks,
      warnings: report.warnings.length,
    });
    return { ok: true, report };
  } catch (err) {
    const message = err instanceof Error ? err.message : "the sync failed";
    await db`UPDATE sync_runs SET status = 'failed', finished_at = now(), error = ${message}
      WHERE id = ${runId}`;
    await audit(workspace.id, actor, "sheet.sync_failed", { error: message });
    return { ok: false, error: message };
  }
}
