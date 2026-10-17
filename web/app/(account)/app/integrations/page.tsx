import Link from "next/link";
import { connectionFor, refreshPending, SHEETS } from "@/lib/integrations";
import { requireWorkspace } from "@/lib/workspace";
import { connectSheets, disconnectSheets } from "./actions";

export const metadata = { title: "Integrations: Node & Nerve" };

const STATE = {
  none: { label: "Not connected", tone: "state-blocked" },
  connecting: { label: "Waiting for Google sign-in to finish", tone: "state-building" },
  connected: { label: "Connected", tone: "state-live" },
  error: { label: "Connection failed", tone: "state-building" },
} as const;

export default async function Integrations({
  searchParams,
}: {
  searchParams: Promise<{ error?: string }>;
}) {
  const { workspace, role, userId } = await requireWorkspace();
  const { error } = await searchParams;
  const admin = role === "org:admin";
  const sheets = await refreshPending(workspace.id, await connectionFor(workspace.id, SHEETS), userId);
  const state = STATE[sheets?.status === "disconnected" || !sheets ? "none" : sheets.status];

  return (
    <section className="app-section">
      <h1 className="app-title">Integrations</h1>
      <p className="app-lede">
        Sources feed {workspace.name}&rsquo;s graph; channels deliver answers and alerts. Sign-in
        happens at the provider, so Node &amp; Nerve never sees your passwords or tokens.
      </p>
      {error && <p className="error-panel">{error}</p>}

      <div className="connectors">
        <div className="connector">
          <span className="connector-name">Google Sheets</span>
          <div>
            <p>
              Read task lists you choose: ids, owners, status, due dates, projects and what each
              task waits on. Read-only; nothing is written back to your sheets.
            </p>
            {sheets?.status === "error" && sheets.error && <p className="note">{sheets.error}</p>}
            <div className="connector-actions">
              {sheets?.status === "connected" ? (
                <>
                  <Link href="/app/integrations/google-sheets" className="btn btn-primary">
                    Choose sheets
                  </Link>
                  {admin && (
                    <form action={disconnectSheets}>
                      <button className="btn btn-quiet" type="submit">
                        Disconnect
                      </button>
                    </form>
                  )}
                </>
              ) : admin ? (
                <form action={connectSheets}>
                  <button className="btn btn-primary" type="submit">
                    {sheets ? "Connect again" : "Connect Google Sheets"}
                  </button>
                </form>
              ) : (
                <p className="note">Ask a workspace admin to connect it.</p>
              )}
              {sheets?.status === "connecting" && (
                <Link href="/app/integrations/google-sheets/callback" className="btn btn-quiet">
                  Check again
                </Link>
              )}
            </div>
          </div>
          <span className={`state ${state.tone}`}>{state.label}</span>
        </div>

        <div className="connector">
          <span className="connector-name">Telegram</span>
          <p>Get answers and alerts in a private chat with the Node &amp; Nerve bot.</p>
          <span className="state state-blocked">Linking a chat is being built</span>
        </div>

        <div className="connector">
          <span className="connector-name">Zoho Books</span>
          <p>
            The account connects through Composio, but it exposes no read actions for Zoho Books,
            so nothing could be synced. Shown here so the gap is visible.
          </p>
          <span className="state state-blocked">Waiting on connector</span>
        </div>
      </div>
    </section>
  );
}
