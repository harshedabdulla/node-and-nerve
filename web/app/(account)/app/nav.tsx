"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

const ITEMS = [
  { href: "/app", label: "Overview" },
  { href: "/app/integrations", label: "Integrations" },
  { href: "/app/settings", label: "Members and settings" },
];

export function AppNav() {
  const path = usePathname();
  return (
    <nav className="app-nav" aria-label="Workspace">
      {ITEMS.map((i) => (
        <Link key={i.href} href={i.href} aria-current={path === i.href ? "page" : undefined}>
          {i.label}
        </Link>
      ))}
    </nav>
  );
}
