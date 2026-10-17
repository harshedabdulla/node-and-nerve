"use client";

import { useMemo, useState } from "react";

// The records and links an answer was built from, laid out in layers from what the
// question matched, and drawn hop by hop in the order the traversal reached them.

export interface GraphNode {
  id: string;
  kind: string;
  ref: string;
  name: string | null;
  status: string | null;
  focus: boolean;
}

export interface GraphEdge {
  from: string;
  to: string;
  type: string;
  source: string | null; // the record that asserted this link
}

const KIND_COLOR: Record<string, string> = {
  project: "var(--calculation)",
  person: "var(--decision)",
  ticket: "var(--paper)",
  incident: "var(--inconsistency)",
  decision: "var(--unknown)",
};

const W = 300; // column width
const H = 64; // row height

// Columns by meaning: projects, then tasks, then what each task waits on (one column per
// "waits on" hop), then people and everything else.
function layout(nodes: GraphNode[], edges: GraphEdge[]) {
  const col = new Map<string, number>();
  for (const n of nodes) if (n.kind === "project") col.set(n.id, 0);
  const tickets = nodes.filter((n) => n.kind === "ticket");
  for (const t of tickets) col.set(t.id, 1);
  for (let pass = 0; pass < tickets.length; pass++) {
    let moved = false;
    for (const e of edges) {
      if (e.type !== "BLOCKED_BY" || !col.has(e.from) || !col.has(e.to)) continue;
      if (col.get(e.to)! <= col.get(e.from)!) {
        col.set(e.to, col.get(e.from)! + 1);
        moved = true;
      }
    }
    if (!moved) break;
  }
  const last = Math.max(1, ...[...col.values()]) + 1;
  for (const n of nodes) if (!col.has(n.id)) col.set(n.id, n.kind === "person" ? last : last + 1);
  const used = [...new Set(col.values())].sort((a, b) => a - b);
  const columns = new Map<number, GraphNode[]>();
  for (const n of nodes) {
    const l = used.indexOf(col.get(n.id)!);
    columns.set(l, [...(columns.get(l) ?? []), n]);
  }
  const tallest = Math.max(...[...columns.values()].map((c) => c.length));
  const pos = new Map<string, { x: number; y: number; layer: number }>();
  for (const [l, column] of columns) {
    column.sort((a, b) => Number(b.focus) - Number(a.focus) || a.ref.localeCompare(b.ref));
    const offset = ((tallest - column.length) * H) / 2;
    column.forEach((n, i) => pos.set(n.id, { x: 40 + l * W, y: 90 + offset + i * H, layer: l }));
  }
  return { pos, width: 40 + columns.size * W, height: 90 + tallest * H };
}

function label(n: GraphNode): string {
  const name = n.name ?? n.ref;
  if (n.kind === "ticket") return `${n.ref} ${name.length > 24 ? name.slice(0, 23) + "…" : name}`;
  return name.length > 28 ? name.slice(0, 27) + "…" : name;
}

function labelWidth(n: GraphNode): number {
  return 22 + label(n).length * 7.2;
}

export function EvidenceGraph({ nodes, edges }: { nodes: GraphNode[]; edges: GraphEdge[] }) {
  const [active, setActive] = useState<string | null>(null);
  const { pos, width, height } = useMemo(() => layout(nodes, edges), [nodes, edges]);
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const ordered = [...edges].sort(
    (a, b) =>
      Math.min(pos.get(a.from)!.layer, pos.get(a.to)!.layer) -
      Math.min(pos.get(b.from)!.layer, pos.get(b.to)!.layer),
  );
  const touches = (e: GraphEdge) => active !== null && (e.from === active || e.to === active);
  const selected = active ? byId.get(active) : null;

  return (
    <div className="graph">
      <div className="graph-canvas">
        <svg
          viewBox={`0 0 ${width} ${height}`}
          width={width}
          height={height}
          role="img"
          aria-label={`Evidence graph: ${nodes.length} records, ${edges.length} links`}
        >
          <defs>
            <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto">
              <path d="M0 0 L10 5 L0 10 z" fill="#6a6a66" />
            </marker>
          </defs>
          {ordered.map((e, i) => {
            const a = pos.get(e.from)!;
            const b = pos.get(e.to)!;
            // Leave from the side facing the target, clear of the label text.
            const rightward = b.x > a.x;
            const x1 = rightward ? a.x + labelWidth(byId.get(e.from)!) : a.x - 13;
            const x2 = rightward ? b.x - 14 : b.x + labelWidth(byId.get(e.to)!) + 4;
            const mx = (x1 + x2) / 2;
            // An edge that skips a column arcs above the row so it never crosses a label.
            const skips = Math.abs(a.layer - b.layer) > 1;
            const lift = Math.min(a.y, b.y) - 30 - 12 * Math.abs(a.layer - b.layer);
            const d = skips
              ? `M${x1},${a.y} C${x1},${lift} ${x2},${lift} ${x2},${b.y}`
              : `M${x1},${a.y} C${mx},${a.y} ${mx},${b.y} ${x2},${b.y}`;
            const ly = skips ? lift + 18 : (a.y + b.y) / 2 - 6;
            const dim = active !== null && !touches(e);
            return (
              <g key={`${e.from}-${e.type}-${e.to}`} className="graph-edge" style={{ opacity: dim ? 0.15 : 1 }}>
                <path
                  d={d}
                  pathLength={1}
                  className="graph-draw"
                  style={{ animationDelay: `${250 + i * 140}ms` }}
                  stroke={e.type === "BLOCKED_BY" ? "var(--inconsistency)" : "#55554f"}
                  strokeWidth={touches(e) ? 2.2 : 1.4}
                  fill="none"
                  markerEnd="url(#arrow)"
                />
                <text x={mx} y={ly} className="graph-edge-label graph-fade" style={{ animationDelay: `${400 + i * 140}ms` }}>
                  {e.type.replace("_", " ").toLowerCase()}
                </text>
              </g>
            );
          })}
          {nodes.map((n) => {
            const p = pos.get(n.id)!;
            const ring = n.status === "blocked" ? "var(--inconsistency)" : n.focus ? "var(--paper)" : "transparent";
            return (
              <g
                key={n.id}
                className="graph-node graph-fade"
                style={{ animationDelay: `${p.layer * 380}ms`, opacity: active && active !== n.id && !edges.some((e) => touches(e) && (e.from === n.id || e.to === n.id)) ? 0.35 : 1 }}
                transform={`translate(${p.x},${p.y})`}
                tabIndex={0}
                role="button"
                aria-label={`${n.kind} ${label(n)}`}
                onClick={() => setActive(active === n.id ? null : n.id)}
                onKeyDown={(ev) => (ev.key === "Enter" || ev.key === " ") && setActive(active === n.id ? null : n.id)}
              >
                <circle r={11} fill="none" stroke={ring} strokeWidth={1.5} />
                <circle r={7} fill={KIND_COLOR[n.kind] ?? "var(--muted)"} opacity={n.status === "done" ? 0.45 : 1} />
                <text x={16} y={4} className="graph-label">
                  {label(n)}
                </text>
              </g>
            );
          })}
        </svg>
      </div>
      <p className="note graph-legend">
        {selected ? (
          <>
            <strong style={{ color: "var(--paper)" }}>{selected.name ?? selected.ref}</strong> · {selected.kind}
            {selected.status ? `, status ${selected.status.replace("_", " ")}` : ""}. Links shown are
            the ones this answer used.
          </>
        ) : (
          <>
            Projects, then tasks, then what they wait on, then people. Red links: waits on.
            Ringed in white: what your question matched; in red: blocked. Select a record to trace
            its links.
          </>
        )}
      </p>
    </div>
  );
}
