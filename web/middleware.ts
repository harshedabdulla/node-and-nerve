import { clerkMiddleware, createRouteMatcher } from "@clerk/nextjs/server";

// Everything under /app requires a signed-in user. Workspace access is then checked
// server-side from the session (lib/workspace.ts). The landing page and /demo are public
// and do not run Clerk at all (see the matcher).
const isApp = createRouteMatcher(["/app(.*)"]);

export default clerkMiddleware(
  async (auth, req) => {
    if (isApp(req)) await auth.protect();
  },
  { signInUrl: "/sign-in", signUpUrl: "/sign-up" },
);

export const config = {
  matcher: ["/app(.*)", "/sign-in(.*)", "/sign-up(.*)"],
};
