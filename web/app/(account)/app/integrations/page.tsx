import { requireWorkspace } from "@/lib/workspace";

export const metadata = { title: "Integrations: Node & Nerve" };

const CATALOG = [
  {
    name: "Google Sheets",
    what: "Read the rows of sheets you choose. You sign in with Google; Node & Nerve never sees your password.",
    state: "Connecting is being built",
  },
  {
    name: "Telegram",
    what: "Get answers and alerts in a private chat with the Node & Nerve bot.",
    state: "Linking a chat is being built",
  },
  {
    name: "Zoho Books",
    what: "The account connects, but the connector exposes no read actions yet, so nothing can be synced.",
    state: "Waiting on connector",
  },
];

export default async function Integrations() {
  const { workspace } = await requireWorkspace();
  return (
    <section className="app-section">
      <h1 className="app-title">Integrations</h1>
      <p className="app-lede">
        Sources feed {workspace.name}&rsquo;s graph; channels deliver answers and alerts. A source
        shows as connected only after its first records reach the graph.
      </p>
      <div className="connectors">
        {CATALOG.map((c) => (
          <div className="connector" key={c.name}>
            <span className="connector-name">{c.name}</span>
            <p>{c.what}</p>
            <span className="state state-blocked">{c.state}</span>
          </div>
        ))}
      </div>
    </section>
  );
}
