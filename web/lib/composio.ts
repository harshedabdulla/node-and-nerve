import "server-only";

// Composio REST v3, server side only. Composio holds the OAuth tokens; we keep only the
// connected-account id and its status. Composio's user_id is always our workspace id, so a
// connected account can be checked against the workspace that claims it.

const BASE = "https://backend.composio.dev/api/v3";

export class ComposioError extends Error {}

async function call<T>(method: string, path: string, body?: unknown): Promise<T> {
  const key = process.env.COMPOSIO_API_KEY;
  if (!key) throw new ComposioError("COMPOSIO_API_KEY is not configured");
  const res = await fetch(`${BASE}${path}`, {
    method,
    headers: { "x-api-key": key, "content-type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
    cache: "no-store",
  });
  const text = await res.text();
  if (!res.ok) {
    let detail = text.slice(0, 300);
    try {
      const j = JSON.parse(text);
      detail = j?.error?.message ?? j?.message ?? detail;
    } catch {}
    throw new ComposioError(`Composio ${method} ${path.split("?")[0]} failed (${res.status}): ${detail}`);
  }
  return (text ? JSON.parse(text) : {}) as T;
}

/** An auth config for the toolkit, creating a Composio-managed one if none exists. */
async function authConfigId(toolkit: string): Promise<string> {
  const existing = await call<{ items: { id: string }[] }>(
    "GET",
    `/auth_configs?toolkit_slug=${encodeURIComponent(toolkit)}`,
  );
  if (existing.items?.[0]?.id) return existing.items[0].id;
  const created = await call<{ auth_config: { id: string } }>("POST", "/auth_configs", {
    toolkit: { slug: toolkit },
    auth_config: { type: "use_composio_managed_auth" },
  });
  return created.auth_config.id;
}

export async function startConnection(toolkit: string, workspaceId: string, callbackUrl: string) {
  const r = await call<{ redirect_url: string; connected_account_id: string }>(
    "POST",
    "/connected_accounts/link",
    { auth_config_id: await authConfigId(toolkit), user_id: workspaceId, callback_url: callbackUrl },
  );
  return { redirectUrl: r.redirect_url, accountId: r.connected_account_id };
}

export interface ConnectedAccount {
  id: string;
  user_id: string;
  status: string; // INITIATED, ACTIVE, FAILED, EXPIRED, ...
  status_reason: string | null;
  toolkit: { slug: string };
}

export function getAccount(id: string): Promise<ConnectedAccount> {
  return call("GET", `/connected_accounts/${encodeURIComponent(id)}`);
}

export async function deleteAccount(id: string): Promise<void> {
  await call("DELETE", `/connected_accounts/${encodeURIComponent(id)}`);
}

/** Runs a read tool for the workspace's own connected account (checked first). */
export async function runTool<T = Record<string, unknown>>(
  workspaceId: string,
  accountId: string,
  tool: string,
  args: Record<string, unknown>,
): Promise<T> {
  const account = await getAccount(accountId);
  if (account.user_id !== workspaceId) {
    throw new ComposioError("this connection does not belong to the workspace");
  }
  if (account.status !== "ACTIVE") {
    throw new ComposioError(`the connection is ${account.status.toLowerCase()}; reconnect it`);
  }
  const r = await call<{ successful: boolean; error: string | null; data: T }>(
    "POST",
    `/tools/execute/${encodeURIComponent(tool)}`,
    { connected_account_id: accountId, user_id: workspaceId, arguments: args },
  );
  if (!r.successful) throw new ComposioError(r.error ?? `${tool} failed`);
  return r.data;
}
