import Link from "next/link";
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
            <li className={synced ? "done" : "next"}>
              Connect a source and sync it
              {!synced && <span>Google Sheets is the first source.</span>}
            </li>
            <li>
              Choose what to keep an eye on
              <span>Available after the first sync.</span>
            </li>
            <li>
              Choose where to be told
              <span>Telegram first, after the first sync.</span>
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
