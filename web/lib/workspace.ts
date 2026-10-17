import "server-only";

import { randomBytes } from "node:crypto";
import { auth, clerkClient } from "@clerk/nextjs/server";
import { redirect } from "next/navigation";
import { sql } from "@/lib/db";

// Authorization for the app. The workspace comes ONLY from the verified Clerk session
// (its active organization); no route accepts a workspace id from the client.

export interface Workspace {
  id: string; // also the FalkorDB graph id, always "ws-" + 12 characters
  clerk_org_id: string;
  name: string;
  created_at: string;
  onboarding: Record<string, unknown>;
}

export interface Session {
  userId: string;
  role: string | null; // "org:admin" or "org:member"
  workspace: Workspace;
}

const ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789";

export function newWorkspaceId(): string {
  const bytes = randomBytes(12);
  return "ws-" + Array.from(bytes, (b) => ALPHABET[b % ALPHABET.length]).join("");
}

/** The signed-in user's active workspace, creating its record on first visit. */
export async function requireWorkspace(): Promise<Session> {
  const { userId, orgId, orgRole } = await auth();
  if (!userId) redirect("/sign-in");
  if (!orgId) redirect("/app/new");

  const db = sql();
  let rows = (await db`SELECT id, clerk_org_id, name, created_at, onboarding
                       FROM workspaces WHERE clerk_org_id = ${orgId}`) as Workspace[];
  if (rows.length === 0) {
    const org = await (await clerkClient()).organizations.getOrganization({ organizationId: orgId });
    const id = newWorkspaceId();
    await db`INSERT INTO workspaces (id, clerk_org_id, name, created_by)
             VALUES (${id}, ${orgId}, ${org.name}, ${userId})
             ON CONFLICT (clerk_org_id) DO NOTHING`;
    rows = (await db`SELECT id, clerk_org_id, name, created_at, onboarding
                     FROM workspaces WHERE clerk_org_id = ${orgId}`) as Workspace[];
    if (rows[0]?.id === id) await audit(id, userId, "workspace.created", { name: org.name });
  }
  return { userId, role: orgRole ?? null, workspace: rows[0] };
}

export async function audit(
  workspaceId: string,
  actor: string,
  action: string,
  detail: Record<string, unknown> = {},
): Promise<void> {
  await sql()`INSERT INTO audit_log (workspace_id, actor, action, detail)
              VALUES (${workspaceId}, ${actor}, ${action}, ${JSON.stringify(detail)}::jsonb)`;
}

export async function recentActivity(workspaceId: string) {
  return (await sql()`SELECT action, detail, at FROM audit_log
                      WHERE workspace_id = ${workspaceId}
                      ORDER BY at DESC LIMIT 10`) as {
    action: string;
    detail: Record<string, unknown>;
    at: string;
  }[];
}

// ---------------------------------------------------------------- the workspace graph

export interface GraphSummary {
  nodes_by_kind: Record<string, number>;
  relationships_by_type: Record<string, number>;
  total_nodes: number;
  total_relationships: number;
  last_updated: string | null;
}

/** What the workspace's graph holds. `null` means no graph exists yet (nothing synced). */
export async function graphSummary(workspace: Workspace): Promise<GraphSummary | null> {
  const base = process.env.NN_API_URL?.replace(/\/$/, "");
  const key = process.env.NN_APP_API_KEY; // scoped to ws-* only
  if (!base || !key) throw new Error("the graph API is not configured");
  const res = await fetch(`${base}/v1/companies/${workspace.id}/summary`, {
    headers: { "x-api-key": key },
    cache: "no-store",
  });
  if (res.status === 404) return null;
  if (!res.ok) throw new Error(`the graph API answered ${res.status}`);
  return (await res.json()) as GraphSummary;
}

// ---------------------------------------------------------------- investigations

export interface Investigation {
  question: string;
  matched_entities: { id: string; name: string }[];
  findings: { category: string; statement: string; supported: boolean; source_ids: string[] }[];
  missing_information: string[];
  warnings: string[];
  sources: { id: string; name: string; uri: string | null; source_record_id: string | null }[];
  queries_executed: string[];
}

async function graphApi<T>(workspace: Workspace, path: string, init?: RequestInit): Promise<T | null> {
  const base = process.env.NN_API_URL?.replace(/\/$/, "");
  const key = process.env.NN_APP_API_KEY;
  if (!base || !key) throw new Error("the graph API is not configured");
  const res = await fetch(`${base}/v1/companies/${workspace.id}${path}`, {
    ...init,
    headers: { "x-api-key": key, "content-type": "application/json" },
    cache: "no-store",
  });
  if (res.status === 404) return null;
  if (!res.ok) throw new Error(`the graph API answered ${res.status}`);
  return (await res.json()) as T;
}

/** The investigation for each project in the workspace graph; null if there is no graph. */
export async function attention(workspace: Workspace): Promise<Investigation[] | null> {
  const r = await graphApi<{ projects: Investigation[] }>(workspace, "/attention");
  return r ? r.projects : null;
}

export function ask(workspace: Workspace, question: string): Promise<Investigation | null> {
  return graphApi<Investigation>(workspace, "/investigations", {
    method: "POST",
    body: JSON.stringify({ question }),
  });
}

/** Engine statements name graph ids; show people by name and records by their reference. */
export function readable(statement: string): string {
  return statement
    .replace(/\b(?:person|team):[a-z0-9._-]+ \(([^)]+)\)/g, "$1")
    .replace(/\bticket:([a-z0-9._-]+)/g, (_, s: string) => s.toUpperCase())
    .replace(/\b(?:project|decision|incident|component|team|person):([a-z0-9._-]+)/g, (_, s: string) =>
      s.replace(/-/g, " ").replace(/^./, (c) => c.toUpperCase()),
    )
    .replace(/\bin_progress\b/g, "in progress");
}

export interface AskResult {
  answer: string | null;
  model: string | null;
  fallback_reason: string | null;
  investigations: (Investigation & {
    current_owners: { entity_id: string; owner_id: string; owner_kind: string; owner_name: string | null; source_id: string | null }[];
    evidence_paths: {
      id: string;
      edges: { type: string; from_id: string; to_id: string; source_id: string | null }[];
    }[];
  })[];
  labels: Record<string, { kind: string; ref: string; name: string | null; status: string | null }>;
}

/** A plain-language answer (or the deterministic one) with the graph behind it. */
export function askWithModel(workspace: Workspace, question: string): Promise<AskResult | null> {
  return graphApi<AskResult>(workspace, "/ask", {
    method: "POST",
    body: JSON.stringify({ question }),
  });
}
