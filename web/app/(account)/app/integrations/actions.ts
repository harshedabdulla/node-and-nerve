"use server";

import { headers } from "next/headers";
import { redirect } from "next/navigation";
import { deleteAccount, startConnection } from "@/lib/composio";
import {
  connectionFor,
  SHEETS,
  saveSource,
  setConnection,
  syncSource,
  type TaskMapping,
} from "@/lib/integrations";
import { audit, requireWorkspace } from "@/lib/workspace";

// Every action re-derives the workspace from the session; nothing trusts ids from the form
// except references that are then checked against that workspace.

async function requireAdmin() {
  const session = await requireWorkspace();
  if (session.role !== "org:admin") {
    redirect("/app/integrations?error=" + encodeURIComponent("Only workspace admins can change integrations."));
  }
  return session;
}

async function origin(): Promise<string> {
  const h = await headers();
  const host = h.get("x-forwarded-host") ?? h.get("host");
  const proto = h.get("x-forwarded-proto") ?? (host?.startsWith("localhost") ? "http" : "https");
  return `${proto}://${host}`;
}

export async function connectSheets() {
  const { workspace, userId } = await requireAdmin();
  const callback = `${await origin()}/app/integrations/google-sheets/callback`;
  const { redirectUrl, accountId } = await startConnection(SHEETS, workspace.id, callback);
  await setConnection(workspace.id, accountId, SHEETS, "connecting", userId);
  await audit(workspace.id, userId, "connection.started", { toolkit: SHEETS });
  redirect(redirectUrl);
}

export async function disconnectSheets() {
  const { workspace, userId } = await requireAdmin();
  const connection = await connectionFor(workspace.id, SHEETS);
  if (connection) {
    try {
      await deleteAccount(connection.id);
    } catch {
      // Already gone at Composio; still mark it disconnected here.
    }
    await setConnection(workspace.id, connection.id, SHEETS, "disconnected", userId);
    await audit(workspace.id, userId, "connection.removed", { toolkit: SHEETS });
  }
  redirect("/app/integrations");
}

const FIELDS = ["status", "owner", "due", "project", "blocked_by", "priority"] as const;

export async function saveAndSync(form: FormData) {
  const { workspace, userId } = await requireAdmin();
  const connection = await connectionFor(workspace.id, SHEETS);
  if (!connection || connection.status !== "connected") redirect("/app/integrations");
  const spreadsheetId = String(form.get("spreadsheet_id") ?? "");
  const spreadsheetName = String(form.get("spreadsheet_name") ?? "").slice(0, 300);
  const sheet = String(form.get("sheet") ?? "").slice(0, 200);
  const mapping: TaskMapping = {
    id: String(form.get("id") ?? ""),
    name: String(form.get("name") ?? ""),
  };
  for (const f of FIELDS) {
    const v = String(form.get(f) ?? "");
    if (v) mapping[f] = v;
  }
  const back = `/app/integrations/google-sheets?spreadsheet=${encodeURIComponent(spreadsheetId)}&name=${encodeURIComponent(spreadsheetName)}&sheet=${encodeURIComponent(sheet)}`;
  if (!spreadsheetId || !sheet || !mapping.id || !mapping.name) {
    redirect(`${back}&error=${encodeURIComponent("Choose the id column and the task column.")}`);
  }
  const sourceId = await saveSource(
    workspace.id,
    connection.id,
    spreadsheetId,
    spreadsheetName,
    sheet,
    mapping,
    userId,
  );
  await syncSource(workspace, sourceId, userId);
  redirect("/app/integrations/google-sheets");
}

export async function syncNow(form: FormData) {
  const { workspace, userId } = await requireAdmin();
  const result = await syncSource(workspace, Number(form.get("source_id")), userId);
  redirect(
    "/app/integrations/google-sheets" +
      (result.ok ? "" : `?error=${encodeURIComponent(result.error)}`),
  );
}
