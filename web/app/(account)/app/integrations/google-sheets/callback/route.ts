import { redirect } from "next/navigation";
import { getAccount } from "@/lib/composio";
import { connectionFor, SHEETS, setConnection } from "@/lib/integrations";
import { audit, requireWorkspace } from "@/lib/workspace";

// Composio sends the user back here after Google sign-in. The query string is not trusted:
// the pending connection is looked up for this workspace and checked with Composio.
export async function GET() {
  const { workspace, userId } = await requireWorkspace();
  const pending = await connectionFor(workspace.id, SHEETS);
  if (!pending) redirect("/app/integrations");

  let status: "connected" | "connecting" | "error" = "error";
  let error: string | null = null;
  try {
    const account = await getAccount(pending.id);
    if (account.user_id !== workspace.id || account.toolkit?.slug !== SHEETS) {
      error = "The returned connection does not belong to this workspace.";
    } else if (account.status === "ACTIVE") {
      status = "connected";
    } else if (account.status === "INITIATED" || account.status === "INITIALIZING") {
      status = "connecting";
    } else {
      error = account.status_reason || `Google sign-in ended with status ${account.status}.`;
    }
  } catch (err) {
    error = err instanceof Error ? err.message : "The connection could not be checked.";
  }
  await setConnection(workspace.id, pending.id, SHEETS, status, userId, error);
  if (status === "connected") await audit(workspace.id, userId, "connection.connected", { toolkit: SHEETS });
  redirect(status === "connected" ? "/app/integrations/google-sheets" : "/app/integrations");
}
