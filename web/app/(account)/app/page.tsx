import Link from "next/link";
import { connectionFor, SHEETS, telegramChats } from "@/lib/integrations";
import {
  attention,
  graphSummary,
  type Investigation,
  readable,
  recentActivity,
  requireWorkspace,
} from "@/lib/workspace";

export const metadata = { title: "Overview: Node & Nerve" };

const ACTIONS: Record<string, string> = {
  "workspace.created": "Workspace created",
  "connection.started": "Google Sheets sign-in started",
  "connection.connected": "Google Sheets connected",
  "connection.removed": "Google Sheets disconnected",
  "sheet.synced": "Sheet synced",
  "sheet.sync_failed": "Sheet sync failed",
  "question.asked": "Question asked",
  "telegram.link_created": "Telegram link created",
  "telegram.linked": "Telegram chat linked",
  "telegram.unlinked": "Telegram chat unlinked",
  "alerts.sent": "Alerts sent to Telegram",
};

function Attention({ projects }: { projects: Investigation[] }) {
  if (projects.length === 0) {
    return <p className="note">No projects are recorded yet, so there is nothing to group work by.</p>;
  }
  return (
    <>
      {projects.map((p) => (
        <div key={p.matched_entities[0]?.id ?? p.question} style={{ marginTop: 14 }}>
          <p style={{ fontWeight: 600 }}>{p.matched_entities[0]?.name}</p>
          <ul className="findings">
            {p.findings
              .filter((f) => f.category !== "expertise")
              .map((f) => (
                <li key={f.statement}>{readable(f.statement)}</li>
              ))}
            {p.missing_information.map((m) => (
              <li key={m} style={{ "--kind-color": "var(--unknown)" } as React.CSSProperties}>
                {readable(m)}
              </li>
            ))}
          </ul>
        </div>
      ))}
    </>
  );
}

export default async function Overview() {
  const { workspace, role } = await requireWorkspace();
  const [graph, activity] = await Promise.all([
    graphSummary(workspace).then(
      (g) => ({ ok: true as const, g }),
      (e: Error) => ({ ok: false as const, error: e.message }),
    ),
    recentActivity(workspace.id),
  ]);
  const synced = graph.ok && graph.g !== null && graph.g.total_nodes > 0;
  const [sheets, chats] = await Promise.all([
    connectionFor(workspace.id, SHEETS),
    telegramChats(workspace.id),
  ]);
  const connected = sheets?.status === "connected";
  const needs = synced
    ? await attention(workspace).then(
        (a) => ({ ok: true as const, a: a ?? [] }),
        (e: Error) => ({ ok: false as const, error: e.message }),
      )
    : null;

  return (
    <section className="app-section">
      <h1 className="app-title">{workspace.name}</h1>
      <p className="app-lede">
        You are {role === "org:admin" ? "an admin" : "a member"} of this workspace.
      </p>

      <div className="app-grid">
        <div className="panel">
          <h2>Company graph</h2>
          {!graph.ok ? (
            <p className="note">The graph could not be read ({graph.error}). Nothing is shown in its place.</p>
          ) : !synced ? (
            <>
              <p className="empty-title">Empty</p>
              <p className="note">
                Nothing has been synced into this workspace yet. Records appear here only after a
                connected source has been read and written to the graph.
              </p>
              <p style={{ marginTop: 18 }}>
                <Link href="/app/integrations" className="btn btn-primary">
                  Connect a source
                </Link>
              </p>
            </>
          ) : (
            <>
              <div className="big num">{graph.g!.total_nodes.toLocaleString("en-IN")}</div>
              <p className="note">
                records joined by {graph.g!.total_relationships.toLocaleString("en-IN")} relationships
              </p>
              {Object.entries(graph.g!.nodes_by_kind).map(([kind, n]) => (
                <div className="row num" key={kind}>
                  <span>{kind}</span>
                  <span>{n}</span>
                </div>
              ))}
            </>
          )}
        </div>

        <div className="panel">
          <h2>Setting up</h2>
          <ol className="setup">
            <li className="done">Create the workspace</li>
            <li className={connected ? "done" : "next"}>
              Connect Google Sheets
              {!connected && (
                <span>
                  <Link href="/app/integrations">Connect it</Link> with your Google account.
                </span>
              )}
            </li>
            <li className={synced ? "done" : connected ? "next" : ""}>
              Sync a task sheet
              {!synced && (
                <span>
                  {connected ? <Link href="/app/integrations/google-sheets">Choose a sheet</Link> : "After connecting."}{" "}
                  Rows need a task id and title; owner, status, due date, project and blockers help.
                </span>
              )}
            </li>
            <li className={chats.length ? "done" : synced ? "next" : ""}>
              Link Telegram for answers and alerts
              {!chats.length && (
                <span>
                  <Link href="/app/integrations">Link a private chat</Link>; alerts arrive when work
                  becomes blocked or overdue.
                </span>
              )}
            </li>
          </ol>
        </div>

        {needs && (
          <div className="panel wide">
            <h2>Needs attention</h2>
            {needs.ok ? (
              <Attention projects={needs.a} />
            ) : (
              <p className="note">The investigation could not run ({needs.error}).</p>
            )}
            <p className="note" style={{ marginTop: 14 }}>
              Found by following owners, blockers and due dates in the graph. Amber items are
              missing from your records, not problems that were confirmed.
            </p>
          </div>
        )}

        <div className="panel wide">
          <h2>Activity</h2>
          {activity.length === 0 ? (
            <p className="note">No activity yet.</p>
          ) : (
            activity.map((a) => (
              <div className="row" key={`${a.action}-${a.at}`}>
                <span>{ACTIONS[a.action] ?? a.action}</span>
                <span className="note">{new Date(a.at).toLocaleString("en-IN", { timeZone: "Asia/Kolkata" })} IST</span>
              </div>
            ))
          )}
        </div>
      </div>
    </section>
  );
}
