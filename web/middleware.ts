import { clerkMiddleware, createRouteMatcher } from "@clerk/nextjs/server";
import { NextResponse } from "next/server";

// Everything under /app requires a signed-in user. Workspace access is then checked
// server-side from the session (lib/workspace.ts). The landing page and /demo are public
// and do not run Clerk at all (see the matcher).
const isApp = createRouteMatcher(["/app(.*)"]);

// Production Clerk serves heyspindle.com and its subdomains only, so the production
// vercel.app host sends accounts and the app to the custom domain. Previews are untouched.
const VERCEL_PRODUCTION_HOST = "node-and-nerve.vercel.app";
const CANONICAL = "https://nodeandnerve.heyspindle.com";

export default clerkMiddleware(
  async (auth, req) => {
    if (req.nextUrl.host === VERCEL_PRODUCTION_HOST) {
      return NextResponse.redirect(new URL(req.nextUrl.pathname + req.nextUrl.search, CANONICAL), 308);
    }
    if (isApp(req)) await auth.protect();
  },
  { signInUrl: "/sign-in", signUpUrl: "/sign-up" },
);

export const config = {
  matcher: ["/app(.*)", "/sign-in(.*)", "/sign-up(.*)"],
};
