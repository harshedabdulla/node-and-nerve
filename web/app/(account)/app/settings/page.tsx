import { OrganizationProfile } from "@clerk/nextjs";
import { requireWorkspace } from "@/lib/workspace";

export const metadata = { title: "Members and settings: Node & Nerve" };

export default async function Settings() {
  const { workspace } = await requireWorkspace();
  return (
    <section className="app-section">
      <h1 className="app-title">Members and settings</h1>
      <p className="app-lede">
        Invite colleagues and set who is an admin of {workspace.name}. Only admins will be able to
        connect or disconnect sources.
      </p>
      <div style={{ marginTop: 28 }}>
        <OrganizationProfile routing="hash" />
      </div>
    </section>
  );
}
