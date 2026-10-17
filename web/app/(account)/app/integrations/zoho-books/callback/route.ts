import { redirect } from "next/navigation";
import { finishConnection, ZOHO_BOOKS } from "@/lib/integrations";
import { requireWorkspace } from "@/lib/workspace";

// Composio sends the user back here after Zoho sign-in.
export async function GET() {
  const { workspace, userId } = await requireWorkspace();
  await finishConnection(workspace.id, ZOHO_BOOKS, userId);
  redirect("/app/integrations");
}
