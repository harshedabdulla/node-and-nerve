import { timingSafeEqual } from "node:crypto";
import { sql } from "@/lib/db";
import { syncSource } from "@/lib/integrations";
import type { Workspace } from "@/lib/workspace";

// Daily: re-sync every connected sheet. Each sync then sends alerts for newly blocked or
// overdue work. Vercel Cron calls this with "Authorization: Bearer $CRON_SECRET".
export const maxDuration = 60;

function authorized(header: string | null): boolean {
  const secret = process.env.CRON_SECRET;
  if (!secret || !header) return false;
  const a = Buffer.from(header);
  const b = Buffer.from(`Bearer ${secret}`);
  return a.length === b.length && timingSafeEqual(a, b);
}

export async function GET(request: Request) {
  if (!authorized(request.headers.get("authorization"))) {
    return Response.json({ error: "unauthorized" }, { status: 401 });
  }
  const rows = (await sql()`SELECT s.id AS source_id, w.id, w.clerk_org_id, w.name, w.created_at,
      w.onboarding
    FROM sheet_sources s
    JOIN connections c ON c.id = s.connection_id AND c.status = 'connected'
    JOIN workspaces w ON w.id = s.workspace_id
    ORDER BY s.id`) as (Workspace & { source_id: number })[];
  const results = [];
  for (const { source_id, ...workspace } of rows) {
    const r = await syncSource(workspace, source_id, "scheduled sync");
    results.push({ source_id, ok: r.ok, ...(r.ok ? { tasks: r.report.tasks } : { error: r.error }) });
  }
  return Response.json({ synced: results.length, results });
}
