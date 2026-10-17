import Link from "next/link";
import { connectionFor, refreshPending, SHEETS, telegramChats, ZOHO_BOOKS } from "@/lib/integrations";
import { requireWorkspace } from "@/lib/workspace";
import { connectToolkit, disconnectToolkit, unlinkTelegramChat } from "./actions";
import { TelegramLink } from "./telegram-link";

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
  const chats = await telegramChats(workspace.id);
  const zoho = await refreshPending(workspace.id, await connectionFor(workspace.id, ZOHO_BOOKS), userId);
  const zohoState = STATE[!zoho || zoho.status === "disconnected" ? "none" : zoho.status];
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
                    <form action={disconnectToolkit}>
                      <input type="hidden" name="toolkit" value={SHEETS} />
                      <button className="btn btn-quiet" type="submit">
                        Disconnect
                      </button>
                    </form>
                  )}
                </>
              ) : admin ? (
                <form action={connectToolkit}>
                  <input type="hidden" name="toolkit" value={SHEETS} />
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
          <div>
            <p>
              Ask about {workspace.name} in a private chat with the Node &amp; Nerve bot, and get an
              alert when work becomes blocked or overdue. Each alert is sent once.
            </p>
            {chats.map((c) => (
              <div className="row" key={c.chat_id}>
                <span>
                  {c.username ? `@${c.username}` : "A private chat"}, alerts {c.alerts ? "on" : "off"}
                </span>
                {(admin || c.user_id === userId) && (
                  <form action={unlinkTelegramChat}>
                    <input type="hidden" name="chat_id" value={c.chat_id} />
                    <button className="btn btn-quiet" type="submit">
                      Unlink
                    </button>
                  </form>
                )}
              </div>
            ))}
            <TelegramLink />
          </div>
          <span className={`state ${chats.length ? "state-live" : "state-blocked"}`}>
            {chats.length ? `${chats.length} chat${chats.length > 1 ? "s" : ""} linked` : "Not linked"}
          </span>
        </div>

        <div className="connector">
          <span className="connector-name">Zoho Books</span>
          <div>
            <p>
              Invoices, bills, customer payments and contacts, so you can ask what is owed to you,
              what you owe and what is overdue. Read-only. Connecting works now; syncing your books
              into the graph is being built against a real account.
            </p>
            {zoho?.status === "error" && zoho.error && <p className="note">{zoho.error}</p>}
            <div className="connector-actions">
              {zoho?.status === "connected" ? (
                admin && (
                  <form action={disconnectToolkit}>
                    <input type="hidden" name="toolkit" value={ZOHO_BOOKS} />
                    <button className="btn btn-quiet" type="submit">
                      Disconnect
                    </button>
                  </form>
                )
              ) : admin ? (
                <form action={connectToolkit}>
                  <input type="hidden" name="toolkit" value={ZOHO_BOOKS} />
                  <button className="btn btn-primary" type="submit">
                    {zoho ? "Connect again" : "Connect Zoho Books"}
                  </button>
                </form>
              ) : (
                <p className="note">Ask a workspace admin to connect it.</p>
              )}
            </div>
          </div>
          <span className={`state ${zohoState.tone}`}>{zohoState.label.replace("Google", "Zoho")}</span>
        </div>
      </div>
    </section>
  );
}
