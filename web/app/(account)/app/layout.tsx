import { OrganizationSwitcher, UserButton } from "@clerk/nextjs";
import Link from "next/link";
import { AppNav } from "./nav";

export default function AppLayout({ children }: { children: React.ReactNode }) {
  return (
    <div className="app">
      <header className="app-bar">
        <Link href="/app" className="brand">
          Node &amp; Nerve
        </Link>
        <AppNav />
        <div className="app-bar-end">
          <OrganizationSwitcher
            hidePersonal
            afterCreateOrganizationUrl="/app"
            afterSelectOrganizationUrl="/app"
            createOrganizationUrl="/app/new"
          />
          <UserButton />
        </div>
      </header>
      <main className="app-main">{children}</main>
    </div>
  );
}
