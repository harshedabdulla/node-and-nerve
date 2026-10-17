import Link from "next/link";
import { graphSummary, recentActivity, requireWorkspace } from "@/lib/workspace";

export const metadata = { title: "Overview: Node & Nerve" };

const ACTIONS: Record<string, string> = {
  "workspace.created": "Workspace created",
};

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
