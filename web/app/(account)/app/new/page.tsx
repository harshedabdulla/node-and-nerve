import { CreateOrganization } from "@clerk/nextjs";

export const metadata = { title: "New workspace: Node & Nerve" };

export default function NewWorkspace() {
  return (
    <section className="app-section narrow">
      <h1 className="app-title">Create a workspace for your company</h1>
      <p className="app-lede">
        A workspace holds one company&rsquo;s connected sources, its graph and who can see them.
        You can invite colleagues after it is created.
      </p>
      <div style={{ marginTop: 28 }}>
        <CreateOrganization afterCreateOrganizationUrl="/app" skipInvitationScreen />
      </div>
    </section>
  );
}
