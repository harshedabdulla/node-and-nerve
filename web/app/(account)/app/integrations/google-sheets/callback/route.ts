import { redirect } from "next/navigation";
import { finishConnection, SHEETS } from "@/lib/integrations";
import { requireWorkspace } from "@/lib/workspace";

// Composio sends the user back here after Google sign-in.
export async function GET() {
  const { workspace, userId } = await requireWorkspace();
  const status = await finishConnection(workspace.id, SHEETS, userId);
  redirect(status === "connected" ? "/app/integrations/google-sheets" : "/app/integrations");
}
