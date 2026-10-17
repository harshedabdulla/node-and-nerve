import Link from "next/link";

const REPO = "https://github.com/harshedabdulla/node-and-nerve";

function Mark() {
  return (
    <svg viewBox="0 0 32 32" width="22" height="22" aria-hidden="true">
      <g fill="currentColor">
        <circle cx="16" cy="16" r="4.5" />
        <circle cx="7" cy="10" r="2.8" />
        <circle cx="25" cy="10" r="2.8" />
        <circle cx="10" cy="24" r="2.8" />
        <circle cx="22" cy="24" r="2.8" />
      </g>
      <g stroke="currentColor" strokeWidth="1.6" strokeLinecap="round">
        <line x1="16" y1="16" x2="7" y2="10" />
        <line x1="16" y1="16" x2="25" y2="10" />
        <line x1="16" y1="16" x2="10" y2="24" />
        <line x1="16" y1="16" x2="22" y2="24" />
      </g>
    </svg>
  );
}

export function SiteHeader({ current }: { current: "home" | "demo" }) {
  return (
    <header className="site-header">
      <div className="wrap">
        <Link href="/" className="brand" aria-label="Node & Nerve home">
          <span className="brand-mark">
            <Mark />
          </span>
          <span>Node &amp; Nerve</span>
        </Link>
        <nav className="nav" aria-label="Primary">
          <Link href="/#how" className="nav-optional">
            How it works
          </Link>
          <Link href="/#integrations" className="nav-optional">
            Integrations
          </Link>
          <Link href="/demo" aria-current={current === "demo" ? "page" : undefined}>
            Live demo
          </Link>
          <a href={REPO} target="_blank" rel="noreferrer" className="nav-optional">
            GitHub
          </a>
        </nav>
      </div>
    </header>
  );
}

export function SiteFooter() {
  return (
    <footer className="site-footer">
      <div className="wrap">
        <span>
          Node &amp; Nerve is open source, built on{" "}
          <a href="https://www.falkordb.com" target="_blank" rel="noreferrer">
            FalkorDB
          </a>{" "}
          for Graph Hacks 2026. Demo companies and their records are fictional.
        </span>
        <span>
          <a href={REPO} target="_blank" rel="noreferrer">
            Source and documentation
          </a>
        </span>
      </div>
    </footer>
  );
}
